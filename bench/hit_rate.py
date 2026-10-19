"""Hit rate of real searches (ROADMAP B6): after each logged MCP search, did the agent then open or edit a file that
the search had returned? Matches each search in ~/.pensieve/usage.db to the same call in the agent's Claude Code
transcript (same query, a few seconds apart), and looks at the agent's next file tools in that session.

  .venv/bin/python bench/hit_rate.py [--next 5]
"""
import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

FILE_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "mcp__pensieve__read"}


def transcript_calls():
    """Every tool call in the Claude Code transcripts: (time, session, tool, input), in order per session."""
    out = {}
    for f in (Path.home() / ".claude" / "projects").glob("*/*.jsonl"):
        calls = []
        for line in open(f, errors="replace"):
            if '"tool_use"' not in line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = r.get("timestamp")
            for c in (r.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and ts:
                    t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    calls.append((t, c.get("name", ""), c.get("input") or {}))
        if calls:
            out[f.stem] = calls
    return out


def opened(name, inp, cwd):
    """The files a later tool call opened or changed (absolute), or the ids it read."""
    if name == "mcp__pensieve__read":
        return {i for i in str(inp.get("id", "")).replace(",", " ").split()}
    p = inp.get("file_path") or inp.get("path")
    return {p if str(p).startswith("/") else str(Path(cwd) / p)} if p else set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--next", type=int, default=5, help="how many of the agent's next tool calls to look at")
    a = ap.parse_args()
    v2 = Path.home() / ".pensieve" / "v2" / "store.db"
    if v2.exists():  # storage v2: the calls are in store.db
        db = sqlite3.connect(f"file:{v2}?mode=ro", uri=True)
        sql = "SELECT at, cwd, args, results FROM usage_calls WHERE tool='search' AND client='claude-code'"
    else:
        db = sqlite3.connect(f"file:{Path.home() / '.pensieve' / 'usage.db'}?mode=ro", uri=True)
        sql = "SELECT ts, cwd, args, results FROM calls WHERE tool='search' AND client='claude-code'"
    searches = db.execute(sql).fetchall()
    sessions = transcript_calls()
    rows = []
    for ts, cwd, args, results in searches:
        q = (json.loads(args or "{}") or {}).get("query", "")
        hits = json.loads(results or "[]")
        ids, paths = {h[0] for h in hits}, {h[1] for h in hits if h[1]}
        found = None
        for calls in sessions.values():
            for i, (t, name, inp) in enumerate(calls):
                if name == "mcp__pensieve__search" and inp.get("query") == q and abs(t - ts) < 30:
                    found = calls[i + 1:i + 1 + a.next]
                    break
            if found is not None:
                break
        if found is None:
            rows.append(("no transcript", q, len(hits)))
            continue
        used = set().union(*[opened(n, inp, cwd) for _, n, inp in found]) if found else set()
        # a shell command that names a hit's file (sed -n, grep -n in it) uses the hit too
        # by the file's last two path parts: the agent may read its own checkout's copy, or use a relative path
        tails = {"/".join(Path(p).parts[-2:]) for p in paths}
        shell = " ".join(str(inp.get("command", "")) for _, n, inp in found if n == "Bash")
        named = any(t and t in shell for t in tails) or any("/".join(Path(u).parts[-2:]) in tails for u in used)
        grep = any(n in ("Grep", "Glob") or (n == "Bash" and "grep" in str(inp.get("command", ""))) for _, n, inp in found)
        verdict = ("no results" if not hits else "used a hit" if used & (ids | paths) or named
                   else "grepped elsewhere" if grep else "used another file" if used else "nothing opened")
        rows.append((verdict, q, len(hits)))
    from collections import Counter
    n = Counter(v for v, *_ in rows)
    print(f"{len(rows)} searches by Claude Code in the usage log")
    for v, k in n.most_common():
        print(f"  {v:<18} {k}")
    for v, q, h in rows:
        if v in ("grepped elsewhere", "used another file", "no results"):
            print(f"  [{v}] {q[:90]} ({h} hits)")


if __name__ == "__main__":
    main()
