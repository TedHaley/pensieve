"""Sinks from the whole session: the branch or PR a session writes to, read from the full transcript.

  .venv/bin/python bench/sinks.py    # writes bench/out/scenes/components_sinks.jsonl

The exported maps cut each reply short, so a branch named late in a reply is missing from them. This pass reads the
full text of every exchange. It finds branches (checkout, push, "pushed to `x`") and pull requests. Each component then
gets:
  sink         its own sink if its own text names one, else the nearest sink the session named before the component
               ends, within the same context chain (bench/inherit.py)
  sink_source  "own", "session", or "" when the session names none

A session sink is only a candidate. The join rules in threads_build decide whether it joins components."""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bench"))
SCENES = ROOT / "bench" / "out" / "scenes"

BRANCH = re.compile(
    r"(?:checkout -b|switch -c|switch -C|checkout -B|push -u origin|push origin|pushed to) +`?([\w][\w./-]{2,})`?")
PR_URL = re.compile(r"https?://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
PR_REF = re.compile(r"(?<![\w/.@-])([\w.-]+/[\w.-]+)#(\d+)\b")
GENERIC = {"main", "master", "develop", "dev", "the", "a", "an", "it", "this", "that", "origin"}


def session_events(s):
    """(exchange number, kind, key) for each sink the full text of an exchange names."""
    from pensieve import segment
    events = []
    for k, group in enumerate(segment.exchanges(s.turns)):
        text = "\n".join(s.turns[i][1] for i in group if s.turns[i][1])
        for m in BRANCH.finditer(text):
            name = m.group(1).rstrip(".,;:)")
            if name.lower() not in GENERIC:
                events.append((k, "branch", name))
        for m in PR_URL.finditer(text):
            events.append((k, "pr", f"{m.group(1)}#{m.group(2)}"))
        for m in PR_REF.finditer(text):
            events.append((k, "pr", f"{m.group(1)}#{m.group(2)}"))
    return events


def main():
    from pensieve import parsers
    from inherit import maps
    comps = [json.loads(l) for l in (SCENES / "components_inherit.jsonl").read_text().splitlines()]
    want = {c["session_id"] for c in comps}
    stems = {sid.split(":", 1)[-1].split("/")[-1] for sid in want}
    events = {}
    for source, path in parsers.discover():
        if path.stem not in stems:
            continue
        s = parsers.parse(source, path)
        if s.id in want:
            events[s.id] = session_events(s)
    marks = maps()
    rows = []
    for c in comps:
        sid = c["session_id"]
        own = [(l["kind"], l["key"]) for l in c["links"] if l["kind"] == "pr"]
        if c.get("branch") and c["branch"].lower() not in GENERIC:
            own.append(("branch", c["branch"]))
        sink, source = None, ""
        if own:
            sink, source = own[0], "own"
        else:
            end = c["exchanges"][1]
            chain_start = max([k for k in marks.get(sid, {"clear": []})["clear"] if k <= c["exchanges"][0]] or [-1])
            cands = [e for e in events.get(sid, []) if chain_start < e[0] <= end]
            if cands:
                k, kind, key = max(cands, key=lambda e: e[0])
                sink, source = (kind, key), "session"
        rows.append(dict(c, sink=list(sink) if sink else None, sink_source=source))
    out = SCENES / "components_sinks.jsonl"
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    from collections import Counter
    print("components", len(rows), Counter(r["sink_source"] for r in rows))
    print("written", out)


if __name__ == "__main__":
    main()
