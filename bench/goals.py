"""Does the task note pick the right main goal, and split off the right other goals? (docs/threads.md rule 10)

Each labelled task says which main goals are right, and which evidence (PRs, ticket keys, commit hashes) must stay
in one goal or in different goals. A variant replaces the goal paragraph of work.NOTE_PROMPT. Each note is written
`reps` times from the same frozen task data; a judge model says if each main goal is one of the right ones.

  .venv/bin/python bench/goals.py freeze --ids t:ABC-12,b:/repo@fix-x
  .venv/bin/python bench/goals.py run --variants last,anchor --reps 3
  .venv/bin/python bench/goals.py report

Files in bench/out/goals: tasks.json (frozen tasks), labels.jsonl, runs.jsonl. A label:
  {"id": "t:ABC-12", "main": ["fix the login timeout"], "together": [["#12", "1a2b3c4d"]], "apart": [["#12"], ["#14"]]}
together: each group's refs are in one goal; apart: the groups are in different goals."""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pensieve import work  # noqa: E402

OUT = Path(__file__).parent / "out" / "goals"
_P = work.NOTE_PROMPT
_START, _END = _P.index("Sessions, a long one"), _P.index("Answer with these fields:")

STEP = ("A question the developer asked on the way (explain this, reply to that) is not a goal, and neither is a step "
        "that serves a goal (a change it needs, a revert, a CI fix, a rebase, a review round): that is part of the goal.")
VARIANTS = {
    "last": "Sessions, a long one most of all, can work on more than one goal. The main goal is the one that the last "
            f"session worked toward. {STEP} The first six fields are about the main goal only: what is done and left "
            "for another goal goes in that goal's fields, not in these.\n",
    "assign": "Sessions, a long one most of all, can work on more than one goal. The main goal is the one that the "
              f"last session worked toward. {STEP} Each PR, commit and ticket in the data belongs to the goal it was "
              "made for: a PR made for another goal makes that goal one of the goals below, with the PR as its "
              "evidence. The first six fields are about the main goal only: what is done and left for another goal "
              "goes in that goal's fields, not in these.\n",
    "worked": "Sessions, a long one most of all, can work on more than one goal. The main goal is the newest goal that "
              "a session did work toward (changed code, made a commit or PR, ran a job, made a decision). A session "
              "that only explained code or answered questions does not set the main goal. "
              f"{STEP} Each PR, commit and ticket in the data belongs to the goal it was made for: a PR made for "
              "another goal makes that goal one of the goals below, with the PR as its evidence. The first six fields "
              "are about the main goal only: what is done and left for another goal goes in that goal's fields, not "
              "in these.\n",
    "worked3": "Sessions, a long one most of all, can work on more than one goal. The main goal is the newest goal that "
               "a session did work toward (changed code, made a commit or PR, ran a job, made a decision). A session "
               "that only explained code or answered questions does not set the main goal. The task's name above comes "
               "from its ticket, branch or first session, and can name an older goal. "
               f"{STEP} Each PR, commit and ticket in the data belongs to the goal it was made for: a PR made for "
               "another goal makes that goal one of the goals below, with the PR as its evidence. The first six fields "
               "are about the main goal only: what is done and left for another goal goes in that goal's fields, not "
               "in these.\n",
    "worked2":"Sessions, a long one most of all, can work on more than one goal. The main goal is the newest goal that "
               "a session did work toward (changed code, made a commit or PR, reviewed a PR, ran a job, made a "
               "decision). A session that only explained code, found a document or answered questions does not set "
               f"the main goal, and finding something is not a goal. {STEP} Each PR, commit and ticket in the data "
               "belongs to the goal it was made for: a PR made for another goal makes that goal one of the goals "
               "below, with the PR as its evidence. The first six fields are about the main goal only: what is done "
               "and left for another goal goes in that goal's fields, not in these.\n",
    "anchor": "Sessions, a long one most of all, can work on more than one goal. The main goal is the task's own: the "
              "work of the ticket or branch named above. A task with neither: the goal with the most data outside the "
              f"sessions (PRs, tickets, commits), or the newest when that ties. {STEP} The first six fields are about "
              "the main goal only: what is done and left for another goal goes in that goal's fields, not in these.\n",
}


def prompt_for(variant: str) -> str:
    """work.NOTE_PROMPT as it is ("shipped"), or with a variant's goal paragraph."""
    return _P if variant == "shipped" else _P[:_START] + VARIANTS[variant] + _P[_END:]


DATED = """Sessions, a long one most of all, can work on more than one goal. List the goals (1 to 3) that these
sessions did work toward: changed code, made a commit or PR, reviewed a PR, ran a job, made a decision. Explaining
code, finding a document or answering a question is not work toward a goal. """ + STEP + """ Each PR, commit and
ticket in the data belongs to the goal it was made for.
For each goal:
- title: at most 8 words that name the goal as an outcome, in sentence case, with no ticket key or branch name.
- problem: at most 25 words on what is wrong or missing.
- goal: at most 20 words on what "done" looks like, and why it matters.
- state: at most 50 words on where it stands: what has been done, the key findings (a cause found, a number measured,
  an idea ruled out, with what it was), and what was decided and why.
- next: at most 25 words on what needs to be done; "Nothing" if it is done.
- question: at most 20 words on the question that a session asked the developer about it and that is not answered;
  "None" if there is none.
- evidence: the PR numbers (#12), ticket keys and commit hashes from the data above that its work is in.
- last_work: the date (YYYY-MM-DD) of the newest data point that did work toward it.
"""
_GOAL = {"type": "object", "properties": {**{k: {"type": "string"} for k in ("title",) + work.CORE + ("last_work",)},
                                          "evidence": {"type": "array", "items": {"type": "string"}}},
         "required": ["title", *work.CORE, "evidence", "last_work"], "additionalProperties": False}
DATED_SCHEMA = {"type": "object", "properties": {"goals": {"type": "array", "items": _GOAL}}, "required": ["goals"],
                "additionalProperties": False}


def make_dated(t: dict) -> dict:
    """The note in work.make_note's form, with the main goal picked by code: the goal with the newest work, then the
    one with the most evidence."""
    from pensieve import llm
    real = lambda g: sum(len(v) for v in work._evidence(t, g.get("evidence")).values())
    prompt = (_P[:_START] + DATED + _P[_P.index("State facts"):]).format(**work.note_fields(t))
    ans = llm.complete(prompt, max_tokens=900, schema=DATED_SCHEMA, kind="task note")
    gs = [g for g in ans.get("goals", []) if g.get("title")]
    if not gs:
        return {}
    gs.sort(key=lambda g: (str(g.get("last_work", "")), real(g)), reverse=True)
    main, rest = gs[0], gs[1:3]
    note = {k: str(main.get(k, "")).strip() for k in ("title",) + work.CORE}
    note["evidence"] = [str(x) for x in main.get("evidence") or []]
    note["goals"] = [{"title": g["title"], "evidence": g.get("evidence") or [],
                      **{k: str(g.get(k, "")).strip() for k in ("state", "next", "question")}} for g in rest]
    return note


MAKERS = {"dated": make_dated}


def freeze(a):
    """The tasks as work.tasks builds them before any note splits them, from PENSIEVE_DATA's index."""
    from pensieve import config
    from threads_replay import _Store
    work._goal_tasks = lambda out, have: []
    ids = [x.strip() for x in a.ids.split(",") if x.strip()]
    ts = [t for t in work._tasks(_Store(config.DATA_DIR / "pensieve.db"), 30) if t["id"] in ids]
    OUT.mkdir(parents=True, exist_ok=True)
    have = json.loads((OUT / "tasks.json").read_text()) if (OUT / "tasks.json").exists() else []
    ts = [t for t in have if t["id"] not in ids] + ts  # tasks frozen before stay as they were
    (OUT / "tasks.json").write_text(json.dumps(ts))
    print(f"froze {len(ts)} of {len(ids)} tasks")


def _ref(x) -> str:
    """#12 for a PR url, the first 8 characters of a commit, a ticket key as it is."""
    if isinstance(x, dict):
        return x["sha"][:8]
    m = re.search(r"/pull/(\d+)", x)
    return f"#{m.group(1)}" if m else x


def placement(t: dict, note: dict) -> dict:
    """Where each ref lands: the index of the split goal that names it, or 0 for the main card (which keeps
    everything no split goal takes)."""
    n = work._split_goals(t, note)
    where = {}
    for i, s in enumerate(n["split"], 1):
        for r in s["prs"] + s["keys"] + s["commits"]:
            where.setdefault(_ref(r), i)
    return where


def structure(label: dict, where: dict) -> tuple[int, int]:
    """(groups right, groups checked): a together group with every ref in one goal, an apart group set with each
    group in a goal of its own."""
    at = lambda r: where.get(r[:8] if re.fullmatch(r"[0-9a-f]{7,40}", r) else r, 0)
    ok = n = 0
    for g in label.get("together", []):
        n += 1
        ok += len({at(r) for r in g}) == 1
    if label.get("apart"):
        n += 1
        homes = [{at(r) for r in g} for g in label["apart"]]
        ok += all(len(h) == 1 for h in homes) and len({next(iter(h)) for h in homes}) == len(homes)
    return ok, n


JUDGE = """A note about a developer's task names its main goal. For each note below, say whether its main goal is one
of the right ones (the same work, in any words), and not another goal of the same sessions. A note whose goal is a
wrong one, or is only near a right one, is false.
Right main goals: {main}
Wrong main goals: {wrong}

{notes}

Answer with JSON only: {{"right": [true or false for each note, in order]}}"""


def judge(label: dict, notes: list[dict], model: str) -> list[bool]:
    text = JUDGE.format(main=" | ".join(label["main"]), wrong=" | ".join(label.get("wrong", [])) or "(none listed)", notes="\n".join(
        f"{i + 1}. title: {n.get('title', '(none)')}; goal: {n.get('goal', '(none)')}" for i, n in enumerate(notes)))
    for _ in range(3):
        p = subprocess.run(["claude", "-p", text, "--model", model, "--no-session-persistence", "--tools", "",
                            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", ""],
                           capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
                           cwd=tempfile.gettempdir())
        out = p.stdout
        try:
            right = json.loads(out[out.index("{"):out.rindex("}") + 1])["right"]
            if len(right) == len(notes):
                return [bool(x) for x in right]
        except (ValueError, KeyError):
            pass
    return [False] * len(notes)


def run(a):
    ts = {t["id"]: t for t in json.loads((OUT / "tasks.json").read_text())}
    labels = [json.loads(x) for x in (OUT / a.labels).read_text().splitlines() if x.strip()]
    labels = [lb for lb in labels if lb["id"] in ts and (not a.only or lb["id"] in a.only.split(","))]
    for v in a.variants.split(","):
        if v not in MAKERS:
            work.NOTE_PROMPT = prompt_for(v)
        jobs = [(lb, r) for lb in labels for r in range(a.reps)]
        with ThreadPoolExecutor(a.jobs) as pool:
            notes = list(pool.map(lambda j: _note(ts[j[0]["id"]], MAKERS.get(v, work.make_note)), jobs))
        by = {}
        for (lb, _), n in zip(jobs, notes):
            by.setdefault(lb["id"], []).append(n)
        with ThreadPoolExecutor(a.jobs) as pool:
            right = dict(zip(by, pool.map(lambda i: judge(next(x for x in labels if x["id"] == i), by[i], a.judge_model), by)))
        with (OUT / "runs.jsonl").open("a") as f:
            for lb in labels:
                for n, ok in zip(by[lb["id"]], right[lb["id"]]):
                    s_ok, s_n = structure(lb, placement(ts[lb["id"]], n)) if n.get("title") else (0, 0)
                    f.write(json.dumps(dict(variant=v, label=a.label, task=lb["id"], main=ok, struct=s_ok, struct_n=s_n,
                                            title=n.get("title"), goals=[g["title"] for g in n.get("goals", [])])) + "\n")
        print(f"{v}: done", flush=True)


def _note(t, make):
    try:
        return make(t)
    except Exception as e:
        return {"error": str(e)}


def report(a):
    rows = [json.loads(x) for x in (OUT / "runs.jsonl").read_text().splitlines() if x.strip()]
    rows = [r for r in rows if not a.label or r["label"] == a.label]
    keys = sorted({(r["label"], r["variant"]) for r in rows})
    print(f"{'label':<12}{'variant':<14}{'notes':>6}{'main':>7}{'struct':>8}{'both':>7}")
    for k in keys:
        rs = [r for r in rows if (r["label"], r["variant"]) == k]
        st = sum(r["struct"] for r in rs) / max(sum(r["struct_n"] for r in rs), 1)
        both = sum(r["main"] and r["struct"] == r["struct_n"] for r in rs) / len(rs)
        print(f"{k[0]:<12}{k[1]:<14}{len(rs):>6}{sum(r['main'] for r in rs) / len(rs):>7.2f}{st:>8.2f}{both:>7.2f}")
    if a.tasks:
        for k in keys:
            print(f"\n{k[0]} {k[1]}:")
            for tid in dict.fromkeys(r["task"] for r in rows):
                rs = [r for r in rows if (r["label"], r["variant"]) == k and r["task"] == tid]
                print(f"  {tid[-28:]:<30} main {sum(r['main'] for r in rs)}/{len(rs)}  struct "
                      f"{sum(r['struct'] for r in rs)}/{sum(r['struct_n'] for r in rs)}  " +
                      " | ".join(f"{r['title']}" + (f" (+{len(r['goals'])})" if r["goals"] else "") for r in rs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["freeze", "run", "report"])
    ap.add_argument("--ids", default="")
    ap.add_argument("--variants", default="last")
    ap.add_argument("--only", default="")
    ap.add_argument("--labels", default="labels.jsonl", help="in bench/out/goals; labels-holdout.jsonl checks a winner")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--judge-model", default="sonnet")
    ap.add_argument("--label", default="v1", help="names a round of runs")
    ap.add_argument("--tasks", action="store_true", help="report: each task's runs")
    a = ap.parse_args()
    {"freeze": freeze, "run": run, "report": report}[a.cmd](a)


if __name__ == "__main__":
    main()
