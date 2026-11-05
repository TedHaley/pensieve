"""What an agent needs next to a code hit, read from disk: the file's own summary (its docstring or header comment),
the files next to it, the definition a line sits in, and the Databricks jobs that the folder defines with their
deployed state. Search finds the place; these say what the place is."""
import ast
import configparser
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HEAD_LINES = 60  # a file's summary sits in its first lines
ABOUT_CHARS = 200
# files that say what a folder's code is for or how it runs: shown first among its siblings
CONTEXT_EXT = {".yml", ".yaml", ".json", ".toml", ".md", ".cfg", ".ini", ".sql", ".sh"}
SKIP = {"__pycache__", "node_modules", ".git", ".DS_Store", "__init__.py"}
MAX_DEFINITION_LINES = 200


def _head(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return [ln for _, ln in zip(range(HEAD_LINES), f)]
    except OSError:
        return []


def _first_sentence(text: str) -> str | None:
    flat = re.sub(r"\s+", " ", text).strip()
    if not flat:
        return None
    m = re.match(r"(.+?[.!?])(?:\s|$)", flat)
    s = m.group(1) if m else flat
    return s[:ABOUT_CHARS] + ("…" if len(s) > ABOUT_CHARS else "")


# comment lines that instruct a tool, not the reader: skipped before a file's summary
_PRAGMA = re.compile(r"^\s*(#|//)\s*(!|-\*-|flake8|noqa|type:|pylint|mypy|ruff|isort|fmt:|eslint|prettier|@ts-|"
                     r"Databricks notebook source|MAGIC|COMMAND ---)", re.I)


def about(path: str) -> str | None:
    """The first sentence of a file's module docstring or leading comment, or None."""
    lines = _head(path)
    i = 0
    while i < len(lines) and (not lines[i].strip() or _PRAGMA.match(lines[i])):
        i += 1
    if i >= len(lines):
        return None
    first = lines[i].strip()
    m = re.match(r'^[rRuU]?("""|\'\'\')(.*)', first)
    if m:  # a Python docstring, to its closing quotes
        q, body = m.group(1), [m.group(2)]
        if q in body[0]:
            return _first_sentence(body[0].split(q)[0])
        for ln in lines[i + 1:]:
            if q in ln:
                body.append(ln.split(q)[0])
                break
            body.append(ln)
        return _first_sentence(" ".join(body))
    if first.startswith("/*"):  # a block comment
        body = []
        for ln in lines[i:]:
            body.append(re.sub(r"^\s*(/\*\*?|\*/|\*)", "", ln).split("*/")[0])
            if "*/" in ln:
                break
        return _first_sentence(" ".join(body))
    for mark in ("#", "//", "--", ";"):  # a run of line comments
        if first.startswith(mark):
            body = []
            for ln in lines[i:]:
                if not ln.strip().startswith(mark):
                    break
                body.append(ln.strip()[len(mark):])
            return _first_sentence(" ".join(body))
    return None


def siblings(path: str, limit: int = 8) -> list[str]:
    """The other entries of a file's folder: test folders and config or doc files first, then the rest. A folder
    ends with "/"."""
    p = Path(path)
    try:
        entries = [e for e in p.parent.iterdir() if e.name not in SKIP and not e.name.startswith(".") and e != p]
    except OSError:
        return []

    def rank(e):
        if e.is_dir():
            return 0 if "test" in e.name else 2
        return 1 if e.suffix in CONTEXT_EXT else 3
    entries.sort(key=lambda e: (rank(e), e.name))
    out = [e.name + ("/" if e.is_dir() else "") for e in entries[:limit]]
    if len(entries) > limit:
        out.append(f"+{len(entries) - limit} more")
    return out


def enclosing(path: str, line: int) -> dict | None:
    """The innermost function or class of a Python file that holds `line` (1-based), as {name, start, end, text}.
    None for another language, a line outside any definition, or a file that does not parse."""
    if not path.endswith(".py"):
        return None
    try:
        src = Path(path).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except (OSError, SyntaxError, ValueError):
        return None
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            if start <= line <= (node.end_lineno or start) and (best is None or start >= best[1]):
                best = (node.name, start, node.end_lineno or start)
    if not best:
        return None
    name, start, end = best
    lines = src.split("\n")
    cut = end - start + 1 > MAX_DEFINITION_LINES
    if cut:  # a long definition: its first 10 lines, then the lines around the hit
        lo = max(start + 10, line - MAX_DEFINITION_LINES // 2)
        gap = [f"    … lines {start + 10}-{lo - 1} …"] if lo > start + 10 else []
        rest = MAX_DEFINITION_LINES - 10 - len(gap)
        text = "\n".join(lines[start - 1:start + 9] + gap + lines[lo - 1:min(end, lo - 1 + rest)])
    else:
        text = "\n".join(lines[start - 1:end])
    return dict(name=name, start=start, end=end, text=text, truncated=cut)


def _mapping(text: str) -> dict:
    """The nested mappings of a YAML file, enough for Databricks bundle files: keys, scalar values and nesting by
    indent. Lists, anchors and multi-line strings are skipped."""
    root: dict = {}
    stack = [(-1, root)]
    for raw in text.split("\n"):
        if not raw.strip() or raw.lstrip().startswith(("#", "-")):
            continue
        m = re.match(r"^(\s*)([\w.${}\-]+):\s*(.*?)\s*$", raw)
        if not m:
            continue
        ind, key, val = len(m.group(1)), m.group(2), m.group(3)
        while stack[-1][0] >= ind:
            stack.pop()
        parent = stack[-1][1]
        if val and not val.startswith("#"):
            parent[key] = val.split(" #")[0].strip().strip("\"'")
        else:
            child: dict = {}
            parent[key] = child
            stack.append((ind, child))
    return root


def _job_files(path: str) -> list[Path]:
    p = Path(path)
    if p.suffix in (".yml", ".yaml"):
        return [p]
    try:
        return sorted(e for e in p.parent.iterdir() if e.suffix in (".yml", ".yaml"))
    except OSError:
        return []


def bundle_jobs(path: str) -> list[dict]:
    """The Databricks bundle jobs defined in a YAML file, or in the YAML files next to a code file:
    [{key, name, file}]. `name` keeps its ${bundle.target} placeholder."""
    out = []
    for f in _job_files(path):
        try:
            jobs = ((_mapping(f.read_text(encoding="utf-8", errors="replace")).get("resources") or {}).get("jobs")
                    or {})
        except OSError:
            continue
        for key, spec in jobs.items():
            if isinstance(spec, dict):
                out.append(dict(key=key, name=spec.get("name") or key, file=str(f)))
    return out


def _bundle_root(start: Path) -> Path | None:
    for d in [start, *start.parents]:
        f = d / "databricks.yml"
        if f.is_file() and "\ntargets:" in f.read_text(encoding="utf-8", errors="replace"):
            return f
    return None


def _profiles() -> dict[str, str]:
    """Host (no trailing /) -> the first ~/.databrickscfg profile for it. Account-level profiles are left out."""
    cfg = configparser.ConfigParser()
    try:
        cfg.read(os.path.expanduser(os.environ.get("DATABRICKS_CONFIG_FILE", "~/.databrickscfg")))
    except configparser.Error:
        return {}
    out = {}
    for name in cfg.sections():
        host = (cfg[name].get("host") or "").rstrip("/")
        if host and "accounts." not in host:
            out.setdefault(host, name)
    return out


def targets(job_file: str) -> list[dict]:
    """The bundle targets that a job file deploys to: [{target, profile}], from the nearest databricks.yml that
    has targets and the ~/.databrickscfg profile of each target's host."""
    root = _bundle_root(Path(job_file).parent)
    if not root:
        return []
    hosts = _profiles()
    out = []
    for name, spec in (_mapping(root.read_text(encoding="utf-8", errors="replace")).get("targets") or {}).items():
        ws = spec.get("workspace") if isinstance(spec, dict) else None
        ws = ws if isinstance(ws, dict) else {}
        profile = ws.get("profile") or hosts.get((ws.get("host") or "").rstrip("/"))
        if profile:
            out.append(dict(target=name, profile=profile))
    return out


CLI_TIMEOUT = 30
CACHE_SECONDS = 300
_cache: dict = {}


def _cli(*args) -> object:
    r = subprocess.run(["databricks", *args, "-o", "json"], capture_output=True, text=True, timeout=CLI_TIMEOUT)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip().split("\n")[0][:200])
    return json.loads(r.stdout or "null")


def _iso(ms):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000)) if ms else None


def _deployed(name: str, target: str, profile: str) -> dict | None:
    """One job's state in one workspace, or None when it is not deployed there."""
    key = (name, profile)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    found = _cli("jobs", "list", "--name", name, "--profile", profile) or []
    state = None
    if found:
        job_id = found[0]["job_id"]
        s = (_cli("jobs", "get", str(job_id), "--profile", profile) or {}).get("settings") or {}
        trigger = next(((k, s[k]) for k in ("continuous", "schedule", "trigger") if s.get(k)), (None, {}))
        runs = _cli("jobs", "list-runs", "--job-id", str(job_id), "--limit", "1", "--profile", profile) or []
        last = runs[0] if runs else None
        st = (last or {}).get("state") or {}
        state = dict(target=target, profile=profile, job_id=job_id, name=name,
                     trigger=trigger[0] or "manual", paused=trigger[1].get("pause_status") == "PAUSED",
                     last_run=dict(run_id=last["run_id"], started=_iso(last.get("start_time")),
                                   ended=_iso(last.get("end_time")),
                                   state=st.get("result_state") or st.get("life_cycle_state")) if last else None)
    _cache[key] = (time.time(), state)
    return state


def job_state(path: str) -> dict:
    """The deployed state of every bundle job defined at `path` (a job YAML, or a code file next to one), in each
    workspace the bundle targets: paused or not, its trigger and its last run. Uses the `databricks` CLI and the
    ~/.databrickscfg profiles."""
    jobs = bundle_jobs(path)
    if not jobs:
        return dict(jobs=[], note="No Databricks bundle jobs are defined in this file or next to it.")
    asks = []
    for j in jobs:
        for t in targets(j["file"]):
            asks.append((j, t["target"], t["profile"], j["name"].replace("${bundle.target}", t["target"])))
    if not asks:
        return dict(jobs=[dict(key=j["key"], file=j["file"]) for j in jobs],
                    note="No databricks.yml target maps to a ~/.databrickscfg profile.")

    failed: dict[str, str] = {}  # profile -> why the CLI could not answer for it

    def one(a):
        j, target, profile, name = a
        try:
            return j, target, _deployed(name, target, profile)
        except FileNotFoundError:
            failed[profile] = "the databricks CLI is not installed"
        except subprocess.TimeoutExpired:
            failed[profile] = f"no answer in {CLI_TIMEOUT}s"
        except (RuntimeError, json.JSONDecodeError) as e:
            failed[profile] = (f"login expired: run `databricks auth login --profile {profile}`"
                               if "token" in str(e).lower() else str(e))
        return j, target, False
    with ThreadPoolExecutor(8) as pool:
        got = list(pool.map(one, asks))
    out = []
    for j in jobs:
        mine = [(t, s) for jj, t, s in got if jj is j]
        out.append(dict(key=j["key"], file=j["file"], deployed=[s for _, s in mine if s],
                        not_deployed=[t for t, s in mine if s is None],
                        unknown=[t for t, s in mine if s is False]))
    return dict(jobs=out, **({"cannot_read": failed} if failed else {}))
