"""Tests of the work model (pensieve/thread_model.py): sinks, their completion, and thread states.
Run: python tests/work_model.py"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import thread_model as tm  # noqa: E402

DAY = 86400
NOW = time.time()
PR = tm.Sink(tm.SinkKind.PR, "org/repo#12")
BRANCH = tm.Sink(tm.SinkKind.BRANCH, "mac-app")
TICKET = tm.Sink(tm.SinkKind.TICKET, "ABC-7")


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))
    return ok


def run():
    results = []
    results.append(check("PR merged is done", tm.sink_complete(PR, {PR.label: "MERGED"}), tm.Completion.DONE))
    results.append(check("PR closed is done", tm.sink_complete(PR, {PR.label: "CLOSED"}), tm.Completion.DONE))
    results.append(check("PR open is open", tm.sink_complete(PR, {PR.label: "OPEN"}), tm.Completion.OPEN))
    results.append(check("PR with no status is unknown", tm.sink_complete(PR, {}), tm.Completion.UNKNOWN))
    results.append(check("ticket Done is done", tm.sink_complete(TICKET, {TICKET.label: "Done"}), tm.Completion.DONE))
    results.append(check("ticket In Progress is open", tm.sink_complete(TICKET, {TICKET.label: "In Progress"}),
                         tm.Completion.OPEN))
    results.append(check("branch has no completion check yet", tm.sink_complete(BRANCH, {}), tm.Completion.UNKNOWN))

    results.append(check("all sinks done: thread closed",
                         tm.thread_state([PR], {PR.label: "MERGED"}, NOW, NOW)[0], tm.ThreadState.CLOSED))
    results.append(check("one sink open and recent: thread open",
                         tm.thread_state([PR], {PR.label: "OPEN"}, NOW, NOW)[0], tm.ThreadState.OPEN))
    results.append(check("open sink, quiet 5 days: loose ends",
                         tm.thread_state([PR], {PR.label: "OPEN"}, NOW - 5 * DAY, NOW)[0],
                         tm.ThreadState.LOOSE_ENDS))
    results.append(check("open sink, quiet 21 days: still loose ends (forgotten is set by hand)",
                         tm.thread_state([PR], {PR.label: "OPEN"}, NOW - 21 * DAY, NOW)[0],
                         tm.ThreadState.LOOSE_ENDS))
    results.append(check("unknown branch counts as closed",
                         tm.thread_state([BRANCH], {}, NOW, NOW)[0], tm.ThreadState.CLOSED))
    results.append(check("branch that exists opens while recent",
                         tm.thread_state([BRANCH], {BRANCH.label: "EXISTS"}, NOW, NOW)[0], tm.ThreadState.OPEN))
    results.append(check("unknown ticket counts as closed, and does not open a thread",
                         tm.thread_state([TICKET], {}, NOW, NOW)[0], tm.ThreadState.CLOSED))
    results.append(check("two sinks, one done and one open: not closed",
                         tm.thread_state([PR, TICKET], {PR.label: "MERGED", TICKET.label: "In Progress"}, NOW, NOW)[0],
                         tm.ThreadState.OPEN))
    results.append(check("two sinks, both done: closed",
                         tm.thread_state([PR, TICKET], {PR.label: "MERGED", TICKET.label: "Done"}, NOW, NOW)[0],
                         tm.ThreadState.CLOSED))
    results.append(check("no sink: no verified open sink, so closed (the caller decides for a task with no sinks)",
                         tm.thread_state([], {}, NOW, NOW)[0], tm.ThreadState.CLOSED))
    results.append(check("sink label", PR.label, "pr org/repo#12"))
    results.append(check("component id", tm.Component("s", 0, (3, 5)).id, "s#3-5"))

    from pensieve import work
    row = dict(key="ABC-1", title="", status="", assignee="", url="", server="", path="", updated=0, text="")
    keeper = {**row, "title": "Fix the gate", "status": "In Progress", "assignee": "Me", "url": "https://j/ABC-1",
              "server": "jira", "updated": 10, "text": "the whole description"}
    seen = {**row, "title": "ABC-1", "status": "Closed", "updated": 20, "text": "short"}  # from an agent's tool call
    merged = work._merge_tickets([keeper, seen])
    results.append(check("one ticket from two rows", len(merged), 1))
    results.append(check("the newest row gives the status", merged[0]["status"], "Closed"))
    results.append(check("a key for a title does not hide the real title", merged[0]["title"], "Fix the gate"))
    results.append(check("the fullest description wins", merged[0]["text"], "the whole description"))
    results.append(check("other fields come from the row that has them", (merged[0]["assignee"], merged[0]["url"]),
                         ("Me", "https://j/ABC-1")))

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
