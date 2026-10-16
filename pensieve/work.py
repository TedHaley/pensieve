"""The Data tab: what belongs to one piece of work, across sources.

A task joins agent sessions, git branches, commits and connection items (tickets, threads) by these links:
- a ticket key such as ABC-123 in a branch name, a commit subject, a session's text or a connection item;
- the git branch a session ran on (feature branches only: a default or long-lived branch holds many tasks);
- for work without a ticket: sessions in the same repo, a few days apart, that edited the same specific files.

`adjacent` finds what coworkers did near a task: their recent commits on remote branches in the same folders, and
tickets and threads about the same subject."""
import json
import re
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pensieve import connections, repos as repos_mod

KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d{1,6})\b")
DEFAULT_BRANCHES = {"", "HEAD", "main", "master", "develop", "dev", "trunk"}
SHARED_FILE_MAX = 3      # a file edited by more sessions than this is shared ground, not one task's
SAME_TASK_DAYS = 3       # sessions this far apart can still be one task through their files
LONG_BRANCH_SESSIONS, LONG_BRANCH_DAYS = 6, 4  # more sessions than this, or a longer span: a long-lived branch
_TEMP = ("/private/tmp/", "/tmp/", "/private/var/folders/", "/var/folders/")
_branch_cache = {}       # transcript path -> (mtime, branch)
_roots = {}              # folder -> its git root, or ""
_cache = {}


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
            out.append(dict(path=str(md), server=d.name, key=key if KEY.fullmatch(key) else "",
                            title=text.split("\n", 1)[0].lstrip("# ").strip(), url=url,
                            status=connections._render(raw.get("status")) if raw.get("status") else "",
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


def tasks(store, days: int = 30) -> list[dict]:
    """The tasks of the last `days` days, newest first. Cached for a minute."""
    hit = _cache.get(days)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    out = _tasks(store, days)
    _cache[days] = (time.time(), out)
    return out


def _tasks(store, days):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    with store.lock:
        db = store.db
        sess = db.execute("SELECT id, project, title, summary, updated, started, path FROM sessions WHERE updated > ? "
                          "AND n_chunks > 0 AND path NOT LIKE '%.md' ORDER BY updated DESC", (since,)).fetchall()
        ids = [s[0] for s in sess]
        texts = defaultdict(list)
        for sid, t in db.execute(f"SELECT session_id, text FROM chunks WHERE session_id IN "
                                 f"({','.join('?' * len(ids))})", ids).fetchall() if ids else []:
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
        repo, wt_branch = repo_of(project or "")
        branch = wt_branch or (_branch(path) if sid.startswith("claude:") else "")
        S[sid] = dict(id=sid, title=title or "", summary=summary or "", updated=_ts(updated), started=_ts(started),
                      project=project or "", repo=repo, branch=branch, edited=edits.get(sid, set()))
        prefixes |= {m.group(1) for m in KEY.finditer(branch)}

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
    # ticket keys a session talks about: all of them if few, else only the ones in its title or branch
    for s in S.values():
        found = Counter(keys_in("\n".join(texts.get(s["id"], [])) + "\n" + s["title"]))
        strong = set(keys_in(s["title"] + " " + s["branch"]))
        pick = set(found) if len(found) <= 3 else strong or {found.most_common(1)[0][0]}
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
        if not ss and not tickets:
            continue  # a message or page on its own is not a task
        keys = sorted({n[2:] for n in nodes if n.startswith("t:")} | {i["key"] for i in tickets})
        cs = sorted((C[n] for n in nodes if n.startswith("c:")), key=lambda c: -c["ts"])
        brs = sorted({n[2:] for n in nodes if n.startswith("b:")})
        files = Counter(p for s in ss for p in s["edited"])
        updated = max([s["updated"] for s in ss] + [i["updated"] for i in its] + [c["ts"] for c in cs] + [0])
        main = max(tickets, key=lambda i: i["updated"]) if tickets else None
        title = main["title"] if main else (brs[0].split("@", 1)[1] if brs else ss[0]["title"])
        out.append(dict(
            events=_events(ss, its, cs),
            id=_task_id(keys, brs, ss), title=title, kind="ticket" if keys else "branch" if brs else "work",
            keys=keys, status=main["status"] if main else "", updated=updated,
            repos=sorted({s["repo"] for s in ss if s["repo"]} | {c["repo"] for c in cs}),
            branches=[dict(repo=b.split("@", 1)[0], branch=b.split("@", 1)[1]) for b in brs],
            tickets=[{k: i[k] for k in ("key", "title", "status", "assignee", "url", "server", "path", "updated")}
                     for i in sorted(tickets, key=lambda i: -i["updated"])],
            items=[{k: i[k] for k in ("title", "url", "server", "path", "updated")}
                   for i in sorted(its, key=lambda i: -i["updated"]) if not i["key"]][:30],
            sessions=[dict(id=s["id"], title=s["title"], updated=s["updated"], branch=s["branch"], repo=s["repo"])
                      for s in ss],
            commits=cs[:30], files=[p for p, _ in files.most_common(15)],
            people=sorted({c["author"] for c in cs} | {t["assignee"] for t in tickets if t["assignee"]})))
    out.sort(key=lambda t: -t["updated"])
    return out


def _events(ss, its, cs) -> list[dict]:
    """The task's data points from every source, newest first: sessions, commits, connection items, and the field
    changes that connections recorded (a ticket's status going from To Do to In Progress)."""
    ev = [dict(ts=s["updated"], source="session", title=s["title"] or "Agent session", detail=s["branch"], ref=s["id"])
          for s in ss]
    ev += [dict(ts=c["ts"], source="commit", title=c["subject"], detail=f"{c['author']} · {c['sha']}", ref=c["sha"])
           for c in cs]
    for i in its:
        src = "ticket" if i["key"] else "message"
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


NOTE_PROMPT = """You write a short note for one task on a developer's work dashboard.
Task: {title}{keys}{status}
Its data points, newest first (date, source, what happened):
{events}

The developer's other current tasks:
{others}

In 2 or 3 short sentences, say where this task stands, how it has changed over time, and how it relates to the
other tasks (name a related task if there is one). State facts from the data points only. No preamble."""


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
    return hashlib.sha1(json.dumps([(e["ts"], e["title"]) for e in task["events"][:30]]).encode()).hexdigest()[:16]


def stale_notes(ts: list[dict], limit: int = 10) -> list[dict]:
    """The newest tasks whose note is missing or older than their data points."""
    have = notes()
    return [t for t in ts[:limit] if have.get(t["id"], {}).get("sig") != _signature(t)]


def write_note(task: dict, all_tasks: list[dict]) -> str:
    """Ask the insights engine for the task's note, and keep it with the signature of the data it saw."""
    from pensieve import llm
    day = lambda ts: datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    events = "\n".join(f"- {day(e['ts'])} {e['source']}: {e['title'][:140]}" + (f" ({e['detail'][:60]})" if e.get("detail") else "")
                       for e in task["events"][:25])
    others = "\n".join(f"- {t['title'][:100]}" + (f" [{', '.join(t['keys'][:2])}]" if t["keys"] else "")
                       for t in all_tasks[:15] if t["id"] != task["id"]) or "- none"
    text = llm.complete(NOTE_PROMPT.format(title=task["title"], keys=f" [{', '.join(task['keys'][:3])}]" if task["keys"] else "",
                                           status=f", status {task['status']}" if task["status"] else "",
                                           events=events, others=others), max_tokens=220).strip()
    cur = notes()
    cur[task["id"]] = dict(text=text, sig=_signature(task), at=time.time())
    path = _notes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cur))
    return text


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
