"""Thread eval: do sessions that belong to one piece of work end up in one thread? Built from session digests.

  python bench/thread_eval.py gold     # the gold threads: each card's sessions, from store.db (read only)
  python bench/thread_eval.py digests  # a digest for each gold session, built from its transcript
  python bench/thread_eval.py score    # each arm's threads, scored on session pairs against gold

Gold: a pair of sessions is positive when some card holds both (card_members, kind session). The gold cards were
joined by git and ticket keys, so an arm that joins by the same keys looks better than it is. Hand labels (below)
are the check on that bias. Labels: bench/out/thread_eval/labels.json, {"same": [[a, b], ...], "different": [...]}.
Out: bench/out/thread_eval/ (gold.json, digests.jsonl, results.jsonl)."""
import argparse
import json
import os
import sqlite3
import sys
import time
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = Path(__file__).parent / "out" / "thread_eval"
STORE = Path(os.environ.get("PENSIEVE_STORE", Path.home() / ".pensieve" / "v2" / "store.db"))

# Arms: which link kinds join two sessions, and how. "pair": two sessions that share a key are one pair.
# "chain": a key joins its sessions, and joins pass through shared sessions (connected components).
ARMS = {
    "links:pairs pr+ticket+branch": (("pr", "ticket", "branch"), "pair"),
    "links:pairs pr+ticket+branch+url": (("pr", "ticket", "branch", "url"), "pair"),
    "links:chain pr+ticket+branch": (("pr", "ticket", "branch"), "chain"),
}


def cmd_gold(a):
    db = sqlite3.connect(f"file:{STORE}?mode=ro", uri=True)
    clusters: dict[str, set[str]] = {}
    for card, ref in db.execute("SELECT card_id, ref FROM card_members WHERE kind = 'session'"):
        clusters.setdefault(card, set()).add(ref)
    clusters = {card: sorted(refs) for card, refs in clusters.items()}
    sessions = sorted({r for refs in clusters.values() for r in refs})
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "gold.json").write_text(json.dumps(dict(made_at=time.time(), source=str(STORE), clusters=clusters,
                                                   sessions=sessions), indent=1))
    print(f"gold: {len(clusters)} threads, {len(sessions)} sessions, "
          f"{sum(1 for refs in clusters.values() if len(refs) >= 2)} threads with 2+ sessions")


def cmd_digests(a):
    from pensieve import digest, parsers
    gold = json.loads((OUT / "gold.json").read_text())
    want = set(gold["sessions"])
    stems = {s.split("/")[-1].split(":", 1)[-1] for s in want}  # the file name, for a quick filter
    found, titles, seen = {}, {}, set()
    for source, path in parsers.discover():
        if path.stem not in stems:
            continue
        s = parsers.parse(source, path)
        if s.id in want and s.id not in seen:
            seen.add(s.id)
            found[s.id] = digest.build(s)
            titles[s.id] = s.title
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "digests.jsonl").open("w") as f:
        for sid in sorted(found):
            f.write(json.dumps(found[sid]) + "\n")
    (OUT / "titles.json").write_text(json.dumps(titles, indent=1))
    missing = sorted(want - seen)
    print(f"digests: {len(found)} of {len(want)} gold sessions; {len(missing)} have no transcript on this machine")


def load_digests():
    return {d["session_id"]: d for d in map(json.loads, (OUT / "digests.jsonl").read_text().splitlines())}


def pairs_by_links(digests: dict, kinds: tuple) -> set[frozenset]:
    """Every pair of sessions that share a link key of the given kinds. No chaining."""
    by_key: dict[tuple, set] = {}
    for sid, d in digests.items():
        for link in d["links"]:
            if link["kind"] in kinds:
                by_key.setdefault((link["kind"], link["key"]), set()).add(sid)
    return pairs_of(by_key.values())


def clusters_by_links(digests: dict, kinds: tuple) -> dict[str, set[str]]:
    """Union the sessions that share a link key of the given kinds. Returns {root: sessions}."""
    parent = {sid: sid for sid in digests}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_key: dict[tuple, list] = {}
    for sid, d in digests.items():
        for link in d["links"]:
            if link["kind"] in kinds:
                by_key.setdefault((link["kind"], link["key"]), []).append(sid)
    for sids in by_key.values():
        for other in sids[1:]:
            parent[find(other)] = find(sids[0])
    out: dict[str, set[str]] = {}
    for sid in digests:
        out.setdefault(find(sid), set()).add(sid)
    return out


def pairs_of(clusters) -> set[frozenset]:
    out = set()
    for members in clusters:
        for a, b in combinations(sorted(members), 2):
            out.add(frozenset((a, b)))
    return out


def score_pairs(gold: set[frozenset], pred: set[frozenset]) -> dict:
    tp = len(gold & pred)
    precision = tp / len(pred) if pred else 1.0
    recall = tp / len(gold) if gold else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return dict(pairs_gold=len(gold), pairs_pred=len(pred), tp=tp, precision=round(precision, 3),
                recall=round(recall, 3), f1=round(f1, 3))


def cmd_score(a):
    gold = json.loads((OUT / "gold.json").read_text())
    digests = load_digests()
    universe = set(digests)  # gold sessions with a digest on this machine
    gold_clusters = [set(refs) & universe for refs in gold["clusters"].values()]
    gold_pairs = pairs_of(c for c in gold_clusters if len(c) >= 2)
    labels_path = OUT / "labels.json"
    results = []
    for arm, (kinds, mode) in ARMS.items():
        if mode == "pair":
            pred = pairs_by_links(digests, kinds)
        else:
            pred = pairs_of(clusters_by_links(digests, kinds).values())
        row = dict(arm=arm, at=time.time(), sessions=len(universe), **score_pairs(gold_pairs, pred))
        covered = {s for p in pred for s in p}
        row["sessions_in_a_thread"] = len(covered)
        results.append(row)
        print(f"{arm:30s} pairs gold {row['pairs_gold']:4d} pred {row['pairs_pred']:4d} "
              f"precision {row['precision']:.3f} recall {row['recall']:.3f} f1 {row['f1']:.3f} "
              f"sessions joined {row['sessions_in_a_thread']}/{len(universe)}")
        if labels_path.exists():
            labels = json.loads(labels_path.read_text())
            same = {frozenset(p) for p in labels.get("same", [])}
            diff = {frozenset(p) for p in labels.get("different", [])}
            row["hand"] = dict(same=len(same & pred), same_total=len(same),
                               different_joined=len(diff & pred), different_total=len(diff))
            print(f"{'':30s} hand labels: same {row['hand']['same']}/{len(same)} joined, "
                  f"different wrongly joined {row['hand']['different_joined']}/{len(diff)}")
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "results.jsonl").open("a") as f:
        for row in results:
            f.write(json.dumps(row) + "\n")


def cmd_review(a):
    """Pairs to label by hand, shown without the gold answer: pairs a link arm joins, and pairs that share no key.
    Writes review.md. Label the pairs in labels.json, as same / different."""
    digests = load_digests()
    titles = json.loads((OUT / "titles.json").read_text())
    joined = pairs_by_links(digests, ARMS["links:pairs pr+ticket+branch+url"][0])
    shared = {}
    for sid, d in digests.items():
        for link in d["links"]:
            if link["kind"] in ("pr", "ticket", "branch", "url"):
                shared.setdefault(sid, set()).add(f"{link['kind']} {link['key']}")
    rng = sorted(joined, key=lambda p: sorted(p))
    lines = ["# Pairs to label", "", "Same work (one thread) or different? Put each pair in labels.json.", ""]
    for pair in rng[:a.n]:
        x, y = sorted(pair)
        common = sorted(shared.get(x, set()) & shared.get(y, set()))
        lines += [f"## {x} | {y}", f"- {titles.get(x, '')}", f"- {titles.get(y, '')}", f"- shared: {', '.join(common)}", ""]
    (OUT / "review.md").write_text("\n".join(lines))
    print(f"review: {min(len(rng), a.n)} of {len(rng)} joined pairs in {OUT / 'review.md'}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    rv = sub.add_parser("review")
    rv.add_argument("-n", type=int, default=30)
    rv.set_defaults(run=cmd_review)
    for name, fn in (("gold", cmd_gold), ("digests", cmd_digests), ("score", cmd_score)):
        sub.add_parser(name).set_defaults(run=fn)
    a = p.parse_args()
    a.run(a)


if __name__ == "__main__":
    main()
