"""Does Pensieve help an agent with everyday developer workflows? A/B benchmark with Claude Code itself.

Each task is a request a developer makes at the start of a session: pick up unfinished work, write a standup, list
open loops, recall why something was done, find a reviewer. A task has a rubric of facts checked against git, gh
and the session transcripts by hand, and traps (stale or wrong claims). A judge model scores each answer.

  .venv/bin/python bench/workflows.py run --arms base,pensieve --reps 2
  .venv/bin/python bench/workflows.py grade
  .venv/bin/python bench/workflows.py report

Tasks: bench/out/workflows/tasks.jsonl, one JSON object per line:
  {"id": "resume-1", "workflow": "resume", "repo": "~/code/my-app", "prompt": "...",
   "facts": ["the work is in the worktree ~/code/my-app-fix on branch fix-x", ...], "traps": ["says PR #12 is merged"]}

Arms (each a `claude -p` run in the task's repo; sessions are not saved, so they never reach the index):
  base       Read, Grep, Glob and read-only Bash (git, gh, ls, find, cat ...): what Claude Code has without Pensieve
  pensieve   base plus Pensieve's read-only MCP tools and the plugin's session-start instructions
  teammate   base in a fresh clone, with the owner's Desktop, Documents and ~/.claude closed: someone else's machine
  +v1, +v2   (suffix) the context card that the owner shared, from the task's "cards", added to the prompt
  +skills    (suffix) also loads the plugin's skills from pensieve/integrations/skills
  +memory    (suffix) keeps Claude Code's auto memory on (it is off otherwise)
  +inject    (suffix) adds what Pensieve's prompt hook adds to the prompt: a context card or code hits
  +injectjson (suffix) the same, with the card as the agent JSON (cards.render_agent) instead of the text card

The user's own CLAUDE.md, auto memory, hooks, plugins and every settings file stay out of each run; the repo's
CLAUDE.md stays in.
"""
import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = os.environ.get("PENSIEVE_BENCH_API", "http://127.0.0.1:8765")
OUT = Path(__file__).parent / "out" / "workflows"
ROOT = Path(__file__).parent.parent
INSTRUCTIONS = (ROOT / "pensieve" / "integrations" / "instructions.md").read_text()
SKILLS = ROOT / "pensieve" / "integrations" / "skills"

READ_ONLY_BASH = [f"Bash({c}:*)" for c in (
    "git log", "git show", "git status", "git branch", "git diff", "git worktree list", "git for-each-ref",
    "git rev-list", "git rev-parse", "git blame", "git shortlog", "git remote", "git reflog", "git ls-files",
    "git grep", "git -C", "gh pr list", "gh pr view", "gh pr status", "gh pr checks", "gh issue list", "gh issue view",
    "gh search", "gh repo view", "ls", "find", "cat", "head", "tail", "wc", "grep", "rg", "jq", "cd", "pwd", "date",
    "sort", "uniq", "tree", "stat", "basename", "dirname", "echo")]
# Pensieve's tools that only read; set_task_state, open, settings and setup tools stay out
PENSIEVE_TOOLS = [f"mcp__pensieve__{t}" for t in (
    "search", "read", "similar", "who_knows", "overview", "team", "unexplored", "knowledge_gaps", "stale", "tasks",
    "task", "list_repos", "current_scope")]
# a repo's own settings can allow much more (deploys, checkouts), so runs load no settings files and deny these too
OFF = ",".join(["Edit", "Write", "NotebookEdit", "Task", "Agent", "WebFetch", "WebSearch", "TodoWrite"] + [
    f"Bash({c}:*)" for c in ("git checkout", "git switch", "git reset", "git commit", "git push", "git fetch", "git pull",
                             "git merge", "git rebase", "git stash", "git worktree add", "git worktree remove",
                             "git branch -d", "git branch -D", "gh pr merge", "gh pr create", "gh pr edit", "gh pr close",
                             "gh pr comment", "gh pr ready", "gh pr review", "gh api", "curl", "rm", "mv")])
PROMPT_TAIL = "\n\nOnly look and report: do not change files, branches, PRs or tickets."
_HOME = str(Path.home())


def isolation(memory=False):
    """Settings that keep the user's own CLAUDE.md and auto memory out of a run."""
    return json.dumps({"claudeMdExcludes": [f"{_HOME}/.claude/CLAUDE.md", "**/.claude/CLAUDE.md"],
                       "autoMemoryEnabled": memory})


def skills_plugin():
    """A throwaway plugin that holds only the repo's skills (the real plugin also starts an MCP server and hooks)."""
    d = Path(tempfile.mkdtemp(prefix="pensieve-skills-"))
    (d / ".claude-plugin").mkdir()
    (d / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "pensieve", "version": "0.0.0",
                                                                  "skills": "./skills/"}))
    shutil.copytree(SKILLS, d / "skills")
    return d


def arm_config(arm, root):
    base, *extra = arm.split("+")
    unknown = set(extra) - {"skills", "memory", "inject", "injectjson", "v1", "v2", "v3"}
    if unknown:
        raise SystemExit(f"unknown arm suffix {unknown}")
    c = dict(tools="Read,Grep,Glob,Bash", allowed=["Read", "Grep", "Glob", *READ_ONLY_BASH], mcp={}, system=None,
             memory="memory" in extra, skills="skills" in extra,
             inject="json" if "injectjson" in extra else "text" if "inject" in extra else None,
             card=next((x for x in extra if x in ("v1", "v2", "v3")), None))
    if base == "teammate":  # someone else's machine: no Pensieve, and the owner's folders and agent files are off
        c["off"] = [f"{tool}(/{_HOME}/{d}/**)" for tool in ("Read", "Grep", "Glob") for d in ("Desktop", ".claude", "Documents")]
    elif base == "pensieve":
        c["mcp"] = {"pensieve": {"type": "http", "url": f"{API}/mcp?" + urllib.parse.urlencode(dict(scope="auto", cwd=root))}}
        c["allowed"] += PENSIEVE_TOOLS + ["Skill"]
        c["system"] = INSTRUCTIONS  # what the plugin's SessionStart hook adds
    elif base != "base":
        raise SystemExit(f"unknown arm {base}")
    if c["skills"]:
        c["tools"] += ",Skill"
    return c


def claude(prompt, cwd, model, c, budget=2.0, timeout=900):
    """One headless Claude Code run: the final result plus each tool call with a short form of its input."""
    plugin = skills_plugin() if c["skills"] else None
    cmd = ["claude", "-p", prompt, "--model", model, "--no-session-persistence", "--output-format", "stream-json",
           "--verbose", "--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": c["mcp"]}),
           "--tools", c["tools"], "--allowedTools", ",".join(c["allowed"]),
           "--disallowedTools", ",".join([OFF, *c.get("off", [])]),
           "--setting-sources", "", "--settings", isolation(c["memory"]),
           "--max-budget-usd", str(budget)]
    cmd += ["--append-system-prompt", c["system"]] if c["system"] else []
    cmd += ["--plugin-dir", str(plugin)] if plugin else []
    t = time.time()
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    finally:
        if plugin:
            shutil.rmtree(plugin, ignore_errors=True)
    calls, denied, final = [], 0, {}
    for line in p.stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "assistant":
            for x in e["message"].get("content", []):
                if x.get("type") == "tool_use":
                    inp = x.get("input") or {}
                    arg = inp.get("command") or inp.get("query") or inp.get("pattern") or inp.get("file_path") or \
                        inp.get("id") or inp.get("skill") or json.dumps(inp)[:120]
                    calls.append([x["name"], str(arg)[:160]])
        elif e.get("type") == "user":
            content = (e.get("message") or {}).get("content")
            denied += sum(1 for x in content if isinstance(x, dict) and x.get("is_error") and
                          "permission" in str(x.get("content", "")).lower()) if isinstance(content, list) else 0
        elif e.get("type") == "result":
            final = e
    mu = final.get("modelUsage") or {}
    error = (p.stderr[-500:] or "no result") if not final else \
        (f"{final.get('subtype')}: {(final.get('result') or '')[:300]}" if final.get("is_error") else None)
    return dict(result=final.get("result") or "", error=error, cost=final.get("total_cost_usd"),
                turns=final.get("num_turns"), secs=round(time.time() - t, 1), calls=calls, denied=denied,
                tokens=sum(m.get("inputTokens", 0) + m.get("cacheReadInputTokens", 0) +
                           m.get("cacheCreationInputTokens", 0) + m.get("outputTokens", 0) for m in mu.values()),
                usage={k: sum(m.get(f, 0) for m in mu.values()) for k, f in (
                    ("input", "inputTokens"), ("cache_write", "cacheCreationInputTokens"),
                    ("cache_read", "cacheReadInputTokens"), ("output", "outputTokens"))})


def injected(prompt, root, card="text"):
    """The prompt as Claude sees it with Pensieve's UserPromptSubmit hook: the hook's answer (a context card, as text
    or as the agent JSON, or code hits) from /api/prompt-context appended, as Claude Code adds a hook's
    additionalContext."""
    body = json.dumps({"prompt": prompt, "cwd": root, "hook_event_name": "UserPromptSubmit"}).encode()
    r = urllib.request.Request(f"{API}/api/prompt-context?card={card}", body, {"content-type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as resp:
        ctx = (json.load(resp).get("hookSpecificOutput") or {}).get("additionalContext")
    return f"{prompt}\n\n{ctx}" if ctx else prompt


def load_tasks(name="tasks.jsonl"):
    return [json.loads(l) for l in (OUT / name).read_text().splitlines() if l.strip()]


def runs_path(a):
    return OUT / f"runs-{a.model}-{a.label}.jsonl"


def run(a):
    tasks = load_tasks(a.tasks)
    if a.only:
        tasks = [t for t in tasks if t["id"] in a.only.split(",") or t["workflow"] in a.only.split(",")]
    path = runs_path(a)
    done = set()
    if path.exists():
        done = {(r["task"], r["arm"], r["rep"]) for r in map(json.loads, path.read_text().splitlines())}
    jobs = [(t, arm, rep) for rep in range(a.reps) for t in tasks for arm in a.arms.split(",")
            if (t["id"], arm, rep) not in done]
    random.Random(0).shuffle(jobs)  # interleave arms so drift (load, prompt caching) doesn't favour one
    print(f"{len(jobs)} runs ({len(done)} already done) -> {path}", file=sys.stderr)

    def one(job):
        t, arm, rep = job
        root = str(Path(t["repo"]).expanduser())
        c = arm_config(arm, root)
        prompt = t["prompt"] + PROMPT_TAIL
        if c["card"]:  # the context card the owner shared, as the reader's agent gets it (tasks-shared.jsonl)
            prompt += "\n\nTed shared this context card with me:\n" + Path(t["cards"][c["card"]]).read_text()
        r = claude(injected(prompt, root, c["inject"]) if c["inject"] else prompt, root, a.model, c, a.budget)
        rec = dict(task=t["id"], workflow=t["workflow"], arm=arm, rep=rep, **r)
        print(f"{t['id']:<10} {arm:<16} ${r['cost'] or 0:.3f} {len(r['calls']):>2} calls {r['secs']:>6}s"
              f"{'  ERROR ' + r['error'][:80] if r['error'] else ''}", file=sys.stderr)
        return rec

    with ThreadPoolExecutor(a.jobs) as ex, path.open("a") as f:
        for rec in ex.map(one, jobs):
            f.write(json.dumps(rec) + "\n")
            f.flush()


JUDGE = """You grade an AI coding agent's answer to a developer's request. Today is {today}.

<request>
{prompt}
</request>

<answer>
{answer}
</answer>

Facts that a complete answer states (checked by hand against git, GitHub and the session transcripts):
{facts}

Wrong or stale claims to watch for:
{traps}

For each fact, decide whether the answer states it or something equivalent (same meaning, any wording; a path may be
given relative to the repo or with ~). A fact the answer contradicts is not stated. Then list each claim in the answer
that is false according to the facts, or that matches a wrong claim above. Do not count claims that the facts don't
cover. Reply with only a JSON object:
{{"facts": [1 or 0 for each fact, in order], "wrong": ["short description of each false claim"]}}"""


def judge(t, answer, model):
    prompt = JUDGE.format(today=time.strftime("%Y-%m-%d"), prompt=t["prompt"], answer=answer or "(no answer)",
                          facts="\n".join(f"{i + 1}. {f}" for i, f in enumerate(t["facts"])),
                          traps="\n".join(f"- {x}" for x in t.get("traps", [])) or "- (none)")
    for _ in range(3):
        p = subprocess.run(["claude", "-p", prompt, "--model", model, "--no-session-persistence", "--tools", "",
                            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
                            "--settings", isolation()], capture_output=True, text=True, timeout=300,
                           stdin=subprocess.DEVNULL, cwd=tempfile.gettempdir())
        text = p.stdout.strip()
        try:
            g = json.loads(text[text.index("{"):text.rindex("}") + 1])
            if len(g["facts"]) == len(t["facts"]):
                return g
        except (ValueError, KeyError):
            pass
    return None


def grade(a):
    tasks = {t["id"]: t for t in load_tasks(a.tasks)}
    path = runs_path(a)
    runs = [json.loads(l) for l in path.read_text().splitlines()]
    todo = [r for r in runs if "score" not in r and r["task"] in tasks]
    print(f"grading {len(todo)} of {len(runs)} runs", file=sys.stderr)

    def one(r):
        g = judge(tasks[r["task"]], r["result"], a.judge_model)
        if g:
            r["facts"], r["wrong"] = g["facts"], g.get("wrong", [])
            r["score"] = sum(g["facts"]) / len(g["facts"])
        return r

    with ThreadPoolExecutor(a.jobs) as ex:
        list(ex.map(one, todo))
    path.write_text("".join(json.dumps(r) + "\n" for r in runs))


def report(a):
    runs = [json.loads(l) for l in runs_path(a).read_text().splitlines()]
    runs = [r for r in runs if "score" in r]
    arms = sorted({r["arm"] for r in runs}, key=lambda x: (x != "base", x))
    mean = lambda xs: sum(xs) / len(xs) if xs else 0
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else 0
    print(f"{a.model}, {len({r['task'] for r in runs})} tasks, {len(runs)} graded runs\n")
    print(f"{'arm':<18}{'runs':>5}{'facts':>8}{'wrong':>7}{'calls':>7}{'med calls':>10}{'$/run':>8}{'tokens':>10}"
          f"{'secs':>7}  tools")
    for arm in arms:
        rs = [r for r in runs if r["arm"] == arm]
        tools = {}
        for r in rs:
            for name, arg in r["calls"]:
                k = name.replace("mcp__pensieve__", "p:") if name != "Bash" else "Bash:" + arg.split()[0] + \
                    (" " + arg.split()[1] if len(arg.split()) > 1 and arg.split()[0] in ("git", "gh") else "")
                tools[k] = tools.get(k, 0) + 1
        top = ", ".join(f"{k} {v}" for k, v in sorted(tools.items(), key=lambda x: -x[1])[:8])
        print(f"{arm:<18}{len(rs):>5}{mean([r['score'] for r in rs]):>8.2f}{mean([len(r['wrong']) for r in rs]):>7.2f}"
              f"{mean([len(r['calls']) for r in rs]):>7.1f}{med([len(r['calls']) for r in rs]):>10}"
              f"{mean([r['cost'] or 0 for r in rs]):>8.3f}{int(mean([r['tokens'] for r in rs])):>10,}"
              f"{mean([r['secs'] for r in rs]):>7.0f}  {top}")
    print("\nby workflow (facts / calls):")
    for w in sorted({r["workflow"] for r in runs}):
        print(f"  {w:<10}" + "   ".join(
            f"{arm} {mean([r['score'] for r in runs if r['workflow'] == w and r['arm'] == arm]):.2f}/"
            f"{mean([len(r['calls']) for r in runs if r['workflow'] == w and r['arm'] == arm]):.0f}" for arm in arms))
    print("\nby task (facts / calls / wrong):")
    for tid in sorted({r["task"] for r in runs}):
        cells = []
        for arm in arms:
            rs = [r for r in runs if r["task"] == tid and r["arm"] == arm]
            cells.append(f"{arm} {mean([r['score'] for r in rs]):.2f}/{mean([len(r['calls']) for r in rs]):.0f}/"
                         f"{sum(len(r['wrong']) for r in rs)}")
        print(f"  {tid:<10}" + "   ".join(cells))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "grade", "report"])
    ap.add_argument("--arms", default="base,pensieve")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--judge-model", default="sonnet")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--budget", type=float, default=2.0, help="max $ per agent run")
    ap.add_argument("--only", help="comma-separated task ids or workflows")
    ap.add_argument("--tasks", default="tasks.jsonl")
    ap.add_argument("--label", default="v1", help="names the runs file")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    {"run": run, "grade": grade, "report": report}[a.cmd](a)


if __name__ == "__main__":
    main()
