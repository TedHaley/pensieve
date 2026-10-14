"""Pensieve against grep in one agent batch, three ways: every task; leaving out the tasks whose answer has a
near-identical twin (bench/twins.py); and counting an answer that names a twin as right.

  .venv/bin/python bench/clean_report.py --label v8
"""
import argparse
import glob
import json
import statistics as st
from collections import defaultdict
from pathlib import Path

OUT = Path(__file__).parent / "out"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--tasks", default="tasks-v2.jsonl")
    a = ap.parse_args()
    twins = {}
    for f in glob.glob(str(OUT / f"*/{a.tasks}")):
        for line in open(f):
            t = json.loads(line)
            twins[t["id"]] = set(t.get("twins") or [])
    rows = [json.loads(line) for f in glob.glob(str(OUT / f"*/runs-sonnet-{a.label}.jsonl")) for line in open(f)]
    by = defaultdict(list)
    for r in rows:
        by[r["arm"]].append(r)
    print(f"{len(rows)} runs, {len({r['task'] for r in rows})} tasks, "
          f"{len({r['task'] for r in rows if twins.get(r['task'])})} with a near-identical twin")
    print(f"{'setup':<10}{'used Pensieve':>15}{'found':>12}{'no twin':>12}{'twin ok':>12}{'secs':>7}{'cost':>8}{'calls':>7}")
    for arm, rs in by.items():
        clean = [r for r in rs if not twins.get(r["task"])]
        twin_ok = sum(r["ok"] or r.get("got") in twins.get(r["task"], ()) for r in rs)
        used = sum(any(c.startswith("mcp__pensieve") for c in r["calls"]) for r in rs)
        print(f"{arm:<10}{used:>9}/{len(rs):<5}{sum(r['ok'] for r in rs):>6}/{len(rs):<5}"
              f"{sum(r['ok'] for r in clean):>6}/{len(clean):<5}{twin_ok:>6}/{len(rs):<5}"
              f"{st.mean(r['secs'] for r in rs):>7.1f}{st.mean(r['cost'] or 0 for r in rs):>8.4f}"
              f"{st.mean(len(r['calls']) for r in rs):>7.2f}")


if __name__ == "__main__":
    main()
