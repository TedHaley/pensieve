"""Thread eval: do sessions that belong to one piece of work end up in one thread? Built from session digests.

  python bench/thread_eval.py tabs        # the Threads tab's threads: each card's sessions, from store.db (read only)
  python bench/thread_eval.py digests     # a digest for each session in them, built from its transcript
  python bench/thread_eval.py score       # each arm's threads, scored on session pairs against the Threads tab
  python bench/thread_eval.py components  # the same, with components as the unit
  python bench/thread_eval.py sweep       # cut the split at several thresholds and score each
  python bench/thread_eval.py review      # pairs for hand labels

Threads tab threads: a pair of sessions is "same" when one Threads tab card holds both (card_members, kind session).
The cards were built from git and ticket keys, so an arm that joins by those same keys looks better than it is.
Hand labels are the check on that: they say "same" or "different" for each pair, from reading the sessions.
Labels: bench/out/thread_eval/labels.json, {"same": [[a, b], ...], "different": [...]}.
Out: bench/out/thread_eval/ (tab_threads.json, digests.jsonl, results.jsonl)."""
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


def cmd_tabs(a):
    db = sqlite3.connect(f"file:{STORE}?mode=ro", uri=True)
    clusters: dict[str, set[str]] = {}
    for card, ref in db.execute("SELECT card_id, ref FROM card_members WHERE kind = 'session'"):
        clusters.setdefault(card, set()).add(ref)
    clusters = {card: sorted(refs) for card, refs in clusters.items()}
    sessions = sorted({r for refs in clusters.values() for r in refs})
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tab_threads.json").write_text(json.dumps(dict(made_at=time.time(), source=str(STORE), clusters=clusters,
                                                           sessions=sessions), indent=1))
    print(f"tabs: {len(clusters)} Threads tab threads, {len(sessions)} sessions, "
          f"{sum(1 for refs in clusters.values() if len(refs) >= 2)} threads with 2+ sessions")


def cmd_digests(a):
    from pensieve import digest, parsers
    tabs = json.loads((OUT / "tab_threads.json").read_text())
    want = set(tabs["sessions"])
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
    print(f"digests: {len(found)} of {len(want)} sessions; {len(missing)} have no transcript on this machine")


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


def score_pairs(truth: set[frozenset], pred: set[frozenset]) -> dict:
    tp = len(truth & pred)
    precision = tp / len(pred) if pred else 1.0
    recall = tp / len(truth) if truth else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return dict(pairs_tab=len(truth), pairs_pred=len(pred), tp=tp, precision=round(precision, 3),
                recall=round(recall, 3), f1=round(f1, 3))


def cmd_score(a):
    tabs = json.loads((OUT / "tab_threads.json").read_text())
    digests = load_digests()
    universe = set(digests)  # sessions with a digest on this machine
    tab_clusters = [set(refs) & universe for refs in tabs["clusters"].values()]
    tab_pairs = pairs_of(c for c in tab_clusters if len(c) >= 2)
    labels_path = OUT / "labels.json"
    results = []
    for arm, (kinds, mode) in ARMS.items():
        if mode == "pair":
            pred = pairs_by_links(digests, kinds)
        else:
            pred = pairs_of(clusters_by_links(digests, kinds).values())
        row = dict(arm=arm, at=time.time(), sessions=len(universe), **score_pairs(tab_pairs, pred))
        covered = {s for p in pred for s in p}
        row["sessions_in_a_thread"] = len(covered)
        results.append(row)
        print(f"{arm:30s} pairs tab {row['pairs_tab']:4d} pred {row['pairs_pred']:4d} "
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


def cmd_components(a):
    """Components as the thread unit. A component pair is joined when two components of different sessions share a
    link key. Truth: the two sessions share a Threads tab card. Same-session pairs are left out."""
    tabs = json.loads((OUT / "tab_threads.json").read_text())
    digests = load_digests()
    session_of, comp_links = {}, {}
    for sid, d in digests.items():
        for c in d.get("components", []):
            session_of[c["id"]] = sid
            comp_links[c["id"]] = {(link["kind"], link["key"]) for link in c["links"]}
    universe = set(session_of)
    card_sets = [set(refs) for refs in tabs["clusters"].values()]
    tab_pairs = set()
    for x, y in combinations(sorted(universe), 2):
        if session_of[x] != session_of[y] and any(session_of[x] in r and session_of[y] in r for r in card_sets):
            tab_pairs.add(frozenset((x, y)))
    for label, kinds in (("components pr+ticket+branch", ("pr", "ticket", "branch")),
                         ("components pr+ticket+branch+url", ("pr", "ticket", "branch", "url"))):
        pred = set()
        for x, y in combinations(sorted(universe), 2):
            if session_of[x] == session_of[y]:
                continue
            shared = {k for k in comp_links[x] & comp_links[y] if k[0] in kinds}
            if shared:
                pred.add(frozenset((x, y)))
        row = dict(arm=label, at=time.time(), components=len(universe), **score_pairs(tab_pairs, pred))
        print(f"{label:34s} pairs tab {row['pairs_tab']:5d} pred {row['pairs_pred']:5d} "
              f"precision {row['precision']:.3f} recall {row['recall']:.3f} f1 {row['f1']:.3f}")
        with (OUT / "results.jsonl").open("a") as f:
            f.write(json.dumps(row) + "\n")


def cmd_sweep(a):
    """Tune the component split: for each SHIFT, cut the sessions into components, join them by links, and score.
    Reads the transcripts again (parsers), so it does not need the digests file.

    Caveat: the Threads tab cards are per session, so a component pair counts as "same" when its two sessions share a
    card. More components give more such pairs, so recall falls as the cut gets finer. Read precision here. Score
    components by hand labels (theme boundaries and component-to-thread) before you tune SHIFT on recall."""
    from pensieve import digest, parsers
    tabs = json.loads((OUT / "tab_threads.json").read_text())
    want = set(tabs["sessions"])
    stems = {s.split("/")[-1].split(":", 1)[-1] for s in want}
    sessions = {}
    for source, path in parsers.discover():
        if path.stem in stems:
            s = parsers.parse(source, path)
            if s.id in want:
                sessions[s.id] = s
    card_sets = [set(refs) & set(sessions) for refs in tabs["clusters"].values()]
    card_sets = [c for c in card_sets if c]
    for shift in a.shifts:
        session_of, comp_links = {}, {}
        for sid, s in sessions.items():
            for c in digest.build_components(s, shift=shift):
                session_of[c["id"]] = sid
                comp_links[c["id"]] = {(link["kind"], link["key"]) for link in c["links"]}
        ids = sorted(session_of)
        tab_pairs = {frozenset((x, y)) for x, y in combinations(ids, 2)
                     if session_of[x] != session_of[y]
                     and any(session_of[x] in r and session_of[y] in r for r in card_sets)}
        for kinds in (("pr", "ticket", "branch"), ("pr", "ticket", "branch", "url")):
            pred = {frozenset((x, y)) for x, y in combinations(ids, 2)
                    if session_of[x] != session_of[y]
                    and any(k[0] in kinds for k in comp_links[x] & comp_links[y])}
            r = score_pairs(tab_pairs, pred)
            print(f"shift {shift:.2f} comps {len(ids):4d} kinds {'+'.join(kinds):28s} "
                  f"precision {r['precision']:.3f} recall {r['recall']:.3f} f1 {r['f1']:.3f} "
                  f"pairs {r['pairs_pred']}/{r['pairs_tab']}")


def cmd_review(a):
    """Pairs to label by hand, shown without the Threads tab answer: pairs a link arm joins.
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
    for name, fn in (("tabs", cmd_tabs), ("digests", cmd_digests), ("score", cmd_score),
                     ("components", cmd_components)):
        sub.add_parser(name).set_defaults(run=fn)
    sw = sub.add_parser("sweep")
    sw.add_argument("--shifts", type=float, nargs="+", default=[0.12, 0.2, 0.3, 0.4, 0.5])
    sw.set_defaults(run=cmd_sweep)
    a = p.parse_args()
    a.run(a)


if __name__ == "__main__":
    main()
