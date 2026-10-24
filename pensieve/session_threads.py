"""Threads per session, built as the bench builds them (bench/graph_build.py, bench/graph_states.py).

Each session is one thread. The thread holds only what that session wrote: the sinks its own transcript changes
(pensieve/transcripts.py, write edges). A session that writes nothing holds no sinks, so it is a thought. Several
threads can hold the same sink.

A task (the app's grouping of sessions that share a branch, PR or ticket) is split into one thread per session, so no
thread takes state from a sink another session wrote."""
import hashlib

from pensieve import sink_status
from pensieve.chains import _mentions


def writes(path: str) -> list[tuple[str, str]]:
    """The (kind, key) sinks a transcript writes to: branches and PRs, from its git and gh calls
    (pensieve/transcripts.py)."""
    from pensieve import transcripts
    return transcripts.writes(path)


def evidence(path: str) -> dict[tuple[str, str], list[str]]:
    """What a transcript did to each sink it wrote to: {(kind, key): ["git commit", "git push x", ...]}, in order."""
    from pensieve import transcripts
    return transcripts.evidence(path)


def uncommitted(t: dict, s: dict) -> list[tuple[str, str]]:
    """The branch a session wrote to without a commit: lines it wrote with an edit tool are in the uncommitted diff
    of a checkout of the task on that branch. One (branch, key) per such place; nothing when another branch is open
    there. A session whose edits were committed since (by it or by another) has no line left in the diff."""
    from pensieve import transcripts, work
    pairs = transcripts.written(s.get("path", ""))
    if not pairs:
        return []
    out = []
    for p in t.get("places", []):
        root, branch = p.get("path", ""), p.get("branch", "")
        if not (p.get("git") and root and branch and p.get("changed")) or p.get("head") or branch in work.DEFAULT_BRANCHES:
            continue
        last_commit = work._git(root, "log", "-1", "--format=%ct", ttl=30)
        if last_commit.isdigit() and (s.get("updated") or 0) < int(last_commit):
            continue  # the session ended before the last commit: what it wrote is committed, or was dropped
        prefix = root.rstrip("/") + "/"
        mine = [(f[len(prefix):], line) for f, line in pairs if f.startswith(prefix)]
        added = _added_lines(root)
        if any(line in added.get(rel, ()) for rel, line in mine):
            slug = sink_status.github_repo(root)
            out.append(("branch", f"{slug}@{branch}" if slug else branch))
    return out


MAIN_MATCH = 3  # lines of a session that must be in a default branch's diff: there, many sessions share one diff


def on_default_branch(s: dict) -> list[tuple[str, str]]:
    """The default branch (main) of each checkout a session wrote lines to, as a branch sink keyed by the checkout
    ("/code/app@main"): two clones on main have their own changes. Its status for this session (sink_status.local,
    by session and sink: sessions on one main have their own lines) comes from where
    the session's lines are now: still uncommitted, or committed and not pushed, is OPEN; in the upstream is MERGED
    (landed); in none of them is DELETED (discarded). A session needs MAIN_MATCH of its lines, or all of them when it
    wrote fewer, in one place."""
    from collections import defaultdict
    from pathlib import Path
    from pensieve import transcripts, work
    by_root = defaultdict(list)
    for f, line in transcripts.written(s.get("path", "")):
        root = work._git_root(str(Path(f).parent))
        if root and f.startswith(root.rstrip("/") + "/"):
            by_root[root].append((f[len(root.rstrip("/")) + 1:], line))
    out = []
    for root, mine in by_root.items():
        branch = work._git(root, "branch", "--show-current", ttl=30)
        if not branch or branch not in work.DEFAULT_BRANCHES:
            continue  # a feature branch: uncommitted() and the transcript's git calls cover it
        need = min(MAIN_MATCH, len(mine))
        files = {rel for rel, _ in mine}
        held = lambda lines: sum(line in lines.get(rel, ()) for rel, line in mine) >= need
        # a diff's line counts only when the file didn't have it before: a copy of a line already there (boilerplate)
        # is in every session's writes
        new = lambda added, base: {rel: ls - before.get(rel, set()) for rel, ls in added.items()
                                   for before in [_lines_at(root, base, files & {rel})]}
        upstream = "@{u}" if work._git(root, "rev-parse", "--abbrev-ref", "@{u}", ttl=60) else "HEAD"
        if held(new(_added_lines(root), "HEAD")):
            status = "OPEN"
        elif upstream != "HEAD" and held(new(_added_lines(root, f"{upstream}..HEAD"), upstream)):
            status = "OPEN"  # committed, not pushed yet
        else:
            status = "MERGED" if held(_lines_at(root, upstream, files)) else "DELETED"
        key = f"{root}@{branch}"
        sink_status.local[(s.get("id", ""), f"branch {key}")] = status
        out.append(("branch", key))
    return out


def _added_lines(root: str, between: str = "") -> dict[str, set]:
    """{file: the lines added} of a checkout, from one `git diff` per build (work._git caches it for a while), not one
    per file and session: the uncommitted diff with the untracked files whole, or with `between` ("@{u}..HEAD") the
    diff of those commits."""
    from pensieve import work
    out: dict[str, set] = {}
    rel = ""
    for ln in work._git(root, "diff", between or "HEAD", "--unified=0", "--no-color", ttl=30).splitlines():
        if ln.startswith("+++ "):
            rel = ln[4:].removeprefix("b/").strip()
        elif ln.startswith("+") and rel:
            out.setdefault(rel, set()).add(ln[1:].strip())
    if not between:  # a new file is not in the diff until it is added
        for rel in work._git(root, "ls-files", "--others", "--exclude-standard", ttl=30).splitlines()[:200]:
            out.setdefault(rel, set()).update(_file_lines(root, rel))
    return out


def _file_lines(root: str, rel: str) -> set[str]:
    from pathlib import Path
    p = Path(root) / rel
    try:
        if p.stat().st_size > 1_000_000:
            return set()
        return {ln.strip() for ln in p.read_text(errors="ignore").splitlines()}
    except OSError:
        return set()


def _lines_at(root: str, rev: str, files: set[str]) -> dict[str, set]:
    """{file: its lines} at a commit, for the files named."""
    from pensieve import work
    return {rel: {ln.strip() for ln in work._git(root, "show", f"{rev}:{rel}", ttl=60).splitlines()} for rel in files}


def _thread(t: dict, s: dict, ws: list, facts: dict | None = None) -> dict:
    """The thread of one writing session `s` inside task `t`: its sessions, PRs, branches, tickets and places only.
    Its text fields (where it stopped, its ending, its files) are the session's own, not the task's. `facts` are the
    session's text facts (work._facts), when the caller has them."""
    pr_keys = {k for kind, k in ws if kind == "pr"}
    branch_keys = {k for kind, k in ws if kind == "branch"}
    facts = facts or {}

    def bkey(b: dict) -> str:
        slug = sink_status.github_repo(b.get("repo", ""))
        return f"{slug}@{b['branch']}" if slug else b["branch"]

    prs = [p for p in t.get("prs", []) if sink_status.pr_key(p.get("url", "")) in pr_keys]
    have = {sink_status.pr_key(p.get("url", "")) for p in prs}
    for k in sorted(pr_keys - have):  # a PR this session wrote that the task does not list: its state from gh
        info = sink_status.pr_info(k)
        org, _, num = k.partition("#")
        prs.append(dict(url=f"https://github.com/{org}/pull/{num}", state=info.get("state", "").lower(),
                        head=info.get("head", ""), title="", own=True))
    branches = [b for b in t.get("branches", []) if bkey(b) in branch_keys or b["branch"] in branch_keys]
    have_b = {bkey(b) for b in branches}
    for k in sorted(branch_keys - have_b):  # a branch this session wrote that the task does not list
        repo, sep, name = k.partition("@")
        branches.append(dict(repo=repo if sep else "", branch=name if sep else k))
    text = " ".join([f"{p.get('title', '')} {p.get('head', '')}" for p in prs] + [b["branch"] for b in branches])
    tickets = [k for k in t.get("tickets", []) if k.get("key") and _mentions(text, k["key"])]
    names = {b["branch"] for b in branches}
    sid = s["id"]
    files = sorted(str(p) for p in (s.get("edited") or ()))
    return {**t, "id": thread_id(sid),
            "kind": "ticket" if tickets else "branch" if branches else "work",
            "title": s.get("title") or t.get("title", ""), "updated": s.get("updated") or t.get("updated", 0),
            "sessions": [x for x in t.get("sessions", []) if x["id"] == sid], "_sessions": [s],
            "_siblings": [dict(id=x["id"], title=x.get("title", ""), updated=x.get("updated") or 0)
                          for x in t.get("sessions", []) if x["id"] != sid],
            "_writes": list(ws),
            "prs": prs, "branch_prs": [], "branches": branches, "tickets": tickets,
            "keys": [k["key"] for k in tickets], "ticket_refs": [], "_shared": [], "shared_sessions": [],
            "places": [p for p in t.get("places", []) if p.get("branch") in names],
            "seen": [], "seen_more": 0, "summary": (s.get("summary") or "")[:400],
            "last_words": facts.get("last", ""), "ending": facts.get("ending", ""),
            "last_summary": (s.get("summary") or "")[:800], "earlier": [],
            "files": files[:15] if files else t.get("files", []),
            "commits": t.get("commits", []) if branches else [],
            "events": [e for e in t.get("events", []) if e.get("source") != "session" or e.get("ref") == sid]}


def thread_id(sid: str) -> str:
    return "th_" + hashlib.sha1(sid.encode()).hexdigest()[:8]


def split(tasks: list[dict], facts: dict | None = None) -> list[dict]:
    """One thread per session of each task. A session with no write has no sinks: its thread is a thought. A session
    that edited files still uncommitted on the task's branch wrote to that branch. A task with no session (a ticket
    of the user's that nothing picked up yet) is one thread of its own, a to-do. `facts`: {session id: text facts}."""
    out = []
    for t in tasks:
        if not t.get("_sessions"):
            out.append(todo_thread(t))
            continue
        for s in t.get("_sessions", []):
            ws = writes(s.get("path", ""))
            ws += [w for w in uncommitted(t, s) + on_default_branch(s) if w not in ws]
            out.append(_thread(t, s, ws, (facts or {}).get(s["id"])))
    return out


def todo_thread(t: dict) -> dict:
    """The thread of a task with no session: its tickets and PRs, nothing a session would bring."""
    return {**t, "sessions": [], "_sessions": [], "_writes": [], "_shared": [], "shared_sessions": [], "seen": [],
            "seen_more": 0, "summary": "", "last_words": "", "ending": "", "last_summary": "", "earlier": [],
            "files": [], "kind": "ticket" if t.get("tickets") else t.get("kind", "work")}
