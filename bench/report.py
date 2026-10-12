"""One table for many search strategies over bench/out/validation.jsonl: hit@1 by kind of query, hit@1/3/8 over all
queries and over the test split, and the median time, next to grep (one grep for the whole query, files ranked by
match count, ties in random order).

Each run is NAME=PATH, where PATH is either an MCP or speed.py dump ([[query, [{"id": ...}, ...]], ...]) or a list of
ranks (1-based rank of the first right file, or null), as retrieval.py --ranks writes. A dump's `.times.json`
(from mcp_bench.py run) gives the time.

  PENSIEVE_DATA=<copy> .venv/bin/python bench/report.py 0.6.3=a.json now=b.json [--grep-data <copy without worktrees>]
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from mcp_bench import grep_ranks  # noqa: E402

TASKS = Path(__file__).parent / "out" / "validation.jsonl"
KINDS = ["question", "name+words", "name", "string", "ambiguous", "runtime"]


def ranks_of(path, tasks, where):
    data = json.load(open(path))
    if data and isinstance(data[0], list):  # a dump: results in order
        assert [q for q, _ in data] == [t["query"] for t in tasks], f"{path} doesn't match the query set"
        out = []
        for t, (_, res) in zip(tasks, data):
            files = list(dict.fromkeys(where[x["id"]] for x in res if x["id"] in where))
            out.append(next((i + 1 for i, (rp, p) in enumerate(files) if rp == t["repo"] and p in t["answers"]), None))
        return out
    assert len(data) == len(tasks), f"{path}: {len(data)} ranks for {len(tasks)} queries"
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", help="NAME=PATH")
    ap.add_argument("--tasks", default=str(TASKS))
    ap.add_argument("--grep-data", help="index to grep (one without linked worktrees: grep in the checkout)")
    a = ap.parse_args()
    db = sqlite3.connect(Path(os.environ["PENSIEVE_DATA"]) / "pensieve.db")
    where = {f"code:{i}": (repo, path) for i, repo, path in db.execute("SELECT id, repo, path FROM code_chunks")}
    tasks = [json.loads(ln) for ln in open(a.tasks)]
    gdb = sqlite3.connect(Path(a.grep_data) / "pensieve.db") if a.grep_data else db
    rows = []
    for field, name in (("query", "grep, whole query"), ("grep_guess", "grep, longest part without digits"),
                        ("grep", "grep, the template's real fixed text")):
        if field != "query" and not any(field in t for t in tasks):
            continue
        g, _ = grep_ranks(gdb, [dict(t, grep=t.get(field) or t["query"]) for t in tasks])
        rows.append((name, {k: np.array(v) for k, v in g.items()}, None))
    for run in a.runs:
        name, path = run.split("=", 1)
        r = ranks_of(path, tasks, where)
        hit = {k: np.array([x is not None and x <= k for x in r], float) for k in (1, 3, 8)}
        tf = Path(path).with_suffix(".times.json")
        ms = np.median([x["ms"] for x in json.load(open(tf))]) if tf.exists() else None
        rows.append((name, hit, ms))
    kinds = [k for k in KINDS if any(t["kind"] == k for t in tasks)]
    test = np.array([t["split"] == "test" for t in tasks])
    print("| Strategy | " + " | ".join(f"{k} @1" for k in kinds) + " | all @1 | all @3 | all @8 | test @1 | ms p50 |")
    print("|---|" + "---:|" * (len(kinds) + 5))
    for name, hit, ms in rows:
        cells = [f"{hit[1][[t['kind'] == k for t in tasks]].mean() * 100:.1f}" for k in kinds]
        cells += [f"{hit[k].mean() * 100:.1f}" for k in (1, 3, 8)] + [f"{hit[1][test].mean() * 100:.1f}"]
        cells.append(f"{ms:.0f}" if ms else "")
        print(f"| {name} | " + " | ".join(cells) + " |")
    print(f"\n{len(tasks)} queries: " + ", ".join(f"{k} {sum(t['kind'] == k for t in tasks)}" for k in kinds)
          + f"; {int(test.sum())} in the test split")


if __name__ == "__main__":
    main()
