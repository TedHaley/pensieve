"""The threads board, built from the components of every real session.

  .venv/bin/python bench/board.py      # writes bench/out/board/board.json

Steps:
  1. Components: components.jsonl (the split), joined with the kind and last-message pass (classify/out-b*.json).
  2. Work components join threads by goal (threads_build: embedding similarity, key bonus, cap). Question and comment
     components become items in the Questions and Comments areas. They never join a thread.
  3. Each thread's area comes from its sessions' folders. The area is the repo name when most of its sessions ran in
     the same repo folder. Otherwise the thread has no area.
  4. Each thread's state comes from no_evidence_state (the thread has no PR or ticket state here). Its last component
     decides closing. Time comes from the session's last write.
Limits are written into the board file, so the board says what it does not know."""
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bench"))
SCENES = ROOT / "bench" / "out" / "scenes"
OUT = ROOT / "bench" / "out" / "board"

from pensieve import thread_model as tm  # noqa: E402
from pensieve.task_states import State  # noqa: E402

GOAL, CAP = 0.78, 8


def load_components():
    comps = [json.loads(l) for l in (SCENES / "components.jsonl").read_text().splitlines()]
    for c in comps:
        c["id"] = f"{c['session_id']}#e{c['exchanges'][0]}-{c['exchanges'][1]}"
    kinds = {}
    for f in sorted((SCENES / "classify").glob("out-b*.json")):
        for row in json.loads(f.read_text()):
            kinds[row["id"]] = row
    for c in comps:
        row = kinds.get(c["id"], {})
        c["kind"] = row.get("kind", "work")
        c["last"] = row.get("last", "unclear")
    return comps


def session_headers():
    """Session id -> (project folder, started, last write). Read from the exported maps."""
    out = {}
    for f in (SCENES / "in").glob("*.txt"):
        lines = f.read_text().splitlines()
        sid = lines[0].split(" | ")[0].replace("# session ", "").strip()
        m = re.search(r"project (\S.*)$", lines[1]) if len(lines) > 1 else None
        project = m.group(1).strip() if m else ""
        started = lines[1].split(" | ")[0].replace("# started ", "") if len(lines) > 1 else ""
        out[sid] = dict(project=project, started=started)
    idx = {r["session_id"]: r for r in json.loads((SCENES / "index.json").read_text())}
    from pensieve import parsers
    stems = {sid.split(":", 1)[-1].split("/")[-1] for sid in out}
    for source, path in parsers.discover():  # the last write of each transcript: the time rules need it
        if path.stem in stems:
            s = parsers.parse(source, path)
            if s.id in out:
                out[s.id]["written"] = s.updated or ""
    for sid in out:
        out[sid]["turns"] = idx.get(sid, {}).get("turns", 0)
        out[sid].setdefault("written", out[sid]["started"])
    return out


def repo_of(project: str) -> str:
    """The repo name of a session's folder, or empty for a temp or unknown folder."""
    if not project or project.startswith(("/private", "/tmp")):
        return ""
    parts = Path(project).parts
    return Path(project).name if len(parts) >= 3 else ""


def iso(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def main():
    import threads_build as tb

    comps = load_components()
    heads = session_headers()
    work = [c for c in comps if c["kind"] == "work"]
    items = [c for c in comps if c["kind"] in ("question", "comment")]

    vecs = tb.embed([tb.goal_text(c) for c in work])
    groups, joins, skipped = tb.build(work, vecs, GOAL, CAP)

    threads, areas_used = [], Counter()
    for g in sorted(groups, key=lambda g: -len(g)):
        members = sorted((work[i] for i in g), key=lambda c: iso(heads.get(c["session_id"], {}).get("started", "")))
        sids = sorted({c["session_id"] for c in members})
        repos = Counter(repo_of(heads.get(s, {}).get("project", "")) for s in sids)
        repos.pop("", None)
        area = ""
        if repos:
            repo, n = repos.most_common(1)[0]
            if n / len(sids) >= 0.6:
                area = repo
        newest = members[-1]
        last_write = max(iso(heads.get(s, {}).get("written", "")) for s in sids)
        model_components = [tm.Component(
            session_id=c["session_id"], index=0, exchanges=tuple(c["exchanges"]), title=c["title"],
            description=c["description"], kind=tm.ComponentKind.WORK,
            last=tm.LastMessage(c["last"]) if c["last"] in tm.LastMessage._value2member_map_ else tm.LastMessage.UNCLEAR,
        ) for c in members]
        state, why = tm.no_evidence_state(model_components, last_write)
        if area:
            areas_used[area] += 1
        threads.append(dict(
            id=f"t{len(threads) + 1:03d}",
            goal=newest["title"],
            goal_text=newest["description"],
            area=area,
            state=state.value,
            state_why=why,
            sessions=sids,
            components=[dict(id=c["id"], session=c["session_id"], title=c["title"], description=c["description"],
                             exchanges=c["exchanges"], last=c["last"],
                             links=[l for l in c["links"]], branch=c.get("branch")) for c in members],
        ))

    question_items = [dict(id=c["id"], kind=c["kind"], area="Questions" if c["kind"] == "question" else "Comments",
                           title=c["title"], description=c["description"], session=c["session_id"],
                           repo=repo_of(heads.get(c["session_id"], {}).get("project", "")))
                      for c in items]

    states = Counter(t["state"] for t in threads)
    board = dict(
        built=datetime.now().isoformat(timespec="seconds"),
        settings=dict(goal_threshold=GOAL, key_cap=CAP, bonus=tb.BONUS),
        counts=dict(sessions=len(heads), components=len(comps), work=len(work), items=len(items),
                    threads=len(threads), single_component_threads=sum(1 for t in threads if len(t["components"]) == 1),
                    states=dict(states)),
        areas=[dict(name=k, threads=v) for k, v in areas_used.most_common()],
        threads=threads,
        items=question_items,
        limits=[
            "States use no PR, ticket or commit state. Only the last-message rule and the time rules apply.",
            "The goal threshold (0.78) and key cap (8) are not yet tested against labels.",
            "Areas come from folder names only. Sessions outside a repo folder have no area.",
            "Kind and last message come from an agent reading short excerpts.",
        ],
    )
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "board.json").write_text(json.dumps(board, indent=1))
    print(json.dumps(board["counts"], indent=1))
    print("states", dict(states))
    print("areas", board["areas"][:8])
    print(f"written: {OUT / 'board.json'}")


if __name__ == "__main__":
    main()
