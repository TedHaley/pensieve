"""The org chart's manager rules: the manager pattern in seniority, the edges it gives, and the user's manager
override. Run: .venv/bin/python tests/org_infer.py"""
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import org  # noqa: E402

NOW = time.time()


def signals(n_prs=0, n_reviews=0, approved=0, title=""):
    return dict(first=NOW - 3 * org.YEAR, n_prs=n_prs, n_reviews=n_reviews, approved=approved, title=title,
                sightings=None, current=bool(title))


def work():
    return dict(repos={"app": {"areas": {"api": 5}}}, blame={}, owns=Counter())


def test_reviewer_who_writes_none_is_a_manager():
    s = org._seniority(signals(n_reviews=5, approved=2), work(), NOW, 0, {"team-a", "team-b"}, 0)
    assert s["level"] == "manager" and s["level_from"] == "signals"
    assert s["level_confidence"] <= 0.55
    assert "writes almost none" in s["level_evidence"][0]


def test_reviewer_who_also_writes_is_not():
    s = org._seniority(signals(n_prs=30, n_reviews=10, approved=2), work(), NOW, 0, {"team-a"}, 0)
    assert s["level"] != "manager"


def test_reviews_alone_are_not_enough():
    s = org._seniority(signals(n_reviews=5, approved=1), work(), NOW, 0, set(), 0)
    assert s["level"] != "manager", "no team to maintain and one person approved: a narrow view, not a manager"


def test_title_wins_over_the_pattern():
    s = org._seniority(signals(n_reviews=5, approved=2, title="Software Engineer"), work(), NOW, 0, {"t"}, 0)
    assert s["level_from"] == "title"


def _people(*names):
    return {n: dict(name=n.title(), rank=i) for i, n in enumerate(names)}


def _op(level, frm="signals"):
    return dict(level=level, level_from=frm, level_confidence=0.4, level_evidence=[])


def test_manager_pattern_approver_is_the_manager():
    people = _people("me", "boss", "peer")
    out = {"me": _op("IC"), "boss": _op("manager"), "peer": _op("senior IC")}
    approvals = {"me": Counter(peer=13.6, boss=3.9)}
    acc = dict(titles={})
    edges = org._reporting(people, out, [], {}, {}, {}, approvals, {}, acc)
    assert [(e["manager"], e["report"]) for e in edges] == [("boss", "me")], "the peer who approves most is no manager"
    assert 0.3 < edges[0]["confidence"] <= 0.6


def test_user_set_manager_wins():
    people = _people("me", "boss", "other")
    out = {"me": _op("IC"), "boss": _op("IC"), "other": _op("manager")}
    inferred = [dict(manager="other", report="me", confidence=0.5, evidence=["x"])]
    edges = org._user_set(inferred, [("boss", "me")], people, out)
    assert [(e["manager"], e["report"], e["confidence"]) for e in edges] == [("boss", "me", 1.0)]
    assert edges[0]["set_by_user"]
    assert out["boss"]["level"] == "manager" and out["boss"]["level_from"] == "you"


def test_user_set_ignores_unknown_people():
    people = _people("me")
    edges = org._user_set([], [("gone", "me"), ("me", "me")], people, {"me": _op("IC")})
    assert edges == []


def main():
    tests = [(k, v) for k, v in globals().items() if k.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok   {name}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    main()
