"""The org chart's rules: the role from depth and breadth, the reviewer pattern and the edges it gives, and the user's
manager override. Run: .venv/bin/python tests/org_infer.py"""
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import org  # noqa: E402

NOW = time.time()


def signals(n_prs=0, n_reviews=0, approved=0, years=1):
    return dict(first=NOW - years * org.YEAR, n_prs=n_prs, n_reviews=n_reviews, approved=approved)


def work(repos=1, areas=1, commits=5, blame=None, owns=None):
    return dict(repos={f"r{i}": {"areas": {f"a{j}": commits for j in range(areas)}} for i in range(repos)},
                blame=blame or {}, owns=Counter(owns or {}))


def test_a_little_work_is_a_contributor():
    r = org._role(signals(n_prs=4), work(), NOW, 0, set(), 0)
    assert r["level"] == "contributor" and r["depth"] < org.DEEP and r["breadth"] < org.BROAD


def test_depth_is_a_domain_expert():
    r = org._role(signals(n_prs=40, years=3), work(commits=40, blame={"r0": 0.4}), NOW, 2, set(), 0)
    assert r["level"] == "domain expert", r
    assert r["level_evidence"][0].startswith("Depth")


def test_depth_and_breadth_is_a_lead():
    r = org._role(signals(n_prs=40, approved=6, years=3), work(repos=5, areas=5, commits=40, blame={"r0": 0.4}),
                  NOW, 3, {"team"}, 0)
    assert r["level"] == "lead", r


def test_breadth_alone_is_a_contributor():
    r = org._role(signals(n_prs=40, approved=6), work(repos=5, areas=5), NOW, 0, {"team"}, 0)
    assert r["level"] == "contributor" and r["breadth"] >= org.BROAD


def test_no_work_has_no_role():
    r = org._role(dict(first=0, n_prs=0, n_reviews=0, approved=0), dict(repos={}, blame={}, owns=Counter()),
                  NOW, 0, set(), 0)
    assert r["level"] is None


def test_reviewer_who_writes_none_stamps():
    assert org._role(signals(n_reviews=5, approved=2), work(), NOW, 0, {"team-a"}, 0)["stamps"]
    assert not org._role(signals(n_prs=30, n_reviews=10, approved=2), work(), NOW, 0, {"team-a"}, 0)["stamps"]
    assert not org._role(signals(n_reviews=5, approved=1), work(), NOW, 0, set(), 0)["stamps"], \
        "no team to maintain and one person approved: a narrow view"


def _people(*names):
    return {n: dict(name=n.title(), rank=i) for i, n in enumerate(names)}


def _op(stamps=False):
    return dict(level="contributor", level_from="signals", level_confidence=0.4, level_evidence=[], stamps=stamps)


def test_reviewer_who_approves_is_the_manager():
    people = _people("me", "boss", "peer")
    out = {"me": _op(), "boss": _op(stamps=True), "peer": _op()}
    approvals = {"me": Counter(peer=13.6, boss=3.9)}
    edges = org._reporting(people, out, [], {}, {}, {}, approvals, {}, dict(titles={}))
    assert [(e["manager"], e["report"]) for e in edges] == [("boss", "me")], "the peer who approves most is no manager"
    assert 0.3 < edges[0]["confidence"] <= 0.6


def test_user_set_manager_wins():
    people = _people("me", "boss", "other")
    inferred = [dict(manager="other", report="me", confidence=0.5, evidence=["x"])]
    edges = org._user_set(inferred, [("boss", "me")], people)
    assert [(e["manager"], e["report"], e["confidence"]) for e in edges] == [("boss", "me", 1.0)]
    assert edges[0]["set_by_user"]


def test_user_set_ignores_unknown_people():
    assert org._user_set([], [("gone", "me"), ("me", "me")], _people("me")) == []


def main():
    tests = [(k, v) for k, v in globals().items() if k.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok   {name}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    main()
