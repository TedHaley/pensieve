"""Build threads from components: one thread per goal, goals matched by embedding (rule 17), with shared keys as support
(rules 9, 12). Reads bench/out/scenes/components.jsonl. Writes bench/out/threads/ for the configuration you choose.

  .venv/bin/python bench/threads_build.py sweep                 # try several goal thresholds and key caps
  .venv/bin/python bench/threads_build.py build --goal 0.78 --cap 8   # one configuration, written to threads.json

Goal text: the component's title and description, with process words removed (rule 16: "release", "merge", version
numbers name an act, not a goal). Matching: average-linkage merging. Two groups merge when the average similarity
between their components is at least --goal. A group never merges with one it cannot match on average, so one
similar pair does not chain two unrelated goals together.

Key joins (rule 12): a branch, PR or ticket shared by at most --cap components can join two components on its own.
Keys shared by more components are reported, not used. `main` and `master` are never used."""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = Path(__file__).parent / "out" / "threads"
COMPS = Path(__file__).parent / "out" / "scenes" / "components.jsonl"

PROCESS = re.compile(
    r"\b(releas\w*|merg\w*|commit\w*|push\w*|deploy\w*|pr|prs|pull request\w*|tag\w*|cut\w*|ships?|shipping|"
    r"bump\w*|version\w*|v\d[\d.]*|\d+(\.\d+)+)\b", re.I)
GENERIC_BRANCHES = {"main", "master", "develop", "dev"}


def goal_text(c: dict) -> str:
    text = f"{c['title']}. {c['description']}"
    return " ".join(PROCESS.sub(" ", text).split())


def embed(texts: list[str]):
    import numpy as np
    from pensieve import indexer
    vecs = np.asarray(indexer.embed(texts), dtype=np.float32)
    return vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9)


def key_joins(comps: list[dict], cap: int) -> dict[tuple[int, int], str]:
    """Pairs of components that share a usable key (branch, PR, ticket), with the key."""
    by_key = defaultdict(set)
    for i, c in enumerate(comps):
        if c.get("branch") and c["branch"] not in GENERIC_BRANCHES:
            by_key[("branch", c["branch"])].add(i)
        for link in c["links"]:
            if link["kind"] in ("pr", "ticket"):
                by_key[(link["kind"], link["key"])].add(i)
    joins, skipped = {}, Counter()
    for key, idx in by_key.items():
        if len(idx) < 2:
            continue
        if len(idx) > cap:
            skipped[key] = len(idx)
            continue
        idx = sorted(idx)
        for a in range(len(idx)):
            for b in range(a + 1, len(idx)):
                joins[(idx[a], idx[b])] = f"{key[0]} {key[1]}"
    return joins, skipped


def average_linkage(sim, n: int, goal: float, forced: list) -> list[list[int]]:
    """Greedy average-linkage. Pairs in `forced` (a shared key) merge first, whatever their similarity. Then the two
    groups with the highest average similarity merge, while that average is at least `goal`."""
    import numpy as np
    sums = np.array(sim, dtype=np.float64)  # sums[a, b]: total similarity between group a and group b
    counts = np.ones(n)
    members = {i: [i] for i in range(n)}
    active = np.ones(n, dtype=bool)

    def merge(a, b):
        sums[a, :] += sums[b, :]
        sums[:, a] = sums[a, :]
        counts[a] += counts[b]
        members[a] += members.pop(b)
        active[b] = False

    group_of = list(range(n))

    def root(x):
        while group_of[x] != x:
            x = group_of[x]
        return x

    for a, b in forced:
        ra, rb = root(a), root(b)
        if ra != rb:
            merge(ra, rb)
            group_of[rb] = ra
    while True:
        avg = sums / np.outer(counts, counts)
        avg[~active, :] = -np.inf
        avg[:, ~active] = -np.inf
        np.fill_diagonal(avg, -np.inf)
        flat = int(np.argmax(avg))
        a, b = divmod(flat, n)
        if not np.isfinite(avg[a, b]) or avg[a, b] < goal:
            break
        merge(a, b)
        group_of[b] = a
    return list(members.values())


BONUS = 0.10  # a shared usable key adds this to a pair's similarity; it never joins a pair on its own


def build(comps: list[dict], vecs, goal: float, cap: int, bonus: float = BONUS):
    import numpy as np
    n = len(comps)
    sim = vecs @ vecs.T
    joins, skipped = key_joins(comps, cap)
    for (a, b) in joins:
        sim[a, b] += bonus
        sim[b, a] += bonus
    groups = average_linkage(sim.tolist(), n, goal, [])
    return groups, joins, skipped


def summarize(comps, groups, joins, skipped, goal, cap):
    sizes = sorted((len(g) for g in groups), reverse=True)
    multi = [g for g in groups if len(g) > 1]
    sessions = [len({comps[i]["session_id"] for i in g}) for g in multi]
    return dict(goal=goal, cap=cap, threads=len(groups), single=len(groups) - len(multi),
                largest=sizes[:6], multi=len(multi),
                cross_session=sum(1 for s in sessions if s > 1),
                key_joins=len(joins), keys_skipped=len(skipped))


def cmd_sweep(a):
    comps = [json.loads(l) for l in COMPS.read_text().splitlines()]
    texts = [goal_text(c) for c in comps]
    vecs = embed(texts)
    print(f"components {len(comps)}")
    for cap in a.caps:
        for goal in a.goals:
            groups, joins, skipped = build(comps, vecs, goal, cap)
            s = summarize(comps, groups, joins, skipped, goal, cap)
            print(f"goal {goal:.2f} cap {cap:3d} threads {s['threads']:4d} single {s['single']:4d} "
                  f"multi {s['multi']:3d} cross-session {s['cross_session']:3d} key joins {s['key_joins']:4d} "
                  f"keys skipped {s['keys_skipped']:3d} largest {s['largest']}")


def cmd_build(a):
    comps = [json.loads(l) for l in COMPS.read_text().splitlines()]
    texts = [goal_text(c) for c in comps]
    vecs = embed(texts)
    groups, joins, skipped = build(comps, vecs, a.goal, a.cap)
    OUT.mkdir(parents=True, exist_ok=True)
    threads = []
    for g in sorted(groups, key=lambda g: -len(g)):
        members = [comps[i] for i in g]
        threads.append(dict(
            components=[f"{c['session_id']}#e{c['exchanges'][0]}-{c['exchanges'][1]}" for c in members],
            sessions=sorted({c["session_id"] for c in members}),
            titles=[c["title"] for c in members],
            descriptions=[c["description"] for c in members],
        ))
    (OUT / "threads.json").write_text(json.dumps(threads, indent=1))
    print(json.dumps(summarize(comps, groups, joins, skipped, a.goal, a.cap), indent=1))
    print(f"written: {OUT / 'threads.json'}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sw = sub.add_parser("sweep")
    sw.add_argument("--goals", type=float, nargs="+", default=[0.70, 0.74, 0.78, 0.82, 0.86])
    sw.add_argument("--caps", type=int, nargs="+", default=[5, 8, 12, 20])
    sw.set_defaults(run=cmd_sweep)
    b = sub.add_parser("build")
    b.add_argument("--goal", type=float, required=True)
    b.add_argument("--cap", type=int, required=True)
    b.set_defaults(run=cmd_build)
    a = p.parse_args()
    a.run(a)


if __name__ == "__main__":
    main()
