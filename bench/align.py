"""Align scenes with threads: join scenes that share a branch, a pull request or a ticket. A scene with no shared key
stays a thread of its own.

  python bench/align.py     # reads bench/out/scenes/components.jsonl, writes bench/out/scenes/threads.json

Join rules, in order of trust:
  branch: the same branch name (a scene names the branch it works on)
  pr, ticket: the same canonical key
A key that many scenes share (main, master, a shared ticket) is too weak to join on: it is reported, not used.
Out: threads.json with each thread's scenes, branches, keys and session ids. Print: a summary to check by eye."""
import collections
import json
from pathlib import Path

OUT = Path(__file__).parent / "out" / "scenes"
GENERIC_BRANCHES = {"main", "master", "develop", "dev"}  # too common to mean one piece of work
MAX_KEY_SCENES = 12  # a pr or ticket on more scenes than this is a project, not a piece of work


def main():
    rows = [json.loads(l) for l in (OUT / "components.jsonl").read_text().splitlines()]
    for i, r in enumerate(rows):
        r["id"] = i
    parent = list(range(len(rows)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    by_key = collections.defaultdict(list)
    skipped = collections.Counter()
    for r in rows:
        if r.get("branch") and r["branch"] not in GENERIC_BRANCHES:
            by_key[("branch", r["branch"])].append(r["id"])
        for link in r["links"]:
            if link["kind"] in ("pr", "ticket"):
                by_key[(link["kind"], link["key"])].append(r["id"])
    used = {}
    for key, ids in by_key.items():
        if len(set(ids)) < 2:
            continue
        if key[0] != "branch" and len(set(ids)) > MAX_KEY_SCENES:
            skipped[key] = len(set(ids))
            continue
        used[key] = sorted(set(ids))
        for other in used[key][1:]:
            union(used[key][0], other)

    groups = collections.defaultdict(list)
    for r in rows:
        groups[find(r["id"])].append(r)

    threads = []
    for members in groups.values():
        keys = sorted({k for k, ids in used.items() if any(m["id"] in ids for m in members)})
        threads.append(dict(
            size=len(members),
            sessions=sorted({m["session_id"] for m in members}),
            branches=sorted({m["branch"] for m in members if m.get("branch")}),
            keys=[f"{k[0]} {k[1]}" for k in keys],
            scenes=[dict(session_id=m["session_id"], exchanges=m["exchanges"], title=m["title"],
                         description=m["description"], branch=m.get("branch"), links=m["links"]) for m in members],
        ))
    threads.sort(key=lambda t: -t["size"])
    (OUT / "threads.json").write_text(json.dumps(threads, indent=1))

    sizes = [t["size"] for t in threads]
    multi = [t for t in threads if t["size"] > 1]
    print(f"scenes {len(rows)} -> threads {len(threads)}; single-scene threads {len(threads) - len(multi)}")
    print(f"largest threads: {sizes[:8]}")
    print(f"threads spanning more than one session: {sum(1 for t in threads if len(t['sessions']) > 1)}")
    print(f"shared keys skipped as too common: {len(skipped)} {skipped.most_common(5)}")
    print("\nlargest threads:")
    for t in threads[:12]:
        print(f"  {t['size']:3d} scenes, {len(t['sessions']):2d} sessions, branches {t['branches'][:3]}, "
              f"keys {t['keys'][:3]}")
        print(f"       e.g. {t['scenes'][0]['title']!r}: {t['scenes'][0]['description'][:90]!r}")


if __name__ == "__main__":
    main()
