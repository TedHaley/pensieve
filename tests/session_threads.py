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

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
