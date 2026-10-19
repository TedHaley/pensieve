"""Tests of the since:, until: and project: filters of search (pensieve/search.py). Run: python tests/search_filters.py"""
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ["PENSIEVE_DATA"] = tempfile.mkdtemp(prefix="pensieve-search-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import search  # noqa: E402


def test_dates():
    """A date starts at local midnight; until: takes the end of that day, so the day itself is in."""
    day = datetime.combine(date(2026, 10, 1), datetime.min.time()).timestamp()
    assert search.when("2026-10-01") == day
    assert search.when("2026-10-01", end=True) == day + 86400


def test_times_back():
    now = time.time()
    for val, secs in (("12h", 12 * 3600), ("3d", 3 * 86400), ("2w", 14 * 86400), ("1m", 30 * 86400)):
        assert abs(now - secs - search.when(val)) < 5, val
    yesterday = datetime.combine(date.today() - timedelta(days=1), datetime.min.time()).timestamp()
    assert search.when("yesterday") == yesterday and search.when("Today") == yesterday + 86400


def test_bad_value():
    for val in ("last-week", "3 days", "2026-13-01"):
        try:
            search.when(val)
        except ValueError as e:
            assert "since: and until:" in str(e)
        else:
            raise AssertionError(val)


def test_parse():
    """The filters leave the words alone, and keep their values as epochs and a lowercase project."""
    p = search.parse("retry backoff since:2026-10-01 until:2026-10-02 project:Payments-API")
    assert p["semantic"] == "retry backoff"
    assert p["filters"]["since"] < p["filters"]["until"] and p["filters"]["project"] == "payments-api"


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t0 = time.time()
        t()
        print(f"ok   {t.__name__}  {round((time.time() - t0) * 1000)} ms")
    print(f"{len(tests)} passed")
