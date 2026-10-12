"""Queries where grep is weak, from an index of main checkouts (no linked worktrees), split into dev (tune on it)
and test (confirm only):

  ambiguous  a string from the code that appears in 2+ files: grep ranks by count, ties in any order
  runtime    a message as it shows up at runtime: the placeholders of a format string from the code filled in
             (f"User {id} not found" -> "User 4821 not found"). Grep for the whole message finds nothing; the
             `grep` field holds what an agent would grep instead: the longest fixed fragment.

  PENSIEVE_DATA=<copy> .venv/bin/python bench/grep_gaps.py [-n 400]     # -> bench/out/grep-gaps.jsonl
"""
import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from exact_tasks import pick_literal  # noqa: E402

OUT = Path(__file__).parent / "out"
QUOTED = re.compile(r"""(?<![\w])(["'`])((?:(?!\1)[^\n]){15,120})\1""")
HOLE = re.compile(r"\{[^{}\n]{0,40}\}|#\{[^{}\n]{1,40}\}|\$\{[^{}\n]{1,40}\}|%[-0-9.]*[sdrfi]|%\([a-z_]+\)[sdrf]")
WORDS = ["acme", "north-42", "pending", "us-east-1", "invoices", "jdoe", "beta", "orders_v2", "report.csv", "ingest"]


def split_of(q):
    return "test" if int(hashlib.sha1(q.encode()).hexdigest(), 16) % 2 else "dev"


def fill(template, rng):
    """The template with each placeholder replaced by a value a log line could carry."""
    def value(m):
        h = m.group(0)
        if h.endswith(("d", "i}")) or re.search(r"(count|num|id|size|len|total|n)\b", h, re.I):
            return str(rng.randint(2, 99999))
        if h.endswith("f"):
            return f"{rng.uniform(0, 100):.2f}"
        return rng.choice(WORDS)
    return HOLE.sub(value, template)


def guess(message):
    """What an agent without the source would grep: the longest part of the message without digits."""
    return max((p.strip(" :,.-=[]()#") for p in re.split(r"\d+", message)), key=len)


def fragments(template):
    return [f.strip(" :,.-=") for f in HOLE.split(template) if len(f.strip(" :,.-=")) >= 8]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=400, help="queries of each kind")
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()
    db = sqlite3.connect(Path(os.environ["PENSIEVE_DATA"]) / "pensieve.db")
    used = set()
    for f in sorted(OUT.glob("*/tasks-*.jsonl")) + [OUT / "queries-1000.jsonl"]:
        for ln in open(f):
            t = json.loads(ln)
            used.update(t.get("answers") or t.get("files") or [t.get("file")])
    rows = db.execute("SELECT repo, path, text FROM code_chunks").fetchall()
    by_repo = defaultdict(list)
    for repo, path, text in rows:
        by_repo[repo].append((path, (text or "").lower()))

    def files_with(repo, s):
        s = s.lower()
        return {p for p, tl in by_repo[repo] if s in tl}

    rng = random.Random(a.seed)
    rng.shuffle(rows)
    out, seen, per = [], set(), Counter()
    cap = a.n // len(by_repo) + 1
    for repo, path, text in rows:
        if path in used:
            continue
        text = text or ""
        if per["ambiguous", repo] < cap:
            s = pick_literal(text)
            if s and s.lower() not in seen:
                n = len(files_with(repo, s))
                if n >= 2:
                    seen.add(s.lower())
                    per["ambiguous", repo] += 1
                    out.append(dict(set="ambiguous", style="literal", bucket="exact", query=s, repo=repo,
                                    answers=[path], n_files=n))
        if per["runtime", repo] < cap:
            for m in QUOTED.finditer(text):
                tpl = m.group(2)
                frags = fragments(tpl)
                static = HOLE.sub(" ", tpl)
                if not HOLE.search(tpl) or not frags or len(re.findall(r"[A-Za-z]{2,}", static)) < 3:
                    continue
                q = fill(tpl, rng)
                if q.lower() in seen or files_with(repo, q):  # the filled message must not be in the code as such
                    continue
                g = max(frags, key=len)
                seen.add(q.lower())
                per["runtime", repo] += 1
                out.append(dict(set="runtime", style="literal", bucket="exact", query=q, repo=repo, answers=[path],
                                template=tpl, grep=g, grep_guess=guess(q), n_files=len(files_with(repo, g))))
                break
        if all(per[k, r] >= cap for k in ("ambiguous", "runtime") for r in by_repo):
            break
    for t in out:
        t["split"] = split_of(t["query"])
    dest = OUT / "grep-gaps.jsonl"
    dest.write_text("".join(json.dumps(t) + "\n" for t in out))
    print(f"{len(out)} queries -> {dest}: " + ", ".join(f"{k} {v}" for k, v in sorted(
        Counter((t["set"], t["split"]) for t in out).items())))


if __name__ == "__main__":
    main()
