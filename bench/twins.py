"""Mark the agent tasks whose answer has a near-identical twin: code in another file that the task's description
can't tell apart from the answer (a vendored copy, a duplicate "x (1).sql", the same pipeline for two domains).
Writes `twins` (the twin files) into each task, so reports can leave those tasks out.

  PENSIEVE_DATA=<copy> .venv/bin/python bench/twins.py --tasks tasks-v2.jsonl
"""
import argparse
import difflib
import json
import os
import sqlite3
from pathlib import Path

import numpy as np

OUT = Path(__file__).parent / "out"
DB = Path(os.environ.get("PENSIEVE_DATA", Path.home() / ".pensieve")).expanduser() / "pensieve.db"
NEAR = 0.9  # the same threshold as the search tool's near-copy note (pensieve/mcp_server.py NEAR_COPY)


def twins_of(db, root, t, k=20):
    """Files other than the answer with a chunk at least NEAR alike to the answer's chunk (among its k nearest)."""
    lo, hi = t["lines"]
    rows = db.execute("SELECT id, text, vec, start, end FROM code_chunks WHERE repo=? AND wt='' AND path=?",
                      (root, t["file"])).fetchall()
    rows = [r for r in rows if r[3] <= hi and r[4] >= lo]
    if not rows:
        return None  # the answer is not in this index
    best = max(rows, key=lambda r: min(r[4], hi) - max(r[3], lo))
    q = np.frombuffer(best[2], np.float16).astype(np.float32)
    ids, paths, texts, V = [], [], [], []
    for i, p, tx, v in db.execute("SELECT id, path, text, vec FROM code_chunks WHERE repo=? AND wt='' AND path!=?",
                                  (root, t["file"])):
        ids.append(i), paths.append(p), texts.append(tx), V.append(np.frombuffer(v, np.float16))
    V = np.stack(V).astype(np.float32)
    top = np.argsort(-(V @ q))[:k]
    return sorted({paths[i] for i in top if difflib.SequenceMatcher(None, best[1], texts[i]).ratio() >= NEAR})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default="tasks-v2.jsonl")
    a = ap.parse_args()
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    for f in sorted(OUT.glob(f"*/{a.tasks}")):
        tasks = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        n = 0
        for t in tasks:
            tw = twins_of(db, t["repo"], t)
            if tw is not None:
                t["twins"] = tw
                n += bool(tw)
                if tw:
                    print(f"{t['id']}: {t['file']} ~ {', '.join(tw)}")
        f.write_text("".join(json.dumps(t) + "\n" for t in tasks))
        print(f"{f.parent.name}: {n} of {len(tasks)} tasks have a near-identical twin")


if __name__ == "__main__":
    main()
