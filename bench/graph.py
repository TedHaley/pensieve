"""A temporal graph of components: what each component is related to, and only what it could know when it ran.

  .venv/bin/python bench/graph.py    # writes bench/out/graph/graph.json and prints a summary

Nodes: work components (classify kinds), in time order: session written time, then exchange order.

Edges run forward in time only (earlier -> later). Each edge has a type:
  repeats     a later component names a PR or branch that an earlier one in its chain named
  refers      a later component writes to a PR or branch it does not name. It refers to the nearest one
              introduced earlier in its chain
  same-sink   both components name the same PR or branch, anywhere in the export
  goal        their goals are similar (embedding, domain words removed), in any session

Visibility rule: an entity is visible only to components after the one that introduced it, and only in the same
context chain (bench/inherit.py). A reset (/clear) ends the chain. The checks at the end of the run assert both
rules."""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bench"))
SCENES = ROOT / "bench" / "out" / "scenes"
OUT = ROOT / "bench" / "out" / "graph"
GOAL_SIM = 0.78


def load():
    import threads_build as tb
    rows = [json.loads(l) for l in (SCENES / "components_inherit.jsonl").read_text().splitlines()]
    cls = {}
    for f in (SCENES / "classify").glob("out-b*.json"):
        for r in json.loads(f.read_text()):
            cls[r["id"]] = r
    for c in rows:
        c["id"] = f"{c['session_id']}#e{c['exchanges'][0]}-{c['exchanges'][1]}"
        c["kind"] = cls.get(c["id"], {}).get("kind", "work")
    return tb, [c for c in rows if c["kind"] == "work"]


def session_times():
    times = {}
    for f in (SCENES / "in").glob("*.txt"):
        lines = f.read_text().splitlines()
        sid = lines[0].split(" | ")[0].replace("# session ", "").strip()
        times[sid] = lines[1].split(" | ")[0].replace("# started ", "") if len(lines) > 1 else ""
    return times


def own_entities(c, tb):
    own = [("pr", l["key"]) for l in c["links"] if l["kind"] == "pr"]
    if c.get("branch") and c["branch"] not in tb.GENERIC_BRANCHES:
        own.append(("branch", c["branch"]))
    return own


def main():
    import numpy as np
    tb, work = load()
    times = session_times()
    work.sort(key=lambda c: (times.get(c["session_id"], ""), c["exchanges"][0]))
    index = {c["id"]: i for i, c in enumerate(work)}
    edges = []

    chains = defaultdict(list)
    for c in work:
        chains[(c["session_id"], c["chain"])].append(c)
    for comps in chains.values():
        introduced = {}
        for c in comps:
            i = index[c["id"]]
            own = own_entities(c, tb)
            writes = bool(tb.WRITE.search(c["title"] + " " + c["description"]))
            if writes and not own:
                for r in c.get("resolved", []):
                    ent = (r["kind"], r["key"])
                    if ent in introduced:
                        edges.append(dict(type="refers", src=introduced[ent], dst=i, entity=f"{ent[0]} {ent[1]}"))
                        break
            for ent in own:
                if ent in introduced and introduced[ent] != i:
                    edges.append(dict(type="repeats", src=introduced[ent], dst=i, entity=f"{ent[0]} {ent[1]}"))
                introduced[ent] = i

    by_sink = defaultdict(set)
    for c in work:
        for ent in own_entities(c, tb):
            by_sink[ent].add(index[c["id"]])
    for ent, idx in by_sink.items():
        idx = sorted(idx)
        if 2 <= len(idx) <= 8:
            edges += [dict(type="same-sink", src=a, dst=b, entity=f"{ent[0]} {ent[1]}")
                      for a, b in zip(idx, idx[1:])]

    vecs = tb.embed([tb.goal_text(c) for c in work])
    sim = vecs @ vecs.T
    n = len(work)
    for a in range(n):
        for b in range(a + 1, n):
            if sim[a, b] >= GOAL_SIM:
                edges.append(dict(type="goal", src=a, dst=b, sim=round(float(sim[a, b]), 3)))

    backward = [e for e in edges if e["src"] >= e["dst"]]
    chain_of = {index[c["id"]]: (c["session_id"], c["chain"]) for c in work}
    visibility = [e for e in edges if e["type"] in ("refers", "repeats") and chain_of[e["src"]] != chain_of[e["dst"]]]
    out = dict(
        nodes=[dict(i=index[c["id"]], id=c["id"], session=c["session_id"], chain=c["chain"], title=c["title"],
                    description=c["description"], links=c["links"], branch=c.get("branch"),
                    written=times.get(c["session_id"], ""), exchanges=c["exchanges"]) for c in work],
        edges=edges,
        checks=dict(backward_edges=len(backward), visibility_violations=len(visibility)),
    )
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "graph.json").write_text(json.dumps(out))
    print(f"nodes {n}; edges {len(edges)} {dict(Counter(e['type'] for e in edges))}")
    print(f"checks: backward edges {len(backward)}, visibility violations {len(visibility)}")
    print(f"written {OUT / 'graph.json'}")


if __name__ == "__main__":
    main()
