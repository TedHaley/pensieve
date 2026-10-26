"""Tests of the setup scan (pensieve/onboarding.py): a scan of all categories, which `pensieve setup` makes, must not
ask for the connections category, which has no setup items. Run: python tests/onboarding_scan.py"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["PENSIEVE_DATA"] = tempfile.mkdtemp(prefix="pensieve-scan-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import onboarding  # noqa: E402

onboarding._agents = onboarding._repos = lambda s, on: []  # these read the index and git: not under test here


def test_scan_all_skips_connections():
    """No categories: every category but connections."""
    ids = [c["id"] for c in onboarding.scan(fresh=True)]
    assert ids == ["agents", "files", "repos"], ids


def test_scan_connections_only():
    """Asking for connections alone gives nothing, not a KeyError."""
    assert onboarding.scan(["connections"], fresh=True) == []


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t0 = time.time()
        t()
        print(f"ok   {t.__name__}  {round((time.time() - t0) * 1000)} ms")
    print(f"{len(tests)} passed")
