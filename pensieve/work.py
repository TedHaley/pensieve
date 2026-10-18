"""The Context tab: one context card for each piece of work, with what belongs to it across sources.

A task joins agent sessions, git branches, commits and connection items (tickets, threads) by these links:
- a ticket key such as ABC-123 in a branch name, a commit subject, a session's text or a connection item;
- the git branch a session ran on (feature branches only: a default or long-lived branch holds many tasks);
- for work without a ticket: sessions in the same repo, a few days apart, that edited the same specific files.

`adjacent` finds what coworkers did near a task: their recent commits on remote branches in the same folders, and
tickets and threads about the same subject."""
import hashlib
import json
import re
import subprocess
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from pensieve import connections, repos as repos_mod, task_states

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
_branch_cache = {}       # transcript path -> (mtime, branch)
_roots = {}              # folder -> its git root, or ""
_cache = {}


def _not_users(project: str) -> bool:
    """A session that ran in a temp folder or under ~/Library: a script's or an app's, not the user's own work."""
    p = project.rstrip("/") + "/"
    return p.startswith(_TEMP) or p.startswith(str(Path.home() / "Library") + "/")


def _branch(path: str) -> str:
    """The last git branch that a Claude Code transcript records."""
    try:
        m = Path(path).stat().st_mtime
    except OSError:
        return ""
    hit = _branch_cache.get(path)
    if hit and hit[0] == m:
        return hit[1]
    b = ""
    try:
        data = Path(path).read_bytes()
        i = data.rfind(b'"gitBranch":"')
        if i >= 0:
            j = data.find(b'"', i + 13)
            b = data[i + 13:j].decode(errors="replace")
    except OSError:
        pass
    _branch_cache[path] = (m, b)
    return b


def _ts(s) -> float:
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _items() -> list[dict]:
    """Connection items: {path, server, key, title, status, assignee, updated, url, text, private}. `private`: a
    direct message, a group message or a private channel (marked when stored, or found now in an item stored before
    the mark), which local cards use and shared cards and notes leave out."""
    out = []
    if not connections.DIR.is_dir():
        return out
    for d in connections.DIR.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        for md in d.glob("*.md"):
            try:
                raw = json.loads((d / ".raw" / f"{md.stem}.json").read_text())
                text = md.read_text()
            except (OSError, ValueError):
                continue
            tool = re.search(r"^- Source: .*\(([^()\n]+)\)$", text, re.M)
            tool = tool.group(1) if tool else ""
            private = bool(raw.get("_private")) or connections.private_tool(tool) or \
                connections.private_item(raw, bool(connections.CHAT.search(f"{d.name} {tool}")))
            kn = connections._id_key(raw)
            key = str(raw.get(kn, "")) if kn in ("key", "identifier") else ""
            url = next((raw[k] for k in ("webUrl", "url", "permalink", "html_url") if isinstance(raw.get(k), str)
                        and raw[k].startswith("http")), "")
            head = raw.get("head") if isinstance(raw.get("head"), dict) else {}
            branch = raw.get("headRefName") or head.get("ref") or raw.get("source_branch") or ""  # a PR or MR
            title = re.search(r"^# (.+)$", text, re.M)  # after the front matter, when there is one
            assignees = [p.get("name") or p.get("email") or p.get("id") for p in raw.get("_people") or []
                         if p.get("role") == "assignee"]
            out.append(dict(path=str(md), server=d.name, key=key if KEY.fullmatch(key) else "",
                            title=raw.get("_title") or (title.group(1).strip() if title else md.stem), url=url,
                            branch=branch if isinstance(branch, str) else "",
                            pr=bool(PR_URL.fullmatch(url)) if url else False,
                            status=connections._render(raw.get("status") or raw.get("state")) if raw.get("status") or raw.get("state") else "",
                            assignee=", ".join(assignees) if assignees else
                            connections._render(raw.get("assignee")) if raw.get("assignee") else "",
                            updated=_ts(raw.get("_updated") or raw.get("updated") or raw.get("updated_at")
                                        or md.stat().st_mtime), text=text,
                            private=private))
    return out


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


def tasks(store, days: int = 30) -> list[dict]:
    """The tasks of the last `days` days, newest first. The last result is served at once; when it is older than a
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
    end = datetime.fromtimestamp(until, timezone.utc) if until else datetime.now(timezone.utc)
    since = (end - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    upto = end.strftime("%Y-%m-%dT%H:%M:%S.999Z")
    with store.lock:
        db = store.db
        sess = db.execute("SELECT id, project, title, summary, updated, started, path FROM sessions WHERE updated > ? "
                          "AND coalesce(started, updated) <= ? AND n_chunks > 0 AND path NOT LIKE '%.md' "
                          "ORDER BY updated DESC", (since, upto)).fetchall()
        ids = [s[0] for s in sess]
        texts = defaultdict(list)
        for sid, t in db.execute(f"SELECT session_id, text FROM chunks WHERE session_id IN "
                                 f"({','.join('?' * len(ids))}) ORDER BY session_id, idx", ids).fetchall() if ids else []:
            texts[sid].append(t)
        edits = defaultdict(set)
        for sid, p in db.execute(f"SELECT session_id, path FROM session_files WHERE edited=1 AND session_id IN "
                                 f"({','.join('?' * len(ids))})", ids).fetchall() if ids else []:
            if not p.startswith(_TEMP):  # scratch files are not the work
                edits[sid].add(p)
        roots = [r[0] for r in db.execute("SELECT root FROM repos").fetchall()]
        wts = {w: (r, b) for w, r, b in db.execute("SELECT path, repo, branch FROM worktrees").fetchall()}
        commits = db.execute("SELECT repo, sha, ts, author, subject FROM commits WHERE ts > ? AND ts <= ?",
                             (_ts(since + "+00:00"), end.timestamp())).fetchall()
    items = [i for i in _items() if not until or i["updated"] <= until]

    def repo_of(cwd):
        for w, (r, b) in wts.items():
            if cwd == w or cwd.startswith(w + "/"):
                return r, b
        r = max((r for r in roots if cwd == r or cwd.startswith(r + "/")), key=len, default="")
        return r, None

    # ticket keys count only with a known project prefix: from collected tickets and branch names
    prefixes = {KEY.fullmatch(i["key"]).group(1) for i in items if i["key"]}
    S = {}
    for sid, project, title, summary, updated, started, path in sess:
        if _not_users(project or ""):
            continue
        repo, wt_branch = repo_of(project or "")
        branch = wt_branch or (_branch(path) if sid.startswith("claude:") else "")
        S[sid] = dict(id=sid, title=title or "", summary=summary or "", updated=min(_ts(updated), until or 9e18), started=_ts(started),
                      project=project or "", repo=repo, branch=branch, edited=edits.get(sid, set()), path=path)
        prefixes |= {m.group(1) for m in KEY.finditer(branch)}
    # ticket links in the sessions' text teach each project prefix and its site, so a key alone becomes a link
    sites = {}
    for chunks in texts.values():
        for t in chunks:
            for base, key in TICKET_URL.findall(t):
                sites.setdefault(KEY.fullmatch(key).group(1), base)
    for i in items:
        if i["key"] and i["url"]:
            sites.setdefault(KEY.fullmatch(i["key"]).group(1), i["url"].rsplit("/" + i["key"], 1)[0])
    prefixes |= set(sites)

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
        found = Counter(keys_in("\n".join(texts.get(s["id"], []))))
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
        ks = set(keys_in(it["text"])) - {it["key"]}
        if it["key"]:
            continue  # a ticket that mentions other tickets stays its own task
        for key in list(ks)[:3]:
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
        files = Counter(p for s in ss for p in s["edited"])
        updated = max([s["updated"] for s in ss] + [i["updated"] for i in its] + [c["ts"] for c in cs] + [0])
        main = max(tickets, key=lambda i: i["updated"]) if tickets else None
        prs_seen = [i for i in its if i["pr"]]
        title = main["title"] if main else prs_seen[0]["title"] if prs_seen and not ss else \
            (brs[0].split("@", 1)[1] if brs else ss[0]["title"])
        # pull requests: PR items from connections, and PR links in the sessions' text (most mentioned first)
        urls = Counter(u_ for s in ss for t in texts.get(s["id"], []) for u_ in PR_URL.findall(t))
        named = {n for s in ss for n in re.findall(r"\bPR #?(\d+)", s["title"])}  # "PR 2074 …" in a session title
        ranked = sorted(urls, key=lambda u_: (u_.rsplit("/", 1)[-1] not in named, -urls[u_]))
        prs = [dict(url=i["url"], title=i["title"], state=i["status"], source=_server_label(i["server"]), head=i["branch"])
               for i in prs_seen]
        prs += [dict(url=u_, title="", state="", source="session") for u_ in ranked[:4] if u_ not in {p["url"] for p in prs}]
        out.append(dict(
            prs=prs[:4], _sessions=ss, _named=named,
            last_words=_tail((texts.get(ss[0]["id"]) or [""])[-1]) if ss else "",  # where the last session stopped
            ending=_tail("\n".join(texts.get(ss[0]["id"], [])[-2:]), 2400) if ss else "",  # what the note reads
            last_summary=(ss[0]["summary"] or "")[:800] if ss else "",
            earlier=[(s["title"], (s["summary"] or "")[:400]) for s in ss[1:3]],  # what the note's decisions come from
            events=_events(ss, its, cs),
            id=_task_id(keys, brs, ss), title=title, kind="ticket" if keys else "branch" if brs else "work",
            keys=keys, status=main["status"] if main else "", updated=updated,
            # tickets the sessions talk about without being their work; a key named once is often an example or a typo
            ticket_refs=[dict(key=k, url=f"{sites[k.split('-')[0]]}/{k}" if k.split("-")[0] in sites else "")
                         for k, n in Counter(k for s in ss for k in keys_in("\n".join(texts.get(s["id"], [])))).most_common()
                         if n >= 2 and k not in keys][:3],
            repos=sorted({s["repo"] for s in ss if s["repo"]} | {c["repo"] for c in cs}),
            branches=[dict(repo=b.split("@", 1)[0], branch=b.split("@", 1)[1]) for b in brs],
            tickets=[{k: i[k] for k in ("key", "title", "status", "assignee", "url", "server", "path", "updated")}
                     for i in sorted(tickets, key=lambda i: -i["updated"])] +
                    [dict(key=k, title="", status="", assignee="", url=f"{sites[k.split('-')[0]]}/{k}", server="",
                          path="", updated=0) for k in keys
                     if k not in {i["key"] for i in tickets} and k.split("-")[0] in sites],  # known only by its key
            items=[{k: i[k] for k in ("title", "url", "server", "path", "updated", "private")}
                   for i in sorted(its, key=lambda i: -i["updated"]) if not i["key"]][:30],
            sessions=[dict(id=s["id"], title=s["title"], updated=s["updated"], branch=s["branch"], repo=s["repo"],
                           project=s["project"], artifacts=_artifacts(s, texts.get(s["id"], []))) for s in ss],
            commits=cs[:30], files=[p for p, _ in files.most_common(15)],
            people=sorted({c["author"] for c in cs} | {t["assignee"] for t in tickets if t["assignee"]})))
    from concurrent.futures import ThreadPoolExecutor
    if until is None:  # a replay has no live folders or PR states
        with ThreadPoolExecutor(8) as pool:  # git and gh calls, each cached
            list(pool.map(lambda t: _pickup(t, wts), out))
    else:
        for t in out:
            t["places"] = []
    _own_prs(out)
    have, manual = notes(), task_states.latest(store)
    out += _goal_tasks(out, have)
    for t in out:
        _attention(t)
        task_states.apply(t, have.get(t["id"], {}), manual.get(t["id"]))
        t.pop("_sessions")
        t.pop("_named", None)
    out.sort(key=_order)
    return out


ARTIFACT_URL = re.compile(r"https://claude\.ai/(?:code/)?artifact/[\w-]+")


def _artifacts(s: dict, chunks: list[str], limit: int = 12) -> list[dict]:
    """What a session made: the files it created or edited that still exist (not scratch files), and the published
    Artifact links in its text, newest file first."""
    files = sorted((p for p in s["edited"] if Path(p).is_file()), key=_mtime, reverse=True)
    out = [dict(kind="file", path=p, name=Path(p).name) for p in files[:limit]]
    links = list(dict.fromkeys(u for t in chunks for u in ARTIFACT_URL.findall(t)))
    out += [dict(kind="link", url=u, name="Artifact " + u.rsplit("/", 1)[-1][:8]) for u in links[:limit]]
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


ACTIVE_MINUTES = 10      # a transcript written this recently: the session is running now
ACTIVE_TURN_MINUTES = 30  # ... and its last turn this recent
DONE = task_states.DONE


def _order(t):
    return task_states.ORDER.index(task_states.State(t["state"])), not t.get("active"), -t["updated"]


def _attention(task: dict):
    """The tags on the card: a session running now, an open pull request, a ticket that is not done, and uncommitted
    files on the task's own branch."""
    now = time.time()
    # the transcript was written in the last minutes AND it has a turn in the last half hour: a session left open that
    # only waits (to check CI again later, say) touches its file without doing work
    active = any(s["path"] and now - _mtime(s["path"]) < ACTIVE_MINUTES * 60 and now - s["updated"] < ACTIVE_TURN_MINUTES * 60
                 for s in task["_sessions"])
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


def matching(ts: list[dict], query: str, notes_: dict) -> list[dict]:
    """The tasks whose title, keys, branches, PRs, session titles or note contain words of the query, the most
    matched words first (ties keep their order)."""
    words = {w for w in re.findall(r"[a-z0-9][a-z0-9_.-]+", query.lower().replace("#", " ")) if len(w) > 2 or w.isdigit()}
    def text(t):
        n = core(notes_.get(t["id"], {}))
        return " ".join([t["title"], *t["keys"], *(b["branch"] for b in t["branches"]), *(p["url"] + " " + (p.get("title") or "")
                         for p in t["prs"]), *(s["title"] for s in t["sessions"][:8]), n.get("problem", ""),
                         n.get("goal", ""), n.get("next", "")]).lower()
    scored = [(sum(1 for w in words if re.search(rf"(?<![a-z0-9]){re.escape(w)}", tx)), i, t)
              for i, t in enumerate(ts) for tx in [text(t)]]
    return [t for s, i, t in sorted(scored, key=lambda x: (-x[0], x[1])) if s]


RESUME_WORDS = re.compile(r"\b(left off|left over|leftover|pick (it |this |that )?(back )?up|continue|resume|status of|"
                          r"where (are|were) we|where did (i|we)|what'?s left|what was left|follow[- ]?up)\b", re.I)
# a prompt about all of the user's work, not one piece of it
LIST_WORDS = re.compile(r"\b(standup|stand-up|unfinished|open loops|still open|in progress|my (open|unfinished|recent) "
                        r"work|what (have|did) i (do|done|work|ship)|last \d+ days|this week)\b", re.I)
STANDUP_WORDS = re.compile(r"\b(standup|stand-up|what (have|did) i|last \d+ days|this week)\b", re.I)


def card_for_prompt(ts: list[dict], prompt: str, notes_: dict) -> dict | None:
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
    ranked = sorted(((len(_words_hit(t, prompt, notes_)), -i, t) for i, t in enumerate(ts)), key=lambda x: x[:2],
                    reverse=True)
    if not ranked:
        return None
    first, second = ranked[0][0], ranked[1][0] if len(ranked) > 1 else 0
    return ranked[0][2] if first >= 2 and first > second else None


def _words_hit(t, prompt, notes_):
    n = core(notes_.get(t["id"], {}))
    text = " ".join([t["title"], *(s["title"] for s in t["sessions"][:8]), n.get("problem", ""), n.get("goal", ""),
                     n.get("next", "")]).lower()
    words = {w for w in re.findall(r"[a-z0-9][a-z0-9_.-]+", prompt.lower()) if len(w) > 3} - _STOP
    return [w for w in words if re.search(rf"(?<![a-z0-9]){re.escape(w)}", text)]


_STOP = {"where", "what", "with", "that", "this", "from", "back", "left", "pick", "work", "working", "tell", "have",
         "were", "still", "open", "continue", "resume", "status", "last", "session", "about", "want", "need", "done",
         "folder", "branch", "state", "there", "which", "when", "they", "them", "then", "does", "into", "anything"}


def brief(t: dict, note: dict) -> str:
    """One line for an agent: id, state, title, where, PRs and what is left."""
    note = reconcile(note, t)[0] if note else note
    p = (t.get("places") or [{}])[0]
    where = ", ".join(x for x in (Path(p.get("path", "")).name, p.get("branch", ""),
                                  p.get("changed") and f"{p['changed']} uncommitted",
                                  p.get("ahead") and f"{p['ahead']} not pushed") if x)
    prs = ", ".join(f"PR #{(re.search(r'(\d+)/?$', x['url']) or [0, '?'])[1]} {x['state'] or ''}".strip() for x in t["prs"][:2])
    bits = [f"[{t['state']}]" + (" (session running)" if t.get("active") else ""), t["title"][:90], where, prs,
            ", ".join(t["keys"][:2]), f"next: {note['next']}" if note.get("next") else "",
            f"waits on you: {note['question']}" if note.get("question") else "", _ago(t["updated"])]
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


def detail(t: dict, note: dict, hist: list[dict]) -> str:
    """The whole card for an agent, as Markdown: enough to pick the task up and finish it."""
    out = [f"# {t['title']}", f"id: {t['id']} · state: {t['state']} ({t['state_why']}) · last activity {_ago(t['updated'])}"]
    last = max((s["updated"] for s in t.get("sessions", [])), default=0)
    note = reconcile(note, t)[0] if note else note
    if note:
        out += ["", f"From the sessions (as of {datetime.fromtimestamp(last).strftime('%Y-%m-%d') if last else 'the last session'}; "
                    "the folders and PRs below are live):"]
        out += [f"{label}: {note[k]}" for k, label in CORE_LABELS if note.get(k)]
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


_PUSH = re.compile(r"\b(push|unpushed|not pushed|ahead of|committed locally|local commits?)\b", re.I)
_UNCOMMITTED = re.compile(r"\buncommitted\b", re.I)
_PR_OPEN = re.compile(r"(?:PR\s*)?#(\d+)[^.;]*\b(open|draft|review|merge it|to merge|waiting|pending)\b|"
                      r"\b(merge|review|open)\b[^.;]*#(\d+)", re.I)


CORE_LABELS = (("problem", "Problem"), ("goal", "Goal"), ("state", "State"), ("next", "Next"), ("question", "Waiting on you"))


def reconcile(note: dict, t: dict) -> tuple[dict, list[str]]:
    """The note's core (problem, goal, state, next, question) with each sentence that the live state contradicts
    marked as outdated, and what the live state says instead. A note is written from the sessions; git and gh show
    what happened after them (a push, a merged PR). A claim about the checkout (pushed, uncommitted) can be outdated
    anywhere but the problem and goal; a claim that a PR waits, only in what is left to do (next, question)."""
    note = core(note)
    places = [p for p in t.get("places", []) if p.get("git")]
    pushed = bool(places) and all(p.get("upstream") and not p.get("ahead") for p in places if p.get("branch"))
    clean = bool(places) and not any(p.get("changed") for p in places)
    state = {m: (p.get("state") or "") for p in t["prs"] + t.get("branch_prs", [])
             for m in re.findall(r"/pull/(\d+)", p["url"])}
    done = _done_items().get(t.get("id"), {}).get("done", {})
    changes, out = [], {}
    for k, v in note.items():
        keep = []
        for s in re.split(r"(?<=[.;])\s+", v):
            why = None
            if k in ("state", "next", "question") and pushed and clean and _PUSH.search(s):  # uncommitted: a push still waits
                why = "every commit is pushed now"
            elif k in ("state", "next", "question") and places and _PUSH.search(s) and (on := _on_remote(places, s)):
                why = on
            elif k in ("state", "next", "question") and _PUSH.search(s) and not _SHA.search(s) and _commits_pushed(t):
                why = "its commits are pushed now"
            elif k in ("state", "next", "question") and clean and _UNCOMMITTED.search(s):
                why = "nothing is uncommitted now"
            elif k in ("next", "question") and s.strip() in done:
                why = done[s.strip()]
            else:
                for m in _PR_OPEN.finditer(s):
                    n = m.group(1) or m.group(4)
                    if state.get(n) in ("merged", "closed") and k in ("next", "question"):  # work to do, not done
                        why = f"PR #{n} is {state[n]} now"
            if why:  # marked, not dropped: the rest of the sentence ("push, then deploy") can still be open
                changes.append(why)
                s = f"{s.rstrip()} [outdated: {why}]"
            keep.append(s)
        out[k] = " ".join(keep).strip()
    return out, list(dict.fromkeys(changes))


DONE_PROMPT = """A developer's task has things left to do, from a note written when its last agent session ended.
Commits made since that session, in the task's repos, on any branch (hash, author, subject, body):
{commits}

Left to do:
{items}

For each item that one of these commits clearly does in full, give the item's number and the commit's hash. Leave out
an item that a commit only starts or only resembles."""
DONE_SCHEMA = {"type": "object", "properties": {"done": {"type": "array", "items": {
    "type": "object", "properties": {"item": {"type": "integer"}, "commit": {"type": "string"}},
    "required": ["item", "commit"], "additionalProperties": False}}}, "required": ["done"], "additionalProperties": False}


def _done_path():
    from pensieve import config
    return config.DATA_DIR / "work_done.json"


def _done_items() -> dict:
    """{task: {key, done: {sentence: why}}}: what check_done found, read again when the file changes."""
    try:
        m = _done_path().stat().st_mtime
        if _done_cache[0] != m:
            _done_cache[:] = [m, json.loads(_done_path().read_text())]
    except (OSError, ValueError):
        return {}
    return _done_cache[1]


_done_cache = [None, {}]


def _left_items(note: dict) -> list[str]:
    """What a note says is left: the sentences of next and question, and the goals that only the sessions talk
    about (also), which a later commit can have built."""
    c = core(note)
    return [s.strip() for k in ("next", "question") for s in re.split(r"(?<=[.;])\s+", c.get(k, "")) if s.strip()] + \
        [a for a in note.get("also", []) if a]


def check_done(ts: list[dict], limit: int = 15) -> int:
    """Mark what a note says is left to do but a later commit did: for each task, the commits on any branch of its
    repos after its last session, and one model call when there are any and they are new. A sentence that a commit
    does in full gets [outdated: done in <hash> (subject)] in reconcile. Returns the number of model calls."""
    from pensieve import llm
    have, saved, calls = notes(), dict(_done_items()), 0
    for t in [t for t in ts if t["state"] not in DONE and t["sessions"]][:limit]:
        items = _left_items(have.get(t["id"], {}))
        if not items:
            continue
        since = datetime.fromtimestamp(t["sessions"][0]["updated"]).isoformat(timespec="seconds")
        log = []
        for root in t["repos"]:
            if Path(root).is_dir():
                out = _git(root, "log", "--all", "--no-merges", f"--since={since}", "-n", "30",
                           "--format=%h%x1f%an%x1f%s%x1f%b%x1e", ttl=300)
                log += [x.strip().split("\x1f") for x in out.split("\x1e") if x.strip()]
        log = [c for c in log if len(c) == 4]
        if not log:
            continue
        key = json.dumps([have[t["id"]].get("at"), items, sorted(c[0] for c in log)])
        if saved.get(t["id"], {}).get("key") == key:
            continue
        try:
            ans = llm.complete(DONE_PROMPT.format(
                commits="\n".join(f"- {h} {a}: {s}" + (f" — {re.sub(r'\s+', ' ', b)[:300]}" if b.strip() else "")
                                  for h, a, s, b in log),
                items="\n".join(f"{i + 1}. {s}" for i, s in enumerate(items))),
                max_tokens=200, schema=DONE_SCHEMA, kind="done check")
        except llm.Offline:  # Queued too: the agent engine takes it later
            break
        except Exception:
            continue
        calls += 1
        subj = {h: s for h, _, s, _ in log}
        done = {}
        for d in ans.get("done", []):
            h = next((x for x in subj if d.get("commit", "").startswith(x) or x.startswith(d.get("commit", "") or "-")), None)
            if h and 1 <= d.get("item", 0) <= len(items):
                done[items[d["item"] - 1]] = f"done in {h} ({subj[h][:80]})"
        saved[t["id"]] = dict(key=key, done=done)
    if calls:
        _done_path().write_text(json.dumps(saved))
    return calls


_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")


def _on_remote(places: list[dict], s: str) -> str:
    """What says that the commits a sentence names ("committed locally as 1a2b3c4d") are pushed: "1a2b3c4d is
    pushed now" when every one is on a remote branch, or ""."""
    shas = list(dict.fromkeys(h for h in _SHA.findall(s) if not h.isdigit()))
    if not shas or not all(_git(places[0]["path"], "branch", "-r", "--contains", h, ttl=300) for h in shas):
        return ""
    return f"{' and '.join(shas)} {'is' if len(shas) == 1 else 'are'} pushed now"


def _commits_pushed(t: dict) -> bool:
    """Every commit of the task (the first 5) is on a remote branch: a push that a note asks for without naming the
    commits ("push the two local commits") is done."""
    cs = [c for c in t.get("commits", [])[:5] if Path(c.get("repo", "")).is_dir()]
    return bool(cs) and all(_git(c["repo"], "branch", "-r", "--contains", c["sha"], ttl=300) for c in cs)


def asked_question(t: dict) -> str:
    """The last session's own final question, checked like a note against the live state ("Push them all?" after
    the push gets [outdated: …]), or ""."""
    q = last_question(t.get("last_words") or "") if not t.get("goals_split") else ""
    return reconcile({"v": NOTE_VERSION, "question": q}, t)[0].get("question", "") if q else ""


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


def compact(t: dict, note: dict, hist: list[dict]) -> str:
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
    note, changed = reconcile(note, t) if note else ({}, [])
    since = [f"{_pr_short(p).split(' ')[0]} merged {day(p['merged_at'])}" for p in prs if last and (p.get("merged_at") or 0) > last]
    since += [c for c in changed if not c.startswith("PR #")]  # a PR's state is on the PRs line already
    if since:
        out.append("Since the last session: " + "; ".join(since))
    if note:
        out += ["", "As of the last session:"] + [f"{label}: {note[k]}" for k, label in CORE_LABELS[:4] if note.get(k)]
    asked = asked_question(t)  # the session's own words, over the note's
    if asked or note.get("question"):
        out.append(f"Waiting on you: {asked or note['question']}")
    raw = notes().get(t["id"], {})
    if raw.get("also"):
        done = _done_items().get(t["id"], {}).get("done", {})
        out.append("Also talked about in these sessions, with no PR, ticket or commit apart from this card's then: " + "; ".join(
            a + (f" [outdated: {done[a]}]" if a in done else "") for a in raw["also"]))
    if split := [s for s in raw.get("split") or [] if not _part_of_main(s, raw)]:
        out.append("Other goals of these sessions, each its own card: " + ", ".join(s["id"] for s in split))
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
    """Set a task's state by hand (or "auto"), and update the tasks in the cache at once."""
    ch = task_states.set_state(store, task_id, state, by, reason)
    have = notes()
    for _, ts in _cache.values():
        for t in ts:
            if t["id"] == task_id:
                task_states.apply(t, have.get(task_id, {}), ch)
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
    if hit and (time.time() - hit[0] < 600 or _net["off"]):
        return hit[1]
    out = {}
    if _net["off"]:
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
            p["changed"] = len([x for x in _git(root, "status", "--porcelain", "--untracked-files=no").splitlines() if x])
            p["worktree"] = root in wts
            ab = _git(root, "rev-list", "--left-right", "--count", "@{upstream}...HEAD").split()
            if len(ab) == 2 and ab[0].isdigit():  # commits behind and ahead of the branch's upstream
                p["behind"], p["ahead"], p["upstream"] = int(ab[0]), int(ab[1]), True
                p["pushed"] = _git(root, "rev-parse", "--short", "@{upstream}")  # moves on a push: history saves the card
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
        hist = Path(i["path"]).parent / ".history" / f"{Path(i['path']).stem}.jsonl"
        try:
            for line in hist.read_text().splitlines():
                h = json.loads(line)
                for k, (a, b) in h["changes"].items():
                    ev.append(dict(ts=_ts(h["at"].replace("Z", ":00+00:00")), source="change",
                                   title=f"{i['key'] or i['title'][:40]}: {k} {a or '(empty)'} → {b or '(empty)'}",
                                   detail=_server_label(i["server"]), url=i["url"], ref=i["path"],
                                   private=i.get("private", False)))
        except (OSError, ValueError, KeyError):
            pass
    return sorted(ev, key=lambda e: -e["ts"])[:80]


def _server_label(sid: str) -> str:
    return "agent sessions" if sid == connections.SESSIONS else sid.removeprefix("claude_ai_").split("_")[-1]


def _task_id(keys, branches, sessions) -> str:
    """Stable while the task grows: its first ticket key, else its first branch, else its oldest session."""
    if keys:
        return f"t:{keys[0]}"
    if branches:
        return f"b:{branches[0]}"
    return f"s:{min(sessions, key=lambda s: s['started'] or s['updated'])['id']}"


NOTE_PROMPT = """You write the status line of one task on a developer's work dashboard.
Task: {title}{keys}{status}
Pull requests: {prs}
Working folders: {places}
Its data points, newest first (date, source, what happened):
{events}
{earlier}The last agent session: "{last_title}"
{last_summary}
How it ended:
{last_words}

Sessions, a long one most of all, can work on more than one goal. The main goal is the newest goal that a session
did work toward (changed code, made a commit or PR, ran a job, made a decision). A session that only explained code
or answered questions does not set the main goal. A question the developer asked on the way (explain this, reply to
that) is not a goal, and neither is a step that serves a goal (a change it needs, a revert, a CI fix, a rebase, a
review round): that is part of the goal. Each PR, commit and ticket in the data belongs to the goal it was made for:
a PR made for another goal makes that goal one of the goals below, with the PR as its evidence. The first six fields are about the main goal only: what is done and left for
another goal goes in that goal's fields, not in these.
Answer with these fields:
- title: at most 8 words that name the goal as an outcome, in sentence case, with no ticket key or branch name.
- problem: at most 25 words on what is wrong or missing, to bring it back to mind.
- goal: at most 20 words on what "done" looks like, and why it matters.
- state: at most 50 words on where it stands: what has been done, the key findings (a cause found, a number measured,
  an idea ruled out, with what it was), and what was decided and why (an approach chosen or dropped).
- next: at most 25 words on what needs to be done, and whether someone is still working on it; "Nothing" if it is
  done.
- question: at most 20 words on the question that the last session asked the developer and that is not answered;
  "None" if there is no such question.
- evidence: the PR numbers (#12), ticket keys and commit hashes from the data above that the main goal's work is in.
- goals: the other goals (0 to 2), each with a title, its evidence (as above; [] when only the sessions talk about
  it), and its state, next and question in at most 20 words each ("None" if none).
State facts from the data above only. Name the real PRs, tickets, functions and errors from the data, but no local
folders or file paths: someone on another machine reads this too. Say how something was done or why it worked only
in the data's own words; if the data doesn't say, leave it out."""
CORE = ("problem", "goal", "state", "next", "question")
_REFS = {"type": "array", "items": {"type": "string"}}
NOTE_SCHEMA = {"type": "object", "properties": {
    **{k: {"type": "string"} for k in ("title",) + CORE}, "evidence": _REFS,
    "goals": {"type": "array", "items": {"type": "object", "properties": {
        "title": {"type": "string"}, "evidence": _REFS, **{k: {"type": "string"} for k in ("state", "next", "question")}},
        "required": ["title", "evidence", "state", "next", "question"], "additionalProperties": False}}},
    "required": ["title", *CORE, "evidence", "goals"], "additionalProperties": False}
NOTE_VERSION = 10  # a new version writes every note again; 9: a title that names the goal; 10: the sessions' other goals


def core(note: dict) -> dict:
    """A note's core (problem, goal, state, next, question), from a note in the format of NOTE_VERSION 8 or older
    (recap, current, decisions, left). Empty fields and "None" answers are left out."""
    if not note:
        return {}
    if "problem" not in note and "goal" not in note:  # a split goal's note has no problem
        note = dict(problem=note.get("recap"), state=" ".join(x for x in (note.get("current"), note.get("decisions"))
                                                              if x and x.strip(" .").lower() != "none"),
                    next=note.get("left"), question=note.get("question"))
    return {k: note[k].strip() for k in CORE if isinstance(note.get(k), str) and note[k].strip(" .").lower() not in ("", "none")}


def card_text(t: dict, note: dict | None) -> str:
    """The words of one card that a search reads: its titles, its note, where the work is, its PRs, tickets and
    sessions."""
    note = note or {}
    places = [" ".join(x for x in (p.get("repo"), p.get("branch")) if x) for p in t.get("places", [])]
    return "\n".join(x for x in [t["title"], note.get("title", ""), *core(note).values(), *places,
                                 *(f"#{p['url'].rstrip('/').rsplit('/', 1)[-1]} {p.get('title') or ''}" for p in t["prs"]),
                                 *(f"{k['key']} {k.get('title') or ''}" for k in t.get("tickets", [])),
                                 *(s.get("title") or "" for s in t["sessions"][:8])] if x.strip())


_card_vecs: dict[str, np.ndarray] = {}  # card text hash -> embedding
# Meaning scores of cards are close together (0.6 to 0.8 on real cards), so a share of the best keeps almost all.
# A card stays when it is this close to the best one; the best few always stay.
CARD_SEARCH_MARGIN, CARD_SEARCH_MIN = 0.06, 3
CARD_SEARCH_WORDS = 0.08  # added to the meaning score when a card has all the query's words, a share for some


def _card_vectors(tasks: list[dict], have: dict) -> dict[str, np.ndarray]:
    """Each task's card vector, by task id. A card's vector is made again only when its text changes."""
    from pensieve import indexer
    keys = {t["id"]: hashlib.sha1(card_text(t, have.get(t["id"])).encode()).hexdigest() for t in tasks}
    todo = {keys[t["id"]]: card_text(t, have.get(t["id"])) for t in tasks if keys[t["id"]] not in _card_vecs}
    if todo:
        for k, v in zip(todo, indexer.embed(list(todo.values()))):
            _card_vecs[k] = v
    return {i: _card_vecs[k] for i, k in keys.items()}


def warm_card_search(tasks: list[dict], have: dict) -> None:
    """Make the card vectors in the background, so the first card search does not wait for them."""
    threading.Thread(target=_card_vectors, args=(tasks, have), daemon=True).start()


def search_cards(tasks: list[dict], have: dict, q: str) -> list[dict]:
    """The cards that match a query, best first: {id, score}. The query is read as in search.parse: "exact phrases"
    and -excluded words filter the cards, the other words rank them by meaning. With no other words, the cards
    keep their order."""
    from pensieve import indexer, search
    pq = search.parse(q)
    if not (pq["semantic"] or pq["exact"] or pq["exclude"]):
        return []
    texts = {t["id"]: card_text(t, have.get(t["id"])) for t in tasks}
    low = {k: v.lower() for k, v in texts.items()}
    keep = [t for t in tasks if all(p.lower() in low[t["id"]] for p in pq["exact"])
            and not any(x.lower() in low[t["id"]] for x in pq["exclude"])]
    if not pq["semantic"] or not keep:
        return [dict(id=t["id"], score=1.0) for t in keep]
    vecs = _card_vectors(keep, have)
    qv = indexer.embed([pq["semantic"]], query=True)[0]
    # a card that has the query's words goes up: a near-empty card ("hello") is close in meaning to almost anything
    words = [w for w in re.findall(r"\w+", pq["semantic"].lower()) if len(w) >= 3]
    lex = {t["id"]: sum(w in low[t["id"]] for w in words) / len(words) if words else 0 for t in keep}
    scored = sorted(((float(vecs[t["id"]] @ qv) + CARD_SEARCH_WORDS * lex[t["id"]], t["id"]) for t in keep), reverse=True)
    best = scored[0][0]
    return [dict(id=i, score=round(s, 4)) for n, (s, i) in enumerate(scored)
            if n < CARD_SEARCH_MIN or s >= best - CARD_SEARCH_MARGIN]
NOTE_MIN_AGE = 600  # seconds before a note is written again for new data


def _notes_path():
    from pensieve import config
    return config.DATA_DIR / "work_notes.json"


def notes() -> dict:
    try:
        return json.loads(_notes_path().read_text())
    except (OSError, ValueError):
        return {}


def _signature(task: dict) -> str:
    import hashlib
    data = [NOTE_VERSION, [(e["ts"], e["title"]) for e in task["events"][:30]], [(p["url"], p["state"]) for p in task["prs"]],
            [(p["path"], p.get("branch"), p.get("changed")) for p in task.get("places", [])]]
    return hashlib.sha1(json.dumps(data).encode()).hexdigest()[:16]


def stale_notes(ts: list[dict], limit: int = 10, ids=None) -> list[dict]:
    """The tasks (the first `limit`, or those in `ids`) whose note is missing, of an older format, or older than their
    data points. A note younger than NOTE_MIN_AGE is kept: a running session changes its data on every turn."""
    have, now = notes(), time.time()

    def stale(t):
        n = have.get(t["id"])
        if not n or n.get("v") != NOTE_VERSION:
            return True
        return n.get("sig") != _signature(t) and now - n.get("at", 0) > NOTE_MIN_AGE
    pick = [t for t in ts if t["id"] in ids] if ids else ts[:limit]
    return [t for t in pick if not t.get("goal_of") and stale(t)]  # a goal's note comes with its task's


def resume_command(store, session_id: str) -> str | None:
    """The shell command that resumes an agent session in its folder, built from the index (never from client text)."""
    import shlex
    source, _, sid = session_id.partition(":")
    if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", sid or ""):
        return None
    with store.lock:
        row = store.db.execute("SELECT project FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row or not row[0] or not Path(row[0]).is_dir():
        return None
    cmd = {"claude": f"claude --resume {sid}", "codex": f"codex resume {sid}"}.get(source)
    return f"cd {shlex.quote(row[0])} && {cmd}" if cmd else None


def write_note(task: dict, all_tasks: list[dict]) -> str:
    """Ask the insights engine for the task's note, and keep it with the signature of the data it saw."""
    note = _split_goals(task, make_note(task))
    cur, sig, now = notes(), _signature(task), time.time()
    for k in [k for k, n in cur.items() if n.get("goal_of") == task["id"]]:
        del cur[k]
    for s in note["split"]:
        g = note["goals"][s["n"]]
        cur[s["id"]] = dict(title=g["title"], goal=g["title"], **{k: g[k] for k in ("state", "next", "question")},
                            goal_of=task["id"], sig=sig, at=now, v=NOTE_VERSION)
    cur[task["id"]] = dict(note, sig=sig, at=now, v=NOTE_VERSION)
    path = _notes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cur))
    return cur[task["id"]]


def note_fields(task: dict) -> dict:
    """The task's data for NOTE_PROMPT: its PRs, folders, data points and how its last session ended."""
    day = lambda ts: datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    events = "\n".join(f"- {day(e['ts'])} {e['source']}: {e['title'][:140]}" + (f" ({e['detail'][:60]})" if e.get("detail") else "")
                       for e in [e for e in task["events"] if not e.get("private")][:25])  # the note goes on shared cards
    prs = "; ".join(f"{p['url']} ({p['state'] or 'state unknown'})" + (f": {p['title'][:90]}" if p.get("title") else "")
                    for p in task["prs"]) or "none"
    places = "; ".join(f"{p['path']}" + (f" on {p['branch']}, {p['changed']} uncommitted files" if p.get("branch") else "")
                       for p in task.get("places", [])) or "none"
    return dict(title=task["title"], keys=f" [{', '.join(task['keys'][:3])}]" if task["keys"] else "",
                status=f", ticket status {task['status']}" if task["status"] else "", prs=prs, places=places, events=events,
                last_words=task.get("ending") or task.get("last_words") or "(none)",
                last_title=(task["sessions"] or [{}])[0].get("title", "(none)"), last_summary=task.get("last_summary") or "",
                earlier="".join(f'An earlier session: "{title}"\n{summary}\n' for title, summary in task.get("earlier", []) if summary))


def make_note(task: dict) -> dict:
    """The insights engine's note for a task ({problem, goal, state, next, question}), not saved: write_note keeps
    it; a replay writes notes for past snapshots with it."""
    from pensieve import llm
    ans = llm.complete(NOTE_PROMPT.format(**note_fields(task)), max_tokens=700, schema=NOTE_SCHEMA, kind="task note")
    note = {k: str(ans.get(k, "")).strip() for k in ("title",) + CORE}
    note["evidence"] = [str(x) for x in ans.get("evidence") or []]
    note["goals"] = [{k: (g.get(k) if k == "evidence" else str(g.get(k, "")).strip()) for k in ("title", "evidence", "state", "next", "question")}
                     for g in (ans.get("goals") or [])[:2] if isinstance(g, dict) and g.get("title")]
    return note


def _evidence(t: dict, refs: list) -> dict:
    """The refs that a note gives for a goal ("#12", "ABC-12", "1a2b3c4d") that are real: the task's PRs (urls) and
    ticket keys, and commits that are the task's or in its repos ({sha, ts, author, subject, repo}). Any other ref
    is left out: the model can't make up a member."""
    out = dict(prs=[], keys=[], commits=[])
    known = set(t["keys"]) | {k["key"] for k in t.get("ticket_refs", [])}
    for r in (str(x).strip() for x in refs or []):
        if KEY.fullmatch(r) and r in known:
            out["keys"].append(r)
        elif m := re.fullmatch(r"(?:PR\s*)?#?(\d{1,6})", r, re.I):
            out["prs"] += [p["url"] for p in t["prs"] if p["url"].rstrip("/").endswith(f"/pull/{m.group(1)}")]
        elif _SHA.fullmatch(r.lower()) and not r.isdigit():
            c = next((c for c in t["commits"] if c["sha"].startswith(r.lower()[:10])), None) or _commit(t, r.lower())
            if c and c["sha"] not in {x["sha"] for x in out["commits"]}:
                out["commits"].append(c)
    return {k: v if k == "commits" else list(dict.fromkeys(v)) for k, v in out.items()}


def _commit(t: dict, sha: str) -> dict | None:
    """A commit of one of the task's repos, by its hash, as work.tasks keeps commits, or None."""
    for root in t["repos"]:
        if Path(root).is_dir():
            f = _git(root, "log", "-1", "--format=%h%x1f%at%x1f%ae%x1f%s", "--abbrev=10", sha, "--", ttl=3600).split("\x1f")
            if len(f) == 4 and f[0].startswith(sha[:7]):
                return dict(repo=root, sha=f[0], ts=int(f[1]), author=f[2], subject=f[3])
    return None


def _split_goals(task: dict, note: dict) -> dict:
    """The note with its other goals sorted by the rule of docs/threads.md: a goal with data outside the sessions (a
    PR, ticket or commit of the task) is its own card, `split` (id and evidence); one that only the sessions talk
    about, or whose data the main goal also names, is a line on the main card, `also`."""
    note = dict(note, split=[], also=[])
    note["main"] = _evidence(task, note.get("evidence"))
    for i, g in enumerate(note.get("goals", [])):
        ev = _evidence(task, g.get("evidence"))
        if not any(ev.values()) or _part_of_main(ev, note):
            note["also"].append(g["title"])
            continue
        first = (ev["keys"] or [f"pr{u.rstrip('/').rsplit('/', 1)[-1]}" for u in ev["prs"]] or [ev["commits"][0]["sha"][:8]])[0]
        gid, n = f"{task['id']}~{first}", 1
        while gid in {s["id"] for s in note["split"]}:
            n, gid = n + 1, f"{task['id']}~{first}-{n + 1}"
        note["split"].append(dict(id=gid, n=i, **ev))
    return note


def _refs(ev: dict) -> set:
    return set(ev.get("prs", [])) | set(ev.get("keys", [])) | {c["sha"] for c in ev.get("commits", [])}


def _part_of_main(ev: dict, note: dict) -> bool:
    """True when a goal is part of the main work, not a second card with the same PR: the main goal's evidence has
    every PR, ticket and commit of the goal, and the main goal's next step names one of them. The note model often
    lists the PR of another goal as main evidence too, so the evidence alone does not show this."""
    own = _refs(ev)
    if not own or not own <= _refs(note.get("main") or {}):
        return False
    nxt = (note.get("next") or "").lower()
    names = [u.rstrip("/").rsplit("/", 1)[-1] for u in ev.get("prs", [])] + [k.lower() for k in ev.get("keys", [])]
    names += [c["sha"][:7].lower() for c in ev.get("commits", [])]
    return any(re.search(rf"(?<![\w-]){re.escape(x)}(?![\w-])", nxt) for x in names)


def _goal_tasks(out: list[dict], have: dict) -> list[dict]:
    """The cards of the goals that the notes split off their tasks (_split_goals): the same sessions and folders,
    with only the goal's own PRs, tickets and commits. A PR or commit that only another goal names leaves the main
    card, so the two link to different threads; a commit that the main goal names and the task didn't have joins it."""
    extra = []
    for t in out:
        n = have.get(t["id"]) or {}
        if n.get("v") != NOTE_VERSION:
            continue
        main = n.get("main") or {}
        t["commits"] = t["commits"] + [c for c in main.get("commits", []) if c["sha"] not in {x["sha"] for x in t["commits"]}]
        split = [g for g in n.get("split") or [] if not _part_of_main(g, n)]  # notes written before that rule
        if not split:
            continue
        mine = set(main.get("prs", [])) | {c["sha"] for c in main.get("commits", [])}
        theirs = {x for g in split for x in g["prs"] + [c["sha"] for c in g["commits"]]} - mine
        for g in split:
            gn = have.get(g["id"]) or {}
            extra.append(dict(t, id=g["id"], goal_of=t["id"], title=gn.get("title") or g["id"], kind="work",
                              keys=g["keys"], tickets=[k for k in t["tickets"] if k["key"] in g["keys"]], ticket_refs=[],
                              prs=[p for p in t["prs"] if p["url"] in g["prs"]], branch_prs=[], commits=g["commits"],
                              branches=[], files=[], last_words="", ending=""))
        t["prs"] = [p for p in t["prs"] if p["url"] not in theirs]
        t["commits"] = [c for c in t["commits"] if c["sha"] not in theirs]
        t["goals_split"] = True  # the last session's own question can be another goal's
    return extra


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


def related(task: dict, searcher, limit: int = 8) -> list[dict]:
    """Tickets, threads and pages about the same subject as the task that are not part of it."""
    from pensieve import scopes
    mine = {t["path"] for t in task["tickets"]} | {i["path"] for i in task["items"]}
    q = " ".join([task["title"]] + task["keys"][:3])
    try:
        hits = searcher.find(q, limit=limit * 3, kinds=["file"], scope=scopes._build("data-tab", ["connections"]),
                             retry=False)
    except Exception:
        return []
    hits = hits.get("results", hits) if isinstance(hits, dict) else hits
    out = []
    for h in hits:
        p = h.get("path", "")
        if p and p not in mine and p.startswith(str(connections.DIR)):
            it = next((i for i in _items() if i["path"] == p), None)
            out.append(dict(path=p, title=(it or {}).get("title") or h.get("title"), url=(it or {}).get("url", ""),
                            status=(it or {}).get("status", ""), assignee=(it or {}).get("assignee", ""),
                            server=Path(p).parent.name, snippet=h.get("snippet", "")))
        if len(out) >= limit:
            break
    return out
