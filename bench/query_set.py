"""A fixed, balanced query set for speed and quality comparisons: a third semantic (plain-language `query`), a
third exact (`ident` or `literal`), a third mixed (one identifier plus plain words), drawn from every repo's
tasks-v2.jsonl and tasks-speed.jsonl with a fixed seed. Each line carries its graded answers.

  .venv/bin/python bench/query_set.py -n 1000          # -> bench/out/queries-1000.jsonl
"""
import argparse
import json
import random
from pathlib import Path

OUT = Path(__file__).parent / "out"
BUCKET = {"query": "semantic", "ident": "exact", "literal": "exact", "mixed": "mixed"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    pool = {"semantic": [], "exact": [], "mixed": []}
    for f in sorted(OUT.glob("*/tasks-v2.jsonl")) + sorted(OUT.glob("*/tasks-speed.jsonl")):
        for ln in open(f):
            t = json.loads(ln)
            for style, bucket in BUCKET.items():
                if t.get(style):
                    ans = t.get("files") or (t.get("ident_files") if style == "ident" else None) or [t["file"]]
                    pool[bucket].append(dict(task=t["id"], style=style, bucket=bucket, query=t[style], repo=t["repo"],
                                             answers=ans))
    rng = random.Random(a.seed)
    per = [a.n // 3 + (1 if i < a.n % 3 else 0) for i in range(3)]
    out = []
    for (bucket, qs), k in zip(pool.items(), per):
        if len(qs) < k:
            raise SystemExit(f"only {len(qs)} {bucket} queries for {k}: generate more tasks first")
        out += rng.sample(qs, k)
    rng.shuffle(out)
    path = OUT / f"queries-{a.n}.jsonl"
    path.write_text("".join(json.dumps(q) + "\n" for q in out))
    by = {}
    for q in out:
        by[(q["bucket"], Path(q["repo"]).name)] = by.get((q["bucket"], Path(q["repo"]).name), 0) + 1
    print(f"{len(out)} queries -> {path}")
    for k, v in sorted(by.items()):
        print(f"  {k[0]:9} {k[1]:16} {v}")


if __name__ == "__main__":
    main()
