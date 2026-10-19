"""Projects as groups of tickets, with the branches and pull requests that tie to each ticket.

A tie joins a ticket to a branch, a pull request, a commit or a context card. Each tie keeps the reason for it:
- the ticket key is in the branch name, the pull request title, its head branch or its body (a body with more than
  BODY_KEYS_MAX keys is a list of tickets, not one ticket's work), or a commit subject;
- a context card (work.py) holds the ticket key, and so holds its pull requests and branches too.

A project is a group of tickets. The service's own group comes first: the parent issue or epic (Jira `parent`), a
Linear project, a Jira component, or the milestone of a tied pull request. Tickets without one are joined by issue
links, a shared pull request, branch or context card, and a shared title prefix ("[on-call] ...", "Billing: ...").
A parent-less ticket whose prefix is the prefix of most tickets in a parent group joins that group. A ticket that
joins nothing is a project of its own. A pull request that ties to no ticket is a project of its own only when the
user is on it, a context card holds it, or someone close to the user wrote it.

A ticket's state comes from the ticket and its pull requests. A merged pull request (and no open one) means done. An
open pull request that has reviews or review requests means close to done. A draft or open pull request means in
progress. With no pull request, the ticket's own status counts. A done ticket stays done.

The user's projects come first, by the recency of their activity, weighted by how much of it is the user's own. The
other projects follow, by recency and by the closeness (People graph) of the people on them.

Only typed dataclasses leave this module; team_projects.py serves them and keeps the last build in a JSON file."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from pensieve import config, work

DIR = config.V2_DIR / "projects"
CACHE = DIR / "cache"

ACTIVE_DAYS = 120          # tickets, pull requests and branches older than this are in no project
STALE_DAYS = 21            # default for the setting projects_stale_days: older projects go under "Show older"
PR_TTL = 15 * 60           # gh results are fetched again after this
PR_RECENT = 60             # the newest pull requests of each repo
PR_MINE = 60               # the pull requests that involve the user, in each repo
MAX_PR_REPOS = 8
BODY_KEYS_MAX = 3
SEEN_MAX = 4               # a session that read more tickets than this ran a search: no involvement in each
PREFIX_GROUP_MAX = 60      # a prefix shared by more tickets than this says nothing about a project
CLOSE_PR_AUTHOR = 0.25     # a pull request with no ticket counts when its author is at least this close
HALF_LIFE_DAYS = 10
ROLE_WEIGHT = {"author": 3.0, "assignee": 3.0, "committer": 2.0, "reviewer": 1.5, "context": 1.0}

_PREFIX = re.compile(r"^\s*(?:\[([^\]]{2,40})\]|([A-Za-z][\w .&/-]{1,30}):\s)")


# ---- the model ---------------------------------------------------------------------------------------------------
class TicketState(StrEnum):
    DONE = "done"
    CLOSE = "close"            # close to done: a pull request in review, or a ticket in review
    PROGRESS = "progress"
    TODO = "todo"


class PrState(StrEnum):
    MERGED = "merged"
    REVIEW = "review"          # open, with reviews or review requests
    DRAFT = "draft"
    OPEN = "open"
    CLOSED = "closed"          # closed without a merge


class TieKind(StrEnum):
    BRANCH_NAME = "branch_name"
    PR_TITLE = "pr_title"
    PR_BRANCH = "pr_branch"
    PR_BODY = "pr_body"
    COMMIT = "commit"
    CARD = "card"


class Role(StrEnum):
    ASSIGNEE = "assignee"
    AUTHOR = "author"
    REVIEWER = "reviewer"
    COMMITTER = "committer"
    CONTEXT = "context"        # in the user's context cards or agent sessions


class GroupKind(StrEnum):
    PARENT = "parent"          # the parent issue or epic
    LINEAR = "linear"          # a Linear project
    COMPONENT = "component"    # a Jira component
    MILESTONE = "milestone"    # a GitHub milestone
    LINKED = "linked"          # tickets joined by links, shared pull requests, branches, cards or a title prefix
    TICKET = "ticket"          # one ticket
    PR = "pr"                  # pull requests and branches with no ticket


class Section(StrEnum):
    MINE = "mine"
    AROUND = "around"


@dataclass
class Member:
    id: str                    # PersonId
    name: str
    roles: list[str]
    me: bool = False
    avatar: bool = False
    closeness: float = 0.0


@dataclass
class PullRequest:
    url: str
    repo: str
    number: int
    title: str
    state: str                 # PrState
    head: str = ""
    review: str = ""           # approved | changes requested | review requested | ""
    author: str = ""           # PersonId, or ""
    author_login: str = ""
    reviewers: list[str] = field(default_factory=list)  # PersonIds
    updated: int = 0
    merged_at: int = 0
    milestone: str = ""


@dataclass
class Branch:
    repo: str
    name: str
    updated: int = 0
    author: str = ""           # PersonId of the tip commit's author, or ""


@dataclass
class Tie:
    ticket: str
    kind: str                  # TieKind
    ref: str                   # PR url, repo@branch, repo@sha or card id
    why: str


@dataclass
class ProjectTicket:
    key: str
    title: str
    url: str = ""
    status: str = ""           # the ticket's own status, as the service says it
    state: str = TicketState.TODO
    why: str = ""              # why the state is what it is
    updated: int = 0
    prs: list[str] = field(default_factory=list)       # PR urls
    branches: list[str] = field(default_factory=list)  # repo@branch
    people: list[Member] = field(default_factory=list)
    path: str = ""             # the collected item's file, for opening it
    known: bool = True         # False: known only by its key (a branch, pull request or commit names it)


@dataclass
class Project:
    id: str
    name: str
    kind: str                  # GroupKind
    ref: str                   # the parent key, project name, milestone, prefix or ticket key
    url: str = ""
    tickets: list[ProjectTicket] = field(default_factory=list)
    prs: list[PullRequest] = field(default_factory=list)
    branches: list[Branch] = field(default_factory=list)
    ties: list[Tie] = field(default_factory=list)
    members: list[Member] = field(default_factory=list)
    last: int = 0
    my_last: int = 0
    my_roles: list[str] = field(default_factory=list)
    involvement: float = 0.0   # 0..1, the user's share of the roles
    closeness: float = 0.0     # 0..1, of the nearest people on it
    section: str = Section.AROUND
    score: float = 0.0
    stale: bool = False
    likely_done: bool = False
    counts: dict = field(default_factory=dict)
    sessions: list[str] = field(default_factory=list)  # session ids of its context cards (the Map's "Open as project")


def to_dict(p: Project) -> dict:
    return asdict(p)


# ---- people --------------------------------------------------------------------------------------------------------
class People:
    """Names, emails and GitHub logins to People ids, with each person's closeness to the user."""

    def __init__(self):
        from pensieve import people
        self.mod = people
        g = people.graph() or {}
        self.me = g.get("me")
        self.people = g.get("people", {})
        self.idx = {}
        for pid, p in sorted(self.people.items(), key=lambda kv: -(kv[1].get("rank") or 1e9)):  # the nearest wins
            for k in p.get("keys", []):
                self.idx[k] = pid
        self._avatars = {}

    def of(self, name="", email="", login="") -> str:
        email = (email or "").strip().lower()
        if email and (pid := self.idx.get("email:" + email)):
            return pid
        if email and (m := self.mod.NOREPLY.match(email)) and (pid := self.idx.get("gh:" + m.group(1).lower())):
            return pid
        if login and (pid := self.idx.get("gh:" + login.lower())):
            return pid
        n = self.mod.norm_name(name or "")
        return self.idx.get("name:" + n, "") if n else ""

    def close(self, pid) -> float:
        return 1.0 if pid and pid == self.me else float(self.people.get(pid, {}).get("closeness", 0) or 0)

    def member(self, pid, roles) -> Member:
        p = self.people.get(pid, {})
        if pid not in self._avatars:
            url = p.get("avatar_url")
            self._avatars[pid] = bool(url and self.mod._avatar_file(url))
        return Member(id=pid, name=p.get("name", ""), roles=sorted(set(roles), key=list(ROLE_WEIGHT).index),
                      me=pid == self.me, avatar=self._avatars[pid], closeness=round(self.close(pid), 3))


# ---- helpers -------------------------------------------------------------------------------------------------------
def _hash(s: str, n=12) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:n]


def _read(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def _write(p: Path, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(p)


def _flat(raw: dict) -> dict:
    f = raw.get("fields")
    return {**{k: v for k, v in raw.items() if k != "fields"}, **f} if isinstance(f, dict) else raw


def _decay(ts: float, now: float) -> float:
    return 0.5 ** (max(0.0, now - ts) / 86400 / HALF_LIFE_DAYS) if ts else 0.0


def prefix_of(title: str) -> str:
    """The group prefix of a title: "[On-call] Disk full" -> "on-call", "Billing: retry" -> "billing"."""
    m = _PREFIX.match(title or "")
    return (m.group(1) or m.group(2) or "").strip().lower() if m else ""


def _status_state(status) -> tuple[str, TicketState]:
    """(the status name, its state) of a Jira, Linear or GitHub status value."""
    cat = ""
    if isinstance(status, dict):
        cat = str((status.get("statusCategory") or {}).get("key") or status.get("type") or "").lower()
        status = status.get("name") or status.get("value") or ""
    name = str(status or "").strip()
    low = name.lower()
    if cat in ("done", "completed", "canceled", "cancelled") or re.search(
            r"\b(done|closed|resolved|complete|completed|released|shipped|merged|won'?t do|cancell?ed|duplicate)\b", low):
        return name, TicketState.DONE
    if re.search(r"\b(review|qa|testing|verif\w*|ready to (deploy|ship|merge)|in staging)\b", low):
        return name, TicketState.CLOSE
    if cat in ("indeterminate", "started") or re.search(r"\b(progress|doing|in dev|started|active|working)\b", low):
        return name, TicketState.PROGRESS
    return name, TicketState.TODO


def _person_fields(raw: dict) -> list[tuple[str, str, str]]:
    """(role, name, email) of the assignee: Jira {displayName, emailAddress}, Linear {name, email}, or a string."""
    out = []
    a = raw.get("assignee")
    if isinstance(a, dict):
        out.append(("assignee", a.get("displayName") or a.get("name") or "", a.get("emailAddress") or a.get("email") or ""))
    elif isinstance(a, str) and a:
        out.append(("assignee", a, a if "@" in a else ""))
    for p in raw.get("_people") or []:
        if isinstance(p, dict) and p.get("role") == "assignee":
            out.append(("assignee", p.get("name") or "", p.get("email") or ""))
    return out


# ---- collection: tickets -----------------------------------------------------------------------------------------
@dataclass
class _Ticket:
    key: str
    title: str = ""
    url: str = ""
    status: str = ""
    state: TicketState = TicketState.TODO
    updated: int = 0
    parent: str = ""
    parent_title: str = ""
    parent_url: str = ""
    linear: str = ""
    component: str = ""
    links: set = field(default_factory=set)
    assignees: list = field(default_factory=list)  # (name, email)
    seen_in: set = field(default_factory=set)      # session ids that looked at it
    path: str = ""
    known: bool = True


_items_cache = [None, None, None]   # [signature, items, tickets]


def _sources() -> tuple[list[dict], dict[str, _Ticket]]:
    """The connection items and their tickets, read again only when the documents table changes."""
    from pensieve import store as v2
    try:
        sig = v2.store_db().one("SELECT count(*), max(id), total(updated_at), total(seen_at) FROM documents")
    except Exception:
        sig = None
    if _items_cache[0] != sig:
        items = work._items()
        _items_cache[:] = [sig, items, _tickets(items)]
    return _items_cache[1], {k: _Ticket(**{**asdict(t), "links": set(t.links), "seen_in": set(t.seen_in)})
                             for k, t in _items_cache[2].items()}


def _tickets(items: list[dict]) -> dict[str, _Ticket]:
    out: dict[str, _Ticket] = {}
    from pensieve import store as v2
    from pensieve.store import documents
    keyed = [it for it in items if it["key"] and not it.get("private")]
    ids, raws, seen_in, db = [it["doc"] for it in keyed], {}, {}, v2.store_db()
    for i in range(0, len(ids), 900):
        part = ids[i:i + 900]
        raws |= {d.id: d.raw for d in documents.get_many(db, part)}
        for doc, sid in db.query(f"SELECT doc_id, session_id FROM document_sessions WHERE doc_id IN "
                                 f"({','.join('?' * len(part))})", part):
            seen_in.setdefault(doc, set()).add(sid)
    for it in keyed:
        try:
            raw = _flat(json.loads(raws.get(it["doc"]) or "{}"))
        except ValueError:
            raw = {}
        t = out.setdefault(it["key"], _Ticket(key=it["key"]))
        # `_seen_at` is when a transcript was read, not when the ticket changed: the sessions' times count instead
        upd = int(work._ts(raw.get("_updated") or raw.get("updated") or raw.get("updated_at") or raw.get("_created") or ""))
        newer = upd >= t.updated
        if newer or not t.title:
            t.title = (raw.get("summary") or raw.get("title") or it["title"] or t.title).strip()
            t.url = it["url"] or t.url
            t.path = it["path"]
        if (newer or not t.status) and (raw.get("status") or raw.get("state")):
            t.status, t.state = _status_state(raw.get("status") or raw.get("state"))
        t.updated = max(t.updated, upd)
        par = raw.get("parent")
        if isinstance(par, dict) and (par.get("key") or par.get("identifier")):
            t.parent = par.get("key") or par.get("identifier")
            pf = _flat(par)
            t.parent_title = pf.get("summary") or pf.get("title") or t.parent_title
            if t.url and t.key in t.url:
                t.parent_url = t.url.replace(t.key, t.parent)
        proj = raw.get("project")
        if isinstance(proj, dict) and (proj.get("name") and not proj.get("key")):  # Linear: a project has no key
            t.linear = proj["name"]
        comps = [c.get("name") for c in raw.get("components") or [] if isinstance(c, dict) and c.get("name")]
        if comps:
            t.component = comps[0]
        for ln in raw.get("issuelinks") or []:
            for side in ("inwardIssue", "outwardIssue"):
                if isinstance(ln, dict) and isinstance(ln.get(side), dict) and ln[side].get("key"):
                    t.links.add(ln[side]["key"])
        for _role, name, email in _person_fields(raw):
            if (name, email) not in t.assignees:
                t.assignees.append((name, email))
        t.seen_in |= seen_in.get(it["doc"], set()) | set(raw.get("_seen_in") or [])
    return out


# ---- collection: pull requests -----------------------------------------------------------------------------------
def _gh_slug(remote: str) -> str:
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", remote or "")
    return m.group(1) if m else ""


_GH_FIELDS = "number,title,url,state,isDraft,headRefName,body,updatedAt,mergedAt,author,reviewDecision,reviews,reviewRequests,milestone"


def _gh_list(slug: str, search: str, limit: int) -> list[dict]:
    args = ["gh", "pr", "list", "--repo", slug, "--state", "all", "--limit", str(limit), "--json", _GH_FIELDS]
    if search:
        args += ["--search", search]
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise OSError(r.stderr[:200])
    return json.loads(r.stdout or "[]")


def _slim(p: dict) -> dict:
    return dict(number=p.get("number"), title=p.get("title") or "", url=p.get("url") or "", state=p.get("state") or "",
                draft=bool(p.get("isDraft")), head=p.get("headRefName") or "", body=(p.get("body") or "")[:4000],
                updated=int(work._ts(p.get("updatedAt") or "")), merged_at=int(work._ts(p["mergedAt"])) if p.get("mergedAt") else 0,
                author=((p.get("author") or {}).get("login") or ""), author_name=((p.get("author") or {}).get("name") or ""),
                decision=p.get("reviewDecision") or "",
                reviewers=sorted({(r.get("author") or {}).get("login", "") for r in p.get("reviews") or []} - {""}),
                requested=sorted({r.get("login") or r.get("slug") or r.get("name") or "" for r in p.get("reviewRequests") or []} - {""}),
                milestone=((p.get("milestone") or {}).get("title") or ""))


_fetching = set()
on_prs = []                # called (from a worker thread) when a repo's pull requests were fetched again


def _repo_prs(slug: str, fetch: bool) -> list[dict]:
    """The newest pull requests of a repo and the ones that involve the user, from the cache. With `fetch`, a
    cache older than PR_TTL is fetched again through gh (read only) in a background thread, which calls `on_prs`."""
    cf = CACHE / f"prs-{_hash(slug)}.json"
    hit = _read(cf, {})
    if fetch and not (hit.get("at", 0) > time.time() - PR_TTL and hit.get("v") == 1) and shutil.which("gh") \
            and slug not in _fetching:
        _fetching.add(slug)

        def run():
            try:
                _fetch_prs(slug, cf, hit)
            finally:
                _fetching.discard(slug)
            for fn in on_prs:
                fn()
        threading.Thread(target=run, daemon=True, name="projects-gh").start()
    return hit.get("prs", [])


def _fetch_prs(slug: str, cf: Path, hit: dict):
    try:
        got = {}
        for p in _gh_list(slug, "", PR_RECENT) + _gh_list(slug, "involves:@me", PR_MINE):
            if p.get("url"):
                got[p["url"]] = _slim(p)
        prs = list(got.values())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        prs = hit.get("prs", [])  # kept, and not asked again before PR_TTL
    _write(cf, {"v": 1, "at": time.time(), "prs": prs})


def _pr_state(state: str, draft: bool, decision: str, reviewers, requested) -> tuple[PrState, str]:
    s = (state or "").upper()
    if s == "MERGED":
        return PrState.MERGED, ""
    if s == "CLOSED":
        return PrState.CLOSED, ""
    if draft or s == "DRAFT":
        return PrState.DRAFT, ""
    review = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes requested"}.get(decision, "")
    if not review and requested:
        review = "review requested"
    if not review and reviewers:
        review = "reviewed"
    return (PrState.REVIEW if review else PrState.OPEN), review


# ---- the build -----------------------------------------------------------------------------------------------------
@dataclass
class Build:
    projects: list[Project]
    me: str | None
    built: float
    took: float
    counts: dict
    stale_days: int


def build(store, fetch: bool = True, stale_days: int = STALE_DAYS, now: float | None = None) -> Build:
    t0 = time.time()
    now = now or t0
    since = now - ACTIVE_DAYS * 86400
    phases = {}

    def lap(name):
        phases[name] = round(time.time() - t0 - sum(phases.values()), 3)
    ppl = People()
    items, tickets = _sources()
    lap("items")
    try:
        cards = work.tasks(store, 30)
    except Exception:
        cards = []
    # A card with no agent session is only a collected ticket: it says nothing about the user's own work.
    cards = [c for c in cards if c.get("sessions") or c.get("shared_sessions")]
    lap("cards")
    repo_rows = store.db.execute("SELECT root, name, remote FROM repos").fetchall()
    commits = store.db.execute("SELECT repo, sha, ts, author, subject FROM commits WHERE ts > ?", (since,)).fetchall()
    seen = sorted({s for t in tickets.values() for s in t.seen_in})
    seen_at = {sid: int(work._ts(u)) for sid, u in store.db.execute(
        f"SELECT id, updated FROM sessions WHERE id IN ({','.join('?' * len(seen))})", seen).fetchall()} if seen else {}
    # A session that read many tickets ran a search or a list: it does not make the user part of each ticket.
    n_seen = Counter(s for t in tickets.values() for s in t.seen_in)
    for t in tickets.values():
        t.seen_in = {s for s in t.seen_in if n_seen[s] <= SEEN_MAX}
        t.updated = max([t.updated] + [seen_at.get(s, 0) for s in t.seen_in])
    name_of = {root: name for root, name, _ in repo_rows}
    lap("db")

    # known ticket prefixes: a key elsewhere counts only with one of these ("CVE-2024", "UTF-8" are not tickets)
    prefixes = {k.split("-")[0] for k in tickets} | {k.split("-")[0] for c in cards for k in c.get("keys", [])}

    def keys_in(text: str) -> list[str]:
        return list(dict.fromkeys(f"{a}-{b}" for a, b in work.KEY.findall(text or "") if a in prefixes))

    ties: list[Tie] = []
    tie_seen = set()

    def tie(key, kind, ref, why):
        if (key, kind, ref) not in tie_seen:
            tie_seen.add((key, kind, ref))
            ties.append(Tie(ticket=key, kind=kind, ref=ref, why=why))

    # pull requests: gh lists of the repos the user works in, the context cards' pull requests, connection items
    mine_n = Counter()
    me_keys = {k.split(":", 1)[1] for k in ppl.people.get(ppl.me, {}).get("keys", []) if k.startswith("email:")}
    for repo, _sha, _ts, author, _s in commits:
        if (author or "").lower() in me_keys:
            mine_n[repo] += 1
    gh_repos = sorted(((root, name, _gh_slug(remote)) for root, name, remote in repo_rows if _gh_slug(remote)),
                      key=lambda r: -mine_n[r[0]])[:MAX_PR_REPOS]
    lists = [(name, _repo_prs(slug, fetch)) for _root, name, slug in gh_repos]
    lap("prs")
    prs: dict[str, PullRequest] = {}
    bodies = {}
    for repo, rows in lists:
        for p in rows:
            if p["updated"] < since:
                continue
            st, review = _pr_state(p["state"], p["draft"], p["decision"], p["reviewers"], p["requested"])
            author = ppl.of(p.get("author_name") or "", login=p["author"]) if p["author"] else ""
            revs = [x for x in dict.fromkeys(ppl.of(login=lg) for lg in p["reviewers"] + p["requested"]) if x and x != author]
            prs[p["url"]] = PullRequest(url=p["url"], repo=repo, number=p["number"] or 0, title=p["title"], state=st,
                                        head=p["head"], review=review, author=author, author_login=p["author"],
                                        reviewers=revs, updated=p["updated"], merged_at=p["merged_at"],
                                        milestone=p["milestone"])
            bodies[p["url"]] = p["body"]
    card_prs = defaultdict(set)   # PR url -> card ids
    for c in cards:
        for p in c.get("prs", []) + c.get("branch_prs", []):
            u = p.get("url") or ""
            if "/pull/" not in u and "merge_requests" not in u:
                continue
            card_prs[u].add(c["id"])
            if u not in prs:
                m = re.search(r"github\.com/[\w.-]+/([\w.-]+)/pull/(\d+)", u)
                st, review = _pr_state(p.get("state") or "open", p.get("state") == "draft", "", [], [])
                if p.get("review") in ("approved", "changes requested", "review required") and st == PrState.OPEN:
                    st, review = PrState.REVIEW, p["review"].replace("review required", "review requested")
                prs[u] = PullRequest(url=u, repo=m.group(1) if m else "", number=int(m.group(2)) if m else 0,
                                     title=p.get("title") or "", state=st if p.get("state") else PrState.OPEN,
                                     head=p.get("head") or "", review=review, updated=int(c.get("updated") or 0),
                                     merged_at=int(p.get("merged_at") or 0))
            elif not prs[u].updated:
                prs[u].updated = int(c.get("updated") or 0)
    for it in items:
        if it.get("pr") and it["url"] and it["url"] not in prs and not it.get("private") and it["updated"] >= since:
            m = re.search(r"github\.com/[\w.-]+/([\w.-]+)/pull/(\d+)", it["url"])
            st, _ = _status_state(it["status"])
            prs[it["url"]] = PullRequest(url=it["url"], repo=m.group(1) if m else "", number=int(m.group(2)) if m else 0,
                                         title=it["title"], state=PrState.MERGED if "merged" in it["status"].lower() else
                                         PrState.CLOSED if st == TicketState.DONE else PrState.OPEN,
                                         head=it["branch"], updated=int(it["updated"]))
            bodies[it["url"]] = it["text"][:4000]
    for u, p in prs.items():
        for k in keys_in(p.title):
            tie(k, TieKind.PR_TITLE, u, f"{k} is in the pull request's title")
        for k in keys_in(p.head):
            tie(k, TieKind.PR_BRANCH, u, f"{k} is in the pull request's branch {p.head}")
        ks = keys_in(bodies.get(u, ""))
        if 0 < len(ks) <= BODY_KEYS_MAX:
            for k in ks:
                tie(k, TieKind.PR_BODY, u, f"the pull request's description names {k}")

    # branches: the local branches of each repo (the user's own), and the context cards' branches
    branches: dict[str, Branch] = {}
    for root, name, _remote in repo_rows:
        out = work._git(root, "for-each-ref", "refs/heads", "--format=%(refname:short)%1f%(committerdate:unix)%1f%(authorname)%1f%(authoremail)", ttl=300)
        for line in out.splitlines():
            bits = line.split("\x1f")
            if len(bits) < 4 or bits[0] in work.DEFAULT_BRANCHES:
                continue
            ts = int(bits[1] or 0)
            ks = keys_in(bits[0])
            if ts < since or not ks:
                continue
            ref = f"{name}@{bits[0]}"
            branches[ref] = Branch(repo=name, name=bits[0], updated=ts, author=ppl.of(bits[2], bits[3].strip("<>")))
            for k in ks:
                tie(k, TieKind.BRANCH_NAME, ref, f"{k} is in the branch name")
    card_of = {}
    for c in cards:
        for b in c.get("branches", []):
            rname = name_of.get(b["repo"], Path(b["repo"]).name)
            ref = f"{rname}@{b['branch']}"
            branches.setdefault(ref, Branch(repo=rname, name=b["branch"], updated=int(c.get("updated") or 0),
                                            author=ppl.me or ""))
            for k in keys_in(b["branch"]):
                tie(k, TieKind.BRANCH_NAME, ref, f"{k} is in the branch name")
        for k in c.get("keys", []):
            if k.split("-")[0] in prefixes:
                tie(k, TieKind.CARD, c["id"], "one of your threads holds it")
                card_of.setdefault(k, []).append(c)
        for u in [p["url"] for p in c.get("prs", [])]:
            for k in c.get("keys", []):
                if u in prs:
                    tie(k, TieKind.PR_TITLE if k in prs[u].title else TieKind.CARD, u,
                        f"{k} is in the pull request's title" if k in prs[u].title else "one of your threads holds both")

    lap("git")
    # commits that name a ticket: their authors work on it
    committers = defaultdict(Counter)   # key -> pid -> n
    commit_last = defaultdict(int)
    commit_subject = {}
    for repo, sha, ts, author, subject in commits:
        for k in keys_in(subject):
            pid = ppl.of(email=author) if "@" in (author or "") else ppl.of(author)
            if pid:
                committers[k][pid] += 1
            if int(ts) > commit_last[k]:
                commit_last[k] = int(ts)
                commit_subject[k] = re.sub(rf"[\s:(\[]*\b{k}\b[\s:)\]]*", " ", subject).strip(" -:") or subject
            tie(k, TieKind.COMMIT, f"{name_of.get(repo, Path(repo).name)}@{sha[:10]}", f"a commit subject names {k}")

    # tickets known only by their key: a branch, a pull request, a commit or a card names them
    # A key that only commit subjects name is noise, unless the user wrote one of those commits.
    strong = {t.ticket for t in ties if t.kind != TieKind.COMMIT} | {k for k, c in committers.items() if ppl.me in c}
    for t in ties:
        if t.ticket not in tickets and t.ticket in strong:
            title = ""
            if t.kind in (TieKind.PR_TITLE, TieKind.PR_BRANCH, TieKind.PR_BODY) and t.ref in prs:
                title = prs[t.ref].title
            elif t.kind == TieKind.CARD:
                c = next((c for c in cards if c["id"] == t.ref), None)
                title = (c or {}).get("title", "")
            title = title or commit_subject.get(t.ticket, "")
            site = next((x.url.rsplit("/", 1)[0] for x in tickets.values() if x.url and x.key.split("-")[0] == t.ticket.split("-")[0]), "")
            tickets[t.ticket] = _Ticket(key=t.ticket, title=title or t.ticket, url=f"{site}/{t.ticket}" if site else "",
                                        known=False)
        elif t.ticket in tickets and not tickets[t.ticket].title:
            tickets[t.ticket].title = t.ticket

    by_ticket = defaultdict(list)
    for t in ties:
        by_ticket[t.ticket].append(t)

    # the state and the people of each ticket
    PT: dict[str, ProjectTicket] = {}
    sessions_of = defaultdict(set)
    for key, t in tickets.items():
        tt = by_ticket.get(key, [])
        pr_urls = list(dict.fromkeys(x.ref for x in tt if x.ref in prs))
        br_refs = list(dict.fromkeys(x.ref for x in tt if x.ref in branches))
        tprs = [prs[u] for u in pr_urls]
        last = max([t.updated] + [p.updated for p in tprs] + [branches[b].updated for b in br_refs] + [commit_last.get(key, 0)] +
                   [int(c.get("updated") or 0) for c in card_of.get(key, [])])
        if last < since:
            continue
        open_ = [p for p in tprs if p.state in (PrState.REVIEW, PrState.DRAFT, PrState.OPEN)]
        merged = [p for p in tprs if p.state == PrState.MERGED]
        if t.state == TicketState.DONE:
            state, why = TicketState.DONE, f"the ticket is {t.status or 'done'}"
        elif merged and not open_:
            state, why = TicketState.DONE, "its pull request is merged" if len(merged) == 1 else f"its {len(merged)} pull requests are merged"
        elif any(p.state == PrState.REVIEW for p in open_):
            p = next(p for p in open_ if p.state == PrState.REVIEW)
            state, why = TicketState.CLOSE, f"a pull request is in review ({p.review})"
        elif open_:
            state, why = TicketState.PROGRESS, "a pull request is a draft" if all(p.state == PrState.DRAFT for p in open_) else "a pull request is open"
        elif t.state == TicketState.CLOSE:
            state, why = TicketState.CLOSE, f"the ticket is {t.status}"
        elif br_refs and t.state == TicketState.TODO:
            state, why = TicketState.PROGRESS, "a branch names it"
        else:
            state, why = t.state, (f"the ticket is {t.status}" if t.status else "no status yet")
        roles = defaultdict(set)
        for name, email in t.assignees:
            if pid := ppl.of(name, email):
                roles[pid].add(Role.ASSIGNEE)
        for p in tprs:
            if p.author:
                roles[p.author].add(Role.AUTHOR)
            for r in p.reviewers:
                roles[r].add(Role.REVIEWER)
        for b in br_refs:
            if branches[b].author:
                roles[branches[b].author].add(Role.COMMITTER)
        for pid in committers.get(key, {}):
            roles[pid].add(Role.COMMITTER)
        if ppl.me and (card_of.get(key) or t.seen_in):
            roles[ppl.me].add(Role.CONTEXT)
        for c in card_of.get(key, []):
            sessions_of[key] |= {s["id"] for s in c.get("sessions", [])}
        sessions_of[key] |= t.seen_in
        members = sorted((ppl.member(pid, rs) for pid, rs in roles.items()),
                         key=lambda m: (not m.me, -max(ROLE_WEIGHT[r] for r in m.roles), -m.closeness))
        PT[key] = ProjectTicket(key=key, title=t.title, url=t.url, status=t.status, state=state, why=why, updated=last,
                                prs=pr_urls, branches=br_refs, people=members, path=t.path, known=t.known)

    # groups: the service's own first, then links, shared pull requests, branches and cards, and title prefixes
    group: dict[str, tuple[GroupKind, str]] = {}
    parents = {x.parent for x in tickets.values() if x.parent}
    for key in PT:
        t = tickets[key]
        if t.parent:
            group[key] = (GroupKind.PARENT, t.parent)
        elif key in parents:
            group[key] = (GroupKind.PARENT, key)   # the parent itself
        elif t.linear:
            group[key] = (GroupKind.LINEAR, t.linear)
        elif t.component:
            group[key] = (GroupKind.COMPONENT, f"{key.split('-')[0]}/{t.component}")
        else:
            ms = Counter(prs[u].milestone for u in PT[key].prs if prs[u].milestone)
            if ms:
                group[key] = (GroupKind.MILESTONE, ms.most_common(1)[0][0])
    # a parent-less ticket joins a parent group when its title prefix is the prefix of most of that group
    pre = {k: prefix_of(PT[k].title) for k in PT}
    group_prefix = {}
    by_group = defaultdict(list)
    for k, g in group.items():
        by_group[g].append(k)
    for g, ks in by_group.items():
        c = Counter(pre[k] for k in ks if pre[k])
        if c:
            p, n = c.most_common(1)[0]
            if n * 2 > len(ks):
                group_prefix.setdefault(p, g)
    shared = defaultdict(list)   # a pull request, a branch or a card -> its tickets
    for k in PT:
        for x in by_ticket.get(k, []):
            if x.kind != TieKind.COMMIT and k not in shared[x.ref]:
                shared[x.ref].append(k)
    # ... and a parent-less ticket that shares a pull request, a branch or a card with a grouped ticket joins its group
    u = work._Union()
    rest = [k for k in PT if k not in group]
    for k in rest:
        near = Counter(group[o] for x in by_ticket.get(k, []) for o in shared.get(x.ref, ()) if o in group)
        if pre[k] in group_prefix:
            group[k] = group_prefix[pre[k]]
        elif near:
            group[k] = near.most_common(1)[0][0]
    rest = [k for k in rest if k not in group]
    rest_set = set(rest)
    n_prefix = Counter(pre[k] for k in rest if pre[k])
    first_of_prefix = {}
    for k in rest:
        u.find(k)
        for o in tickets[k].links & rest_set:
            u.join(k, o)
        if pre[k] and 1 < n_prefix[pre[k]] <= PREFIX_GROUP_MAX:
            u.join(k, first_of_prefix.setdefault(pre[k], k))
    for ks in shared.values():
        ks = [k for k in ks if k in rest_set]
        for o in ks[1:]:
            u.join(ks[0], o)
    comps = defaultdict(list)
    for k in rest:
        comps[u.find(k)].append(k)
    for ks in comps.values():
        if len(ks) == 1:
            group[ks[0]] = (GroupKind.TICKET, ks[0])
        else:
            for k in ks:
                group[k] = (GroupKind.LINKED, min(ks, key=lambda x: (x.split("-")[0], int(x.split("-")[1]))))

    projects: dict[tuple, Project] = {}
    for key in sorted(PT, key=lambda k: -PT[k].updated):
        g = group[key]
        p = projects.get(g)
        if not p:
            kind, ref = g
            if kind == GroupKind.PARENT:
                par = tickets.get(ref)
                name = (par.title if par and par.known else "") or next(
                    (tickets[k].parent_title for k in PT if tickets[k].parent == ref and tickets[k].parent_title), ref)
                url = (par.url if par else "") or next((tickets[k].parent_url for k in PT if tickets[k].parent == ref and tickets[k].parent_url), "")
            elif kind == GroupKind.TICKET:
                name, url = PT[key].title, PT[key].url
            elif kind == GroupKind.LINKED:
                ks = [k for k in PT if group[k] == g]
                c = Counter(pre[k] for k in ks if pre[k])
                if c and c.most_common(1)[0][1] * 2 > len(ks):
                    p0 = c.most_common(1)[0][0]
                    name = next(m.group(1) or m.group(2) for k in ks if pre[k] == p0 and (m := _PREFIX.match(PT[k].title)))
                    name = name.strip()
                    name = name[:1].upper() + name[1:]
                else:
                    name = PT[key].title
                url = ""
            else:
                name, url = ref.split("/", 1)[-1], ""
            p = projects[g] = Project(id=_hash(f"{kind}:{ref}"), name=name, kind=kind, ref=ref, url=url)
        p.tickets.append(PT[key])

    # Pull requests with no ticket, by repo: the user's own (author, reviewer or in a context card) in one project,
    # and those of the people close to the user in another. The rest stay out.
    tied_prs = {u for t in PT.values() for u in t.prs}
    loose = defaultdict(list)
    for url_, pr in prs.items():
        if url_ in tied_prs or pr.updated < since:
            continue
        if pr.author == ppl.me or ppl.me in pr.reviewers or url_ in card_prs:
            loose[(pr.repo, True)].append(url_)
        elif ppl.close(pr.author) >= CLOSE_PR_AUTHOR:
            loose[(pr.repo, False)].append(url_)
    for (repo, mine), urls in loose.items():
        ref = f"{repo}:{'mine' if mine else 'team'}"
        projects[(GroupKind.PR, ref)] = Project(
            id=_hash(f"pr:{ref}"), name=f"{repo}: {'your' if mine else 'team'} work without a ticket", kind=GroupKind.PR,
            ref=ref, prs=[prs[u] for u in urls])

    out = []
    for g, p in projects.items():
        pr_urls = list(dict.fromkeys(u for t in p.tickets for u in t.prs)) if p.kind != GroupKind.PR else [x.url for x in p.prs]
        p.prs = sorted((prs[u] for u in pr_urls), key=lambda x: -x.updated)
        p.branches = sorted({b: branches[b] for t in p.tickets for b in t.branches}.values(), key=lambda b: -b.updated)
        keys = {t.key for t in p.tickets}
        p.ties = [x for x in ties if x.ticket in keys]
        p.sessions = sorted({s for k in keys for s in sessions_of.get(k, ())} |
                            {s["id"] for u in pr_urls for c in cards if c["id"] in card_prs.get(u, ()) for s in c.get("sessions", [])})
        roles = defaultdict(set)
        my_last = 0
        for t in p.tickets:
            for m in t.people:
                roles[m.id] |= set(m.roles)
                if m.me:
                    my_last = max(my_last, t.updated)
        for pr in (p.prs if p.kind == GroupKind.PR else []):
            pids = {pr.author, *pr.reviewers} - {""}
            if pr.author:
                roles[pr.author].add(Role.AUTHOR)
            for r in pr.reviewers:
                roles[r].add(Role.REVIEWER)
            if card_prs.get(pr.url) and ppl.me:
                roles[ppl.me].add(Role.CONTEXT)
                pids.add(ppl.me)
            if ppl.me in pids:
                my_last = max(my_last, pr.updated)
        p.members = sorted((ppl.member(pid, rs) for pid, rs in roles.items()),
                           key=lambda m: (not m.me, -max(ROLE_WEIGHT[r] for r in m.roles), -m.closeness))
        p.last = max([t.updated for t in p.tickets] + [x.updated for x in p.prs] + [0])
        p.my_last = my_last
        me = next((m for m in p.members if m.me), None)
        p.my_roles = me.roles if me else []
        # the user's involvement: the weight of their strongest role on each ticket, as a share of the tickets
        if me:
            tw = [max((ROLE_WEIGHT[r] for m in t.people if m.me for r in m.roles), default=0) for t in p.tickets] or \
                 [max(ROLE_WEIGHT[r] for r in me.roles)]
            p.involvement = round(min(1.0, sum(tw) / (3.0 * len(tw))), 3)
        others = sorted((m.closeness for m in p.members if not m.me), reverse=True)[:3]
        p.closeness = round(sum(others) / len(others), 3) if others else 0.0
        p.section = Section.MINE if me else Section.AROUND
        rec = _decay(max(p.last, p.my_last), now)
        p.score = round(rec * (0.4 + 0.6 * p.involvement) if me else rec * (0.15 + p.closeness), 5)
        p.stale = p.last < now - stale_days * 86400
        n = len(p.tickets)
        st = Counter(t.state for t in p.tickets)
        prst = Counter(x.state for x in p.prs)
        # Likely done: every ticket is done, or every pull request is merged (or closed) and no ticket is still in
        # progress or close to done. A bucket of pull requests with no ticket is never done.
        p.likely_done = p.kind != GroupKind.PR and bool(
            (n and st[TicketState.DONE] == n) or
            (p.prs and prst[PrState.MERGED] and all(x.state in (PrState.MERGED, PrState.CLOSED) for x in p.prs)
             and not st[TicketState.PROGRESS] and not st[TicketState.CLOSE]))
        p.counts = dict(tickets=n, done=st[TicketState.DONE], close=st[TicketState.CLOSE],
                        progress=st[TicketState.PROGRESS], todo=st[TicketState.TODO], prs=len(p.prs),
                        merged=prst[PrState.MERGED], review=prst[PrState.REVIEW], draft=prst[PrState.DRAFT],
                        open=prst[PrState.OPEN], closed=prst[PrState.CLOSED], branches=len(p.branches),
                        people=len([m for m in p.members if not m.me]))
        out.append(p)
    out.sort(key=lambda p: (p.section != Section.MINE, p.stale, -p.score, -p.last))
    lap("model")
    counts = dict(projects=len(out), tickets=sum(len(p.tickets) for p in out), ties=len(ties), prs=len(prs),
                  branches=len(branches), mine=sum(p.section == Section.MINE for p in out),
                  by_kind=dict(Counter(p.kind for p in out)), phases=phases)
    return Build(projects=out, me=ppl.me, built=now, took=round(time.time() - t0, 3), counts=counts, stale_days=stale_days)
