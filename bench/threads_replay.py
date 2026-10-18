"""Replay the last days of work to tune the thread linker (pensieve/threads.py, docs/threads.md).

  PENSIEVE_DATA=<a copy of ~/.pensieve> .venv/bin/python bench/threads_replay.py build --days 30 --step 12
  .venv/bin/python bench/threads_replay.py label     # a judge model: do two snapshots pursue the same goal?
  .venv/bin/python bench/threads_replay.py eval      # members only, goals only, both: wrong joins and wrong splits
  .venv/bin/python bench/threads_replay.py run       # the linker over every build: threads and events

build: the tasks as of every `step` hours (work.tasks_at), each one a snapshot (members, goal text, its embedding).
Pairs: a snapshot that is new in a build, with each snapshot of the build before that shares a member or has a near
goal in the same repo. A pair whose snapshot did not change is not a decision and is left out.
Out: bench/out/threads/ (builds.jsonl, vecs.npy, pairs.jsonl, labels.jsonl)."""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
OUT = Path(__file__).parent / "out" / "threads"


class _Store:  # what work._tasks needs from the index: its database and lock
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()


def build(a):
    from pensieve import config, indexer, threads, work
    store = _Store(config.DATA_DIR / "pensieve.db")
    now = time.time()
    builds, texts = [], []
    for k in range(int(a.days * 24 / a.step), -1, -1):
        until = now - k * a.step * 3600
        ts = [t for t in work.tasks_at(store, until, 30)
              if t["files"] or t["branches"] or t["prs"] or t["keys"] or t["commits"]]  # a question alone is not a thread
        snaps = []
        for t in ts:
            m = threads.members(t)
            snaps.append(dict(snapshot=threads.snapshot_id(m), members=m, goal=threads.goal_text(t), task=t["id"],
                              title=t["title"], repos=t["repos"], vec=len(texts)))
            texts.append(snaps[-1]["goal"])
        builds.append(dict(at=until, snaps=snaps))
        print(f"{time.strftime('%m-%d %H:%M', time.localtime(until))}: {len(snaps)} snapshots", file=sys.stderr)
    vecs = np.asarray(indexer.embed(texts), dtype=np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
    OUT.mkdir(parents=True, exist_ok=True)
    np.save(OUT / "vecs.npy", vecs)
    (OUT / "builds.jsonl").write_text("".join(json.dumps(b) + "\n" for b in builds))
    pairs, seen = [], set()
    for prev, cur in zip(builds, builds[1:]):
        old = {s["snapshot"] for s in prev["snaps"]}
        for s in cur["snaps"]:
            if s["snapshot"] in old:
                continue
            for p in prev["snaps"]:
                ov, strong = threads.overlap(s["members"], p["members"])
                sim = float(vecs[s["vec"]] @ vecs[p["vec"]])
                near = sim >= 0.6 and set(s["repos"]) & set(p["repos"])
                key = (p["snapshot"], s["snapshot"])
                if (ov > 0 or near) and key not in seen:
                    seen.add(key)
                    pairs.append(dict(a=p["snapshot"], b=s["snapshot"], at=cur["at"], overlap=round(ov, 3), strong=strong,
                                      sim=round(sim, 3), a_title=p["title"], b_title=s["title"], a_goal=p["goal"],
                                      b_goal=s["goal"], a_members=_brief(p["members"]), b_members=_brief(s["members"])))
    (OUT / "pairs.jsonl").write_text("".join(json.dumps(p) + "\n" for p in pairs))
    print(f"{len(builds)} builds, {len(texts)} snapshots, {len(pairs)} pairs to decide", file=sys.stderr)


def _brief(m):
    """Members for the judge: tickets, PRs and branches, and how many sessions, commits and files."""
    strong = [k for k in m if k.split(":")[0] in ("ticket", "pr", "branch")]
    count = {k: sum(1 for x in m if x.startswith(k + ":")) for k in ("session", "commit", "file")}
    return strong[:8] + [f"{n} {k}s" for k, n in count.items() if n]


JUDGE = """Two snapshots of a developer's work, a day or less apart. A snapshot is what one piece of work looked like at
one time: its title, what it was about, and its tickets, pull requests and branches.

Snapshot A: {a_title}
About: {a_goal}
Members: {a_members}

Snapshot B: {b_title}
About: {b_goal}
Members: {b_members}

Do A and B pursue the same goal: one outcome that someone works toward, where B can be a later stage of A (a fix on
the same PR, the next step of the same change)? Different goals that share a branch, a file or a session are not the
same goal. Reply with only a JSON object: {{"same": true or false, "confidence": 0 to 1, "why": "a few words"}}"""


def label(a):
    pairs = [json.loads(l) for l in (OUT / "pairs.jsonl").read_text().splitlines()]
    path = OUT / "labels.jsonl"
    have = {(x["a"], x["b"]) for x in map(json.loads, path.read_text().splitlines())} if path.exists() else set()
    todo = [p for p in pairs if (p["a"], p["b"]) not in have][:a.limit]
    print(f"labelling {len(todo)} of {len(pairs)} pairs", file=sys.stderr)

    def one(p):
        prompt = JUDGE.format(**{k: (", ".join(v) if isinstance(v, list) else v) for k, v in p.items()})
        r = subprocess.run(["claude", "-p", prompt, "--model", a.judge, "--no-session-persistence", "--tools", "",
                            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", ""],
                           capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL, cwd=tempfile.gettempdir())
        try:
            t = r.stdout
            return dict(a=p["a"], b=p["b"], **json.loads(t[t.index("{"):t.rindex("}") + 1]))
        except ValueError:
            return None
    with ThreadPoolExecutor(a.jobs) as ex, path.open("a") as f:
        for x in ex.map(one, todo):
            if x:
                f.write(json.dumps(x) + "\n")
                f.flush()


def _decide(p, rule, join, goal):
    linked = p["strong"] or p["overlap"] >= join
    if rule == "members":
        return linked
    if rule == "goals":
        return p["sim"] >= goal
    return linked and p["sim"] >= goal  # both: the members join, the goal can split


def eval_(a):
    pairs = {(p["a"], p["b"]): p for p in map(json.loads, (OUT / "pairs.jsonl").read_text().splitlines())}
    labels = [x for x in map(json.loads, (OUT / "labels.jsonl").read_text().splitlines()) if (x["a"], x["b"]) in pairs]
    sure = [x for x in labels if x.get("confidence", 1) >= a.min_conf]
    same = sum(1 for x in sure if x["same"])
    print(f"{len(labels)} labelled pairs, {len(sure)} with confidence >= {a.min_conf}: {same} same goal, {len(sure) - same} different\n")
    print(f"{'rule':<9}{'join':>6}{'goal':>6}{'right':>8}{'wrong join':>12}{'wrong split':>13}{'F1 same':>9}")
    best = {}
    for rule in ("members", "goals", "both"):
        rows = []
        for join in ((0.1, 0.2, 0.3, 0.4, 0.5, 0.6) if rule != "goals" else (0,)):
            for goal in ((0.3, 0.4, 0.5, 0.6, 0.7, 0.8) if rule != "members" else (0,)):
                tp = fp = fn = tn = 0
                for x in sure:
                    d = _decide(pairs[(x["a"], x["b"])], rule, join, goal)
                    tp, fp, fn, tn = tp + (d and x["same"]), fp + (d and not x["same"]), fn + (not d and x["same"]), tn + (not d and not x["same"])
                f1 = 2 * tp / max(2 * tp + fp + fn, 1)
                rows.append((f1, join, goal, (tp + tn) / max(len(sure), 1), fp, fn))
        rows.sort(reverse=True)
        best[rule] = rows[0]
        for f1, join, goal, acc, fp, fn in rows[:3]:
            print(f"{rule:<9}{join:>6}{goal:>6}{acc:>8.2f}{fp:>12}{fn:>13}{f1:>9.2f}")
    f1, join, goal = best["both"][:3]
    for s in (0.8, 0.85, 0.9):  # a split with no shared member: would the linker suggest the link?
        miss = [x for x in sure if x["same"] and not _decide(pairs[(x["a"], x["b"])], "both", join, goal)]
        hit = sum(1 for x in miss if pairs[(x["a"], x["b"])]["overlap"] == 0 and pairs[(x["a"], x["b"])]["sim"] >= s)
        noise = sum(1 for x in sure if not x["same"] and pairs[(x["a"], x["b"])]["overlap"] == 0 and pairs[(x["a"], x["b"])]["sim"] >= s)
        print(f"suggest at {s}: {hit} of {len(miss)} wrong splits suggested, {noise} suggestions for different goals")
    (OUT / "best.json").write_text(json.dumps({k: dict(join=v[1], goal=v[2], f1=v[0]) for k, v in best.items()}))


def run(a):
    from pensieve import threads
    vecs = np.load(OUT / "vecs.npy")
    best = json.loads((OUT / "best.json").read_text())["both"] if (OUT / "best.json").exists() else {}
    rules = threads.Rules(join=a.join or best.get("join", 0.3), same_goal=a.goal or best.get("goal", 0.5))
    linker = threads.Linker(rules)
    names = {}
    for b in map(json.loads, (OUT / "builds.jsonl").read_text().splitlines()):
        snaps = [dict(s, vec=vecs[s["vec"]]) for s in b["snaps"]]
        out = linker.step(snaps, b["at"])
        for s in b["snaps"]:
            names.setdefault(out[s["snapshot"]], s["title"])
    ev = {}
    for e in linker.events:
        ev[e["event"]] = ev.get(e["event"], 0) + 1
    live = [t for t in linker.threads.values() if not t.merged_into]
    print(f"rules: join {rules.join}, same goal {rules.same_goal}")
    print(f"{len(linker.threads)} threads ({len(live)} not merged); events: {ev}")
    many = sorted(live, key=lambda t: -len(t.snapshots))[:8]
    print("\nthreads with the most snapshots:")
    for t in many:
        print(f"  {t.id} {len(t.snapshots):>3} snapshots  {names.get(t.id, '')[:70]}")
    for kind in ("fork", "split", "merge", "suggest"):
        xs = [e for e in linker.events if e["event"] == kind][:4]
        if xs:
            print(f"\n{kind}:")
            for e in xs:
                other = e.get("parent") or e.get("other")
                print(f"  {names.get(e['thread'], '')[:50]!r}  <-  {names.get(other, '')[:50]!r}" + (f"  sim {e['sim']}" if "sim" in e else ""))


def backup(a):
    """When to back up a thread's card: what each policy would save over the replay. A change is a new snapshot of a
    thread; it is structural when a ticket, PR, branch, commit or session came or went (files alone don't count), and
    textual when only the goal text moved."""
    from pensieve import threads
    vecs = np.load(OUT / "vecs.npy")
    linker, last = threads.Linker(), {}  # thread -> (members, vec) at its last build
    changes = []  # (thread, hours since the thread's last change, structural, goal distance)
    seen_at = {}
    for b in map(json.loads, (OUT / "builds.jsonl").read_text().splitlines()):
        snaps = [dict(s, vec=vecs[s["vec"]]) for s in b["snaps"]]
        out = linker.step(snaps, b["at"])
        for s in snaps:
            tid = out[s["snapshot"]]
            if tid in last:
                m0, v0 = last[tid]
                strong0 = {k for k in m0 if not k.startswith("file:")}
                strong1 = {k for k in s["members"] if not k.startswith("file:")}
                dist = 1 - float(v0 @ s["vec"])
                if m0.keys() != s["members"].keys() or dist > 1e-4:
                    changes.append((tid, (b["at"] - seen_at.get(tid, b["at"])) / 3600, strong0 != strong1, dist))
                    seen_at[tid] = b["at"]
            else:
                seen_at[tid] = b["at"]
            last[tid] = (s["members"], s["vec"])
    builds = len(list((OUT / "builds.jsonl").read_text().splitlines()))
    active = sum(1 for _ in last)
    structural = [c for c in changes if c[2]]
    print(f"{builds} builds, {active} threads, {len(changes)} changes: {len(structural)} structural, "
          f"{len(changes) - len(structural)} text or files only\n")
    print("goal distance of the changes (1 - cosine):")
    for q in (50, 75, 90, 99):
        print(f"  p{q}: all {np.percentile([c[3] for c in changes], q):.3f}   structural "
              f"{np.percentile([c[3] for c in structural] or [0], q):.3f}")
    print("\nbackups that each policy takes (every 12 h build):")
    print(f"  time only (each build, each thread seen in the last 3 days): "
          f"{sum(1 for b in map(json.loads, (OUT / 'builds.jsonl').read_text().splitlines()) for s in b['snaps'])}")
    for d in (0.02, 0.05, 0.1):
        text = sum(1 for c in changes if not c[2] and c[3] >= d)
        print(f"  structural change, or goal distance >= {d}: {len(structural) + text} "
              f"({text} from text alone; misses {sum(1 for c in changes if not c[2] and c[3] < d)} small text changes)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "label", "eval", "run", "backup"])
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--step", type=float, default=12, help="hours between builds")
    ap.add_argument("--judge", default="sonnet")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--min-conf", type=float, default=0.7)
    ap.add_argument("--join", type=float, default=0)
    ap.add_argument("--goal", type=float, default=0)
    a = ap.parse_args()
    {"build": build, "label": label, "eval": eval_, "run": run, "backup": backup}[a.cmd](a)


if __name__ == "__main__":
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    main()
