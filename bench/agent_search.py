"""Does Pensieve help an agent find code? A/B benchmark with Claude Code itself.

Tasks are made from the index: a code chunk is described by behaviour only (no identifiers, file names or
string literals, so plain grep can't shortcut it) and the agent is asked which file holds it. The answer is the
chunk's file.

  uv run python bench/agent_search.py gen  --repo ~/Desktop/databricks-bfp -n 10   # -> bench/out/<repo>/tasks.jsonl
  uv run python bench/agent_search.py find --repo ~/Desktop/databricks-bfp         # Pensieve search alone, no agent
  uv run python bench/agent_search.py run  --repo ~/Desktop/databricks-bfp --arms grep,pensieve
  uv run python bench/agent_search.py report --repo ~/Desktop/databricks-bfp

Arms (each a `claude -p` run in the repo, sessions not saved so they never reach the index):
  grep      Read, Grep, Glob only
  pensieve  the same plus Pensieve's search/read/similar MCP tools, scoped to the repo
  nudged    like pensieve, plus one system-prompt line saying to search by meaning with Pensieve first
  guided    like pensieve, plus the CLAUDE.md instructions block from pensieve/integrations/instructions.md
  only      Pensieve's MCP tools plus Read (no Grep/Glob)
"""
import argparse
import json
import random
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DB = Path.home() / ".pensieve" / "pensieve.db"
API = "http://127.0.0.1:8765"
OUT = Path(__file__).parent / "out"
SRC_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".sql", ".scala", ".go", ".rs", ".java", ".kt", ".rb", ".sh", ".swift",
           ".vue", ".php", ".cs", ".c", ".cc", ".cpp", ".h"}
SKIP = re.compile(r"(^|/)(tests?|__tests__|spec|fixtures?|migrations|vendor|node_modules|dist|build|\.[^/]+)/|"
                  r"(_test|\.test|\.spec|_pb2)\.|/__init__\.py$")
ALWAYS_OFF = "Bash,Edit,Write,NotebookEdit,Task,Agent,WebFetch,WebSearch,TodoWrite"
PENSIEVE_TOOLS = ["mcp__pensieve__search", "mcp__pensieve__read", "mcp__pensieve__similar"]
NUDGE = ("To find code by what it does, search by meaning with mcp__pensieve__search first; "
         "use Grep/Glob for exact names or to confirm.")


def out_dir(repo):
    d = OUT / Path(repo).name
    d.mkdir(parents=True, exist_ok=True)
    return d


def claude(prompt, cwd, model, tools, allowed, mcp=None, budget=1.0, timeout=600, system=None):
    """One headless Claude Code run. Returns the final result event plus the tool calls it made."""
    cmd = ["claude", "-p", prompt, "--model", model, "--no-session-persistence", "--output-format", "stream-json",
           "--verbose", "--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": mcp or {}}),
           "--tools", tools, "--allowedTools", allowed, "--disallowedTools", ALWAYS_OFF,
           "--max-budget-usd", str(budget)] + (["--append-system-prompt", system] if system else [])
    t = time.time()
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    calls, final = [], {}
    for line in p.stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "assistant":
            calls += [c["name"] for c in e["message"].get("content", []) if c.get("type") == "tool_use"]
        elif e.get("type") == "result":
            final = e
    mu = final.get("modelUsage") or {}
    return dict(result=final.get("result") or "", error=None if final else (p.stderr[-500:] or "no result"),
                cost=final.get("total_cost_usd"), turns=final.get("num_turns"), secs=round(time.time() - t, 1),
                tokens=sum(m.get("inputTokens", 0) + m.get("cacheReadInputTokens", 0) +
                           m.get("cacheCreationInputTokens", 0) + m.get("outputTokens", 0) for m in mu.values()),
                calls=calls)


DESCRIBE = """Here is a chunk of code from a repository.

<code>
{code}
</code>

Write two things a developer might say when looking for this code, in plain language:
- "desc": one or two sentences describing what it does and why
- "query": a short search an agent would type, at most 10 words

Strict rules for both: do not use any identifier from the code (function, class, variable, table, column, file
or module names), no string literals, no file paths, no quotes, no camelCase or snake_case words.
If the chunk is too generic or trivial to describe distinctively (e.g. only imports, config, boilerplate),
reply exactly SKIP. Otherwise reply with only a JSON object: {{"desc": "...", "query": "..."}}"""


def leaks(desc, code):
    """Identifiers from the code that made it into the description (shared plain English words are fine)."""
    raw = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", code))
    words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", desc))
    return sorted(w for w in words & raw if "_" in w or re.search(r"[a-z][A-Z]", w) or (w.isupper() and len(w) > 3))


def gen(a):
    root = str(Path(a.repo).expanduser().resolve())
    db = sqlite3.connect(DB)
    rows = db.execute("SELECT id, path, start, end, text FROM code_chunks WHERE repo=? AND wt=''", (root,)).fetchall()
    files = {}
    for cid, path, s, e, text in rows:
        if Path(path).suffix in SRC_EXT and not SKIP.search("/" + path) and 400 <= len(text) <= 4000:
            files.setdefault(path, []).append((cid, s, e, text))
    print(f"{len(rows)} chunks, {len(files)} eligible source files", file=sys.stderr)
    rng = random.Random(a.seed)
    paths = sorted(files)
    rng.shuffle(paths)
    picks = [(p, rng.choice(files[p])) for p in paths[:int(a.n * 1.4) + 5]]  # spare picks for SKIPs and leaks

    def describe(pick):
        path, (cid, s, e, text) = pick
        # run outside the repo, with no tools: the describer only sees the chunk
        r = claude(DESCRIBE.format(code=text), "/private/tmp", a.gen_model, "", "")
        m = re.search(r"\{.*\}", r["result"], re.S)
        try:
            d = json.loads(m.group(0)) if m else None
        except ValueError:
            d = None
        if not d or not d.get("desc") or not d.get("query"):
            return None
        bad = leaks(d["desc"] + " " + d["query"], text)
        if bad:
            print(f"  leak {path}: {bad}", file=sys.stderr)
            return None
        return dict(repo=root, chunk=cid, file=path, lines=[s, e], desc=d["desc"].strip(), query=d["query"].strip())

    with ThreadPoolExecutor(a.jobs) as ex:
        made = [t for t in ex.map(describe, picks) if t][:a.n]
    name = Path(root).name
    for i, t in enumerate(made):
        t["id"] = f"{name}-{i + 1:03d}"
        t["split"] = "test" if i % 3 == 2 else "dev"
    (out_dir(root) / a.tasks).write_text("".join(json.dumps(t) + "\n" for t in made))
    print(f"{len(made)} tasks from {len(picks)} picks -> {out_dir(root) / a.tasks}")


def load_tasks(repo, name="tasks.jsonl"):
    return [json.loads(l) for l in (out_dir(repo) / name).read_text().splitlines() if l.strip()]


def find(a):
    """Pensieve's own search (the live /api/find), no agent: the rank of the answer file among distinct files,
    per query style. --repo all scores every repo with generated tasks."""
    roots = ([json.loads(f.read_text().splitlines()[0])["repo"] for f in sorted(OUT.glob(f"*/{a.tasks}"))]
             if a.repo == "all" else [str(Path(a.repo).expanduser().resolve())])
    res, t0 = {}, time.time()
    for root in roots:
        for t in load_tasks(root, a.tasks):
            if a.split != "all" and t.get("split", "dev") != a.split:
                continue
            for style in a.styles.split(","):
                if not t.get(style):
                    continue
                q = urllib.parse.urlencode(dict(q=t[style], limit=50, kinds="code", scope="auto", cwd=root))
                out = json.load(urllib.request.urlopen(f"{API}/api/find?{q}", timeout=120))["results"]
                seen = []
                for r in out:
                    p = r.get("path") or ""
                    rel = p[len(root) + 1:] if p.startswith(root + "/") else None
                    if rel is None:  # a worktree copy: match on the repo-relative path after the worktree root
                        rel = next((p[len(w) + 1:] for w in [p[:p.find("/" + t["file"])]] if p.endswith(t["file"])), None)
                    if rel and rel not in seen:
                        seen.append(rel)
                answers = t.get("files") or (t.get("ident_files") if style == "ident" else None) or [t["file"]]
                rank = min((seen.index(f) + 1 for f in answers if f in seen), default=999)
                res.setdefault(style, []).append(rank)
                if a.show:
                    print(f"{t['id']} {style:<7} {rank:>4}  {t[style][:70]}")
    n_q = sum(len(v) for v in res.values())
    print(f"{n_q} queries in {time.time() - t0:.0f}s ({(time.time() - t0) / max(1, n_q) * 1000:.0f} ms each), split {a.split}")
    print(f"{'style':<9}{'R@1':>6}{'R@5':>6}{'R@10':>6}{'MRR':>7}{'n':>5}")
    for style, rs in res.items():
        n = len(rs)
        print(f"{style:<9}{sum(r <= 1 for r in rs) / n:6.2f}{sum(r <= 5 for r in rs) / n:6.2f}"
              f"{sum(r <= 10 for r in rs) / n:6.2f}{sum(1 / r for r in rs) / n:7.3f}{n:5}")


PROMPT = """Find where this is implemented in the repository you are in:

"{desc}"

Search efficiently. When you are confident, end your reply with a single line:
ANSWER: <path of the file, relative to the repository root>"""
ASK = {  # how the request reads for each query style
    "desc": 'Find where this is implemented in the repository you are in:\n\n"{q}"',
    "query": 'Find where this is implemented in the repository you are in:\n\n"{q}"',
    "mixed": 'Find where this is implemented in the repository you are in:\n\n"{q}"',
    "ident": "Find the file in the repository you are in that defines `{q}`.",
    "literal": 'Find the code in the repository you are in that contains the text "{q}".',
}
ANSWER = """

Search efficiently. When you are confident, end your reply with a single line:
ANSWER: <path of the file, relative to the repository root>"""
MIX = ["query", "ident", "desc", "literal", "mixed"]
GUIDE = (Path(__file__).parent.parent / "pensieve" / "integrations" / "instructions.md").read_text()


def styled(tasks):
    """One query style per task, rotating through MIX (skipping styles a task doesn't have)."""
    out = []
    for i, t in enumerate(tasks):
        for k in range(len(MIX)):
            st = MIX[(i + k) % len(MIX)]
            if t.get(st):
                out.append({**t, "style": st, "prompt": ASK[st].format(q=t[st]) + ANSWER})
                break
    return out


def arm_config(arm, root):
    mcp = {"pensieve": {"type": "http", "url": f"{API}/mcp?" + urllib.parse.urlencode(dict(scope="auto", cwd=root))}}
    if arm == "grep":
        return dict(tools="Read,Grep,Glob", allowed="Read,Grep,Glob", mcp=None)
    if arm in ("pensieve", "nudged", "guided"):
        # guided: the instructions block users can add to CLAUDE.md (pensieve/integrations/instructions.md)
        system = NUDGE if arm == "nudged" else GUIDE if arm == "guided" else None
        return dict(tools="Read,Grep,Glob", allowed=",".join(["Read", "Grep", "Glob", *PENSIEVE_TOOLS]), mcp=mcp,
                    system=system)
    if arm == "only":
        return dict(tools="Read", allowed=",".join(["Read", *PENSIEVE_TOOLS]), mcp=mcp)
    raise SystemExit(f"unknown arm {arm}")


def grade(answer_text, t, root):
    m = re.findall(r"ANSWER:\s*`?([^\s`]+)`?", answer_text)
    if not m:
        return None, False
    got = m[-1].strip().rstrip(".")
    if got.startswith(root + "/"):
        got = got[len(root) + 1:]
    if got.startswith("./"):
        got = got[2:]
    ok = t.get("ident_files") if t.get("style") == "ident" and t.get("ident_files") else [t["file"]]
    return got, got in ok


def run(a):
    root = str(Path(a.repo).expanduser().resolve())
    tasks = load_tasks(root, a.tasks)
    if a.tasks != "tasks.jsonl":
        tasks = styled([t for t in tasks if a.split == "all" or t.get("split") == a.split])
    if a.only:
        tasks = [t for t in tasks if t["id"] in a.only.split(",")]
    path = out_dir(root) / (f"runs-{a.model}.jsonl" if a.tasks == "tasks.jsonl" else f"runs-{a.model}-{a.label}.jsonl")
    done = set()
    if path.exists():
        done = {(r["task"], r["arm"], r["rep"]) for r in map(json.loads, path.read_text().splitlines())}
    jobs = [(t, arm, rep) for rep in range(a.reps) for t in tasks for arm in a.arms.split(",")
            if (t["id"], arm, rep) not in done]
    random.Random(0).shuffle(jobs)  # interleave arms so drift (load, caching) doesn't favour one
    print(f"{len(jobs)} runs ({len(done)} already done) -> {path}", file=sys.stderr)

    def one(job):
        t, arm, rep = job
        c = arm_config(arm, root)
        r = claude(t.get("prompt") or PROMPT.format(desc=t["desc"]), root, a.model, c["tools"], c["allowed"], c["mcp"],
                   a.budget, system=c.get("system"))
        got, ok = grade(r["result"], t, root)
        rec = dict(task=t["id"], arm=arm, rep=rep, ok=ok, got=got, style=t.get("style", "desc"),
                   **{k: r[k] for k in ("cost", "turns", "secs", "tokens", "calls", "error")})
        print(f"{t['id']} {t.get('style', 'desc'):<7} {arm:<8} {'OK ' if ok else 'MISS'} ${rec['cost'] or 0:.3f} {rec['turns']} turns "
              f"{rec['secs']}s {len(rec['calls'])} calls  got={got}", file=sys.stderr)
        return rec

    with ThreadPoolExecutor(a.jobs) as ex, path.open("a") as f:
        for rec in ex.map(one, jobs):
            f.write(json.dumps(rec) + "\n")
            f.flush()


def report(a):
    root = str(Path(a.repo).expanduser().resolve())
    name = f"runs-{a.model}.jsonl" if a.tasks == "tasks.jsonl" else f"runs-{a.model}-{a.label}.jsonl"
    runs = [json.loads(l) for l in (out_dir(root) / name).read_text().splitlines()]
    arms = sorted({r["arm"] for r in runs}, key=["grep", "pensieve", "nudged", "guided", "only"].index)
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else 0
    print(f"{Path(root).name}, {a.model}, {len({r['task'] for r in runs})} tasks\n")
    print(f"{'arm':<9}{'found':>8}{'cost/task':>11}{'med tokens':>12}{'med calls':>11}{'med secs':>10}  tools used")
    for arm in arms:
        rs = [r for r in runs if r["arm"] == arm]
        tools = {}
        for r in rs:
            for c in r["calls"]:
                k = c.replace("mcp__pensieve__", "p:")
                tools[k] = tools.get(k, 0) + 1
        print(f"{arm:<9}{sum(r['ok'] for r in rs):>4}/{len(rs):<3}{sum(r['cost'] or 0 for r in rs) / len(rs):>11.3f}"
              f"{med([r['tokens'] for r in rs]):>12,}{med([len(r['calls']) for r in rs]):>11}"
              f"{med([r['secs'] for r in rs]):>10}  "
              + ", ".join(f"{k} {v}" for k, v in sorted(tools.items(), key=lambda x: -x[1])))
    styles = sorted({r.get("style", "desc") for r in runs})
    if len(styles) > 1:
        print("\nfound, by query style:")
        for st in styles:
            print(f"  {st:<8}" + "   ".join(
                f"{arm} {sum(r['ok'] for r in runs if r['arm'] == arm and r.get('style') == st)}/"
                f"{sum(1 for r in runs if r['arm'] == arm and r.get('style') == st)}" for arm in arms))
    print("\nper task:")
    for tid in sorted({r["task"] for r in runs}):
        cells = []
        for arm in arms:
            rs = [r for r in runs if r["task"] == tid and r["arm"] == arm]
            cells.append(f"{arm} {''.join('✓' if r['ok'] else '✗' for r in rs)} {med([len(r['calls']) for r in rs])}c")
        print(f"  {tid}  " + "   ".join(cells))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["gen", "find", "run", "report"])
    ap.add_argument("--repo", required=True)
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--gen-model", default="sonnet")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--arms", default="grep,pensieve")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--budget", type=float, default=1.0, help="max $ per agent run")
    ap.add_argument("--only", help="comma-separated task ids")
    ap.add_argument("--tasks", default="tasks.jsonl", help="task file name under bench/out/<repo>/")
    ap.add_argument("--split", default="dev", help="dev | test | all")
    ap.add_argument("--styles", default="desc", help="query styles to score, e.g. query,desc,mixed,ident,literal")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--label", default="v2", help="names the runs file for generated task sets")
    a = ap.parse_args()
    {"gen": gen, "find": find, "run": run, "report": report}[a.cmd](a)


if __name__ == "__main__":
    main()
