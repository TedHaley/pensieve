"""The Context tab: one context card for each piece of work, with what belongs to it across sources.

A task joins agent sessions, git branches, commits and connection items (tickets, threads) by these links:
- a ticket key such as ABC-123 in a branch name, a commit subject, a session's text or a connection item;
- the git branch a session ran on (feature branches only: a default or long-lived branch holds many tasks);
- for work without a ticket: sessions in the same repo, a few days apart, that edited the same specific files.

`adjacent` finds what coworkers did near a task: their recent commits on remote branches in the same folders, and
tickets and threads about the same subject."""
import json
import re
import subprocess
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    """Connection items: {path, server, key, title, status, assignee, updated, url, text}."""
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
            kn = connections._id_key(raw)
            key = str(raw.get(kn, "")) if kn in ("key", "identifier") else ""
            url = next((raw[k] for k in ("webUrl", "url", "permalink", "html_url") if isinstance(raw.get(k), str)
                        and raw[k].startswith("http")), "")
            head = raw.get("head") if isinstance(raw.get("head"), dict) else {}
            branch = raw.get("headRefName") or head.get("ref") or raw.get("source_branch") or ""  # a PR or MR
            out.append(dict(path=str(md), server=d.name, key=key if KEY.fullmatch(key) else "",
                            title=text.split("\n", 1)[0].lstrip("# ").strip(), url=url,
                            branch=branch if isinstance(branch, str) else "",
                            pr=bool(PR_URL.fullmatch(url)) if url else False,
                            status=connections._render(raw.get("status") or raw.get("state")) if raw.get("status") or raw.get("state") else "",
                            assignee=connections._render(raw.get("assignee")) if raw.get("assignee") else "",
                            updated=_ts(raw.get("updated") or raw.get("updated_at") or md.stat().st_mtime), text=text))
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


def _tasks(store, days):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    with store.lock:
        db = store.db
        sess = db.execute("SELECT id, project, title, summary, updated, started, path FROM sessions WHERE updated > ? "
                          "AND n_chunks > 0 AND path NOT LIKE '%.md' ORDER BY updated DESC", (since,)).fetchall()
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
        commits = db.execute("SELECT repo, sha, ts, author, subject FROM commits WHERE ts > ?",
                             (_ts(since + "+00:00"),)).fetchall()
    items = _items()

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
        S[sid] = dict(id=sid, title=title or "", summary=summary or "", updated=_ts(updated), started=_ts(started),
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
            repos=sorted({s["repo"] for s in ss if s["repo"]} | {c["repo"] for c in cs}),
            branches=[dict(repo=b.split("@", 1)[0], branch=b.split("@", 1)[1]) for b in brs],
            tickets=[{k: i[k] for k in ("key", "title", "status", "assignee", "url", "server", "path", "updated")}
                     for i in sorted(tickets, key=lambda i: -i["updated"])] +
                    [dict(key=k, title="", status="", assignee="", url=f"{sites[k.split('-')[0]]}/{k}", server="",
                          path="", updated=0) for k in keys
                     if k not in {i["key"] for i in tickets} and k.split("-")[0] in sites],  # known only by its key
            items=[{k: i[k] for k in ("title", "url", "server", "path", "updated")}
                   for i in sorted(its, key=lambda i: -i["updated"]) if not i["key"]][:30],
            sessions=[dict(id=s["id"], title=s["title"], updated=s["updated"], branch=s["branch"], repo=s["repo"],
                           project=s["project"], artifacts=_artifacts(s, texts.get(s["id"], []))) for s in ss],
            commits=cs[:30], files=[p for p, _ in files.most_common(15)],
            people=sorted({c["author"] for c in cs} | {t["assignee"] for t in tickets if t["assignee"]})))
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as pool:  # git and gh calls, each cached
        list(pool.map(lambda t: _pickup(t, wts), out))
    _own_prs(out)
    have, manual = notes(), task_states.latest(store)
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
    changes, out = [], {}
    for k, v in note.items():
        keep = []
        for s in re.split(r"(?<=[.;])\s+", v):
            why = None
            if k in ("state", "next", "question") and pushed and clean and _PUSH.search(s):  # uncommitted: a push still waits
                why = "every commit is pushed now"
            elif k in ("state", "next", "question") and clean and _UNCOMMITTED.search(s):
                why = "nothing is uncommitted now"
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
    asked = last_question(t.get("last_words") or "")  # the session's own words, over the note's
    if asked or note.get("question"):
        out.append(f"Waiting on you: {asked or note['question']}")
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
    changes that connections recorded (a ticket's status going from To Do to In Progress)."""
    ev = [dict(ts=s["updated"], source="session", title=s["title"] or "Agent session", detail=s["branch"], ref=s["id"])
          for s in ss]
    ev += [dict(ts=c["ts"], source="commit", title=c["subject"], detail=f"{c['author']} · {c['sha']}", ref=c["sha"])
           for c in cs]
    for i in its:
        src = "ticket" if i["key"] else "pr" if i["pr"] else "message"
        ev.append(dict(ts=i["updated"], source=src, title=(f"{i['key']} " if i["key"] else "") + i["title"],
                       detail=" · ".join(x for x in (i["status"], i["assignee"], _server_label(i["server"])) if x),
                       url=i["url"], ref=i["path"]))
        hist = Path(i["path"]).parent / ".history" / f"{Path(i['path']).stem}.jsonl"
        try:
            for line in hist.read_text().splitlines():
                h = json.loads(line)
                for k, (a, b) in h["changes"].items():
                    ev.append(dict(ts=_ts(h["at"].replace("Z", ":00+00:00")), source="change",
                                   title=f"{i['key'] or i['title'][:40]}: {k} {a or '(empty)'} → {b or '(empty)'}",
                                   detail=_server_label(i["server"]), url=i["url"], ref=i["path"]))
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

Answer with five fields:
- problem: at most 25 words on what is wrong or missing, to bring it back to mind.
- goal: at most 20 words on what "done" looks like, and why it matters.
- state: at most 40 words on where it stands: what has been done, and what was decided and why (a cause found, an
  approach chosen or dropped).
- next: at most 25 words on what needs to be done, and whether someone is still working on it; "Nothing" if it is
  done.
- question: at most 20 words on the question that the last session asked the developer and that is not answered;
  "None" if there is no such question.
State facts from the data above only. Name the real PRs, tickets, functions and errors from the data, but no local
folders or file paths: someone on another machine reads this too. Say how something was done or why it worked only
in the data's own words; if the data doesn't say, leave it out."""
CORE = ("problem", "goal", "state", "next", "question")
NOTE_SCHEMA = {"type": "object", "properties": {k: {"type": "string"} for k in CORE},
               "required": list(CORE), "additionalProperties": False}
NOTE_VERSION = 8  # a new version writes every note again; 7: the longer ending; 8: problem, goal, state, next


def core(note: dict) -> dict:
    """A note's core (problem, goal, state, next, question), from a note in the format of NOTE_VERSION 8 or older
    (recap, current, decisions, left). Empty fields and "None" answers are left out."""
    if not note:
        return {}
    if "problem" not in note:
        note = dict(problem=note.get("recap"), state=" ".join(x for x in (note.get("current"), note.get("decisions"))
                                                              if x and x.strip(" .").lower() != "none"),
                    next=note.get("left"), question=note.get("question"))
    return {k: note[k].strip() for k in CORE if isinstance(note.get(k), str) and note[k].strip(" .").lower() not in ("", "none")}
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
    return [t for t in pick if stale(t)]


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
    from pensieve import llm
    day = lambda ts: datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    events = "\n".join(f"- {day(e['ts'])} {e['source']}: {e['title'][:140]}" + (f" ({e['detail'][:60]})" if e.get("detail") else "")
                       for e in task["events"][:25])
    prs = "; ".join(f"{p['url']} ({p['state'] or 'state unknown'})" for p in task["prs"]) or "none"
    places = "; ".join(f"{p['path']}" + (f" on {p['branch']}, {p['changed']} uncommitted files" if p.get("branch") else "")
                       for p in task.get("places", [])) or "none"
    ans = llm.complete(NOTE_PROMPT.format(title=task["title"], keys=f" [{', '.join(task['keys'][:3])}]" if task["keys"] else "",
                                          status=f", ticket status {task['status']}" if task["status"] else "",
                                          prs=prs, places=places, events=events,
                                          last_words=task.get("ending") or task.get("last_words") or "(none)",
                                          last_title=(task["sessions"] or [{}])[0].get("title", "(none)"),
                                          last_summary=task.get("last_summary") or "",
                                          earlier="".join(f'An earlier session: "{title}"\n{summary}\n'
                                                          for title, summary in task.get("earlier", []) if summary)),
                       max_tokens=320, schema=NOTE_SCHEMA, kind="task note")
    cur = notes()
    cur[task["id"]] = dict({k: str(ans.get(k, "")).strip() for k in NOTE_SCHEMA["required"]},
                           sig=_signature(task), at=time.time(), v=NOTE_VERSION)
    path = _notes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cur))
    return cur[task["id"]]


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
