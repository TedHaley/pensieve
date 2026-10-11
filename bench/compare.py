"""Paired quality comparison of two `speed.py search --dump` runs over the same index and tasks.

For each query: the rank of the first right file (graded like retrieval.py: `files`, else `ident_files` for an
identifier query, else `file`). Reports hit@1/3/10 and MRR for both runs, the exact McNemar test on the queries
whose hit@k differs, and a bootstrap 95% interval for each difference.

  PENSIEVE_DATA=<scratch> .venv/bin/python bench/compare.py A.json B.json [--tasks bench/out/*/tasks-v2.jsonl]
"""
import argparse
import glob
import json
import os
import sqlite3
from math import comb
from pathlib import Path

import numpy as np


def answers(paths):
    out = {}
    for p in paths:
        for ln in open(p):
            t = json.loads(ln)
            if "bucket" in t:  # a query set from query_set.py
                out[t["query"]] = (t["repo"], set(t["answers"]), t["bucket"])
                continue
            for style in ("query", "ident", "literal", "mixed"):
                if t.get(style):
                    ans = t.get("files") or (t.get("ident_files") if style == "ident" else None) or [t["file"]]
                    out[t[style]] = (t["repo"], set(ans), style)
    return out


def ranks(dump, ans, where):
    r = []
    for (q, res), (repo, want, _) in zip(dump, ans):
        files = []
        for x in res:
            if x["id"].startswith("code:") and int(x["id"][5:]) in where:
                f = where[int(x["id"][5:])]
                if f not in files:
                    files.append(f)
        r.append(next((i + 1 for i, (rp, p) in enumerate(files) if rp == repo and p in want), None))
    return r


def mcnemar(b, c):
    """Exact two-sided p for b vs c discordant pairs."""
    n = b + c
    if not n:
        return 1.0
    return min(1.0, 2 * sum(comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--tasks", nargs="+", default=sorted(glob.glob(str(Path(__file__).parent / "out/*/tasks-v2.jsonl"))))
    x = ap.parse_args()
    db = sqlite3.connect(Path(os.environ["PENSIEVE_DATA"]) / "pensieve.db")
    where = {i: (repo, path) for i, repo, path in db.execute("SELECT id, repo, path FROM code_chunks")}
    A, B = (json.load(open(f)) for f in (x.a, x.b))
    assert [q for q, _ in A] == [q for q, _ in B], "the two runs must cover the same queries in the same order"
    first = json.loads(open(x.tasks[0]).readline())
    if "bucket" in first:  # a query set: its lines are the runs' queries, in order
        ans = [(t["repo"], set(t["answers"]), t["bucket"]) for t in map(json.loads, open(x.tasks[0]))]
        assert [q for q, _ in A] == [json.loads(ln)["query"] for ln in open(x.tasks[0])], "runs don't match the query set"
    else:
        by_q = answers(x.tasks)
        ans = [by_q[q] for q, _ in A]
    ra, rb = ranks(A, ans, where), ranks(B, ans, where)
    report("all", ra, rb, ans)
    for bucket in sorted({a[2] for a in ans}):
        keep = [i for i, a in enumerate(ans) if a[2] == bucket]
        report(bucket, [ra[i] for i in keep], [rb[i] for i in keep], [ans[i] for i in keep])


def report(name, ra, rb, ans):
    n = len(ra)
    rng = np.random.default_rng(0)
    boot = rng.integers(0, n, size=(10000, n))
    print(f"\n{name}: {n} queries, {len({a[0] for a in ans})} repos")
    for k in (1, 3, 10):
        ha = np.array([r is not None and r <= k for r in ra])
        hb = np.array([r is not None and r <= k for r in rb])
        lost, won = int((ha & ~hb).sum()), int((~ha & hb).sum())
        d = (hb[boot].mean(1) - ha[boot].mean(1)) * 100
        print(f"hit@{k:<2} A {ha.mean() * 100:5.1f}%  B {hb.mean() * 100:5.1f}%  diff {(hb.mean() - ha.mean()) * 100:+5.2f} pts"
              f"  95% CI [{np.percentile(d, 2.5):+.2f}, {np.percentile(d, 97.5):+.2f}]  B lost {lost}, won {won}"
              f"  McNemar p={mcnemar(lost, won):.3f}")
    ma = np.array([1 / r if r else 0 for r in ra])
    mb = np.array([1 / r if r else 0 for r in rb])
    d = (mb[boot].mean(1) - ma[boot].mean(1))
    print(f"MRR    A {ma.mean():.4f}  B {mb.mean():.4f}  diff {mb.mean() - ma.mean():+.4f}  "
          f"95% CI [{np.percentile(d, 2.5):+.4f}, {np.percentile(d, 97.5):+.4f}]")
    same = sum(r1 == r2 for r1, r2 in zip(ra, rb))
    print(f"same rank of the right file: {same}/{n}")


if __name__ == "__main__":
    main()
