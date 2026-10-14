"""More code-finding tasks for a repo, with every query style, in bench/out/<repo>/tasks-speed.jsonl. Files used by
tasks-v2.jsonl are left out, so the two sets never share an answer. Same prompts and checks as agent_search.py gen
(desc, query) and exact_tasks.py (ident, literal, mixed).

  PENSIEVE_DATA=<index> .venv/bin/python bench/more_tasks.py --repo ~/code/my-app -n 35
"""
import argparse
import json
import random
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from agent_search import DB, DESCRIBE, SKIP, SRC_EXT, claude, leaks, out_dir  # noqa: E402
from exact_tasks import MIXED, pick_ident, pick_literal  # noqa: E402
from retrieval import SYMBOL  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("-n", type=int, default=35)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--append", action="store_true", help="add to tasks-speed.jsonl instead of replacing it")
    a = ap.parse_args()
    root = str(Path(a.repo).expanduser().resolve())
    out = out_dir(root) / "tasks-speed.jsonl"
    have = [json.loads(ln) for ln in open(out)] if a.append and out.exists() else []
    used = {json.loads(ln)["file"] for ln in open(out_dir(root) / "tasks-v2.jsonl")} | {t["file"] for t in have}
    db = sqlite3.connect(DB)
    rows = db.execute("SELECT id, path, start, end, text FROM code_chunks WHERE repo=? AND wt=''", (root,)).fetchall()
    files, defs_count, defs_in = {}, {}, {}
    for cid, path, s, e, text in rows:
        for m in SYMBOL.finditer(text):
            n = m.group(1).split(".")[-1]
            defs_count[n] = defs_count.get(n, 0) + 1
            defs_in.setdefault(n, set()).add(path)
        if path not in used and Path(path).suffix in SRC_EXT and not SKIP.search("/" + path) and 400 <= len(text) <= 4000:
            files.setdefault(path, []).append((cid, s, e, text))
    print(f"{len(rows)} chunks, {len(files)} eligible files not in tasks-v2", file=sys.stderr)
    rng = random.Random(a.seed)
    paths = sorted(files)
    rng.shuffle(paths)
    picks = [(p, rng.choice(files[p])) for p in paths[:int(a.n * 1.4) + 5]]

    def make(pick):
        path, (cid, s, e, text) = pick
        r = claude(DESCRIBE.format(code=text), "/private/tmp", "sonnet", "", "")
        m = re.search(r"\{.*\}", r["result"], re.S)
        try:
            d = json.loads(m.group(0)) if m else None
        except ValueError:
            d = None
        if not d or not d.get("desc") or not d.get("query") or leaks(d["desc"] + " " + d["query"], text):
            return None
        t = dict(repo=root, chunk=cid, file=path, lines=[s, e], desc=d["desc"].strip(), query=d["query"].strip())
        t["ident"] = pick_ident(text, path, defs_count)
        t["ident_files"] = sorted(defs_in.get(t["ident"], set()) | {path}) if t["ident"] else None
        t["literal"] = pick_literal(text)
        q = claude(MIXED.format(code=text, avoid=t["ident"] or "(none)"), "/private/tmp", "sonnet", "", "")["result"]
        q = q.strip().strip('"')
        t["mixed"] = q if q and len(q.split()) <= 14 else None
        return t

    with ThreadPoolExecutor(a.jobs) as ex:
        made = [t for t in ex.map(make, picks) if t][:a.n]
    name = Path(root).name
    for i, t in enumerate(made, start=len(have)):
        t["id"], t["split"] = f"{name}-s{i + 1:03d}", "speed"
    made = have + made
    out.write_text("".join(json.dumps(t) + "\n" for t in made))
    print(f"{name}: {len(made)} tasks, ident {sum(bool(t['ident']) for t in made)}, "
          f"literal {sum(bool(t['literal']) for t in made)}, mixed {sum(bool(t['mixed']) for t in made)}")


if __name__ == "__main__":
    main()
