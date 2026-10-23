"""Tests of the thread states (pensieve/task_states.py) set from sinks: open, loose ends, closed, forgotten.
A task with no sink is a thought: thoughts, then loose ends and forgotten by its quiet days. Unknown counts as closed.
Run: python tests/thread_states.py"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import task_states as ts  # noqa: E402
from pensieve.task_states import State  # noqa: E402

DAY = 86400
NOW = time.time()
PR_URL = "https://github.com/org/repo/pull/12"


def task(quiet_days=0, **extra):
    base = dict(prs=[], tickets=[], branches=[], places=[], files=[], commits=[], events=[],
                updated=NOW - quiet_days * DAY, active=False)
    return {**base, **extra}


def state_of(t):
    ts.apply(t, {}, None)
    return t["state"]


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))
    return ok


def run():
    results = []
    open_pr = [dict(url=PR_URL, state="open", head="feat")]
    results.append(check("open PR, work in the last 3 days: open",
                         state_of(task(0, prs=open_pr)), "open"))
    results.append(check("open PR, quiet 10 days: loose ends",
                         state_of(task(10, prs=open_pr)), "loose_ends"))
    results.append(check("ticket in progress, quiet 10 days: loose ends",
                         state_of(task(10, tickets=[dict(key="ABC-1", status="In Progress")])), "loose_ends"))
    results.append(check("ticket with no status: unknown, so closed",
                         state_of(task(0, tickets=[dict(key="ABC-2")])), "closed"))
    results.append(check("merged PR: closed",
                         state_of(task(1, prs=[dict(url=PR_URL, state="merged", head="feat")])), "closed"))
    results.append(check("ticket done: closed",
                         state_of(task(0, tickets=[dict(key="ABC-3", status="Done")])), "closed"))
    results.append(check("no sink, written now: thoughts", state_of(task(0)), "thoughts"))
    results.append(check("no sink, quiet 5 days: forgotten", state_of(task(5)), "forgotten"))
    results.append(check("no sink, quiet 30 days: forgotten", state_of(task(30)), "forgotten"))

    manual = ts.StateChange("t1", "forgotten", "user", "parked", NOW - 1)
    held = task(0, prs=open_pr, updated=NOW - 5)
    ts.apply(held, {}, manual)
    results.append(check("a state set by hand holds while nothing newer happens",
                         held["state"], "forgotten"))
    newer = task(0, prs=open_pr, updated=NOW + 60)
    ts.apply(newer, {}, manual)
    results.append(check("new activity after a state set by hand: automatic again",
                         newer["state"], "open"))

    results.append(check("five states in order", [s.value for s in ts.ORDER],
                         ["open", "thoughts", "loose_ends", "closed", "forgotten"]))
    results.append(check("todo and closing fold into loose ends", (ts.RENAMED["todo"], ts.RENAMED["closing"]),
                         ("loose_ends", "loose_ends")))
    results.append(check("complete becomes closed", ts.RENAMED["complete"], "closed"))

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
