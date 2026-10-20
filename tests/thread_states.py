"""Tests of the thread states: open, loose ends, closing, closed and forgotten (task_states.derive).
Run: python tests/thread_states.py

Only task_states is imported, so the test needs no store and no repos."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import task_states as ts  # noqa: E402
from pensieve.task_states import State  # noqa: E402

DAY = 86400
NOW = time.time()


def task(quiet_days=0, **extra):
    """A thread with material work (one commit) that was last changed `quiet_days` ago."""
    base = dict(prs=[], tickets=[], branches=[], places=[], files=[], commits=[{"sha": "abc1234"}],
                updated=NOW - quiet_days * DAY, active=False)
    return {**base, **extra}


LEFT = {"next": "write the tests"}
NOTHING = {"next": "nothing"}


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got}, want {want}"))
    return ok


def run():
    results = []
    # Open: recent work with something left.
    results.append(check("open: recent work, work left", ts.derive(task(0), LEFT, NOW)[0], State.OPEN))
    # Loose ends: work left, quiet, not yet 21 days.
    results.append(check("loose ends: work left, quiet 10 days", ts.derive(task(10), LEFT, NOW)[0], State.LOOSE_ENDS))
    # Forgotten: loose ends left for FORGET_DAYS.
    results.append(check("forgotten: work left, quiet 21 days", ts.derive(task(21), LEFT, NOW)[0], State.FORGOTTEN))

    # Closing: nothing left, no outside evidence.
    results.append(check("closing: nothing left, no evidence, quiet 0", ts.derive(task(0), NOTHING, NOW)[0],
                         State.CLOSING))
    results.append(check("closing: holds at 6 days", ts.derive(task(6), NOTHING, NOW)[0], State.CLOSING))
    results.append(check("closed: closing for 7 days with no change", ts.derive(task(7), NOTHING, NOW)[0],
                         State.CLOSED))
    # Closing never goes to forgotten, even after the forgotten period.
    results.append(check("closing never forgotten: quiet 30 days", ts.derive(task(30), NOTHING, NOW)[0],
                         State.CLOSED))

    # Closed with outside evidence: no waiting.
    merged = [dict(state="merged", merged_at=NOW - 3600, own=True, number=5)]
    results.append(check("closed: PR merged, nothing after", ts.derive(task(1, prs=merged), LEFT, NOW)[0],
                         State.CLOSED))
    done_ticket = [dict(key="ABC-1", status="Done")]
    results.append(check("closed: ticket done, work left in note", ts.derive(task(0, tickets=done_ticket), LEFT, NOW)[0],
                         State.CLOSED))

    # Legacy names and the state list.
    results.append(check("rename: complete -> closed", ts.RENAMED.get("complete"), "closed"))
    results.append(check("state list has closing and closed", [s.value for s in ts.ORDER],
                         ["open", "loose_ends", "todo", "closing", "closed", "forgotten"]))
    results.append(check("CLOSE_DAYS is less than FORGET_DAYS", ts.CLOSE_DAYS < ts.FORGET_DAYS, True))

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
