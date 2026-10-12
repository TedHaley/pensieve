"""Search quality and time as agents get them: every query goes through the MCP `search` tool of a running server,
scoped the way an agent in that repo is (?scope=auto&cwd=<repo>), with the tool's default limit. Times are the round
trip the agent waits for. Compared with one grep for the whole query (random order among files with the same count).

  PENSIEVE_DATA=<copy> .venv/bin/pensieve serve --no-open --port 8796       # a copy of an index, never ~/.pensieve
  .venv/bin/python bench/mcp_bench.py run --url http://localhost:8796 --out A.json [--tasks bench/out/queries-1000.jsonl]
  PENSIEVE_DATA=<copy> .venv/bin/python bench/mcp_bench.py report A.json       # hit@k by query kind, vs grep
  PENSIEVE_DATA=<copy> .venv/bin/python bench/compare.py A.json B.json --tasks bench/out/queries-1000.jsonl
"""
import argparse
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

TASKS = Path(__file__).parent / "out" / "queries-1000.jsonl"
HIT = re.compile(r"\[((?:code|file|doc|session|folder|repo|person):[^\]]+)\]$")


def call(url, repo, query):
    q = urllib.parse.urlencode({"scope": "auto", "cwd": repo, "client": "bench"})
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "search", "arguments": {"query": query}}}).encode()
    req = urllib.request.Request(f"{url}/mcp?{q}", body, {"content-type": "application/json",
                                                          "accept": "application/json, text/event-stream"})
    t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)
    ms = (time.perf_counter() - t) * 1000
    text = d["result"]["content"][0]["text"]
    ids = [m.group(1) for ln in text.splitlines() if not ln.startswith(" ") and (m := HIT.search(ln.rstrip()))]
    return ids, ms, len(text)


def cmd_run(a):
    tasks = [json.loads(ln) for ln in open(a.tasks)]
    call(a.url, tasks[0]["repo"], "warm up")
    dump, times = [], []
    for i, t in enumerate(tasks):
        ids, ms, chars = call(a.url, t["repo"], t["query"])
        dump.append([t["query"], [{"id": x} for x in ids]])
        times.append(dict(style=t["style"], ms=ms, chars=chars))
        if (i + 1) % 100 == 0:
            print(f"{i + 1}/{len(tasks)}", flush=True)
    Path(a.out).write_text(json.dumps(dump, indent=1))
    Path(a.out).with_suffix(".times.json").write_text(json.dumps(times))
    ms = np.array([x["ms"] for x in times])
    print(f"{len(ms)} MCP searches: p50 {np.percentile(ms, 50):.1f} ms, p95 {np.percentile(ms, 95):.1f} ms, "
          f"output {np.median([x['chars'] for x in times]):.0f} chars median")


def grep_ranks(db, tasks, field="grep"):
    """Expected hit@k of one case-insensitive grep for the whole query, files ranked by match count, ties in random
    order (k -> list of probabilities), and how many files contain the query."""
    by_repo = defaultdict(list)
    for repo, path, text in db.execute("SELECT repo, path, text FROM code_chunks"):
        by_repo[repo].append((path, (text or "").lower()))
    out, n_files = {1: [], 3: [], 8: []}, []
    for t in tasks:
        cnt, ql = Counter(), (t.get(field) or t["query"]).lower()  # e.g. `grep`: what an agent would grep instead
        for p, tl in by_repo[t["repo"]]:
            if ql in tl:
                cnt[p] += tl.count(ql)
        n_files.append(len(cnt))
        want = [c for p, c in cnt.items() if p in t["answers"]]
        for k in out:
            if not want:
                out[k].append(0.0)
                continue
            best = max(want)
            above = sum(c > best for c in cnt.values())
            tied = sum(c == best for c in cnt.values())
            right = sum(c == best for p, c in cnt.items() if p in t["answers"])
            slots = k - above  # places left for the tied files; chance one of them is a right file
            if slots <= 0:
                out[k].append(0.0)
            elif slots >= tied:
                out[k].append(1.0)
            else:
                from math import comb
                out[k].append(1 - comb(tied - right, slots) / comb(tied, slots))
    return out, n_files


def cmd_report(a):
    db = sqlite3.connect(Path(os.environ["PENSIEVE_DATA"]) / "pensieve.db")
    where = {f"code:{i}": (repo, path) for i, repo, path in db.execute("SELECT id, repo, path FROM code_chunks")}
    tasks = [json.loads(ln) for ln in open(a.tasks)]
    dump = json.load(open(a.dump))
    assert [q for q, _ in dump] == [t["query"] for t in tasks], "the run doesn't match the query set"
    rank = []
    for t, (_, res) in zip(tasks, dump):
        files = list(dict.fromkeys(where[x["id"]] for x in res if x["id"] in where))
        rank.append(next((i + 1 for i, (rp, p) in enumerate(files) if rp == t["repo"] and p in t["answers"]), None))
    g, n_files = grep_ranks(sqlite3.connect(Path(a.grep_data) / "pensieve.db") if a.grep_data else db, tasks,
                            a.grep_field)
    tf = Path(a.dump).with_suffix(".times.json")
    times = json.load(open(tf)) if tf.exists() else None
    print(f"{'kind':<14}{'n':>5}  {'Pensieve @1':>11} {'@3':>6} {'@8':>6}   {'grep @1':>7} {'@3':>6} {'@8':>6}"
          + ("   ms p50   p95" if times else ""))
    lit = lambda lo, hi: lambda i, t: t["style"] == "literal" and lo <= n_files[i] <= hi
    groups = [(s, lambda i, t, s=s: t["style"] == s) for s in ("literal", "ident", "mixed", "query")] + [
        ("all", lambda i, t: True), ("literal, 1 file", lit(1, 1)), ("literal, 2-3", lit(2, 3)),
        ("literal, 4+", lit(4, 10 ** 9))]
    groups += [(f"{s} {sp}", lambda i, t, s=s, sp=sp: t.get("set") == s and t.get("split") == sp)
               for s in sorted({t["set"] for t in tasks if t.get("set")}) for sp in ("dev", "test")]
    for name, pick in groups:
        keep = [i for i, t in enumerate(tasks) if pick(i, t)]
        if not keep:
            continue
        row = f"{name:<14}{len(keep):>5}  "
        row += " ".join(f"{np.mean([rank[i] is not None and rank[i] <= k for i in keep]) * 100:{w}.1f}%"
                        for k, w in ((1, 10), (3, 5), (8, 5)))
        row += "   " + " ".join(f"{np.mean([g[k][i] for i in keep]) * 100:{w}.1f}%"
                                for k, w in ((1, 6), (3, 5), (8, 5)))
        if times:
            ms = [times[i]["ms"] for i in keep]
            row += f"   {np.percentile(ms, 50):6.1f} {np.percentile(ms, 95):5.1f}"
        print(row)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--url", default="http://localhost:8796")
    r.add_argument("--out", required=True)
    r.add_argument("--tasks", default=str(TASKS))
    p = sub.add_parser("report")
    p.add_argument("dump")
    p.add_argument("--tasks", default=str(TASKS))
    p.add_argument("--grep-field", default="grep", help="task field to grep for when it has one (grep, grep_guess)")
    p.add_argument("--grep-data", help="index to grep instead (one without linked worktrees: grep in the checkout)")
    a = ap.parse_args()
    {"run": cmd_run, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    main()
