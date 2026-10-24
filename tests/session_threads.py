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

    # a session's edits still in the uncommitted diff of the task's checkout: a write to that branch
    from pensieve import transcripts, work
    ROOT = "/code/app"
    transcripts.written = lambda path: ((f"{ROOT}/x.py", "a line the session wrote"),) if path == "/x/s4.jsonl" else ()
    gits = {("log", "-1", "--format=%ct"): "100",
            ("diff", "HEAD", "--unified=0", "--no-color"): "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+a line the session wrote\n-old"}
    work._git = lambda root, *args, ttl=60: gits.get(args, "")
    place = dict(path=ROOT, branch="feat", git=True, changed=1)
    results.append(check("edits in the uncommitted diff write the branch",
                         st.uncommitted(dict(places=[place]), dict(path="/x/s4.jsonl", updated=200)), [("branch", "feat")]))
    results.append(check("a session that ended before the last commit wrote nothing uncommitted",
                         st.uncommitted(dict(places=[place]), dict(path="/x/s4.jsonl", updated=50)), []))
    results.append(check("no write when another branch is open in the checkout",
                         st.uncommitted(dict(places=[dict(place, head="main")]), dict(path="/x/s4.jsonl", updated=200)), []))
    results.append(check("no write when the session's lines are not in the diff",
                         st.uncommitted(dict(places=[place]), dict(path="/x/s5.jsonl", updated=200)), []))

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

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
