"""Tests of the card builder's gap (pensieve/cardbuild.py): background events wait GAP_SECONDS after the last build,
so a busy session cannot keep the builder running. An event of a user action, or one that a caller waits for, is
built at once. Run: python tests/cardbuild_gap.py"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ["PENSIEVE_DATA"] = tempfile.mkdtemp(prefix="pensieve-cardbuild-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import cardbuild  # noqa: E402

GAP = 0.6


def builder(gap: float):
    """A running builder whose builds only record the kinds of their events and when they ran."""
    cardbuild.GAP_SECONDS = gap
    b = cardbuild.Builder(store=None)
    b.ran = []
    b.full = lambda net=True: None
    b.update = lambda events: b.ran.append((time.monotonic(), sorted({k for k, _, _ in events})))
    threading.Thread(target=b._loop, daemon=True).start()
    time.sleep(0.1)
    b.last_full = time.time() + 3600  # no full build during the test
    return b


def test_background_events_wait_for_the_gap():
    b = builder(GAP)
    b.emit(cardbuild.SESSION, ["a"])
    time.sleep(0.4)
    assert len(b.ran) == 1, b.ran  # the first event: no build before it, so no gap
    for sid in "bcd":  # a busy session: three events within the gap make one build
        b.emit(cardbuild.SESSION, [sid])
        time.sleep(0.1)
    time.sleep(GAP + 0.4)
    assert len(b.ran) == 2, b.ran
    assert b.ran[1][0] - b.ran[0][0] >= GAP - 0.05, b.ran


def test_user_actions_skip_the_gap():
    b = builder(30)
    b.emit(cardbuild.SESSION, ["a"])
    time.sleep(0.4)
    b.emit(cardbuild.SESSION, ["b"])  # waits for the 30 s gap
    time.sleep(0.1)
    b.emit(cardbuild.STATE, ["card"])  # a user action ends the wait, and takes the session event with it
    time.sleep(0.5)
    assert [kinds for _, kinds in b.ran] == [["session"], ["session", "state"]], b.ran
    t = time.monotonic()
    assert b.emit(cardbuild.SESSION, ["c"], wait=5)  # a caller that waits: built at once
    assert time.monotonic() - t < 2, "waited for the gap"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t0 = time.time()
        t()
        print(f"ok   {t.__name__}  {round((time.time() - t0) * 1000)} ms")
    print(f"{len(tests)} passed")
