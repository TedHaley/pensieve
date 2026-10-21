"""Work model v2, built from the sessions: threads on sinks, loose work, and notes.

  .venv/bin/python bench/build_v2.py     # writes bench/out/board/v2.json

Work components that write to a PR or a branch they name themselves join one thread per connected group. A
connected group is the set of components linked through a shared sink (union-find over the sink keys). Sinks at
most CAP components write to join them. Tickets are recorded as sinks but do not join components, because one ticket
can hold many unrelated goals.

Work components with no sink are loose work. They are listed, not threaded. Questions and comments are notes. Notes
never join a thread and never close.

A thread's state comes from thread_model.thread_state, with each sink's completion. Completion for a PR comes from
bench/out/board/sink_states.json. Branches and tickets have no check yet, so a thread on them cannot close."""
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bench"))
SCENES = ROOT / "bench" / "out" / "scenes"
OUT = ROOT / "bench" / "out" / "board"
CAP = 8
GENERIC_BRANCHES = {"main", "master", "develop", "dev"}

from pensieve import thread_model as tm  # noqa: E402
import threads_build as tb  # noqa: E402


def load():
    comps = [json.loads(l) for l in (SCENES / "components_sinks.jsonl").read_text().splitlines()]
    kinds = {}
    for f in sorted((SCENES / "classify").glob("out-b*.json")):
        for row in json.loads(f.read_text()):
            kinds[row["id"]] = row["kind"]
    for c in comps:
        c["id"] = f"{c['session_id']}#e{c['exchanges'][0]}-{c['exchanges'][1]}"
        c["kind"] = kinds.get(c["id"], "work")
    return comps


PR_NUMBER = re.compile(r"\b(?:PR|pr|pull request) ?#?(\d{2,5})\b")


def session_prs(comps) -> dict:
    """Session id -> {PR number: full PR key}, from every PR a session names in full anywhere in its text
    (bench/sinks.py writes it from the full transcripts) and from the exported links."""
    out = defaultdict(dict)
    full = SCENES / "session_prs.json"
    if full.exists():
        for sid, m in json.loads(full.read_text()).items():
            out[sid].update(m)
    for c in comps:
        for l in c["links"]:
            if l["kind"] == "pr":
                out[c["session_id"]][l["key"].rsplit("#", 1)[-1]] = l["key"]
    return out


def writes_of(c, prs: dict) -> list[tm.Sink]:
    """The sinks a component writes to by its own text: PRs and branches it names, and tickets it names. A bare
    "PR 2069" resolves to the full PR key the same session names."""
    if not tb.WRITE.search(c["title"] + " " + c["description"]):
        return []
    sinks = []
    if c.get("branch") and c["branch"] not in GENERIC_BRANCHES:
        repo = project_repo(c["session_id"])
        sinks.append(tm.Sink(tm.SinkKind.BRANCH, f"{repo}@{c['branch']}" if repo else c["branch"]))
    for l in c["links"]:
        if l["kind"] == "pr":
            sinks.append(tm.Sink(tm.SinkKind.PR, l["key"]))
        if l["kind"] == "ticket":
            sinks.append(tm.Sink(tm.SinkKind.TICKET, l["key"]))
    text = c["title"] + " " + c["description"]
    for num in PR_NUMBER.findall(text):
        key = prs.get(c["session_id"], {}).get(num) or (
            f"{project_repo(c['session_id'])}#{num}" if project_repo(c["session_id"]) else "")
        if key:
            sinks.append(tm.Sink(tm.SinkKind.PR, key))
    return list(dict.fromkeys(sinks))


_REPO_CACHE: dict[str, str] = {}


def project_repo(session_id: str) -> str:
    """The GitHub repo (org/repo) of the folder a session ran in, from its git remote. Empty when unknown."""
    if session_id in _REPO_CACHE:
        return _REPO_CACHE[session_id]
    import subprocess
    repo = ""
    for f in (SCENES / "in").glob("*.txt"):
        lines = f.read_text().splitlines()
        if lines and session_id in lines[0]:
            m = re.search(r"project (\S.*)$", lines[1]) if len(lines) > 1 else None
            if m and Path(m.group(1)).is_dir():
                r = subprocess.run(["git", "-C", m.group(1), "remote", "get-url", "origin"],
                                   capture_output=True, text=True)
                g = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", r.stdout.strip())
                repo = g.group(1) if g else ""
            break
    _REPO_CACHE[session_id] = repo
    return repo


def session_times():
    from pensieve import parsers
    times = {}
    for f in (SCENES / "in").glob("*.txt"):
        sid = f.read_text().splitlines()[0].split(" | ")[0].replace("# session ", "").strip()
        times[sid] = 0.0
    stems = {sid.split(":", 1)[-1].split("/")[-1] for sid in times}
    for source, path in parsers.discover():
        if path.stem in stems:
            s = parsers.parse(source, path)
            if s.id in times:
                try:
                    times[s.id] = datetime.fromisoformat((s.updated or "").replace("Z", "+00:00")).timestamp()
                except ValueError:
                    times[s.id] = 0.0
    return times


def main():
    comps = load()
    times = session_times()
    status = json.loads((OUT / "sink_states.json").read_text()) if (OUT / "sink_states.json").exists() else {}
    status = {f"pr {k}": v for k, v in status.items()}
    if (OUT / "branch_states.json").exists():
        status.update(json.loads((OUT / "branch_states.json").read_text()))

    notes = [c for c in comps if c["kind"] in ("question", "comment")]
    work = [c for c in comps if c["kind"] == "work"]
    prs = session_prs(comps)
    for c in work:
        c["sinks"] = writes_of(c, prs)

    branch_keys = sorted({s.key for c in work for s in c["sinks"] if s.kind == tm.SinkKind.BRANCH})
    (OUT / "branch_sinks.json").write_text(json.dumps(branch_keys, indent=1))

    # Union components that share a PR or branch sink, unless the sink is used by more than CAP components.
    by_sink = defaultdict(list)
    for i, c in enumerate(work):
        for s in c["sinks"]:
            if s.kind in (tm.SinkKind.PR, tm.SinkKind.BRANCH):
                by_sink[s].append(i)
    parent = list(range(len(work)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    joined_sinks = {}
    for s, idx in by_sink.items():
        idx = sorted(set(idx))
        if 2 <= len(idx) <= CAP:
            joined_sinks[s] = idx
            for other in idx[1:]:
                parent[find(other)] = find(idx[0])

    groups = defaultdict(list)
    loose = []
    for i, c in enumerate(work):
        if not c["sinks"]:
            loose.append(c)
            continue
        groups[find(i)].append(i)

    threads = []
    for root, idx in groups.items():
        members = sorted((work[i] for i in idx), key=lambda c: times.get(c["session_id"], 0.0))
        sinks = []
        for c in members:
            for s in c["sinks"]:
                if s not in sinks:
                    sinks.append(s)
        last = max(times.get(c["session_id"], 0.0) for c in members)
        state, why = tm.thread_state(sinks, status, last)
        label = members[-1]["title"]  # placeholder until the titles pass names the thread
        threads.append(dict(
            id="t" + hashlib.sha1("|".join(sorted(c["id"] for c in members)).encode()).hexdigest()[:8],
            label=label,
            named=False,
            sinks=[dict(kind=s.kind.value, key=s.key, label=s.label,
                        completion=tm.sink_complete(s, status).value) for s in sinks],
            components=[dict(id=c["id"], session=c["session_id"], title=c["title"], description=c["description"],
                             exchanges=c["exchanges"], links=c["links"], branch=c.get("branch"))
                        for c in members],
            sessions=sorted({c["session_id"] for c in members}),
            last_active=last,
            state=state.value,
            state_why=why,
        ))
    threads.sort(key=lambda t: (t["state"] != "open", -t["last_active"]))  # open first, then most recent session

    loose_rows = [dict(id=c["id"], session=c["session_id"], title=c["title"], description=c["description"],
                       exchanges=c["exchanges"], last_active=times.get(c["session_id"], 0.0)) for c in loose]
    loose_rows.sort(key=lambda r: r["last_active"], reverse=True)
    note_rows = [dict(id=c["id"], session=c["session_id"], kind=c["kind"], title=c["title"],
                      description=c["description"], last_active=times.get(c["session_id"], 0.0)) for c in notes]
    note_rows.sort(key=lambda r: r["last_active"], reverse=True)

    # Names: a titles pass writes one label per thread, keyed by the thread id, and the same loader as board.py.
    names = {}
    for f in sorted((OUT / "titles-v2").glob("*.json")) if (OUT / "titles-v2").exists() else []:
        for row in json.loads(f.read_text()):
            names[row["id"]] = row["title"]
    for t in threads:
        if t["id"] in names:
            t["label"], t["named"] = names[t["id"]], True

    out = dict(
        built=datetime.now().isoformat(timespec="seconds"),
        counts=dict(sessions=len(times), components=len(comps), work=len(work), notes=len(notes),
                    loose_work=len(loose), threads=len(threads), joined_sinks=len(joined_sinks),
                    states=dict(Counter(t["state"] for t in threads))),
        threads=threads,
        loose_work=loose_rows,
        notes=note_rows,
        limits=[
            "Only PR sinks have a completion check. Branch and ticket sinks are unknown, so their threads cannot close.",
            "Thread membership comes from shared PR or branch sinks. Work with no sink is loose work, not a thread.",
            "Kinds (work, question, comment) come from an agent reading excerpts.",
        ],
    )
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "v2.json").write_text(json.dumps(out, indent=1))
    (OUT / "v2_titles_input.json").write_text(json.dumps([
        dict(id=t["id"], titles=[c["title"] for c in t["components"]],
             descriptions=[c["description"][:120] for c in t["components"]]) for t in threads], indent=1))
    print(json.dumps(out["counts"], indent=1))
    print(f"written: {OUT / 'v2.json'}")


if __name__ == "__main__":
    main()
