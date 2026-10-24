"""Write the relationship graph with thread states, as one HTML page.

  .venv/bin/python bench/graph_page_states.py

Reads relations.json, threads.json and sink_states_cache.json. Output: graph_states.html."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "bench" / "out" / "graph"
SINK_STATE = {"MERGED": "closed", "CLOSED": "closed", "DELETED": "closed", "OPEN": "open", "EXISTS": "open"}

TEMPLATE = Path(__file__).resolve().parent / "graph_page_template.html"


def sink_state(cache: dict, target: str) -> str:
    return SINK_STATE.get(cache.get(target, ""), "unknown")


def main():
    rel = json.loads((OUT / "relations.json").read_text())
    threads = json.loads((OUT / "threads.json").read_text())
    cache_path = OUT / "sink_states_cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    state_of = {s: t["state"] for t in threads for s in t["sessions"]}
    nodes = []
    for n in rel["nodes"]:
        m = dict(n)
        m["state"] = state_of.get(n["id"]) if n["kind"] == "session" else sink_state(cache, n["id"])
        nodes.append(m)
    data = dict(nodes=nodes, relations=rel["relations"],
                threads=[dict(id=t["id"], state=t["state"], sessions=t["sessions"],
                              sinks=[s["target"] for s in t["sinks"]], quiet_days=t["quiet_days"],
                              titles=t["titles"]) for t in threads])
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.read_text().replace("__DATA__", blob)
    (OUT / "graph_states.html").write_text(html)
    print(f"written: {OUT / 'graph_states.html'}; threads {len(threads)}, nodes {len(nodes)}")


if __name__ == "__main__":
    main()
