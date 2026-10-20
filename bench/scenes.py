"""Scenes: split each real session into components by theme, the way a video splits into scenes.

  python bench/scenes.py export    # one text file per real session, in bench/out/scenes/in/
  python bench/scenes.py merge     # join the agents' answers in bench/out/scenes/out/ into components.jsonl

A real session has at least two turns and is not one of Pensieve's own summary calls (those are written as sessions
too, and start with "You summarize"). Memory files are left out.

Each text file lists the session's exchanges: an exchange is a user turn and the agent's reply after it. Each line
is cut short, so the file is a map of the session, not a copy of it. The split into components is made by reading
these files, not by a rule."""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = Path(__file__).parent / "out" / "scenes"
USER_CHARS, AGENT_CHARS = 160, 100


def real_sessions():
    from pensieve import parsers, segment
    for src, path in parsers.discover():
        if str(path).endswith(".md"):
            continue
        try:
            s = parsers.parse(src, path)
        except Exception:
            continue
        if len(s.turns) < 2 or s.turns[0][1].startswith("You "):  # "You summarize", "You name": Pensieve's own jobs
            continue
        if (s.project or "").startswith(("/private/var/folders", "/tmp", "/private/tmp")):  # its temp workspace
            continue
        yield s, segment.exchanges(s.turns)


def one_line(text: str, n: int) -> str:
    return " ".join(text.split())[:n]


def cmd_export(a):
    from pensieve import digest
    indir = OUT / "in"
    indir.mkdir(parents=True, exist_ok=True)
    index = []
    for s, groups in real_sessions():
        lines = [f"# session {s.id} | source {s.source} | {len(s.turns)} turns | {len(groups)} exchanges",
                 f"# started {s.started or '?'} | project {s.project or '?'}", ""]
        for k, g in enumerate(groups):
            user = next((t for r, t in ((s.turns[i]) for i in g) if r == "user"), "")
            agent = " ".join(t for r, t in ((s.turns[i]) for i in g) if r != "user")
            links = digest.extract_links("\n".join(t for _, t in (s.turns[i] for i in g)))
            tail = ""
            if links:
                tail = "  [links: " + ", ".join(f"{x['kind']} {x['key']}" for x in links[:6]) + "]"
            lines.append(f"[e{k}] U: {one_line(user, USER_CHARS)}")
            if agent:
                lines.append(f"      A: {one_line(agent, AGENT_CHARS)}{tail}")
            elif tail:
                lines.append(f"      {tail.strip()}")
        name = s.id.replace("/", "_").replace(":", "_")
        (indir / f"{name}.txt").write_text("\n".join(lines) + "\n")
        index.append(dict(file=f"{name}.txt", session_id=s.id, source=s.source, turns=len(s.turns),
                          exchanges=len(groups), chars=sum(len(l) for l in lines)))
    (OUT / "index.json").write_text(json.dumps(index, indent=1))
    total = sum(r["chars"] for r in index)
    print(f"export: {len(index)} sessions, {sum(r['exchanges'] for r in index)} exchanges, {total} characters, "
          f"in {indir}")


def cmd_merge(a):
    outdir = OUT / "out"
    rows = []
    for f in sorted(outdir.glob("*.json")):
        data = json.loads(f.read_text())
        for sess in data["sessions"]:
            for c in sess["components"]:
                links, branch = [], c.get("branch")
                for link in c.get("links", []):
                    key = link["key"].strip().strip("*").rstrip(".,;:!?")  # the agents copied "**" from markdown
                    if link["kind"] == "branch":  # a branch tag is a branch, not a link
                        branch = branch or key
                        continue
                    links.append(dict(kind=link["kind"], key=key))
                rows.append(dict(session_id=sess["session_id"], **{**c, "links": links, "branch": branch}))
    (OUT / "components.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"merge: {len(rows)} components from {len(list(outdir.glob('*.json')))} batch files, at {time.ctime()}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("export").set_defaults(run=cmd_export)
    sub.add_parser("merge").set_defaults(run=cmd_merge)
    a = p.parse_args()
    a.run(a)


if __name__ == "__main__":
    main()
