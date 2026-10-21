"""Tests of the app's four thread states (pensieve/task_states.py): open, loose ends, closed, forgotten.
Run: python tests/thread_states.py"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import task_states as ts  # noqa: E402
from pensieve.task_states import State  # noqa: E402

DAY = 86400
NOW = time.time()
LEFT = {"next": "write the tests"}
NOTHING = {"next": "nothing"}


def task(quiet_days=0, **extra):
    base = dict(prs=[], tickets=[], branches=[], places=[], files=[], commits=[{"sha": "abc1234"}],
                updated=NOW - quiet_days * DAY, active=False)
    return {**base, **extra}


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))
    return ok


def run():
    results = []
    results.append(check("open: recent work, work left", ts.derive(task(0), LEFT, NOW)[0], State.OPEN))
    results.append(check("loose ends: work left, quiet 10 days", ts.derive(task(10), LEFT, NOW)[0], State.LOOSE_ENDS))
    results.append(check("forgotten: work left, quiet 21 days", ts.derive(task(21), LEFT, NOW)[0], State.FORGOTTEN))
    results.append(check("nothing left, no evidence, recent: not closed",
                         ts.derive(task(0), NOTHING, NOW)[0], State.OPEN))
    results.append(check("nothing left, no evidence, quiet: not closed",
                         ts.derive(task(10), NOTHING, NOW)[0], State.LOOSE_ENDS))
    results.append(check("merged PR, nothing after: closed",
                         ts.derive(task(1, prs=[dict(state="merged", merged_at=NOW - 3600, own=True)]), LEFT, NOW)[0],
                         State.CLOSED))
    results.append(check("ticket done: closed",
                         ts.derive(task(0, tickets=[dict(key="ABC-1", status="Done")]), LEFT, NOW)[0], State.CLOSED))
    results.append(check("ticket not started: loose ends",
                         ts.derive(task(0, commits=[], tickets=[dict(key="ABC-2", status="To Do")]), NOTHING, NOW)[0],
                         State.LOOSE_ENDS))
    results.append(check("four states in order", [s.value for s in ts.ORDER],
                         ["open", "loose_ends", "closed", "forgotten"]))
    results.append(check("todo and closing fold into loose ends", (ts.RENAMED["todo"], ts.RENAMED["closing"]),
                         ("loose_ends", "loose_ends")))
    results.append(check("complete becomes closed", ts.RENAMED["complete"], "closed"))

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
