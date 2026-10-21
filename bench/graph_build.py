"""Build the relationship graph from your sessions: sessions, branches, PRs and tickets, with the edges the extractors find.

  .venv/bin/python bench/graph_build.py     # writes bench/out/graph/relations.json

Each extractor in pensieve/extract adds its edges. A write is strong. A mention is weak. Sessions with no edges are
kept, so the graph shows thoughts too (work that stayed in a session)."""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "bench" / "out" / "graph"

from pensieve import extract, parsers  # noqa: E402
from pensieve.extract import git_tools, mentions  # noqa: E402,F401  (registers the extractors)


def records_of(path: Path) -> list:
    out = []
    for line in path.read_text(errors="ignore").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def main():
    sessions, edges = {}, []
    for source, path in parsers.discover():
        if source != "claude" or str(path).endswith(".md"):
            continue
        try:
            s = parsers.parse(source, path)
        except Exception:
            continue
        if not s.turns or s.turns[0][1].startswith("You ") or (s.project or "").startswith(("/private", "/tmp")):
            continue
        sess = extract.Session(s.id, s.turns, records_of(path))
        sessions[s.id] = dict(project=s.project, title=s.title, started=s.started, updated=s.updated)
        edges.extend(extract.run_all(sess))

    nodes = {sid: dict(id=sid, kind="session", label=v["title"][:80], project=v["project"]) for sid, v in sessions.items()}
    for e in edges:
        nid = f"{e.kind}:{e.key}"
        nodes.setdefault(nid, dict(id=nid, kind=e.kind, label=e.key))
    relations = [dict(source=e.session, target=f"{e.kind}:{e.key}", strength=e.strength, evidence=e.evidence,
                      extractor=e.extractor) for e in edges]
    linked = {r["source"] for r in relations} | {r["target"] for r in relations}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "relations.json").write_text(json.dumps(dict(nodes=list(nodes.values()), relations=relations,
                                                         thoughts=sum(1 for s in sessions if s not in linked)),
                                                    indent=1))
    kinds = Counter(n["kind"] for n in nodes.values())
    strength = Counter(r["strength"] for r in relations)
    print(f"sessions {len(sessions)}; nodes {dict(kinds)}; relations {len(relations)} {dict(strength)}")
    print(f"sessions with no relation (thoughts): {sum(1 for s in sessions if s not in linked)}")
    print(f"written {OUT / 'relations.json'}")


if __name__ == "__main__":
    main()
