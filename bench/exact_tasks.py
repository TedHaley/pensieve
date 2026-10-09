"""Add exact-match query styles to the generated tasks (bench/out/*/tasks-v2.jsonl), so the benchmark covers
what grep is good at as well as search by meaning:

  ident    a name the chunk defines (function, class, ...), alone: the answer is the defining file
  literal  a distinctive string from the chunk (message, log line), typed without quotes
  mixed    a short query mixing one identifier with plain words (written by Claude)

Tasks without a usable symbol or string just lack that style.
"""
import json
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from agent_search import DB, claude  # noqa: E402
from retrieval import SYMBOL  # noqa: E402

OUT = Path(__file__).parent / "out"
STRING = re.compile(r"""(?<![\w])(["'`])((?:(?!\1).){12,90})\1""")

MIXED = """Here is a chunk of code from a repository.

<code>
{code}
</code>

Write a short search query (at most 10 words) a developer might type to find this code: exactly ONE identifier
that appears in the code (a function, class, variable or table name, written exactly as in the code) plus a few
plain words about what it does. Do not use the name {avoid}. Reply with only the query."""


def pick_ident(text, path, defs_count):
    """The chunk's most distinctive defined name: long enough to mean something, defined in as few files as possible."""
    names = [m.group(1).split(".")[-1] for m in SYMBOL.finditer(text)]
    names = [n for n in names if len(n) >= 6 and not n.isupper() and n.lower() not in Path(path).stem.lower()]
    return min(names, key=lambda n: (defs_count.get(n, 0), -len(n)), default=None)


def pick_literal(text):
    best = None
    for m in STRING.finditer(text):
        s = m.group(2).strip()
        if len(s.split()) >= 3 and not re.search(r"[{}$%<>\\]|https?:|^\W", s) and (best is None or len(s) > len(best)):
            best = s
    return best


def main():
    db = sqlite3.connect(DB)
    for f in sorted(OUT.glob("*/tasks-v2.jsonl")):
        tasks = [json.loads(l) for l in f.read_text().splitlines()]
        root = tasks[0]["repo"]
        defs_count, defs_in = {}, {}
        for path, text in db.execute("SELECT path, text FROM code_chunks WHERE repo=? AND wt=''", (root,)):
            for m in SYMBOL.finditer(text):
                n = m.group(1).split(".")[-1]
                defs_count[n] = defs_count.get(n, 0) + 1
                defs_in.setdefault(n, set()).add(path)
        texts = {t["id"]: db.execute("SELECT text FROM code_chunks WHERE id=?", (t["chunk"],)).fetchone()[0] for t in tasks}

        def mixed(t):
            if t.get("mixed"):
                return t["mixed"]
            r = claude(MIXED.format(code=texts[t["id"]], avoid=t.get("ident") or "(none)"), "/private/tmp", "sonnet", "", "")
            q = r["result"].strip().strip('"')
            return q if q and len(q.split()) <= 14 else None

        for t in tasks:
            t["ident"] = pick_ident(texts[t["id"]], t["file"], defs_count)
            # every file defining the name is a right answer to "which file defines it"
            t["ident_files"] = sorted(defs_in.get(t["ident"], set()) | {t["file"]}) if t["ident"] else None
            t["literal"] = pick_literal(texts[t["id"]])
        with ThreadPoolExecutor(8) as ex:
            for t, q in zip(tasks, ex.map(mixed, tasks)):
                t["mixed"] = q
        f.write_text("".join(json.dumps(t) + "\n" for t in tasks))
        print(f"{f.parent.name}: {len(tasks)} tasks, ident {sum(bool(t['ident']) for t in tasks)}, "
              f"literal {sum(bool(t['literal']) for t in tasks)}, mixed {sum(bool(t['mixed']) for t in tasks)}")


if __name__ == "__main__":
    main()
