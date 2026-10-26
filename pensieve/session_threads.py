"""Threads per session and branch, built as the bench builds them (bench/graph_build.py, bench/graph_states.py).

A thread is one session's work on one branch. It holds only what that session wrote there: the sinks its own
transcript changes (pensieve/transcripts.py, write edges), and its lines that are uncommitted on the branch or not in
the remote's default branch yet. A session that did work on two branches has two threads. A session that writes
nothing holds no sinks, so it is a thought. The threads of one branch are its cluster.

A task (the app's grouping of sessions that share a branch, PR or ticket) is split into these threads, so no thread
takes state from a sink another session wrote."""
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


def headless(path: str) -> bool:
    from pensieve import transcripts
    return transcripts.headless(path)


def on_branch(t: dict, s: dict) -> list[tuple[str, str]]:
    """The feature branches a session did work on: lines it wrote with an edit tool are uncommitted in a checkout of
    the task on that branch, or are in the branch's commits that the remote's default branch does not have yet (by
    whoever committed them). One (branch, key) per such branch. Uncommitted lines count only where the branch is open
    in the checkout. A session needs MAIN_MATCH of its lines there, or all of them when it wrote fewer."""
    from pensieve import transcripts, work
    pairs = transcripts.written(s.get("path", ""))
    if not pairs:
        return []
    out = []
    known = {p.get("path") for p in t.get("places", [])}
    # a checkout that the session wrote to and the task does not list: a worktree that the session edited from
    # another folder
    extra = [dict(path=root, branch=branch, git=True, changed=len(work._git(root, "status", "--porcelain", ttl=30).splitlines()))
             for root, branch in written_roots(s).items() if root not in known]
    for p in t.get("places", []) + extra:
        root, branch = p.get("path", ""), p.get("branch", "")
        if not (p.get("git") and root and branch) or branch in work.DEFAULT_BRANCHES:
            continue
        prefix = root.rstrip("/") + "/"
        mine = [(f[len(prefix):], line) for f, line in pairs if f.startswith(prefix)]
        if not mine:
            continue
        need = min(MAIN_MATCH, len(mine))
        held = lambda added: sum(line in added.get(rel, ()) for rel, line in mine) >= need
        if (p.get("changed") and not p.get("head") and held(_added_lines(root))) \
                or ((base := _base(root)) and held(_added_lines(root, f"{base}...refs/heads/{branch}"))):
            slug = sink_status.github_repo(root)
            key = ("branch", f"{slug}@{branch}" if slug else branch)
            if key not in out:
                out.append(key)
    return out


def ran_on(s: dict) -> list[tuple[str, str]]:
    """The feature branch a session ran on without writing anything: a review, or a read of the work. It is on that
    branch's cluster with no share of its lines (claims.apply), so the branch's state is its state."""
    from pensieve import work
    b = s.get("branch") or ""
    return [("branch", b)] if b and b not in work.DEFAULT_BRANCHES and b != "HEAD" else []


SINK_WORKERS = 8  # threads that run the sessions' git calls in split()
MAIN_MATCH = 3  # lines of a session that must be in a default branch's diff: there, many sessions share one diff


def written_roots(s: dict) -> dict[str, str]:
    """{checkout: its current branch} of each checkout that a session wrote files in with its edit tools."""
    import os
    from pensieve import transcripts, work
    roots = {work._git_root(os.path.dirname(f)) for f, _ in transcripts.written(s.get("path", ""))}
    return {r: work._git(r, "branch", "--show-current", ttl=30) for r in sorted(roots) if r}


def _base(root: str) -> str:
    """The ref of a checkout's default branch, as stale._default_branch finds it, from git calls that work._git caches:
    a build asks it for every session on the checkout."""
    from pensieve import work
    head = work._git(root, "symbolic-ref", "-q", "refs/remotes/origin/HEAD", ttl=600)
    for ref in ([head] if head else []) + ["refs/heads/main", "refs/heads/master"]:
        if work._git(root, "rev-parse", "-q", "--verify", ref, ttl=600):
            return ref
    return ""


def on_default_branch(s: dict) -> list[tuple[str, str]]:
    """The default branch (main) of each checkout a session wrote lines to, as a branch sink keyed by the checkout
    ("/code/app@main"): two clones on main have their own changes. Its status for this session (sink_status.local,
    by session and sink: sessions on one main have their own lines) comes from where
    the session's lines are now: still uncommitted, or committed and not pushed, is OPEN; in the upstream is MERGED
    (landed); in none of them is DELETED (discarded). A session needs MAIN_MATCH of its lines, or all of them when it
    wrote fewer, in one place."""
    import os
    from collections import defaultdict
    from pensieve import transcripts, work
    by_root = defaultdict(list)
    for f, line in transcripts.written(s.get("path", "")):
        root = work._git_root(os.path.dirname(f))
        if root and f.startswith(root.rstrip("/") + "/"):
            by_root[root].append((f[len(root.rstrip("/")) + 1:], line))
    out = []
    for root, mine in by_root.items():
        branch = work._git(root, "branch", "--show-current", ttl=30)
        if not branch or branch not in work.DEFAULT_BRANCHES:
            continue  # a feature branch: on_branch() and the transcript's git calls cover it
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
            moved = _moved_to(root, files)
            if moved:
                MOVED[(s.get("id", ""), f"{root}@{branch}")] = moved
        elif upstream != "HEAD" and held(new(_added_lines(root, f"{upstream}..HEAD"), upstream)):
            status = "OPEN"  # committed, not pushed yet
        else:
            status = "MERGED" if held(_lines_at(root, upstream, files)) else "DELETED"
        key = f"{root}@{branch}"
        sink_status.local[(s.get("id", ""), f"branch {key}")] = status
        out.append(("branch", key))
    return out


MOVED: dict[tuple[str, str], dict] = {}  # (session id, "<checkout>@main") -> _moved_to's answer, from on_default_branch
MOVED_BRANCHES = 20  # the checkout's most recently changed local branches that _moved_to looks in


def _moved_to(root: str, files: set[str]) -> dict | None:
    """The local branch that already holds the uncommitted work of `files` on a default branch: every line added in
    them, by any session, is in that branch's commits that the remote's default branch lacks. Then the copy on main
    can go without losing anything. {branch, commit, files}, or None."""
    from pensieve import work
    base = _base(root)
    unc = _added_lines(root)
    want = {rel: unc[rel] for rel in files if unc.get(rel)}
    if not (base and want):
        return None
    heads = work._git(root, "for-each-ref", "--sort=-committerdate", f"--count={MOVED_BRANCHES}",
                      "--format=%(refname:short) %(objectname:short)", "refs/heads", ttl=30)
    for line in heads.splitlines():
        b, _, commit = line.partition(" ")
        if not b or b in work.DEFAULT_BRANCHES:
            continue
        added = _added_lines(root, f"{base}...refs/heads/{b}")
        if all(lines <= added.get(rel, set()) for rel, lines in want.items()):
            return dict(branch=b, commit=commit, files=sorted(want))
    return None


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


def branch_name(key: str) -> str:
    """A branch sink's name across checkouts: "org/app@feat" and "feat" are one branch, "feat". A default branch keeps
    its checkout ("/code/app@main"): main in two repos, or in two clones, is not one piece of work."""
    from pensieve import work
    short = key.split("@", 1)[-1]
    return key if short in work.DEFAULT_BRANCHES else short


def by_branch(t: dict, ws: list) -> list[tuple[str, list]]:
    """A session's writes, one group per branch it did work on: [(branch name, writes)]. A PR goes with its head
    branch, and with the first branch when its head is not one of them. With no branch, one group named ""."""
    names = list(dict.fromkeys(branch_name(k) for kind, k in ws if kind == "branch"))
    if not names:
        return [("", list(ws))]
    groups = {n: [] for n in names}
    heads = {sink_status.pr_key(p.get("url", "")): p.get("head", "") for p in t.get("prs", [])}
    for kind, k in ws:
        if kind == "branch":
            groups[branch_name(k)].append((kind, k))
            continue
        head = heads.get(k) or (sink_status.pr_info(k).get("head", "") if kind == "pr" else "")
        groups[head if head in groups else names[0]].append((kind, k))
    return list(groups.items())


def _thread(t: dict, s: dict, ws: list, facts: dict | None = None, name: str = "", place_of=None) -> dict:
    """The thread of one writing session `s` inside task `t` on one branch (`name`, from by_branch; "" for a session
    that did work on no branch): its sessions, PRs, branches, tickets and places only. Its text fields (where it
    stopped, its ending, its files) are the session's own, not the task's. `facts` are the session's text facts
    (work._facts), when the caller has them. `place_of(root, branch)` gives the place of a default branch's checkout
    that the task does not list (a session of other work that also wrote there)."""
    from pensieve import work
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
    have_b = {bkey(b) for b in branches} | {b["branch"] for b in branches}
    for k in sorted(branch_keys - have_b):  # a branch this session wrote that the task does not list
        repo, sep, bname = k.partition("@")
        branches.append(dict(repo=repo if sep else "", branch=bname if sep else k))
    text = " ".join([f"{p.get('title', '')} {p.get('head', '')}" for p in prs] + [b["branch"] for b in branches])
    tickets = [k for k in t.get("tickets", []) if k.get("key") and _mentions(text, k["key"])]
    sid = s["id"]
    root, _, short = name.rpartition("@")
    on_main = bool(root) and short in work.DEFAULT_BRANCHES
    if on_main:  # main keeps its checkout: the places of that checkout only
        places = [p for p in t.get("places", []) if p.get("path") == root and p.get("branch") == short]
        if not places and place_of:
            places = [p for p in [place_of(root, short)] if p]
    else:
        names = {b["branch"] for b in branches}
        places = [p for p in t.get("places", []) if p.get("branch") in names]
        have_p = {p.get("path") for p in places}
        if place_of and names:
            places += [p for r, b in written_roots(s).items() if b in names and r not in have_p
                       for p in [place_of(r, b)] if p]
    roots = [p["path"].rstrip("/") + "/" for p in places if p.get("path")]
    files = sorted(str(f) for f in (s.get("edited") or ()) if not roots or any(str(f).startswith(r) for r in roots))
    return {**t, "id": thread_id(f"{sid} {name}" if name else sid),
            "kind": "ticket" if tickets else "branch" if branches else "work",
            "title": s.get("title") or t.get("title", ""), "updated": s.get("updated") or t.get("updated", 0),
            "sessions": [x for x in t.get("sessions", []) if x["id"] == sid], "_sessions": [s],
            "_siblings": [dict(id=x["id"], title=x.get("title", ""), updated=x.get("updated") or 0)
                          for x in t.get("sessions", []) if x["id"] != sid],
            "_writes": list(ws), "_branch": name, "moved": MOVED.get((sid, name)) if on_main else None,
            "prs": prs, "branch_prs": [], "branches": branches, "tickets": tickets,
            "keys": [k["key"] for k in tickets], "ticket_refs": [], "_shared": [], "shared_sessions": [],
            "places": places,
            "seen": [], "seen_more": 0, "summary": (s.get("summary") or "")[:400],
            "last_words": facts.get("last", ""), "ending": facts.get("ending", ""),
            "last_summary": (s.get("summary") or "")[:800], "earlier": [],
            "files": files[:15] if files or name else t.get("files", []),
            "commits": t.get("commits", []) if branches and not on_main else [],
            "resume": work.resume_of(s) or t.get("resume"),
            "events": [e for e in t.get("events", []) if e.get("source") != "session" or e.get("ref") == sid]}


def thread_id(key: str) -> str:
    """A thread's id: of a session and the branch it did work on ("<session id> <branch name>"), or of the session
    alone when it did work on no branch."""
    return "th_" + hashlib.sha1(key.encode()).hexdigest()[:8]


def split(tasks: list[dict], facts: dict | None = None, place_of=None) -> list[dict]:
    """One thread per session and branch it did work on, in each task. Work on a branch is the session's lines that
    are uncommitted there or in commits the remote's default branch does not have yet, or the branch's git and gh
    writes in its transcript. A session that did work on two branches has two threads, one in each branch's cluster.
    A session that wrote nothing but ran on a feature branch is on that branch (ran_on), as a review (`review`). Any
    other session with no
    write has no sinks: its thread is a thought. A task with no session (a ticket of the user's that nothing picked
    up yet) is one thread of its own, a to-do. `facts`: {session id: text facts}. `place_of`: see _thread. A thread
    on main whose uncommitted work is on another branch too has `moved` (_moved_to)."""
    from concurrent.futures import ThreadPoolExecutor
    out = []
    MOVED.clear()

    def sinks(ts):
        t, s = ts
        ws = writes(s.get("path", ""))
        return ws + [w for w in on_branch(t, s) + on_default_branch(s) if w not in ws]
    # each session's sinks come from git calls: run them side by side (git runs outside the GIL)
    pairs = [(t, s) for t in tasks for s in t.get("_sessions", [])]
    with ThreadPoolExecutor(SINK_WORKERS) as pool:
        found = dict(zip(((id(t), s["id"]) for t, s in pairs), pool.map(sinks, pairs)))
    for t in tasks:
        if not t.get("_sessions"):
            out.append(todo_thread(t))
            continue
        for s in t.get("_sessions", []):
            ws = found[(id(t), s["id"])]
            if not ws and not s.get("edited") and headless(s.get("path", "")):
                continue  # a `claude -p` run or an SDK app that changed nothing: no thread, not even a thought
            review = not ws and bool(ran_on(s))
            if review:
                ws = ran_on(s)
            for name, part in by_branch(t, ws):
                out.append({**_thread(t, s, part, (facts or {}).get(s["id"]), name, place_of), "review": review})
    return out


def todo_thread(t: dict) -> dict:
    """The thread of a task with no session: its tickets and PRs, nothing a session would bring."""
    return {**t, "sessions": [], "_sessions": [], "_writes": [], "_shared": [], "shared_sessions": [], "seen": [],
            "seen_more": 0, "summary": "", "last_words": "", "ending": "", "last_summary": "", "earlier": [],
            "files": [], "kind": "ticket" if t.get("tickets") else t.get("kind", "work")}
