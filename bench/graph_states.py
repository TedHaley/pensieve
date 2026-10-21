"""Threads around sessions, with the state of each sink.

  .venv/bin/python bench/graph_states.py     # reads relations.json, writes threads.json and prints a summary

A session is the centre of its work. Sessions that write to the same PR or branch join one thread, so a thread is a
connected group of sessions. Each sink's state comes from GitHub (gh), cached in sink_states_cache.json:
  PR      MERGED, CLOSED, or OPEN
  branch  DELETED, MERGED, EXISTS, or OPEN (see pensieve/sink_status.py)
Tickets have no writes yet (no extractor reads Jira writes), so they are not sinks here.

Thread states:
  closed   every sink is done (merged, closed or deleted)
  open     a sink is open and the thread was written to in the last RECENT_DAYS
  stale    a sink is open, and the thread has been quiet for longer than RECENT_DAYS
  unknown  a sink could not be checked
A session with no write is a thought, not a thread."""
import hashlib
import json
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "bench" / "out" / "graph"
RECENT_DAYS = 3
DAY = 86400


def token(user: str) -> str:
    r = subprocess.run(["gh", "auth", "token", "--user", user], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


TOKENS = {}  # gh's accounts: the active one first, then TedHaley for repos the active one cannot read


def _env(user: str | None) -> dict:
    import os
    if not user:
        return dict(os.environ)
    if user not in TOKENS:
        TOKENS[user] = token(user)
    return {**os.environ, "GH_TOKEN": TOKENS[user]}


def pr_state(key: str) -> str:
    repo, num = key.rsplit("#", 1)
    for user in (None, "TedHaley"):
        r = subprocess.run(["gh", "pr", "view", num, "-R", repo, "--json", "state"], capture_output=True, text=True,
                           timeout=60, env=_env(user))
        if r.returncode == 0:
            return json.loads(r.stdout)["state"]
    return "UNKNOWN"


def branch_state(key: str) -> str:
    from pensieve.sink_status import branch_status
    if "@" not in key:
        return "UNKNOWN"
    repo, branch = key.split("@", 1)
    for user in (None, "TedHaley"):
        state = branch_status(repo, branch, env=_env(user))
        if state != "UNKNOWN":
            return state
    return "UNKNOWN"


def main():
    g = json.load(open(OUT / "relations.json"))
    written = [r for r in g["relations"] if r["strength"] == "write"]
    nodes = {n["id"]: n for n in g["nodes"]}
    cache_path = OUT / "sink_states_cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    sinks = sorted({r["target"] for r in written})
    for i, t in enumerate(sinks, 1):
        if t in cache:
            continue
        kind, key = t.split(":", 1)
        if kind == "pr":
            cache[t] = pr_state(key)
        elif kind == "branch":
            cache[t] = branch_state(key)
        else:
            cache[t] = "UNKNOWN"
        if i % 20 == 0:
            cache_path.write_text(json.dumps(cache, indent=1))
    cache_path.write_text(json.dumps(cache, indent=1))

    by_sink = defaultdict(set)
    for r in written:
        by_sink[r["target"]].add(r["source"])
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for sessions in by_sink.values():
        ss = sorted(sessions)
        for s in ss[1:]:
            parent[find(s)] = find(ss[0])

    sessions_of = defaultdict(set)
    sinks_of = defaultdict(set)
    for r in written:
        root = find(r["source"])
        sessions_of[root].add(r["source"])
        sinks_of[root].add(r["target"])

    now = time.time()
    done_states = {"MERGED", "CLOSED", "DELETED"}
    threads = []
    for root in sessions_of:
        ss = sessions_of[root]
        sk = sorted(sinks_of[root])
        states = {t: cache.get(t, "UNKNOWN") for t in sk}
        last = max((nodes.get(s, {}).get("written", 0) for s in ss), default=0)
        quiet = (now - last) / DAY
        if all(v in done_states for v in states.values()):
            state = "closed"
        elif any(v in ("OPEN", "EXISTS") for v in states.values()):
            state = "open" if quiet <= RECENT_DAYS else "stale"
        else:
            state = "unknown"
        threads.append(dict(id="t" + hashlib.sha1("|".join(sorted(ss)).encode()).hexdigest()[:8], state=state, sessions=sorted(ss),
                            sinks=[dict(target=t, state=states[t]) for t in sk],
                            quiet_days=round(quiet, 1),
                            titles=[nodes.get(s, {}).get("label", s) for s in sorted(ss)]))
    threads.sort(key=lambda t: (t["state"] != "open", t["state"] != "stale", t["quiet_days"]))
    (OUT / "threads.json").write_text(json.dumps(threads, indent=1))

    thoughts = sum(1 for s, n in nodes.items() if n["kind"] == "session" and s not in {r["source"] for r in written})
    counts = defaultdict(int)
    for t in threads:
        counts[t["state"]] += 1
    print(f"threads {len(threads)}: {dict(counts)}")
    print(f"thoughts (sessions with no write): {thoughts}")
    print(f"sinks checked: {len(sinks)}: {dict(Counter(cache.get(t, 'UNKNOWN') for t in sinks))}")
    print(f"written {OUT / 'threads.json'}")


if __name__ == "__main__":
    main()
