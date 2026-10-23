"""The Context tab: one context card for each piece of work, with what belongs to it across sources.

A task joins agent sessions, git branches, commits and connection items (tickets, threads) by these links:
- a ticket key such as ABC-123 in a branch name, a commit subject, a session's text or a connection item;
- the git branch a session ran on (feature branches only: a default or long-lived branch holds many tasks);
- for work without a ticket: sessions in the same repo, a few days apart, that edited the same specific files.

`coworkers` finds what coworkers did near a task: their recent commits on remote branches in the same folders."""
from dataclasses import asdict
from functools import lru_cache
import json
import re
import subprocess
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pensieve import cardtext, connections, repos as repos_mod, task_states

KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d{1,6})\b")
# a ticket's page: Jira (…/browse/KEY) and Linear (…/issue/KEY); group 1 is the site part that comes before the key
TICKET_URL = re.compile(r"(https://[\w-]+\.atlassian\.net/browse|https://linear\.app/[\w-]+/issue)/([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
# pull and merge requests on GitHub, GitLab, Bitbucket and Azure DevOps
PR_URL = re.compile(r"https://(?:github\.com/[\w.-]+/[\w.-]+/pull/\d+|[\w.-]*gitlab[\w.-]*/[\w./-]+?/-/merge_requests/\d+"
                    r"|bitbucket\.org/[\w.-]+/[\w.-]+/pull-requests/\d+|dev\.azure\.com/[\w./%-]+?/pullrequest/\d+)")
DEFAULT_BRANCHES = {"", "HEAD", "main", "master", "develop", "dev", "trunk"}
SHARED_FILE_MAX = 3      # a file edited by more sessions than this is shared ground, not one task's
SAME_TASK_DAYS = 3       # sessions this far apart can still be one task through their files
LONG_BRANCH_SESSIONS, LONG_BRANCH_DAYS = 6, 4  # more sessions than this, or a longer span: a long-lived branch
_TITLE_WORD = re.compile(r"[a-z0-9][a-z0-9.+#-]*")
_STOP = {"a", "an", "the", "and", "or", "for", "of", "to", "in", "on", "with", "from", "into", "by", "at", "is", "it",
         "this", "that", "my", "our", "how", "what", "why", "do", "does", "can", "we", "i", "vs", "via", "new"}
_TEMP = ("/private/tmp/", "/tmp/", "/private/var/folders/", "/var/folders/")
_roots = {}              # folder -> its git root, or ""
_cache = {}


def _not_users(project: str) -> bool:
    """A session that ran in a temp folder or under ~/Library: a script's or an app's, not the user's own work."""
    p = project.rstrip("/") + "/"
    return p.startswith(_TEMP) or p.startswith(str(Path.home() / "Library") + "/")


def _in_temp(project: str) -> bool:
    """A session in a system temp folder, when `skip_temp_sessions` is on (as the indexer skips them)."""
    from pensieve import indexer
    return indexer.in_temp(project)


def _branches(path: str) -> dict[tuple[str, str], float]:
    """Every git branch that a Claude Code transcript records, by (cwd, branch), with the time of its last message
    (pensieve/transcripts.py). "HEAD" is left out: Claude Code records it during some git operations, so it is not
    the branch the work is on."""
    from pensieve import transcripts
    return transcripts.branches(path)


def _worktrees(root: str) -> dict[str, tuple[str, str]]:
    """{worktree path: (repo root, branch)} of a repo, from git."""
    out, path, branch = {}, "", ""
    for line in _git(root, "worktree", "list", "--porcelain", ttl=60).splitlines():
        if line.startswith("worktree "):
            path, branch = line[9:], ""
        elif line.startswith("branch "):
            branch = line[7:].removeprefix("refs/heads/")
        elif not line.strip() and path:
            out[path] = (root, branch)
            path = ""
    if path:
        out[path] = (root, branch)
    return out


def _commits(root: str, since: float, end: float) -> list[tuple]:
    """(repo, sha, ts, author, subject) of a repo's commits on any branch in the window, from git."""
    out = []
    raw = _git(root, "log", "--all", "--no-merges", f"--since={int(since)}", f"--until={int(end)}", "-n", "2000",
               "--format=%H%x1f%ct%x1f%an%x1f%s", ttl=300)
    for line in raw.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4 and parts[1].isdigit():
            out.append((root, parts[0], int(parts[1]), parts[2], parts[3]))
    return out


def _branch(path: str, cwd: str = "") -> str:
    """The branch a Claude Code session works on now: its newest recorded branch, in its own folder when one is
    given."""
    bs = _branches(path)
    mine = {k: v for k, v in bs.items() if not cwd or k[0] == cwd or k[0].startswith(cwd + "/")} or bs
    return max(mine, key=mine.get)[1] if mine else ""


def _ts(s) -> float:
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _items(since: float = 0, until: float | None = None) -> list[dict]:
    """Connection items of switched-on sources whose time is in (since, until]: {path, doc, server, key, title,
    status, assignee, updated, url, branch, pr, text, private}, from the `documents` table (an SQL range on its time
    index). `path` is the item's id, doc:<id>. `private`: a direct message, a group message or a private channel,
    which local cards use and shared cards and notes leave out."""
    from pensieve import store as v2
    from pensieve.store import documents
    db = v2.store_db()
    docs = documents.window(db, int(since), int(until) if until else None, sources=connections.indexed())
    return [_item(d, ppl) for d, ppl in zip(docs, _assignees(db, [d.id for d in docs]))]


def _merge_tickets(tickets: list[dict]) -> list[dict]:
    """One entry per key, newest first. Several rows hold one ticket (the keeper's copy and the copies an agent's
    tool calls returned): the newest row gives the status, and each other field comes from the newest row that has
    it, since a copy from a tool call often has the key for a title and no description."""
    fields = ("key", "title", "status", "assignee", "url", "server", "path", "updated")
    by_key: dict[str, list[dict]] = {}
    for i in sorted(tickets, key=lambda i: -i["updated"]):
        by_key.setdefault(i["key"], []).append(i)
    out = []
    for key, rows in by_key.items():
        t = {**{k: rows[0][k] for k in fields}, "text": ""}
        for k in ("title", "assignee", "url", "server"):
            t[k] = next((r.get(k) for r in rows if r.get(k) and (k != "title" or r[k] != key)), rows[0].get(k) or "")
        t["text"] = _ticket_body(max((r.get("text") or "" for r in rows), key=len))[:2000]  # the fullest description
        out.append(t)
    return out


def _ticket_body(text: str) -> str:
    """The ticket's description out of a stored item's text, which starts with a heading and a list of fields (key,
    URL, author, status), and holds the description last, after a `description:` (Jira, Linear) or `notes:` (Asana)
    line."""
    m = re.search(r"^(description|notes):\s*", text, re.M)
    if m:
        return text[m.end():].strip()
    lines = [ln for ln in text.splitlines() if not ln.startswith(("# ", "- ")) and ":" not in ln[:12]]
    return "\n".join(lines).strip()


def _older_items(items: list[dict], prefixes: set, S: dict, facts: dict, commits, until) -> list[dict]:
    """Items from before the window that the window's work names: tickets by their keys (in session titles, branches
    and text, commit subjects, and the window's own tickets) and pull requests by their head branches. A ticket that last changed 40 days ago
    still gives its title and status to this week's session about it."""
    from pensieve import store as v2
    from pensieve.store import documents
    db, srcs = v2.store_db(), connections.indexed()
    prefixes = prefixes | documents.ticket_prefixes(db, srcs)
    have = {i["doc"] for i in items}
    keys = {f"{a}-{b}" for s in S.values() for t in [s["title"], s["branch"]] for a, b in KEY.findall(t) if a in prefixes}
    keys |= {f"{a}-{b}" for s in S.values() for a, b in facts[s["id"]]["keys"] if a in prefixes}
    keys |= {f"{a}-{b}" for c in commits for a, b in KEY.findall(c[4]) if a in prefixes}
    keys |= {i["key"] for i in items if i["key"]}  # older copies of a window's ticket (another tool's, with its people)
    branches = {s["branch"] for s in S.values() if s["branch"] not in DEFAULT_BRANCHES}
    docs = [d for d in documents.by_keys(db, keys, srcs) + documents.by_branches(db, branches, srcs)
            if d.id not in have and (not until or (d.time or 0) <= until)]
    docs = list({d.id: d for d in docs}.values())
    return [_item(d, ppl) for d, ppl in zip(docs, _assignees(db, [d.id for d in docs]))]


def _assignees(db, ids) -> list[list]:
    """The people of each document, in the order of `ids`."""
    from pensieve.store import documents
    by = documents.people_of(db, ids)
    return [by.get(i, []) for i in ids]


def _owners(d, people) -> set[str]:
    """The people a ticket belongs to, by name and email, lower case (see _is_mine): its assignees, authors and
    owners, and the assignee, reporter and creator of the service's item."""
    out = set()
    for p in people:
        if p.role in ("assignee", "author", "owner"):
            out.add((p.name or "").strip().lower())
            kind, _, v = p.key.partition(":")
            if kind in ("email", "name"):
                out.add(v.strip().lower())
    if d.kind == "ticket":
        try:
            raw = json.loads(d.raw or "{}")
        except ValueError:
            raw = {}
        for f in ("assignee", "reporter", "creator"):
            v = raw.get(f) if isinstance(raw.get(f), dict) else (raw.get("fields") or {}).get(f)
            if isinstance(v, dict):
                out |= {str(v.get(k) or "").strip().lower() for k in ("displayName", "emailAddress", "name", "email")}
    return out - {""}


def _item(d, people: list) -> dict:
    url = d.url or ""
    assignees = [p.name or p.key.split(":", 1)[1] for p in people if p.role == "assignee"]
    return dict(path=f"doc:{d.id}", doc=d.id, server=d.source_id, key=d.key if d.kind == "ticket" else "",
                title=d.title, url=url, branch=d.branch or "", pr=bool(PR_URL.fullmatch(url)) if url else False,
                status=d.status or "", assignee=", ".join(dict.fromkeys(assignees)),
                updated=float(d.time or 0), text=d.text, private=d.private, owners=_owners(d, people))


_my_ids_cache = (0.0, set())


def _my_ids() -> set[str]:
    """The user's names and emails, lower case: from the People graph's "me" person, and git's user.name and
    user.email."""
    global _my_ids_cache
    if time.time() - _my_ids_cache[0] < 300:
        return _my_ids_cache[1]
    out = {_git(str(Path.home()), "config", "--global", k, ttl=3600).strip().lower() for k in ("user.name", "user.email")}
    try:
        from pensieve import people
        g = people.graph()
        me = next((p for p in (g or {}).get("people", {}).values() if p.get("me")), None)
        for k in (me or {}).get("keys", []):
            kind, _, v = k.partition(":")
            if kind in ("email", "name"):
                out.add(v.lower())
        if me and me.get("name"):
            out.add(me["name"].lower())
    except Exception:
        pass
    _my_ids_cache = (time.time(), out - {""})
    return _my_ids_cache[1]


def _is_mine(item: dict) -> bool:
    """A ticket that belongs to the user: they are its assignee, creator or owner."""
    me = _my_ids()
    norm = {n for n in me if "@" not in n}
    return any(o in me or o in norm for o in item.get("owners", ()))


class _Union:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def join(self, a, b):
        self.p[self.find(a)] = self.find(b)


listeners = []      # called (from a worker thread) when a background refresh has new tasks
_refreshing = set()
_net = {"off": False}     # True: answer gh lookups from the cache only


def tasks(store, days: int = 30, ids=None) -> list[dict]:
    """The tasks of the last `days` days (or only those in `ids`), in the order of the Context tab. Up to 30 days they
    are the rows of the `cards` table, which the card builder (cardbuild.py) keeps up to date; a shorter window keeps
    the cards updated in it. When no builder has written the table yet (the MCP process before the app ran), or for a
    longer window, they are built here, as below."""
    from pensieve import cardbuild
    if days <= cardbuild.DAYS:
        got = _card_views(ids)
        if got is not None:
            since = time.time() - days * 86400
            return sorted((t for t in got if t["updated"] >= since or t.get("active")), key=_order)
    out = _built(store, max(days, cardbuild.DAYS) if days <= cardbuild.DAYS else days)
    return [t for t in out if ids is None or t["id"] in ids]


_views_cache = [None, []]


def _card_views(ids=None) -> list[dict] | None:
    """The cards of the `cards` table, parsed (again only after a commit), or None when it has none yet."""
    from pensieve import store as v2
    from pensieve.store import cards as store_cards
    db = v2.store_db()
    if ids is not None:
        return [json.loads(v) for v in store_cards.views(db, ids).values()]
    key = (db.one("PRAGMA data_version")[0], threading.get_ident())
    if _views_cache[0] != key:
        views = store_cards.views(db)
        _views_cache[:] = [key, [json.loads(v) for v in views.values()] if views else None]
    return _views_cache[1]


def _built(store, days: int) -> list[dict]:
    """The tasks of the last `days` days, built here. The last result is served at once; when it is older than a
    minute, a background refresh replaces it and calls `listeners`. The first build makes no network calls (gh), so
    it is fast; the refresh after it adds the pull request states."""
    hit = _cache.get(days)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    if not hit:
        _net["off"] = True
        try:
            out = _tasks(store, days)
        finally:
            _net["off"] = False
        _cache[days] = (0.0, out)  # counts as old: the refresh below adds what the network knows
        hit = _cache[days]
    if days not in _refreshing:
        _refreshing.add(days)

        def run():
            try:
                _cache[days] = (time.time(), _tasks(store, days))
                for fn in listeners:
                    fn()
            except Exception:
                import traceback
                traceback.print_exc()
            finally:
                _refreshing.discard(days)
        threading.Thread(target=run, daemon=True).start()
    return hit[1]


def tasks_at(store, until: float, days: int = 30) -> list[dict]:
    """The tasks as they were at `until` (a past time), for a replay: only the sessions that had started, and the
    commits and connection items from before it. A session that ran on past `until` keeps its later text (chunks
    have no time). No git or gh calls: folders and PR states are not part of a replay."""
    return _tasks(store, days, until)


def _tasks(store, days, until: float | None = None):
    inp = Inputs(days, until)
    inp.load(store)
    return finish(group(inp), inp, store)


class Inputs:
    """What a card build reads: the sessions of the window with the facts of their text, their edited files, the
    repos and worktrees, the commits and the collected items. The card builder (cardbuild.py) keeps one and loads
    again only the sessions and items that an event names."""

    def __init__(self, days: int, until: float | None = None):
        self.days, self.until = days, until
        self.window()
        self.sessions: dict[str, tuple] = {}  # id -> (id, project, title, summary, updated, started, path)
        self.facts: dict[str, dict] = {}      # id -> what the session's text names (_facts)
        self.edits: dict[str, set] = {}
        self.roots, self.wts, self.commits = [], {}, []
        self.items: dict[int, dict] = {}      # doc id -> item, of the window

    def window(self):
        end = datetime.fromtimestamp(self.until, timezone.utc) if self.until else datetime.now(timezone.utc)
        self.end = end.timestamp()
        self.since_iso = (end - timedelta(days=self.days)).strftime("%Y-%m-%dT%H:%M:%S")
        self.upto_iso = end.strftime("%Y-%m-%dT%H:%M:%S.999Z")
        self.since = _ts(self.since_iso + "+00:00")

    def load(self, store):
        """Everything, for a full build."""
        self.sessions, self.facts, self.edits = {}, {}, {}
        self.load_sessions(store)
        self.load_repos(store)
        self.items = {i["doc"]: i for i in _items(self.since, self.until)}

    def load_sessions(self, store=None, ids=None):
        """The sessions of the window (or only `ids`, which leave when they are no longer in it), their text facts
        and their edited files, from the transcripts (pensieve/transcripts.py: read once, incrementally). With no
        `ids`, the transcript folders are read for changes first."""
        from pensieve import transcripts
        if ids is None:
            transcripts.sync()
            by_id = {f.id: f for f in transcripts.sessions_since(self.since)}
        else:
            by_id = {f.id: f for f in transcripts.all_facts().values()}
        for sid in (list(ids) if ids is not None else list(by_id)):
            self.sessions.pop(sid, None)
            self.facts.pop(sid, None)
            self.edits.pop(sid, None)
            f = by_id.get(sid)
            if not f or not f.n_turns or not f.updated or f.state.get("own") or _in_temp(f.project):
                continue
            upd, started = f.updated_ts(), _ts(f.started)
            if not (self.since < upd and (started or upd) <= self.end):
                continue
            self.sessions[sid] = (sid, f.project, f.title, "", f.updated, f.started, f.path)
            self.facts[sid] = transcripts.text_facts(f)
            self.edits[sid] = {p for p in f.edited if not p.startswith(_TEMP)}  # scratch files are not the work

    def load_repos(self, store=None):
        """The repos, worktrees and commits of the window: from the index when the repo indexer runs (developer
        mode), else from git, for the repos the window's sessions ran in."""
        rows = []
        if store is not None:
            try:
                rows = store.db.execute("SELECT root FROM repos").fetchall()
            except Exception:
                rows = []
        if rows:
            db = store.db
            self.roots = [r[0] for r in rows]
            self.wts = {w: (r, b) for w, r, b in db.execute("SELECT path, repo, branch FROM worktrees").fetchall()}
            self.commits = db.execute("SELECT repo, sha, ts, author, subject FROM commits WHERE ts > ? AND ts <= ?",
                                      (self.since, self.end)).fetchall()
            return
        roots = sorted({r for r in (_git_root(s[1]) for s in self.sessions.values() if s[1]) if r})
        self.roots, self.wts, self.commits = roots, {}, []
        for root in roots:
            self.wts.update(_worktrees(root))
            self.commits += _commits(root, self.since, self.end)

    def load_items(self, ids):
        """The collected items `ids` again: a changed one is replaced, one that left the window or the switched-on
        sources goes."""
        from pensieve import store as v2
        from pensieve.store import documents
        db, srcs = v2.store_db(), set(connections.indexed())
        ids = list(ids)
        for i in ids:
            self.items.pop(i, None)
        docs = [d for d in documents.get_many(db, ids) if d.source_id in srcs and self.since < (d.time or 0)
                and (not self.until or (d.time or 0) <= self.until)]
        for d, ppl in zip(docs, _assignees(db, [d.id for d in docs])):
            self.items[d.id] = _item(d, ppl)


def group(inp: Inputs) -> list[dict]:
    """The cards of the inputs, before their folders, PR states and states: sessions, branches, tickets, commits
    and items joined by the links of the module docstring."""
    until, wts, roots, commits = inp.until, inp.wts, inp.roots, inp.commits
    items = list(inp.items.values())
    facts = inp.facts

    def repo_of(cwd):
        for w, (r, b) in wts.items():
            if cwd == w or cwd.startswith(w + "/"):
                return r, b
        r = max((r for r in roots if cwd == r or cwd.startswith(r + "/")), key=len, default="")
        return r, None

    # ticket keys count only with a known project prefix: from collected tickets and branch names
    prefixes = {KEY.fullmatch(i["key"]).group(1) for i in items if i["key"]}
    S, worked_of = {}, {}
    for sid, project, title, summary, updated, started, path in sorted(inp.sessions.values(), key=lambda r: r[4] or "",
                                                                       reverse=True):
        if _not_users(project or ""):
            continue
        repo, wt_branch = repo_of(project or "")
        branch = wt_branch or (_branch(path, project or "") if sid.startswith("claude:") else "")
        # every branch the session worked on, by repo: a long session can move between branches and worktrees
        worked = {}
        if sid.startswith("claude:") and until is None:
            for (cwd, b), at in _branches(path).items():
                r, wb = repo_of(cwd)
                if r and b not in DEFAULT_BRANCHES:
                    worked[(r, wb or b)] = max(worked.get((r, wb or b), 0), at)
        S[sid] = dict(id=sid, title=title or "", summary=summary or "", updated=min(_ts(updated), until or 9e18), started=_ts(started),
                      project=project or "", repo=repo, branch=branch, edited=inp.edits.get(sid, set()), path=path)
        worked_of[sid] = worked  # apart from S: its keys are tuples, and S goes into the card builder's JSON
        prefixes |= {m.group(1) for m in KEY.finditer(branch)}
    # ticket links in the sessions' text teach each project prefix and its site, so a key alone becomes a link
    sites = {}
    for sid in S:
        for base, key in facts[sid]["ticket_urls"]:
            sites.setdefault(KEY.fullmatch(key).group(1), base)
    for i in items:
        if i["key"] and i["url"]:
            sites.setdefault(KEY.fullmatch(i["key"]).group(1), i["url"].rsplit("/" + i["key"], 1)[0])
    prefixes |= set(sites)
    items += _older_items(items, prefixes, S, facts, commits, until)
    prefixes |= {KEY.fullmatch(i["key"]).group(1) for i in items if i["key"]}

    def said(sid) -> Counter:
        """The ticket keys of known projects in a session's text, counted."""
        return Counter({f"{a}-{b}": n for (a, b), n in facts[sid]["keys"].items() if a in prefixes})

    def keys_in(text):
        return [f"{a}-{b}" for a, b in KEY.findall(text) if a in prefixes]

    u = _Union()
    # branches: feature branches only; a branch with many sessions over many days holds many tasks
    by_branch = defaultdict(list)
    for s in S.values():
        if s["branch"] not in DEFAULT_BRANCHES and s["repo"]:
            by_branch[(s["repo"], s["branch"])].append(s)
    long_lived = {k for k, v in by_branch.items() if len(v) > LONG_BRANCH_SESSIONS or
                  (max(x["updated"] for x in v) - min(x["started"] or x["updated"] for x in v)) > LONG_BRANCH_DAYS * 86400}
    for k, v in by_branch.items():
        if k in long_lived:
            continue
        for s in v:
            u.join(f"s:{s['id']}", f"b:{k[0]}@{k[1]}")
        for key in keys_in(k[1]):
            u.join(f"b:{k[0]}@{k[1]}", f"t:{key}")
    # A pull request linked from a session, whose title or head branch names a ticket, ties the ticket to the PR and
    # to the PR's head branch, when the session worked on that branch at some point. A long session moves on to other
    # branches: the ticket's thread keeps its PR and branch, and the session is only shared with it. A session that
    # talks about another ticket's PR, on a branch it never worked on, ties nothing.
    branch_nodes = {k for k in by_branch if k not in long_lived}
    for sid, s in S.items():
        repo = s["repo"]
        if not repo:
            continue
        for url in list(facts[sid]["prs"])[:8]:
            info = _gh_pr(url) if "github.com" in url else {}
            head = info.get("head") or ""
            if not head or head in DEFAULT_BRANCHES or (repo, head) in long_lived \
                    or ((repo, head) not in worked_of[sid] and (repo, head) not in branch_nodes):
                continue
            ks = keys_in(f"{info.get('title', '')} {head}")
            for key in ks:
                u.join(f"b:{repo}@{head}", f"t:{key}")
            if ks:
                u.join(f"p:{url}", f"b:{repo}@{head}")
    # Ticket keys a session talks about. A key in the session's title or branch always links. A key only in its text
    # links when the session runs in the key's home repo (where a branch, commit or session title names it), so a
    # session that just quotes another repo's ticket does not join that ticket's task.
    home = defaultdict(set)
    for s in S.values():
        for key in keys_in(s["title"] + " " + s["branch"]):
            if s["repo"]:
                home[key].add(s["repo"])
    for repo, sha, ts, author, subject in commits:
        for key in keys_in(subject):
            home[key].add(repo)
    # a key with no home repo of its own: the repos where its project's other keys live (ABC-12 lives where ABC-7 does)
    home_project = defaultdict(set)
    for key, repos in home.items():
        home_project[key.split("-")[0]] |= repos
    homes = lambda k: home[k] or home_project[k.split("-")[0]]
    for s in S.values():
        found = said(s["id"])
        strong = set(keys_in(s["title"] + " " + s["branch"]))
        weak = {k for k in found if k not in strong and s["repo"] and (not homes(k) or s["repo"] in homes(k))}
        # one ticket per session from its text, so a session that mentions two tickets doesn't merge them; more than
        # three keys: a list of tickets, not one task
        pick = strong or ({max(weak, key=lambda k: found[k])} if weak and len(found) <= 3 else set())
        for key in pick:
            u.join(f"s:{s['id']}", f"t:{key}")
    # work without a ticket: sessions in one repo, a few days apart, that edited the same specific files
    # (two specific files in common, or one file that only these two sessions edited)
    n_edits = Counter(p for s in S.values() for p in s["edited"])
    with_edits = sorted((s for s in S.values() if s["edited"] and s["repo"]), key=lambda x: x["updated"])
    for i, a in enumerate(with_edits):
        for b in with_edits[i + 1:]:
            if b["updated"] - a["updated"] > SAME_TASK_DAYS * 86400:
                break
            if a["repo"] != b["repo"]:
                continue
            shared = [p for p in a["edited"] & b["edited"] if n_edits[p] <= SHARED_FILE_MAX]
            if len(shared) >= 2 or any(n_edits[p] == 2 for p in shared):
                u.join(f"s:{a['id']}", f"s:{b['id']}")
    # the same topic in sessions a few days apart, maybe started in different folders: titles that share at least half
    # of their words. Not across two repos, so separate projects stay apart.
    words = {s["id"]: set(_TITLE_WORD.findall(s["title"].lower())) - _STOP for s in S.values()}
    by_time = sorted(S.values(), key=lambda x: x["updated"])
    for i, a in enumerate(by_time):
        for b in by_time[i + 1:]:
            if b["updated"] - a["updated"] > SAME_TASK_DAYS * 86400:
                break
            wa, wb = words[a["id"]], words[b["id"]]
            if len(wa) >= 2 and len(wb) >= 2 and len(wa & wb) * 2 >= len(wa | wb) and \
                    (not a["repo"] or not b["repo"] or a["repo"] == b["repo"]):
                u.join(f"s:{a['id']}", f"s:{b['id']}")
    C = {}
    for repo, sha, ts, author, subject in commits:
        ks = keys_in(subject)
        if ks:
            C[f"c:{sha}"] = dict(repo=repo, sha=sha[:10], ts=ts, author=author, subject=subject)
            for key in ks:
                u.join(f"c:{sha}", f"t:{key}")
    I = {}
    for it in items:
        node = f"i:{it['path']}"
        I[node] = it
        for k in by_branch:  # a pull request joins the feature branch it was opened from
            if it["branch"] and k[1] == it["branch"] and k not in long_lived:
                u.join(node, f"b:{k[0]}@{k[1]}")
        if it["key"]:
            u.join(node, f"t:{it['key']}")
        ks = [k for k in dict.fromkeys(keys_in(it["text"])) if k != it["key"]]  # in the order of the text
        if it["key"]:
            continue  # a ticket that mentions other tickets stays its own task
        for key in ks[:3]:
            u.join(node, f"t:{key}")
        u.find(node)
    for s in S.values():
        u.find(f"s:{s['id']}")

    groups = defaultdict(list)
    for node in list(u.p):
        groups[u.find(node)].append(node)
    out = []
    for nodes in groups.values():
        ss = sorted((S[n[2:]] for n in nodes if n.startswith("s:")), key=lambda x: -x["updated"])
        its = [I[n] for n in nodes if n.startswith("i:")]
        tickets = [i for i in its if i["key"]]
        seen = {i["title"] for i in tickets}  # the same issue from another tool, without its key: show it once
        its = tickets + [i for i in its if not i["key"] and i["title"] not in seen]
        if not ss and not tickets and not any(i["pr"] for i in its):
            continue  # a message or page on its own is not a task
        keys = sorted({n[2:] for n in nodes if n.startswith("t:")} | {i["key"] for i in tickets})
        cs = sorted((C[n] for n in nodes if n.startswith("c:")), key=lambda c: -c["ts"])
        brs = sorted({n[2:] for n in nodes if n.startswith("b:")})
        # Tickets alone (no session, branch or commit of the user) are a card only when the ticket is the user's:
        # agents' ticket searches and collection also bring in other people's tickets.
        if not ss and not brs and tickets and not any(c["author"].lower() in _my_ids() for c in cs) \
                and not any(_is_mine(i) for i in tickets):
            continue
        files = Counter(p for s in ss for p in s["edited"])
        updated = max([s["updated"] for s in ss] + [i["updated"] for i in its] + [c["ts"] for c in cs] + [0])
        main = max(tickets, key=lambda i: i["updated"]) if tickets else None
        prs_seen = [i for i in its if i["pr"]]
        title = main["title"] if main else prs_seen[0]["title"] if prs_seen and not ss else \
            (brs[0].split("@", 1)[1] if brs else ss[0]["title"])
        # pull requests: PR items from connections, and PR links in the sessions' text (most mentioned first)
        urls = sum((facts[s["id"]]["prs"] for s in ss), Counter())
        named = {n for s in ss for n in re.findall(r"\bPR #?(\d+)", s["title"])}  # "PR 2074 …" in a session title
        ranked = sorted(urls, key=lambda u_: (u_.rsplit("/", 1)[-1] not in named, -urls[u_]))
        prs = [dict(url=i["url"], title=i["title"], state=i["status"], source=_server_label(i["server"]), head=i["branch"])
               for i in prs_seen]
        tied = [n[2:] for n in nodes if n.startswith("p:")]  # PRs tied to the thread's ticket (see above)
        prs += [dict(url=u_, title="", state="", source="session") for u_ in dict.fromkeys(tied + ranked[:4])
                if u_ not in {p["url"] for p in prs}]
        out.append(dict(
            prs=prs[:4], _sessions=ss, _named=named,
            last_words=facts[ss[0]["id"]]["last"] if ss else "",  # where the last session stopped
            ending=facts[ss[0]["id"]]["ending"] if ss else "",  # what the note reads
            last_summary=(ss[0]["summary"] or "")[:800] if ss else "",
            earlier=[(s["title"], (s["summary"] or "")[:400]) for s in ss[1:3]],  # what the note's decisions come from
            events=_events(ss, its, cs),
            id=_task_id(keys, brs, ss, its), title=title, kind="ticket" if keys else "branch" if brs else "work",
            keys=keys, status=main["status"] if main else "", updated=updated,
            # tickets the sessions talk about without being their work; a key named once is often an example or a typo
            ticket_refs=[dict(key=k, url=f"{sites[k.split('-')[0]]}/{k}" if k.split("-")[0] in sites else "")
                         for k, n in sum((said(s["id"]) for s in ss), Counter()).most_common()
                         if n >= 2 and k not in keys][:3],
            repos=sorted({s["repo"] for s in ss if s["repo"]} | {c["repo"] for c in cs}),
            branches=[dict(repo=b.split("@", 1)[0], branch=b.split("@", 1)[1]) for b in brs],
            tickets=_merge_tickets(tickets) +
                    [dict(key=k, title="", status="", assignee="", url=f"{sites[k.split('-')[0]]}/{k}", server="",
                          path="", updated=0) for k in keys
                     if k not in {i["key"] for i in tickets} and k.split("-")[0] in sites],  # known only by its key
            items=[{k: i[k] for k in ("title", "url", "server", "path", "updated", "private")}
                   for i in sorted(its, key=lambda i: -i["updated"]) if not i["key"]][:30],
            sessions=[dict(id=s["id"], title=s["title"], updated=s["updated"], branch=s["branch"], repo=s["repo"],
                           project=s["project"], summary=(s["summary"] or "")[:600],
                           artifacts=_artifacts(s, facts[s["id"]]["links"])) for s in ss],
            commits=cs[:30], files=[p for p, _ in files.most_common(15)],
            people=sorted({c["author"] for c in cs} | {t["assignee"] for t in tickets if t["assignee"]})))
    # A ticket that a thread in another repo works on belongs there: sessions here that only name it (a pasted log,
    # a printed list of threads) give it no reference here
    worked_in = defaultdict(set)
    for t in out:
        for k in t["keys"]:
            worked_in[k].update(t["repos"])
    for t in out:
        t["ticket_refs"] = [r for r in t["ticket_refs"] if not worked_in[r["key"]] or worked_in[r["key"]] & set(t["repos"])]
    # What the card's sessions read through MCP (Slack threads, tickets, pages): shown on the card, not joined to it,
    # so that a session that read many items does not merge their cards. v2: the documents that document_sessions
    # links to the card's sessions.
    seen_by = _seen_by([s for t in out for s in (x["id"] for x in t["_sessions"])], until)
    for t in out:
        have = {i["path"] for i in t["items"]} | {k["path"] for k in t["tickets"] if k.get("path")}
        keys = set(t["keys"])
        cand = {i["path"]: i for s in t["_sessions"] for i in seen_by.get(s["id"], []) if i["path"] not in have}
        ranked = [x for x in (_seen_row(i) for i in cand.values()) if x]
        ranked.sort(key=lambda x: (not (keys and any(k in x[1]["text"] for k in keys)), -x[1]["updated"]))
        t["seen"] = [row for row, _ in ranked[:10]]
        t["seen_more"] = max(0, len(ranked) - 10)
    # A session that also worked on a card's branch, without being one of its sessions, is shared with the card: a
    # long session moves between branches, and joining it to every branch would merge their cards into one.
    for t in out:
        feat = {(b["repo"], b["branch"]) for b in t["branches"]}
        own = {s["id"] for s in t["_sessions"]}
        t["_shared"] = [(dict(id=s["id"], title=s["title"], path=s["path"]),
                         max(at for k, at in worked_of[s["id"]].items() if k in feat)) for s in S.values()
                        if s["id"] not in own and feat & worked_of[s["id"]].keys()] if feat else []
        t["shared_sessions"] = [dict(id=s["id"], title=s["title"], at=at)
                                for s, at in sorted(t["_shared"], key=lambda x: -x[1])[:5]]
    return out


def finish(out: list[dict], inp: Inputs, store, pickup=None) -> list[dict]:
    """The cards of `group` with their folders and PRs (`pickup`, git and gh; by default _pickup for every card),
    PR ownership, split goals, tags and states, in the order of the Context tab."""
    from concurrent.futures import ThreadPoolExecutor
    if inp.until is None:  # a replay has no live folders or PR states
        with ThreadPoolExecutor(8) as pool:  # git and gh calls, each cached
            list(pool.map(pickup or (lambda t: _pickup(t, inp.wts)), out))
    else:
        for t in out:
            t["places"] = []
    _own_prs(out)
    from pensieve import session_threads
    out = session_threads.split(out, inp.facts)  # one thread per writing session, each with only its own sinks
    from pensieve import claims
    claims.apply(out)  # a branch shared by several threads goes to the session with the majority claim
    _adopt(out)  # the holder of a branch takes the activity of the other sessions that wrote to it
    manual = task_states.latest(store)
    if inp.until is None:
        _integrated(out)
    for t in out:
        _attention(t)
        _thread(t, inp)
        t["events"] = sorted(t["events"] + _branch_events(t), key=lambda e: -e["ts"])[:80]
        task_states.apply(t, manual.get(t["id"]))
        _title(t)
    _kin(out)
    _related_cards(out)
    for t in out:
        t.pop("_sessions")
        t.pop("_shared", None)
        t.pop("_named", None)
        t.pop("_siblings", None)
        t.pop("_writes", None)
    out.sort(key=_order)
    return out


def _sink_names(t: dict) -> set[str]:
    """The branches (by name), PRs and tickets a thread's session touched: what it wrote to, the branches it holds,
    and the branch its session ran on."""
    names = {key.split("@", 1)[-1] if kind == "branch" else key for kind, key in t.get("_writes", [])}
    names |= {b["branch"] for b in t.get("branches", [])}
    s = t["_sessions"][0] if t.get("_sessions") else {}
    if s.get("branch") and s["branch"] not in DEFAULT_BRANCHES:
        names.add(s["branch"])
    return names


def _adopt(threads: list[dict]) -> None:
    """A thread that holds a branch takes the other sessions that wrote to the branch as shared sessions: their
    activity keeps it fresh, a running one marks it active, and the card lists them. (claims.apply gave the branch
    to one thread; the others keep their own cards.)"""
    from pensieve import session_threads
    by_id = {t["id"]: t for t in threads}
    for t in threads:
        held = {b["branch"] for b in t.get("branches", [])}
        if not held or not t.get("_siblings"):
            continue
        for s in t["_siblings"]:
            o = by_id.get(session_threads.thread_id(s["id"]))
            if not o or not o.get("_sessions"):
                continue
            wrote = {key.split("@", 1)[-1] for kind, key in o.get("_writes", []) if kind == "branch"}
            if not wrote & held:
                continue
            os_ = o["_sessions"][0]
            t["_shared"].append((dict(id=os_["id"], title=os_.get("title", ""), path=os_.get("path", "")), os_.get("updated") or 0))
            t["events"].append(dict(ts=os_.get("updated") or 0, source="session", title=os_.get("title") or "Agent session",
                                    detail=os_.get("branch", ""), ref=os_["id"]))
            t["updated"] = max(t["updated"], os_.get("updated") or 0)
        t["shared_sessions"] = [dict(id=s["id"], title=s["title"], at=at)
                                for s, at in sorted(t["_shared"], key=lambda x: -x[1])[:5]]


def _did(evs: list[str]) -> str:
    """What a session did to a branch, from the evidence of its write edges: created, committed, pushed, or
    uncommitted edits."""
    done = []
    if any(e.startswith("created") for e in evs):
        done.append("created")
    if any(e == "git commit" for e in evs):
        done.append("committed")
    if any(e.startswith("git push") for e in evs):
        done.append("pushed")
    if not done:
        return "uncommitted edits on"
    return ", ".join(done[:-1]) + " and " + done[-1] if len(done) > 1 else done[0]


def _kin(threads: list[dict]) -> None:
    """Each thread's `kin`: the sessions of the same work, oldest first: those that touched a branch, PR or ticket
    this thread's session touched. Each row says what that session did there, its thread's state and its claim on the
    branch. The thread itself is in the list, marked `me`, so the card can draw one piece of work in time order."""
    from pensieve import session_threads
    by_id = {t["id"]: t for t in threads}
    for t in threads:
        sibs = t.get("_siblings")
        if sibs is None:
            continue
        mine = _sink_names(t)
        me = t["_sessions"][0]
        rows = [dict(id=t["id"], session=me["id"], title=t["title"], updated=me.get("updated") or t["updated"],
                     state=t["state"], me=True, did=_what(t, mine), share=_share(t), active=bool(t.get("active")))]
        for s in sibs:
            o = by_id.get(session_threads.thread_id(s["id"]))
            if not o or not mine & _sink_names(o):
                continue
            os_ = o["_sessions"][0]
            rows.append(dict(id=o["id"], session=s["id"], title=o["title"], updated=os_.get("updated") or o["updated"],
                             state=o["state"], me=False, did=_what(o, mine), share=_share(o), active=bool(o.get("active"))))
        t["kin"] = sorted(rows, key=lambda r: r["updated"]) if len(rows) > 1 else []


def _what(o: dict, names: set[str]) -> str:
    """What thread `o`'s session did on the sinks in `names`: "committed and pushed session-summaries", "opened #12",
    "uncommitted edits on x", or "worked on x, no changes" when it only ran there."""
    from pensieve import session_threads
    ev = session_threads.evidence(o["_sessions"][0].get("path", ""))
    parts = []
    for kind, key in dict.fromkeys(o.get("_writes", [])):
        name = key.split("@", 1)[-1] if kind == "branch" else key
        if name not in names:
            continue
        if kind == "branch":
            parts.append(f"{_did(ev.get((kind, key), []))} {name}")
        elif kind == "pr":
            merged = any(e == "gh pr merge" for e in ev.get((kind, key), []))
            parts.append(f"{'merged' if merged else 'opened'} #{key.rsplit('#', 1)[-1]}")
    if parts:
        return ", ".join(parts)
    ran = o["_sessions"][0].get("branch", "")
    return f"worked on {ran}, no changes" if ran in names else "no changes"


def _share(o: dict) -> float | None:
    c = o.get("claim")
    return round(c["share"], 2) if c and c.get("share") is not None else None


RELATED_MAX = 5          # the related threads a card lists
RELATED_FILES = 0.25     # the weighted share of edited files two sessions have in common that relates their threads


def _sinks(t: dict) -> dict[str, str]:
    """The sinks a card writes to, each with the reason a shared sink makes two cards related: its branches, its
    pull requests and its tickets."""
    out = {}
    for b in t.get("branches", []):
        out[f"branch:{b['branch']}"] = f"same branch {b['branch']}"
    for p in t.get("prs", []):
        out[f"pr:{p['url']}"] = f"same pull request #{p['url'].rstrip('/').rsplit('/', 1)[-1]}"
    for k in t.get("tickets", []):
        if not k.get("ref"):
            out[f"ticket:{k['key']}"] = f"same ticket {k['key']}"
    return out


def _related_cards(tasks_: list[dict]) -> None:
    """Each card's `related`: the other cards that share a sink (the reason names it), then the cards whose sessions
    edited the same files (the reason names the rarest ones). At most RELATED_MAX, in that order."""
    if len(tasks_) < 2:
        for t in tasks_:
            t["related"] = []
        return
    by_id = {t["id"]: t for t in tasks_}
    sinks = {t["id"]: _sinks(t) for t in tasks_}
    holders = defaultdict(list)
    for tid, ss in sinks.items():
        for key in ss:
            holders[key].append(tid)
    files = _file_kin(tasks_)
    for tid in by_id:
        rel, seen = [], {tid}
        for key, why in sinks[tid].items():
            for other in holders[key]:
                if other not in seen:
                    seen.add(other)
                    rel.append(dict(id=other, reason=why))
        for other, why in files.get(tid, []):
            if other not in seen:
                seen.add(other)
                rel.append(dict(id=other, reason=why))
        t = by_id[tid]
        t["related"] = [dict(id=r["id"], title=by_id[r["id"]]["title"], state=by_id[r["id"]]["state"], reason=r["reason"],
                             updated=by_id[r["id"]]["updated"]) for r in rel[:RELATED_MAX]]


def _file_kin(tasks_: list[dict]) -> dict[str, list[tuple[str, str]]]:
    """For each thread, the threads whose session edited the same files, best first, with the reason "edited the
    same files: a.py, b.py +2". A file that many sessions edit says little, so each shared file weighs
    1 / log(1 + sessions that edited it), and the share is that weight over the weight of both sessions' files."""
    import math
    edited = {t["id"]: {str(p) for p in ((t.get("_sessions") or [{}])[0].get("edited") or ()) if "/.claude/" not in str(p)}
              for t in tasks_}
    n_edits = Counter(p for fs in edited.values() for p in fs)
    weight = lambda p: 1 / math.log(1 + n_edits[p]) if n_edits[p] > 1 else 0.0  # a file one session edited links nothing
    out: dict[str, list] = {}
    ids = list(edited)
    for i, a in enumerate(ids):
        if not edited[a]:
            continue
        found = []
        for b in ids[i + 1:]:
            common = edited[a] & edited[b]
            if not common:
                continue
            both = sum(weight(p) for p in edited[a] | edited[b])
            share = sum(weight(p) for p in common) / both if both else 0
            if share >= RELATED_FILES:
                names = list(dict.fromkeys(Path(p).name for p in sorted(common, key=lambda p: (n_edits[p], p))))
                why = "edited the same files: " + ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
                found.append((share, b, why))
                out.setdefault(b, []).append((share, a, why))
        out.setdefault(a, []).extend(found)
    return {k: [(other, why) for _, other, why in sorted(v, key=lambda x: -x[0])] for k, v in out.items()}




def _thread(t: dict, inp: "Inputs"):
    """What the card says about its thread (pensieve/cardtext.py): its anchor, the user's last words and the agent's
    last reply in its newest own session, and what is left to close it."""
    s = cardtext.pick_session(t)
    turns = (inp.facts.get(s["id"]) or {}).get("turns", []) if s else []
    reply = cardtext.agent_reply(turns) if turns else None
    t["anchor"] = asdict(cardtext.anchor(t))
    t["mine"] = cardtext.my_messages(turns)
    t["reply"] = asdict(reply) if reply and (reply.text or reply.waiting) else None
    t["left"] = [asdict(x) for x in cardtext.pending(t, reply)]
    t["closeout"] = cardtext.closeout(t, reply)  # the steps that merge the work
    t["discard"] = cardtext.discard(t)  # the steps that close it without merging


_REFLOG_CREATED = re.compile(r"^(\S+)@\{(\d+)\}\|branch: Created")


@lru_cache(maxsize=512)
def _branch_created(path: str, branch: str) -> float:
    """When a branch was made in its checkout: the time of its reflog's "branch: Created" entry, or 0 when the reflog
    does not say (git keeps a branch's reflog for 90 days by default, and a clone has none)."""
    r = subprocess.run(["git", "-C", path, "reflog", "show", "--date=unix", "--format=%gd|%gs", f"refs/heads/{branch}"],
                       capture_output=True, text=True, timeout=10)
    for line in r.stdout.splitlines():
        m = _REFLOG_CREATED.match(line)
        if m:
            return float(m.group(2))
    return 0.0


def _branch_events(t: dict) -> list[dict]:
    """A "Branch created" event for each of the card's branches whose checkout still has its reflog."""
    out = []
    for b in t.get("branches", []):
        pl = next((p for p in t.get("places", []) if p.get("branch") == b["branch"] and p.get("path")), None)
        if not pl:
            continue
        at = _branch_created(pl["path"], b["branch"])
        if at:
            out.append(dict(ts=at, source="branch", title=f"Branch {b['branch']} created",
                            detail=b.get("repo", ""), ref=""))
    return out


def _title(t: dict):
    """The card's title: its ticket's title, then its pull request's, then its session's. An open ticket or PR comes
    first. The thread that holds a branch several sessions wrote to is named by the session with the largest share of
    its lines (claims.apply sets `_lead_title`); a thread that lost the branch keeps its own session's title, so the
    card says what that session was about."""
    lead = t.pop("_lead_title", None)
    lost = bool((t.get("claim") or {}).get("lost"))
    own = (lead if not lost else "") or (t["sessions"][0]["title"] if t.get("sessions") else t.get("title", ""))
    tickets = sorted(t.get("tickets", []), key=lambda k: k.get("status", "").lower() in DONE)
    prs = sorted(t.get("prs", []), key=lambda p: (p.get("state") or "").lower() not in ("open", "draft"))
    tk = next((k["title"] for k in tickets if k.get("title")), "")
    pr = next((p["title"] for p in prs if p.get("title")), "")
    t["title"] = tk or pr or own


ARTIFACT_URL = re.compile(r"https://claude\.ai/(?:code/)?artifact/[\w-]+")


def _artifacts(s: dict, links: list[str], limit: int = 20) -> list[dict]:
    """What a session made: the published Artifact links in its text (`links`) first, then the files it created or
    edited that still exist (not scratch files), newest file first, `limit` in all."""
    out = [dict(kind="link", url=u, name=u.rsplit("/", 1)[-1]) for u in links[:limit]]
    files = sorted((p for p in s["edited"] if Path(p).is_file()), key=_mtime, reverse=True)
    out += [dict(kind="file", path=p, name=Path(p).name) for p in files[:max(0, limit - len(out))]]
    return out


def _own_prs(ts: list[dict]):
    """A pull request belongs to the task that works on its head branch. Another task that only mentions it in a
    session's text loses it, so one task's open PR does not keep a related, finished task open. A task whose session
    title names the PR ("PR 412 CI job") keeps it: that session was about the PR."""
    owner = {}
    for t in ts:
        mine = {b["branch"] for b in t["branches"]} | {p.get("branch") for p in t.get("places", [])
                                                         if p.get("branch") not in DEFAULT_BRANCHES}
        for p in t["prs"]:
            if p.get("head") and p["head"] in mine:
                owner.setdefault(p["url"], t["id"])
    for t in ts:
        named = t.get("_named", set())
        keep = [p for p in t["prs"] if owner.get(p["url"], t["id"]) == t["id"] or p["url"].rstrip("/").rsplit("/", 1)[-1] in named]
        # another task's PR from a branch where this task's work is too: shown on the card, but it doesn't set the state
        places = {p.get("branch") for p in t.get("places", []) if p.get("branch") not in DEFAULT_BRANCHES}
        t["branch_prs"] = [p for p in t["prs"] if p not in keep and p.get("head") in places]
        t["prs"] = keep
        for p in t["prs"]:
            p["own"] = owner.get(p["url"]) == t["id"]


_squash_cache = {}  # (repo, default branch tip, branch tips) -> the squash-merged branches
PR_SUBJECT = re.compile(r"^Merge pull request #(\d+)\b|\(#(\d+)\)\s*$")  # a GitHub merge commit, a squash commit


def _integrated(ts: list[dict]):
    """Where a task's work reached the default branch of its repo, from git. Pensieve only reads: the merge is the
    evidence, and the card names the PR that merged it. A thread's state comes from its sinks, not from this. Without an
    open PR, a task is integrated when:
    - it has a feature branch and every commit of the task is in the default branch (a merge or a fast-forward);
    - its feature branch is squash-merged (stale._squashed).
    Sets task["integrated"] (why, and the PR that merged it) and adds that PR to the card, so the record names it."""
    from pensieve import stale
    bases, webs, squash = {}, {}, defaultdict(list)

    def base(root):
        if root not in bases:
            bases[root] = stale._default_branch(root) or ""
        return bases[root]

    def in_base(root, sha):
        return bool(_git(root, "rev-parse", "-q", "--verify", f"{sha}^{{commit}}", ttl=3600)) and \
            not _git(root, "rev-list", "-1", f"{base(root)}..{sha}", ttl=300)

    for t in ts:
        if any(p.get("state") in ("open", "draft") for p in t["prs"]):
            continue
        cs = [c for c in t["commits"] if Path(c.get("repo", "")).is_dir() and base(c["repo"])]
        feature = [b for b in t["branches"] if b["branch"] not in DEFAULT_BRANCHES and Path(b["repo"]).is_dir()]
        if cs and len(cs) == len(t["commits"]) and all(in_base(c["repo"], c["sha"]) for c in cs):
            root, name = cs[0]["repo"], base(cs[0]["repo"]).rsplit("/", 1)[-1]
            if feature:
                _mark(t, root, base(root), cs[0]["sha"], f"its branch {feature[0]['branch']} is merged into {name}", webs)
                continue
        for b in feature:
            if base(b["repo"]) and _git(b["repo"], "rev-parse", "-q", "--verify", f"refs/heads/{b['branch']}", ttl=300):
                squash[b["repo"]].append((t, b["branch"]))
    for root, pairs in squash.items():  # one git pass per repo: _squashed reads the default branch's patches once
        names = sorted({b for _, b in pairs})
        key = (root, _git(root, "rev-parse", base(root), ttl=60),
               _git(root, "rev-parse", *(f"refs/heads/{b}" for b in names), ttl=60))
        if key not in _squash_cache:  # the same tips give the same answer
            _squash_cache[key] = set(stale._squashed(root, base(root), names))
        got = _squash_cache[key]
        for t, b in pairs:
            if b in got and not t.get("integrated"):
                _mark(t, root, base(root), "", f"its branch {b} is squash-merged into {base(root).rsplit('/', 1)[-1]}",
                      webs)


def _mark(t: dict, root: str, base: str, sha: str, why: str, webs: dict):
    """Record that `t` is integrated, and the PR that merged `sha` into `base`: the first GitHub merge commit after
    it, or the squash commit itself ("… (#12)"). Without `sha` (a squash-merged branch), the PR is the one that gh
    found for the branch, if any."""
    if root not in webs:
        webs[root] = web_url(_git(root, "remote", "get-url", "origin", ttl=3600))
    pr = ""
    if sha and "github.com" in webs[root]:
        subjects = [_git(root, "log", "-1", "--format=%s", sha, ttl=3600)] + _git(
            root, "log", "--ancestry-path", "--merges", "--reverse", "--format=%s", f"{sha}..{base}",
            ttl=300).splitlines()
        n = next((m.group(1) or m.group(2) for s in subjects if (m := PR_SUBJECT.search(s))), "")
        if n:
            pr = f"{webs[root]}/pull/{n}"
            if pr not in {p["url"] for p in t["prs"]}:
                t["prs"].append(dict(url=pr, title="", state="merged", source="git", own=False))
            why += f" (#{n})"
    t["integrated"] = dict(why=why, pr=pr)


ACTIVE_MINUTES = 10      # a transcript written this recently: the session is running now
ACTIVE_TURN_MINUTES = 30  # ... and its last turn this recent
DONE = {"done", "closed", "resolved", "won't do", "wont do", "cancelled", "canceled", "merged", "complete", "completed",
        "fixed", "invalid", "duplicate", "released", "shipped"}  # ticket statuses that count as finished


def _order(t):
    return task_states.ORDER.index(task_states.State(task_states.RENAMED.get(t["state"], t["state"]))), \
        not t.get("active"), -t["updated"]


def _attention(task: dict):
    """The tags on the card: a session running now, an open pull request, a ticket that is not done, and uncommitted
    files on the task's own branch."""
    now = time.time()
    # the transcript was written in the last minutes AND it has a turn in the last half hour: a session left open that
    # only waits (to check CI again later, say) touches its file without doing work
    active = any(s["path"] and now - _mtime(s["path"]) < ACTIVE_MINUTES * 60 and now - s["updated"] < ACTIVE_TURN_MINUTES * 60
                 for s in task["_sessions"])
    # ... or a shared session that is running and worked on this card's branch in the last half hour
    active = active or any(s["path"] and now - _mtime(s["path"]) < ACTIVE_MINUTES * 60 and now - at < ACTIVE_TURN_MINUTES * 60
                           for s, at in task.get("_shared", []))
    reasons = []
    for p in task["prs"]:
        if p["state"] in ("open", "draft"):
            n = (re.search(r"(\d+)/?$", p["url"]) or [None, ""])[1]
            reasons.append(f"PR #{n} {p['state']}")
    for k in task["tickets"]:
        if k["status"] and k["status"].lower() not in DONE:
            reasons.append(f"{k['key']} {k['status']}")
    feature = {b["branch"] for b in task["branches"]}
    for p in task["places"]:
        if p.get("changed") and p.get("branch") in feature and not p.get("head"):
            reasons.append(f"{p['changed']} uncommitted")
    task["active"], task["reasons"] = active, reasons[:4]


def _ago(ts: float) -> str:
    s = time.time() - ts
    return "now" if s < 120 else f"{int(s // 60)} min ago" if s < 5400 else f"{int(s // 3600)} h ago" if s < 172800 \
        else f"{int(s // 86400)} days ago"


def matching(ts: list[dict], query: str) -> list[dict]:
    """The tasks whose title, keys, branches, PRs or session titles contain words of the query, the most matched
    words first (ties keep their order)."""
    words = {w for w in re.findall(r"[a-z0-9][a-z0-9_.-]+", query.lower().replace("#", " ")) if len(w) > 2 or w.isdigit()}
    def text(t):
        return " ".join([t["title"], *t["keys"], *(b["branch"] for b in t["branches"]), *(p["url"] + " " + (p.get("title") or "")
                         for p in t["prs"]), *(s["title"] for s in t["sessions"][:8])]).lower()
    scored = [(sum(1 for w in words if re.search(rf"(?<![a-z0-9]){re.escape(w)}", tx)), i, t)
              for i, t in enumerate(ts) for tx in [text(t)]]
    return [t for s, i, t in sorted(scored, key=lambda x: (-x[0], x[1])) if s]


RESUME_WORDS = re.compile(r"\b(left off|left over|leftover|pick (it |this |that )?(back )?up|continue|resume|status of|"
                          r"where (are|were) we|where did (i|we)|what'?s left|what was left|follow[- ]?up)\b", re.I)
# a prompt about all of the user's work, not one piece of it
LIST_WORDS = re.compile(r"\b(standup|stand-up|unfinished|open loops|still open|in progress|my (open|unfinished|recent) "
                        r"work|what (have|did) i (do|done|work|ship)|last \d+ days|this week)\b", re.I)
STANDUP_WORDS = re.compile(r"\b(standup|stand-up|what (have|did) i|last \d+ days|this week)\b", re.I)


def card_for_prompt(ts: list[dict], prompt: str) -> dict | None:
    """The task that a prompt is about, or None: a prompt that names its ticket key, branch or PR number, or one that
    asks to pick work up ("where did I leave off on …") and matches one task clearly better than the next."""
    low = prompt.lower()
    for t in ts:
        names = [k.lower() for k in t["keys"]] + [b["branch"].lower() for b in t["branches"] if len(b["branch"]) > 5]
        nums = {m for p in t["prs"] for m in re.findall(r"/pull/(\d+)", p["url"])}
        if any(re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", low) for n in names) or \
                any(re.search(rf"(?:#|\bpr\s*#?)\s*{n}\b", low) for n in nums):
            return t
    if not RESUME_WORDS.search(prompt):
        return None
    ranked = sorted(((len(_words_hit(t, prompt)), -i, t) for i, t in enumerate(ts)), key=lambda x: x[:2],
                    reverse=True)
    if not ranked:
        return None
    first, second = ranked[0][0], ranked[1][0] if len(ranked) > 1 else 0
    return ranked[0][2] if first >= 2 and first > second else None


def _words_hit(t, prompt):
    text = " ".join([t["title"], *(s["title"] for s in t["sessions"][:8])]).lower()
    words = {w for w in re.findall(r"[a-z0-9][a-z0-9_.-]+", prompt.lower()) if len(w) > 3} - _STOP
    return [w for w in words if re.search(rf"(?<![a-z0-9]){re.escape(w)}", text)]


_STOP = {"where", "what", "with", "that", "this", "from", "back", "left", "pick", "work", "working", "tell", "have",
         "were", "still", "open", "continue", "resume", "status", "last", "session", "about", "want", "need", "done",
         "folder", "branch", "state", "there", "which", "when", "they", "them", "then", "does", "into", "anything"}


def brief(t: dict) -> str:
    """One line for an agent: id, state, title, where, PRs and what is left."""
    p = (t.get("places") or [{}])[0]
    where = ", ".join(x for x in (Path(p.get("path", "")).name, p.get("branch", ""),
                                  p.get("changed") and f"{p['changed']} uncommitted",
                                  p.get("ahead") and f"{p['ahead']} not pushed") if x)
    prs = ", ".join(f"PR #{(re.search(r'(\d+)/?$', x['url']) or [0, '?'])[1]} {x['state'] or ''}".strip() for x in t["prs"][:2])
    bits = [f"[{t['state']}]" + (" (session running)" if t.get("active") else ""), t["title"][:90], where, prs,
            ", ".join(t["keys"][:2]), f"next: {t['left'][0]['text']}" if t.get("left") else "",
            f"waits on you: {q}" if (q := asked_question(t)) else "", _ago(t["updated"])]
    return f"{t['id']}  " + " · ".join(b for b in bits if b)


_NOISE = re.compile(r"^\s*(tip: .*|to get shorter replies.*|.*/output-style.*|the messages after this summary.*)$", re.I)


def _clean_words(text: str) -> str:
    """The last session's last words without the agent's tips and the notes that compaction adds."""
    return "\n".join(x for x in text.strip().splitlines() if not _NOISE.match(x)).strip()


def _tail(text: str, n: int = 600) -> str:
    """The last n characters of text, from the start of a line when the cut falls inside one."""
    if len(text) <= n:
        return text
    cut = text[-n:]
    return cut.split("\n", 1)[1] if "\n" in cut[:n // 2] else cut


def detail(t: dict, hist: list[dict]) -> str:
    """The whole card for an agent, as Markdown: enough to pick the task up and finish it."""
    out = [f"# {t['title']}", f"id: {t['id']} · state: {t['state']} ({t['state_why']}) · last activity {_ago(t['updated'])}"]
    last = max((s["updated"] for s in t.get("sessions", [])), default=0)
    since = [p for p in t["prs"] + t.get("branch_prs", []) if last and (p.get("merged_at") or 0) > last]
    if since:
        out += ["", "## Since the last session"] + [
            f"- {p['url']} merged {datetime.fromtimestamp(p['merged_at']).strftime('%Y-%m-%d')}" for p in since]
    if t.get("places"):
        out += ["", "## Where the work is"] + [
            f"- {p['path']}" + (f" (branch {p.get('branch')}" + (", worktree" if p.get("worktree") else "") +
                                (f", checked out now: {p['head']}" if p.get("head") else "") +
                                (f", {p['changed']} uncommitted files" if p.get("changed") else "") +
                                (f", {p['ahead']} ahead" if p.get("ahead") else "") +
                                (f", {p['behind']} behind" if p.get("behind") else "") + ")" if p.get("git") else "")
            for p in t["places"]]
    if t["prs"]:
        out += ["", "## Pull requests"] + [
            f"- {p['url']} {p['state'] or 'state unknown'}" + (f", checks {p['checks']}" if p.get("checks") else "") +
            (f", {p['review']}" if p.get("review") else "") + (f": {p['title']}" if p.get("title") else "") for p in t["prs"]]
    if t.get("branch_prs"):
        out += ["", "## Pull requests of the branch (shared with other work)"] + [
            f"- {p['url']} {p['state'] or 'state unknown'} (branch {p['head']})" + (f": {p['title']}" if p.get("title") else "")
            for p in t["branch_prs"]]
    if t["tickets"]:
        out += ["", "## Tickets"] + [f"- {k['key']} {k['status']}: {k['title']} {k['url']}".rstrip() for k in t["tickets"]]
    if t.get("resume"):
        out += ["", "## Resume the last session", f"`{t['resume']['command']}`"]
    if t.get("last_words"):
        out += ["", "## Where the last session stopped", _clean_words(t["last_words"])]
    made = [(s["title"], a) for s in t["sessions"][:5] for a in s.get("artifacts", [])]
    if made:
        out += ["", "## What its sessions made"] + [f"- {a.get('path') or a.get('url')} (session: {title})" for title, a in made[:20]]
    elif t["files"]:
        out += ["", "## Files edited"] + [f"- {f}" for f in t["files"][:10]]
    out += ["", "## Timeline (newest first)"] + [
        f"- {datetime.fromtimestamp(e['ts']).strftime('%Y-%m-%d')} {e['source']}: {e['title']}" for e in t["events"][:15]]
    if hist:
        out += ["", "## State changes"] + [f"- {datetime.fromtimestamp(h['at']).strftime('%Y-%m-%d %H:%M')} {h['state']} by "
                                           f"{h['by']}" + (f": {h['reason']}" if h["reason"] else "") for h in hist[-5:]]
    return "\n".join(out)


def asked_question(t: dict) -> str:
    """The last session's own final question, or ""."""
    return last_question(t.get("last_words") or "")


def last_question(words: str) -> str:
    """The question that a session's last message ends with ("Should I add the fix to #2069, or open a follow-up?"),
    with the sentence before it when the question alone is short ("Want me to build it?"), or ""."""
    text = _clean_words(words).strip()
    if not text.endswith("?"):
        return ""
    parts = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text))
    q = parts[-1]
    if len(q.split()) < 8 and len(parts) > 1:
        q = f"{parts[-2]} {q}"
    return q[-300:]


def _pr_short(p: dict) -> str:
    """owner/repo#12 draft, checks failing: title"""
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", p["url"])
    name = f"{m.group(1)}#{m.group(2)}" if m else p["url"]
    return (f"{name} {p.get('state') or 'state unknown'}" + (f", checks {p['checks']}" if p.get("checks") else "") +
            (f", {p['review']}" if p.get("review") else "") + (f": {p['title'][:80]}" if p.get("title") else ""))


def compact(t: dict, hist: list[dict]) -> str:
    """The card in about a quarter of the tokens of `detail`: its state, where the work is, PRs, what changed since
    the last session, what is left, the last session's last words, the main files and the resume command."""
    home = str(Path.home())
    day = lambda ts: datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    last = max((s["updated"] for s in t.get("sessions", [])), default=0)
    out = [f"# {t['title']}", f"id: {t['id']} · {t['state']} ({t['state_why']})" +
           (f" · last session {day(last)}" if last else "") + f" · last activity {_ago(t['updated'])}"]
    feature = [p for p in t.get("places", []) if p.get("branch") not in DEFAULT_BRANCHES]
    for p in t.get("places", []):
        if feature and p not in feature and not p.get("changed"):
            continue  # the main checkout, when the work has its own branch
        if p.get("branch") in DEFAULT_BRANCHES:
            p = dict(p, behind=0)  # how far main is behind its remote says nothing about the work
        flags = [x for x in (p.get("branch") and f"branch {p['branch']}", p.get("worktree") and "worktree",
                             p.get("head") and f"checked out now: {p['head']}",
                             p.get("changed") and f"{p['changed']} uncommitted",
                             p.get("ahead") and f"{p['ahead']} commits not pushed",
                             p.get("upstream") and not p.get("ahead") and "every commit pushed",
                             p.get("behind") and f"{p['behind']} behind") if x]
        out.append(f"Where: {p['path'].replace(home, '~')}" + (f" ({', '.join(flags)})" if flags else ""))
    prs = t["prs"] + [dict(p, title="(shared branch) " + (p.get("title") or "")) for p in t.get("branch_prs", [])]
    if prs:
        out.append("PRs: " + "; ".join(_pr_short(p) for p in prs[:5]))
    if t["tickets"]:
        out.append("Tickets: " + "; ".join(f"{k['key']} {k['status']}".strip() + (f": {k['title'][:60]}" if k["title"] else "")
                                           for k in t["tickets"][:3]))
    if t.get("kin"):
        out.append("Sessions of this work, oldest first: " + "; ".join(
            f"{day(k['updated'])} {'this session' if k['me'] else k['title'][:50]} ({k['did']}" +
            (f", {round(k['share'] * 100)}% of the branch" if k.get("share") else "") + (", running" if k.get("active") else "") +
            f") [{k['id']} {k['state']}]" for k in t["kin"][:8]))
    since = [f"{_pr_short(p).split(' ')[0]} merged {day(p['merged_at'])}" for p in prs if last and (p.get("merged_at") or 0) > last]
    if since:
        out.append("Since the last session: " + "; ".join(since))
    asked = asked_question(t)
    if asked:
        out.append(f"Waiting on you: {asked}")
    who = {"agent": "you, the agent", "you": "the user", "parent": "in its tracker or on GitHub"}
    if t.get("left"):
        out += ["", "Left to close this thread (Pensieve only reads: these are steps, not changes it made):"] + \
            [f"- {x['text']} ({who.get(x['how'], x['how'])})" for x in t["left"]]
    if t.get("last_words"):
        out += ["", "Last words of the last session:", _clean_words(t["last_words"])[-450:]]
    roots = [p["path"] + "/" for p in t.get("places", [])]
    files = Counter()
    for s in t["sessions"][:5]:
        for a in s.get("artifacts", []):
            f = a.get("path") or ""
            if f and "/.claude/" not in f:
                files[next((f[len(r):] for r in roots if f.startswith(r)), f.replace(home, "~"))] += 1
    files = [f for f, _ in files.most_common()] or [f for f in t["files"] if "/.claude/" not in f]
    if files:
        out += ["", "Files: " + ", ".join(files[:6]) + (f" (+{len(files) - 6} more)" if len(files) > 6 else "")]
    if t.get("resume"):
        out.append(f"Resume: `{t['resume']['command']}`")
    if hist:
        h = hist[-1]
        out.append(f"Marked {h['state']} by {h['by']} on {day(h['at'])}" + (f": {h['reason']}" if h["reason"] else ""))
    return "\n".join(out)


def set_state(store, task_id: str, state: str, by: str = "user", reason: str = "") -> dict:
    """Set a task's state by hand (or "auto"), and update the card at once: the card builder's row (it waits up to 2
    seconds for it) and the tasks built here."""
    from pensieve import cardbuild
    ch = task_states.set_state(store, task_id, state, by, reason)
    cardbuild.emit(cardbuild.STATE, [task_id], wait=2)
    for _, ts in _cache.values():
        for t in ts:
            if t["id"] == task_id:
                task_states.apply(t, ch)
        ts.sort(key=_order)
    return dict(id=task_id, state=state, by=by)


def _mtime(path: str) -> float:
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def _git(path: str, *args, ttl=60) -> str:
    """`git -C path args`, cached for `ttl` seconds."""
    k = (path, args)
    hit = _git_cache.get(k)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        out = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    _git_cache[k] = (time.time(), out)
    return out


_git_cache = {}
_gh_cache = {}


def web_url(remote: str) -> str:
    """https://host/org/repo for a git remote (ssh or https), or ""."""
    m = re.match(r"(?:git@|ssh://git@|https://(?:[^@/]+@)?)([^:/]+)[:/](.+?)(?:\.git)?/?$", remote or "")
    return f"https://{m.group(1)}/{m.group(2)}" if m else ""


def _branch_url(web: str, branch: str) -> str:
    if "bitbucket.org" in web:
        return f"{web}/branch/{branch}"
    if "gitlab" in web:
        return f"{web}/-/tree/{branch}"
    return f"{web}/tree/{branch}"


def _gh_prs(root: str, branch: str) -> list[dict]:
    """GitHub pull requests from this branch, through the gh command line (cached for 10 minutes)."""
    import shutil
    k = (root, branch)
    hit = _gh_cache.get(k)
    if hit and (time.time() - hit[0] < 600 or _net["off"]):
        return hit[1]
    out = []
    if _net["off"]:
        return out
    if shutil.which("gh"):
        try:
            r = subprocess.run(["gh", "pr", "list", "--head", branch, "--state", "all", "--limit", "3", "--json",
                                "number,title,state,url,isDraft,mergedAt"], cwd=root, capture_output=True, text=True, timeout=15)
            out = [dict(url=p["url"], title=p["title"], state="draft" if p.get("isDraft") and p["state"] == "OPEN"
                        else p["state"].lower(), source="GitHub", head=branch,
                        merged_at=_ts(p["mergedAt"]) if p.get("mergedAt") else 0) for p in json.loads(r.stdout or "[]")]
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError):
            pass
    _gh_cache[k] = (time.time(), out)
    return out


def _gh_pr(url: str) -> dict:
    """The state, title, CI checks and review of one GitHub pull request, through gh (cached for 10 minutes).
    checks: passing | failing | pending; review: approved | changes requested | review required."""
    import shutil
    hit = _gh_cache.get(url)
    # a merged or closed PR seldom changes: a day; an open one, or one gh could not read: 10 minutes
    if hit and (time.time() - hit[0] < (86400 if hit[1].get("state") in ("merged", "closed") else 600) or _net["off"]):
        return hit[1]
    out = {}
    if _net["off"]:  # no network now (the first build after a start): the last answer kept on disk, so that a
        try:          # restart does not drop the PR titles that name threads
            from pensieve import store as v2
            from pensieve.store import cache
            out = cache.get_json(v2.index_db(), "ghpr:" + url) or {}
        except Exception:
            out = {}
        if out:
            _gh_cache[url] = (0.0, out)  # old: the next build with network asks gh again
        return out
    if shutil.which("gh"):
        try:
            r = subprocess.run(["gh", "pr", "view", url, "--json",
                                "state,title,isDraft,reviewDecision,statusCheckRollup,headRefName,mergedAt"],
                               capture_output=True, text=True, timeout=15)
            p = json.loads(r.stdout or "{}")
            if p:
                out = dict(title=p["title"], state="draft" if p.get("isDraft") and p["state"] == "OPEN" else p["state"].lower(),
                           head=p.get("headRefName") or "", merged_at=_ts(p["mergedAt"]) if p.get("mergedAt") else 0)
                runs = [(c.get("conclusion") or c.get("state") or c.get("status") or "").upper() for c in p.get("statusCheckRollup") or []]
                if runs and p["state"] == "OPEN":
                    out["checks"] = "failing" if any(x in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED") for x in runs) else \
                        "pending" if any(x in ("PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "") for x in runs) else "passing"
                review = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes requested",
                          "REVIEW_REQUIRED": "review required"}.get(p.get("reviewDecision") or "")
                if review and p["state"] == "OPEN":
                    out["review"] = review
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError):
            pass
    _gh_cache[url] = (time.time(), out)
    if out:
        try:
            from pensieve import store as v2
            from pensieve.store import cache
            cache.put_json(v2.index_db(), "ghpr:" + url, out, ttl=30 * 86400, wait=False)
        except Exception:
            pass
    return out


def _pickup(task: dict, wts: dict):
    """What the card needs to pick the task back up: the folders where the work happens (worktree or checkout, its
    branch, uncommitted files, the branch on the remote), its pull requests, and how to resume the last session."""
    places, seen = [], set()
    feature = {x["branch"] for x in task["branches"]}  # the task's own feature branches (not long-lived ones)
    cands = [(w, b) for w, (r, b) in wts.items() if any(b == x["branch"] and r == x["repo"] for x in task["branches"])]
    cands += [(s["project"], s["branch"]) for s in task["_sessions"] if s["project"]]
    for c, used in cands:
        root = _git_root(c) if Path(c).is_dir() else ""
        path = root or (c if Path(c).is_dir() else "")
        if not path or path in seen or path.startswith(_TEMP):
            continue
        seen.add(path)
        p = dict(path=path, git=bool(root))
        if root:
            head = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
            p["branch"] = used or head  # the branch the task's sessions used here
            if used and head and head != used:
                p["head"] = head  # the checkout is on another branch now
            # uncommitted files belong to the branch that is open: none for the task when another branch is open
            p["changed"] = 0 if p.get("head") else \
                len([x for x in _git(root, "status", "--porcelain", "--untracked-files=no").splitlines() if x])
            p["worktree"] = root in wts
            if p["branch"] not in DEFAULT_BRANCHES:  # each commit of the branch: is its patch already in the default branch
                from pensieve import stale
                base = stale._default_branch(root)
                cherry = _git(root, "cherry", base, p["branch"], ttl=300).splitlines() if base else []
                p["commits"] = len(cherry)
                p["unmerged"] = sum(line.startswith("+") for line in cherry)
            # ahead and behind of the task's branch, not of the branch that is open in the checkout now
            ref = p["branch"] if p.get("head") else "HEAD"
            ab = _git(root, "rev-list", "--left-right", "--count", f"{ref}@{{upstream}}...{ref}").split()
            if len(ab) == 2 and ab[0].isdigit():  # commits behind and ahead of the branch's upstream
                p["behind"], p["ahead"], p["upstream"] = int(ab[0]), int(ab[1]), True
                p["pushed"] = _git(root, "rev-parse", "--short", f"{ref}@{{upstream}}")  # moves on a push: history saves the card
            p["repo"] = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir", ttl=3600) or root).parent.name
            web = web_url(_git(root, "remote", "get-url", "origin", ttl=3600))
            if web and p["branch"] not in DEFAULT_BRANCHES and _git(root, "rev-parse", "--verify", "--quiet",
                                                                      f"refs/remotes/origin/{p['branch']}"):
                p["remote_url"] = _branch_url(web, p["branch"])
            # the PRs of the branch where the work is: the task's own branch, or a long-lived one that its sessions used
            # (a merged PR there is often the end of the work)
            if web and "github.com" in web and (p["branch"] in feature or p["branch"] not in DEFAULT_BRANCHES):
                for pr in _gh_prs(root, p["branch"]):
                    if pr["url"] not in {x["url"] for x in task["prs"]}:
                        task["prs"].insert(0, pr)
        places.append(p)
        if len(places) >= 3:
            break
    if not (task["files"] or task["commits"] or task["branches"] or task["prs"]):
        # nothing was done here: the checkout's branch and changes belong to other work, so keep only the folder
        places = [{k: p[k] for k in ("path", "git", "repo") if k in p} for p in places]
    task["places"] = places
    for pr in [p for p in task["prs"] if "github.com" in p["url"] and (not p["state"] or p["state"] in ("open", "draft"))][:6]:
        pr.update({k: v for k, v in _gh_pr(pr["url"]).items() if v})
    last = next((s for s in task["_sessions"] if s["project"]), None)
    if last:
        sid = last["id"].split(":", 1)[1]
        cmd = {"claude": f"claude --resume {sid}", "codex": f"codex resume {sid}"}.get(last["id"].split(":", 1)[0])
        if cmd:
            task["resume"] = dict(command=f"cd {_quote(last['project'])} && {cmd}", title=last["title"], session=last["id"])


def _quote(path: str) -> str:
    import shlex
    return shlex.quote(path)


def _events(ss, its, cs) -> list[dict]:
    """The task's data points from every source, newest first: sessions, commits, connection items, and the field
    changes that connections recorded (a ticket's status going from To Do to In Progress). A private item's points
    are marked `private`: the note and shared cards leave them out."""
    ev = [dict(ts=s["updated"], source="session", title=s["title"] or "Agent session", detail=s["branch"], ref=s["id"])
          for s in ss]
    ev += [dict(ts=c["ts"], source="commit", title=c["subject"], detail=f"{c['author']} · {c['sha']}", ref=c["sha"])
           for c in cs]
    for i in its:
        src = "ticket" if i["key"] else "pr" if i["pr"] else "message"
        ev.append(dict(ts=i["updated"], source=src, title=(f"{i['key']} " if i["key"] else "") + i["title"],
                       detail=" · ".join(x for x in (i["status"], i["assignee"], _server_label(i["server"])) if x),
                       url=i["url"], ref=i["path"], private=i.get("private", False)))
    if its:
        from pensieve import store as v2
        from pensieve.store import documents
        by_doc = {i["doc"]: i for i in its if i.get("doc")}
        for doc_id, chs in documents.changes_of(v2.store_db(), by_doc).items():
            i = by_doc[doc_id]
            ev += [dict(ts=float(c.at), source="change",
                        title=f"{i['key'] or i['title'][:40]}: {c.field} {c.old or '(empty)'} → {c.new or '(empty)'}",
                        detail=_server_label(i["server"]), url=i["url"], ref=i["path"], private=i.get("private", False))
                   for c in chs]
    return sorted(ev, key=lambda e: -e["ts"])[:80]


_seen_cache: dict[int, tuple[str, dict]] = {}  # doc id -> (hash, item with via): a card build reads each once


def _seen_by(sids, until=None) -> dict[str, list[dict]]:
    """session id -> the documents of switched-on sources that the session saw, as items with `via`."""
    from pensieve import store as v2
    from pensieve.store import documents
    db, srcs, out, pairs = v2.store_db(), set(connections.indexed()), defaultdict(list), []
    sids = list(dict.fromkeys(sids))
    for i in range(0, len(sids), 500):
        part = sids[i:i + 500]
        pairs += db.query(f"SELECT ds.session_id, d.id, d.hash FROM document_sessions ds JOIN documents d ON "
                          f"d.id = ds.doc_id WHERE ds.session_id IN ({','.join('?' * len(part))})", part)
    need = [d for _, d, h in pairs if _seen_cache.get(d, ("",))[0] != h]
    for i in range(0, len(need), 500):
        docs = documents.get_many(db, need[i:i + 500])
        for d, ppl in zip(docs, _assignees(db, [d.id for d in docs])):
            try:
                tool = json.loads(d.raw or "{}").get("_tool") or ""
            except ValueError:
                tool = ""
            _seen_cache[d.id] = (d.hash, {**_item(d, ppl), "via": _via(tool, d.source_id)})
    for sid, d, _ in pairs:
        it = _seen_cache.get(d, (None, None))[1]
        if it and it["server"] in srcs and (not until or it["updated"] <= until):
            out[sid].append(it)
    return out


_TOOL_TITLE = re.compile(r"^[\w.-]+\.[\w-]+ \{")  # the title store() gives a result without one: "<server>.<tool> {args}"
_LINE = re.compile(r"^(Title|URL):\s*(.+)$", re.M)


def _seen_row(i: dict):
    """A card's row for an item that its sessions read, with the item: its own title and link, else the "Title:" and
    "URL:" lines of the content (as browser page reads give them). None when it has neither."""
    title, url = i["title"], i["url"]
    if _TOOL_TITLE.match(title):
        found = dict(_LINE.findall(i["text"][:4000]))
        title, url = found.get("Title", "").strip(), url or found.get("URL", "").strip()
        if not title and not url:
            return None
    row = dict(title=title or url, url=url, via=i["via"], path=i["path"], updated=i["updated"], key=i["key"],
               private=i["private"])
    return row, i


def _via(tool: str, server: str) -> str:
    """The service an item came from (Slack, Jira…), from the MCP tool that read it, else from its source."""
    from pensieve import connectors
    pid = connectors.match(tool, server)
    if pid:
        return connectors.PROFILES[pid]["label"]
    for word, label in (("jira", "Jira"), ("confluence", "Confluence"), ("chrome", "Web page"), ("browser", "Web page")):
        if word in tool.lower():
            return label
    srv = tool.split(".", 1)[0] if "." in tool else ""  # tools of agent sessions are "<server>.<tool>"
    return _server_label(srv or server)


def _server_label(sid: str) -> str:
    return "agent sessions" if sid == connections.SESSIONS else sid.removeprefix("claude_ai_").split("_")[-1]


def _task_id(keys, branches, sessions, items=()) -> str:
    """Stable while the task grows: its first ticket key, else its first branch, else its oldest session, else (a
    collected PR with no branch or session) its first PR's URL."""
    if keys:
        return f"t:{keys[0]}"
    if branches:
        return f"b:{branches[0]}"
    if sessions:
        return f"s:{min(sessions, key=lambda s: s['started'] or s['updated'])['id']}"
    return f"p:{min(i['url'] for i in items if i['pr'])}"


SIGNATURE_VERSION = 11  # in each card's signature: a new version marks every card as changed


def _signature(task: dict) -> str:
    import hashlib
    data = [SIGNATURE_VERSION, [(e["ts"], e["title"]) for e in task["events"][:30]], [(p["url"], p["state"]) for p in task["prs"]],
            [(p["path"], p.get("branch"), p.get("changed")) for p in task.get("places", [])],
            task.get("state"), [(k.get("key"), k.get("status")) for k in task.get("tickets", [])],
            task.get("chains", [])]  # the state and chains come from the sinks: a change in either rewrites the card
    return hashlib.sha1(json.dumps(data).encode()).hexdigest()[:16]


def resume_command(store, session_id: str) -> str | None:
    """The shell command that resumes an agent session in its folder. The folder comes from the session's transcript
    (the threads' own reading), else from the index (never from client text)."""
    import shlex
    from pensieve import agents, transcripts
    source, _, sid = session_id.partition(":")
    if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", sid or ""):
        return None
    f = transcripts.find(session_id)
    folder = f.project if f else ""
    if not folder and store is not None:
        row = store.db.execute("SELECT project FROM sessions WHERE id=?", (session_id,)).fetchone()
        folder = row[0] if row and row[0] else ""
    if not folder or not Path(folder).is_dir():
        return None
    cmd = agents.resume(source, sid)  # only the agent that wrote a session can resume it
    return f"cd {shlex.quote(folder)} && {cmd}" if cmd else None


def _me(root: str) -> set[str]:
    out = set()
    for args in (["config", "--global"], ["-C", root, "config"]):
        for k in ("user.email", "user.name"):
            try:
                v = subprocess.run(["git", *args, k], capture_output=True, text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                v = ""
            if v:
                out.add(v.lower())
    return out


def _git_root(folder: str) -> str:
    if folder not in _roots:
        p = Path(folder)
        _roots[folder] = next((str(q) for q in [p, *p.parents] if (q / ".git").exists()), "") if p.is_dir() else ""
    return _roots[folder]


def coworkers(task: dict, days: int = 21) -> list[dict]:
    """People who committed in the task's folders in the last `days` days, on any remote branch, not the user."""
    by_repo = defaultdict(set)
    for f in task["files"]:
        root = _git_root(str(Path(f).parent))  # a clone or worktree of the repo is fine: --remotes is the same
        if root:
            by_repo[root].add(str(Path(f).parent.relative_to(root)) or ".")
    people = {}
    for root, dirs in by_repo.items():
        me = _me(root)
        dirs = sorted(dirs - {"."})[:12] or ["."]
        try:
            out = subprocess.run(["git", "-C", root, "log", "--remotes", f"--since={days}.days", "--no-merges",
                                  "--source", "--format=%x1e%H%x1f%an%x1f%ae%x1f%ct%x1f%s%x1f%S", "--name-only",
                                  "--", *dirs], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        for rec in out.split("\x1e")[1:]:
            head, _, names = rec.partition("\n")
            parts = head.split("\x1f")
            if len(parts) < 6:
                continue
            sha, name, email, ts, subject, ref = parts
            if name.lower() in me or email.lower() in me or repos_mod.is_bot(name):
                continue
            p = people.setdefault(email.lower(), dict(name=name, email=email, commits=0, last=0, branches=set(),
                                                      subjects=[], files=Counter(), repo=Path(root).name))
            p["commits"] += 1
            p["last"] = max(p["last"], int(ts))
            ref = ref.removeprefix("refs/remotes/").removeprefix("origin/")
            if ref and ref not in DEFAULT_BRANCHES and ref != "HEAD":
                p["branches"].add(ref)
            if len(p["subjects"]) < 4:
                p["subjects"].append(subject)
            p["files"].update(n for n in names.split("\n") if n)
    return [dict(name=p["name"], email=p["email"], repo=p["repo"], commits=p["commits"], last=p["last"],
                 branches=sorted(p["branches"])[:5], subjects=p["subjects"], files=[f for f, _ in p["files"].most_common(5)])
            for p in sorted(people.values(), key=lambda p: -p["last"])][:12]


