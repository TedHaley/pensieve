"""The org chart's role rules: contributor, domain expert and lead from the depth and breadth of a person's work.
Run: .venv/bin/python tests/org_infer.py"""
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


def main():
    tests = [(k, v) for k, v in globals().items() if k.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok   {name}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    main()
