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
    results.append(check("open sink, quiet 21 days: forgotten",
                         tm.thread_state([PR], {PR.label: "OPEN"}, NOW - 21 * DAY, NOW)[0],
                         tm.ThreadState.FORGOTTEN))
    results.append(check("unverifiable branch never closes",
                         tm.thread_state([BRANCH], {}, NOW, NOW)[0], tm.ThreadState.LOOSE_ENDS))
    results.append(check("two sinks, one done and one open: not closed",
                         tm.thread_state([PR, TICKET], {PR.label: "MERGED", TICKET.label: "In Progress"}, NOW, NOW)[0],
                         tm.ThreadState.OPEN))
    results.append(check("two sinks, both done: closed",
                         tm.thread_state([PR, TICKET], {PR.label: "MERGED", TICKET.label: "Done"}, NOW, NOW)[0],
                         tm.ThreadState.CLOSED))
    results.append(check("no sink: loose ends, never open",
                         tm.thread_state([], {}, NOW, NOW)[0], tm.ThreadState.LOOSE_ENDS))
    results.append(check("sink label", PR.label, "pr org/repo#12"))
    results.append(check("component id", tm.Component("s", 0, (3, 5)).id, "s#3-5"))

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
