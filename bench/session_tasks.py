"""Realistic tasks from past agent sessions: the session's first prompt as the query, and the repo files it went
on to edit as the answers (any one counts). Writes bench/out/sessions.jsonl."""
import hashlib
import json
import re
import sqlite3
from pathlib import Path

DB = Path.home() / ".pensieve" / "pensieve.db"
OUT = Path(__file__).parent / "out"

db = sqlite3.connect(DB)
repos = {r for (r,) in db.execute("SELECT DISTINCT repo FROM code_chunks WHERE wt=''")}
indexed = {}
for repo, path in db.execute("SELECT DISTINCT repo, path FROM code_chunks WHERE wt=''"):
    indexed.setdefault(repo, set()).add(path)
worktrees = dict(db.execute("SELECT path, repo FROM worktrees")) if db.execute(
    "SELECT name FROM sqlite_master WHERE name='worktrees'").fetchone() else {}


def locate(p):
    """(repo root, repo-relative path) for an absolute path in a repo's main checkout or one of its worktrees."""
    for base, repo in [*worktrees.items(), *((r, r) for r in repos)]:
        if p.startswith(base + "/"):
            return repo, p[len(base) + 1:]
    return None, None


tasks = []
for sid, project in db.execute("SELECT id, project FROM sessions"):
    first = db.execute("SELECT text FROM chunks WHERE session_id=? ORDER BY id LIMIT 1", (sid,)).fetchone()
    if not first:
        continue
    m = re.match(r"User:\s*(.*?)(?:\n\nAssistant:|\Z)", first[0], re.S)
    prompt = (m.group(1) if m else "").strip()
    if len(prompt) < 40 or prompt.startswith(("<", "/", "Caveat")):
        continue
    hits = {}
    for (p,) in db.execute("SELECT path FROM session_files WHERE session_id=? AND edited=1", (sid,)):
        repo, rel = locate(p)
        if repo and rel in indexed.get(repo, ()):
            hits.setdefault(repo, set()).add(rel)
    if not hits:
        continue
    repo, files = max(hits.items(), key=lambda kv: len(kv[1]))
    tasks.append(dict(id="s-" + hashlib.sha1(sid.encode()).hexdigest()[:8], repo=repo, query=prompt[:600], files=sorted(files)))

(OUT / "sessions.jsonl").write_text("".join(json.dumps(t) + "\n" for t in tasks))
print(len(tasks), "session tasks;", {Path(r).name: sum(t["repo"] == r for t in tasks) for r in {t["repo"] for t in tasks}})
