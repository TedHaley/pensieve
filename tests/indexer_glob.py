"""Tests of indexer.glob_match, which stands in for Path.full_match (Python 3.13) on Python 3.12: the session
folders' globs pick the transcripts that the file watcher reports. Run: python tests/indexer_glob.py"""
import sys
import time
from pathlib import PurePath

sys.path.insert(0, str(PurePath(__file__).parent.parent))

from pensieve import config  # noqa: E402
from pensieve.indexer import glob_match  # noqa: E402

CASES = [  # (relative path, glob, matches)
    ("-Users-me-app/abc.jsonl", "*/*.jsonl", True),
    ("abc.jsonl", "*/*.jsonl", False),
    ("-Users-me-app/abc/subagents/x.jsonl", "*/*.jsonl", False),
    ("-Users-me-app/memory/notes.md", "*/*.jsonl", False),
    ("proj/chats/s1.jsonl", "*/chats/*.jsonl", True),
    ("proj/other/s1.jsonl", "*/chats/*.jsonl", False),
    ("2026/10/26/rollout-1.jsonl", "**/rollout-*.jsonl", True),
    ("rollout-1.jsonl", "**/rollout-*.jsonl", True),
    ("2026/10/26/other-1.jsonl", "**/rollout-*.jsonl", False),
    ("a/b.jsonl", "a/?.jsonl", True),
    ("a/bc.jsonl", "a/?.jsonl", False),
    ("a+b/c.jsonl", "a+b/*.jsonl", True),
]


def test_fixed_cases():
    for rel, pat, want in CASES:
        assert glob_match(rel, pat) is want, (rel, pat, want)


def test_same_as_full_match():
    """Where Python has Path.full_match, both agree on every case and on the configured session globs."""
    if not hasattr(PurePath, "full_match"):
        return
    pats = {p for _, _, _, p in config.session_sources()} | {p for _, p, _ in CASES}
    for rel, _, _ in CASES:
        for pat in pats:
            assert glob_match(rel, pat) is PurePath(rel).full_match(pat), (rel, pat)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t0 = time.time()
        t()
        print(f"ok   {t.__name__}  {round((time.time() - t0) * 1000)} ms")
    print(f"{len(tests)} passed")
