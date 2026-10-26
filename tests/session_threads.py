"""Tests of the threads per session (pensieve/session_threads.py): a task splits into one thread per writing session,
and each thread holds only what its session wrote. Transcripts and gh are stubbed.
Run: python tests/session_threads.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import session_threads as st, sink_status  # noqa: E402

PR_A = "https://github.com/org/app/pull/1"
PR_B = "https://github.com/org/app/pull/2"


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))
    return ok


def run():
    results = []
    task = dict(id="t", title="shared", updated=100, prs=[dict(url=PR_A, state="merged", head="feat-a", title="A")],
                branches=[dict(repo="/code/app", branch="feat-a")], places=[dict(branch="feat-a")],
                tickets=[dict(key="DP-9", status="In Progress")], branch_prs=[], events=[],
                sessions=[dict(id="s1", title="one", updated=50), dict(id="s2", title="two", updated=90),
                          dict(id="s3", title="thought", updated=10)],
                _sessions=[dict(id="s1", title="one", updated=50, path="/x/s1.jsonl"),
                           dict(id="s2", title="two", updated=90, path="/x/s2.jsonl"),
                           dict(id="s3", title="thought", updated=10, path="/x/s3.jsonl")])
    writes = {"/x/s1.jsonl": [("pr", "org/app#1"), ("branch", "org/app@feat-a")],
              "/x/s2.jsonl": [("pr", "org/app#2")]}
    st.writes = lambda path: writes.get(path, [])
    sink_status.pr_info = lambda key: {"state": "OPEN", "head": "feat-b"} if key == "org/app#2" else {}

    out = st.split([task])
    results.append(check("a session with no write is a thread with no sinks", sorted(t["sessions"][0]["id"] for t in out),
                         ["s1", "s2", "s3"]))
    thought = next(t for t in out if t["sessions"][0]["id"] == "s3")
    results.append(check("the thought holds no PR, branch or ticket", (thought["prs"], thought["branches"], thought["tickets"]),
                         ([], [], [])))
    one = next(t for t in out if t["sessions"][0]["id"] == "s1")
    two = next(t for t in out if t["sessions"][0]["id"] == "s2")
    results.append(check("s1 holds its own PR and branch", ([p["url"] for p in one["prs"]], [b["branch"] for b in one["branches"]]),
                         ([PR_A], ["feat-a"])))
    results.append(check("s2 holds its own PR, from gh, not the task's other PR",
                         ([(p["url"], p["state"]) for p in two["prs"]]), [("https://github.com/org/app/pull/2", "open")]))
    results.append(check("threads have distinct ids", len({t["id"] for t in out}), 3))
    results.append(check("each thread's updated time is its session's", (one["updated"], two["updated"]), (50, 90)))

    # each thread's text and files are its own session's, not the task's; a thought holds no commits
    task["commits"] = [dict(sha="abc", subject="x")]
    task["files"] = ["/code/app/task.py"]
    task["_sessions"][0]["edited"] = {"/code/app/one.py"}
    out = st.split([task], facts={"s1": dict(last="one stopped here", ending="one's ending")})
    one = next(t for t in out if t["sessions"][0]["id"] == "s1")
    thought = next(t for t in out if t["sessions"][0]["id"] == "s3")
    results.append(check("a thread's last words and ending are its session's", (one["last_words"], one["ending"]),
                         ("one stopped here", "one's ending")))
    results.append(check("a session with no facts has no last words", thought["last_words"], ""))
    results.append(check("a thread's files are what its session edited", one["files"], ["/code/app/one.py"]))
    results.append(check("a thread with a branch keeps the commits; a thought has none",
                         (len(one["commits"]), thought["commits"]), (1, [])))
    results.append(check("a thread knows the task's other sessions", sorted(s["id"] for s in one["_siblings"]), ["s2", "s3"]))
    results.append(check("a thread keeps what its session wrote", one["_writes"], writes["/x/s1.jsonl"]))

    # a session's edits uncommitted in the task's checkout, or in the branch's commits not yet in the remote's main:
    # work on that branch
    from pensieve import transcripts, work
    ROOT = "/code/app"
    transcripts.written = lambda path: ((f"{ROOT}/x.py", "a line the session wrote"),) if path == "/x/s4.jsonl" else ()
    line_diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+a line the session wrote\n-old"
    st._base = lambda root: "refs/remotes/origin/main"
    gits = {("diff", "HEAD", "--unified=0", "--no-color"): line_diff}
    work._git = lambda root, *args, ttl=60: gits.get(args, "")
    place = dict(path=ROOT, branch="feat", git=True, changed=1)
    s4 = dict(path="/x/s4.jsonl", updated=200)
    results.append(check("edits in the uncommitted diff are work on the branch",
                         st.on_branch(dict(places=[place]), s4), [("branch", "feat")]))
    results.append(check("uncommitted edits don't count when another branch is open in the checkout",
                         st.on_branch(dict(places=[dict(place, head="main")]), s4), []))
    results.append(check("no work when the session's lines are not in the diff",
                         st.on_branch(dict(places=[place]), dict(path="/x/s5.jsonl", updated=200)), []))
    gits = {("diff", "refs/remotes/origin/main...refs/heads/feat", "--unified=0", "--no-color"): line_diff}
    results.append(check("lines in the branch's commits that main lacks are work on it, by whoever committed them",
                         st.on_branch(dict(places=[dict(place, changed=0, head="main")]), s4), [("branch", "feat")]))
    gits = {}
    results.append(check("lines that reached main are not work on the branch any more",
                         st.on_branch(dict(places=[dict(place, changed=0)]), s4), []))
    git_root = work._git_root
    work._git_root = lambda folder: ROOT if folder.startswith(ROOT) else ""
    gits = {("diff", "HEAD", "--unified=0", "--no-color"): line_diff, ("branch", "--show-current"): "feat",
            ("status", "--porcelain"): " M x.py"}
    results.append(check("edits in a checkout that the task does not list (a worktree) are work on its branch",
                         st.on_branch(dict(places=[]), s4), [("branch", "feat")]))
    work._git_root, gits = git_root, {}
    results.append(check("a session that wrote nothing on a feature branch ran on it",
                         (st.ran_on(dict(branch="feat")), st.ran_on(dict(branch="main"))), ([("branch", "feat")], [])))

    # a session that did work on two branches: one thread on each, with that branch's sinks, places and files
    sink_status.pr_info = lambda key: {"state": "OPEN", "head": "feat-b"} if key == "org/app#2" else {}
    st.writes = lambda path: [("branch", "org/app@feat-a"), ("branch", "/code/app@main"), ("pr", "org/app#1")]
    two = dict(task, _sessions=[dict(id="s9", title="two jobs", updated=95, path="/x/s9.jsonl",
                                     edited={"/code/app/a.py", "/code/other/b.py"})],
               sessions=[dict(id="s9", title="two jobs", updated=95)],
               places=[dict(path="/code/feat-a", branch="feat-a", git=True)])
    work.resume_of = lambda s: None
    got = st.split([two], place_of=lambda root, branch: dict(path=root, branch=branch, git=True))
    results.append(check("two branches, two threads", sorted(t["_branch"] for t in got), ["/code/app@main", "feat-a"]))
    results.append(check("each thread's id is of its session and branch",
                         sorted(t["id"] for t in got), sorted([st.thread_id("s9 feat-a"), st.thread_id("s9 /code/app@main")])))
    a = next(t for t in got if t["_branch"] == "feat-a")
    m = next(t for t in got if t["_branch"] == "/code/app@main")
    results.append(check("the PR goes with its head branch", ([p["url"] for p in a["prs"]], m["prs"]), ([PR_A], [])))
    results.append(check("main's thread has its checkout's place, from place_of",
                         [p["path"] for p in m["places"]], ["/code/app"]))
    results.append(check("main's thread lists only the files in its checkout", m["files"], ["/code/app/a.py"]))
    st.writes = lambda path: []
    thought = st.split([dict(two, places=[])])
    results.append(check("a session with no work on a branch keeps its own id", [t["id"] for t in thought], [st.thread_id("s9")]))
    seen = st.split([dict(two, places=[], _sessions=[dict(two["_sessions"][0], branch="feat-a")])])
    results.append(check("a session that only ran on a feature branch is a review of it, and one that wrote is not",
                         ([(t["_branch"], t["review"]) for t in seen], a["review"]), ([("feat-a", True)], False)))
    st.headless = lambda path: path == "/x/s9.jsonl"
    quiet = dict(two["_sessions"][0], edited=set())
    results.append(check("a headless session that changed nothing has no thread; one that edited files, or a typed "
                         "session, has one",
                         (st.split([dict(two, places=[], _sessions=[quiet])]),
                          len(st.split([dict(two, places=[])])),
                          len(st.split([dict(two, places=[], _sessions=[dict(quiet, path="/x/s10.jsonl")])]))),
                         ([], 1, 1)))
    st.headless = lambda path: False
    st.writes = lambda path: writes.get(path, [])

    # main: a sink of the checkout, open while the session's lines are uncommitted or unpushed, then landed or gone
    lines = [f"line {i} that the session wrote on main" for i in range(4)]
    transcripts.written = lambda path: tuple((f"{ROOT}/m.py", ln) for ln in lines) + ((f"{ROOT}/new.py", "a brand new file's first line"),)
    work._git_root = lambda folder: ROOT
    diff = lambda ls: "+++ b/m.py\n" + "".join(f"+{ln}\n" for ln in ls)

    def main_status(git, untracked=None):
        sink_status.local.clear()
        st._file_lines = lambda root, rel: (untracked or {}).get(rel, set())
        work._git = lambda root, *args, ttl=60: {("branch", "--show-current"): "main",
                                                 ("rev-parse", "--abbrev-ref", "@{u}"): "origin/main", **git}.get(args, "")
        ws = st.on_default_branch(dict(id="m", path="/x/m.jsonl"))
        return ws, sink_status.local.get(("m", f"branch {ROOT}@main"))

    no_diff = ("diff", "HEAD", "--unified=0", "--no-color")
    ahead = ("diff", "@{u}..HEAD", "--unified=0", "--no-color")
    results.append(check("lines uncommitted on main: an open sink keyed by the checkout",
                         main_status({no_diff: diff(lines)}), ([("branch", f"{ROOT}@main")], "OPEN")))
    results.append(check("fewer than MAIN_MATCH of its lines in a shared diff is not its work",
                         main_status({no_diff: diff(lines[:2])})[1], "DELETED"))
    results.append(check("lines the file already had (copied boilerplate) are not its work",
                         main_status({no_diff: diff(lines), ("show", "HEAD:m.py"): "\n".join(lines)})[1], "DELETED"))
    results.append(check("committed and not pushed: still open", main_status({ahead: diff(lines)})[1], "OPEN"))
    results.append(check("in the upstream: landed", main_status({("show", "@{u}:m.py"): "\n".join(lines)})[1], "MERGED"))
    results.append(check("in none of them: discarded", main_status({})[1], "DELETED"))
    transcripts.written = lambda path: ((f"{ROOT}/new.py", "a brand new file's first line"),)
    results.append(check("a new file it created, untracked, counts as uncommitted",
                         main_status({("ls-files", "--others", "--exclude-standard"): "new.py"},
                                     {"new.py": {"a brand new file's first line"}})[1], "OPEN"))
    work._git = lambda root, *args, ttl=60: {("branch", "--show-current"): "feat"}.get(args, "")
    results.append(check("a feature branch is not a main sink", st.on_default_branch(dict(path="/x/m.jsonl")), []))

    # two sessions on one main: each thread's state comes from its own lines, not the other's
    from pensieve import thread_model as tm
    sink_status.local.clear()
    label = f"branch {ROOT}@main"
    sink_status.local.update({("a", label): "OPEN", ("b", label): "MERGED"})
    sink = tm.Sink(tm.SinkKind.BRANCH, f"{ROOT}@main")
    of = lambda sid: sink_status.status_of_task(dict(_sessions=[dict(id=sid)], places=[dict(branch="main", commits=3)]), [sink])
    results.append(check("each session on main has its own status, and main's commits don't mark it merged",
                         (of("a")[label], of("b")[label]), ("OPEN", "MERGED")))
    feat = tm.Sink(tm.SinkKind.BRANCH, "org/app@feat")
    import time
    cache = {"branch org/app@feat": dict(value="DELETED", at=time.time(), pushed="")}
    asked = []
    sink_status._load, sink_status._save = lambda: cache, lambda c: None
    sink_status.branch_status = lambda repo, branch, env=None: asked.append(branch) or "EXISTS"
    work._net["off"] = False
    after = lambda sha: sink_status.statuses_for([feat], "c", {"branch org/app@feat": sha})["branch org/app@feat"]
    results.append(check("a cached state from before a push is asked again, and one from the same push is kept",
                         (after(""), after("abc123"), after("abc123"), asked), ("DELETED", "EXISTS", "EXISTS", ["feat"])))
    sink_status.statuses_for = lambda sinks, session="", pushed=None: {"branch org/app@feat": "DELETED"}
    gone = lambda **p: sink_status.status_of_task(dict(_sessions=[dict(id="c")], places=[dict(branch="feat", **p)]),
                                                  [feat])["branch org/app@feat"]
    work._git = lambda root, *args, ttl=60: {"/pushed": "refs/heads/feat", "/cut": "refs/heads/main"}.get(root, "") \
        if args[0] == "config" else ""
    results.append(check("a branch never pushed is open while its checkout has uncommitted files or commits main lacks",
                         (gone(changed=2), gone(commits=1, unmerged=1), gone(changed=2, path="/cut"),
                          gone(changed=2, head="main"), gone(changed=2, path="/pushed")),
                         ("OPEN", "OPEN", "OPEN", "DELETED", "DELETED")))

    from pensieve import cardtext
    added_lines, base = st._added_lines, st._base
    st._base = lambda root: "refs/remotes/origin/main"
    diffs = {"": {"a.css": {"x {}", "y {}"}, "b.js": {"go()"}},
             "refs/remotes/origin/main...refs/heads/feat": {"a.css": {"x {}", "y {}", "z {}"}, "b.js": {"go()"}},
             "refs/remotes/origin/main...refs/heads/half": {"a.css": {"x {}"}}}
    st._added_lines = lambda root, between="": diffs.get(between, {})
    work._git = lambda root, *args, ttl=60: "half 1111111\nmain 2222222\nfeat abc1234" if args[0] == "for-each-ref" else ""
    results.append(check("uncommitted work on main is moved to the branch that holds every line of it, not one with some",
                         (st._moved_to("/code/app", {"a.css", "b.js"}), st._moved_to("/code/app", {"c.py"})),
                         (dict(branch="feat", commit="abc1234", files=["a.css", "b.js"]), None)))
    mv = dict(branch="feat", commit="abc1234", files=["a.css", "b.js"])
    main_card = dict(moved=mv, places=[dict(branch="main", changed=2, git=True)], branches=[dict(branch="main")],
                     prs=[], tickets=[])
    results.append(check("a moved thread's Discard drops the copy on main, and its Ship points to the branch",
                         ([s["text"] for s in cardtext.discard(main_card)],
                          [p.text for p in cardtext.pending(main_card)][:1]),
                         (["Discard the uncommitted changes in a.css, b.js on main: every line is also on feat (abc1234)"],
                          ["Ship feat: this work is on it (abc1234), so it needs no commit on main"])))
    st._added_lines, st._base = added_lines, base

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
