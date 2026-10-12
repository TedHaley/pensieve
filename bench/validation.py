"""One validation set for every search strategy, from an index of main checkouts (no linked worktrees):

  question         plain words, no names from the code              (queries-1000.jsonl, style query)
  name             a function or class name                         (ident)
  name+words       one name plus a few plain words                  (mixed)
  string           a string from the code, typed without quotes      (literal, plus 600 more from random chunks)
  ambiguous        a string from the code that 2+ files contain     (grep-gaps.jsonl)
  runtime          a message with its format-string values filled in (grep-gaps.jsonl)

Each query has a split: dev (used to tune Pensieve) or test (never used to tune it).

  PENSIEVE_DATA=<copy> .venv/bin/python bench/validation.py         # -> bench/out/validation.jsonl
"""
import glob
import hashlib
import json
import os
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from exact_tasks import pick_literal  # noqa: E402

OUT = Path(__file__).parent / "out"
KIND = {"query": "question", "ident": "name", "mixed": "name+words", "literal": "string"}


def split_of(q):
    return "test" if int(hashlib.sha1(q.encode()).hexdigest(), 16) % 2 else "dev"


def strings(db, used, n=600, seed=7):
    """n strings picked from random code chunks (a quarter per repo), in files no other query uses."""
    rows = db.execute("SELECT repo, path, text FROM code_chunks").fetchall()
    by_repo = defaultdict(list)
    for repo, path, text in rows:
        by_repo[repo].append((path, (text or "").lower()))
    random.Random(seed).shuffle(rows)
    per, out, seen = Counter(), [], set()
    for repo, path, text in rows:
        if path in used or per[repo] >= n // len(by_repo):
            continue
        s = pick_literal(text or "")
        if not s or s.lower() in seen:
            continue
        seen.add(s.lower())
        per[repo] += 1
        out.append(dict(task=f"str-{len(out)}", style="literal", kind="string", query=s, repo=repo, answers=[path],
                        split=split_of(s), n_files=sum(s.lower() in tl for _, tl in by_repo[repo])))
        if len(out) >= n:
            break
    return out


def main():
    db = sqlite3.connect(Path(os.environ["PENSIEVE_DATA"]) / "pensieve.db")
    tuned = set()  # queries of the tasks Pensieve was tuned on (tasks-v2 dev split)
    for f in glob.glob(str(OUT / "*/tasks-v2.jsonl")):
        for ln in open(f):
            t = json.loads(ln)
            if t.get("split", "dev") == "dev":
                tuned.update(t.get(s) for s in KIND if t.get(s))
    base = [json.loads(ln) for ln in open(OUT / "queries-1000.jsonl")]
    for t in base:
        t.update(kind=KIND[t["style"]], split="dev" if t["query"] in tuned else "test")
    gaps = [json.loads(ln) for ln in open(OUT / "grep-gaps.jsonl")]
    for t in gaps:
        t["kind"] = t["set"]
    used = {a for t in base + gaps for a in t["answers"]}
    out = base + strings(db, used) + gaps
    (OUT / "validation.jsonl").write_text("".join(json.dumps(t) + "\n" for t in out))
    print(f"{len(out)} queries -> {OUT / 'validation.jsonl'}: " + ", ".join(
        f"{k} {v}" for k, v in sorted(Counter((t["kind"], t["split"]) for t in out).items())))


if __name__ == "__main__":
    main()
