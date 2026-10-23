"""The status of a thread's sinks: the PRs and branches it writes to, and the tickets it names.

A sink is verified done when its system says so (thread_model.sink_complete). This module gathers the statuses:
  pr      from the task's own PR state (the state the app already reads through gh)
  branch  from GitHub, with gh (bench/sink_status.py is the same check)
  ticket  from the user's agent: the agent answers a job with the Atlassian connector it already has (agent_jobs)

Statuses are cached in DATA_DIR/sink_status.json with the time they were read. A cached branch status is used for
BRANCH_TTL seconds; a ticket status for TICKET_TTL seconds. A status that is not in the cache yet is UNKNOWN, and an
unknown sink does not close a thread."""
import json
import os
import re
import subprocess
import time
import urllib.parse

from pensieve import config
from pensieve import thread_model as tm

CACHE = config.DATA_DIR / "sink_status.json"
BRANCH_TTL = 15 * 60
TICKET_TTL = 60 * 60
PR_URL = re.compile(r"https?://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
TICKET_SYS = ("You read the status of Jira tickets with the Atlassian tools you have. Answer with the current status "
              "name of each key, exactly as Jira shows it. A key you cannot read gets an empty string.")
TICKET_SCHEMA = {"type": "object", "properties": {"statuses": {"type": "object", "additionalProperties": {"type": "string"}}},
                 "required": ["statuses"]}


def pr_key(url: str) -> str:
    """org/repo#n from a GitHub pull request URL, or empty."""
    m = PR_URL.search(url or "")
    return f"{m.group(1)}#{m.group(2)}" if m else ""


def _load() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def _save(cache: dict) -> None:
    CACHE.write_text(json.dumps(cache, indent=1))


def branch_status(repo: str, branch: str, env: dict | None = None) -> str:
    """DELETED, MERGED, OPEN, EXISTS, or UNKNOWN when gh cannot answer. DELETED needs the repo to be visible: GitHub
    answers "not found" for a branch in a repo the account cannot read, too."""
    if not repo or not branch:
        return "UNKNOWN"
    api = subprocess.run(["gh", "api", f"repos/{repo}/branches/{urllib.parse.quote(branch, safe='')}"],
                         capture_output=True, text=True, env=env)
    if api.returncode != 0:
        if "Branch not found" not in (api.stdout + api.stderr):
            return "UNKNOWN"
        visible = subprocess.run(["gh", "api", f"repos/{repo}"], capture_output=True, text=True, env=env)
        return "DELETED" if visible.returncode == 0 else "UNKNOWN"
    r = subprocess.run(["gh", "pr", "list", "-R", repo, "--head", branch, "--state", "all", "--json", "state"],
                       capture_output=True, text=True, env=env)
    states = {p["state"] for p in json.loads(r.stdout)} if r.returncode == 0 and r.stdout.strip() else set()
    if "MERGED" in states:
        return "MERGED"
    if "OPEN" in states:
        return "OPEN"
    return "EXISTS"


def ticket_statuses(keys: list[str]) -> dict[str, str]:
    """Ticket key -> status name, asked of the user's agent. Raises llm.Queued while the agent has not answered."""
    from pensieve import llm
    if not keys:
        return {}
    prompt = "Ticket keys: " + ", ".join(sorted(keys))
    answer = llm.complete(prompt, TICKET_SYS, max_tokens=400, schema=TICKET_SCHEMA, kind="ticket status")
    return {k: str(v) for k, v in (answer.get("statuses") or {}).items()}


_tedhaley: list = []  # the gh env with TedHaley's token, found once (repos the default account cannot read)


def _fallback_env() -> dict | None:
    """The environment with TedHaley's gh token: for a repo the active account cannot see. None when there is none."""
    if not _tedhaley:
        tok = subprocess.run(["gh", "auth", "token", "--user", "TedHaley"], capture_output=True, text=True)
        _tedhaley.append({**os.environ, "GH_TOKEN": tok.stdout.strip()} if tok.returncode == 0 and tok.stdout.strip() else None)
    return _tedhaley[0]


def pr_info(key: str) -> dict:
    """{state, head} of a PR, from gh, cached for BRANCH_TTL. state is MERGED, CLOSED or OPEN; {} when gh cannot answer."""
    cache = _load()
    entry = cache.get(f"pr {key}")
    if entry and time.time() - entry["at"] < BRANCH_TTL:
        return entry["value"]
    repo, _, num = key.partition("#")
    value = {}
    for env in (None, _fallback_env()):
        r = subprocess.run(["gh", "pr", "view", num, "-R", repo, "--json", "state,headRefName"], capture_output=True,
                           text=True, env=env)
        if r.returncode == 0 and r.stdout.strip():
            d = json.loads(r.stdout)
            value = {"state": d.get("state", ""), "head": d.get("headRefName", "")}
            break
    cache = _load()
    cache[f"pr {key}"] = dict(value=value, at=time.time())
    _save(cache)
    return value


def statuses_for(sinks: list[tm.Sink]) -> dict[str, str]:
    """The status of each sink, label -> status. Uses the cache where it is fresh, else asks GitHub or the agent."""
    cache = _load()
    now = time.time()
    out = {}
    stale_tickets = []
    for s in sinks:
        if s.kind == tm.SinkKind.PR:
            entry = cache.get(s.label)
            if entry and now - entry["at"] < BRANCH_TTL:
                out[s.label] = entry["value"]
        elif s.kind == tm.SinkKind.BRANCH:
            entry = cache.get(s.label)
            if entry and now - entry["at"] < BRANCH_TTL:
                out[s.label] = entry["value"]
            else:
                repo, _, branch = s.key.partition("@")
                value = branch_status(github_repo(repo), branch)
                if value == "UNKNOWN" and _fallback_env():  # the active account cannot read this repo
                    value = branch_status(github_repo(repo), branch, env=_fallback_env())
                cache[s.label] = dict(value=value, at=now)
                out[s.label] = value
        elif s.kind == tm.SinkKind.TICKET:
            entry = cache.get(s.label)
            if entry and now - entry["at"] < TICKET_TTL:
                out[s.label] = entry["value"]
            else:
                stale_tickets.append(s)
    _save(cache)
    if stale_tickets:
        try:
            fresh = ticket_statuses([s.key for s in stale_tickets])
        except Exception:  # the agent has not answered: the tickets stay unknown until it does
            fresh = {}
        cache = _load()
        for s in stale_tickets:
            if s.key in fresh and fresh[s.key]:
                cache[s.label] = dict(value=fresh[s.key], at=now)
                out[s.label] = fresh[s.key]
        _save(cache)
    return out


def sinks_of_task(task: dict) -> list[tm.Sink]:
    """The sinks of a task: its PRs (state from the task), its branches, and its tickets."""
    sinks = []
    for p in task.get("prs", []):
        key = pr_key(p.get("url", ""))
        if key:
            sinks.append(tm.Sink(tm.SinkKind.PR, key))
    for b in task.get("branches", []):
        if b.get("repo") and b.get("branch"):
            sinks.append(tm.Sink(tm.SinkKind.BRANCH, f"{b['repo']}@{b['branch']}"))
    for k in task.get("tickets", []):
        if k.get("key"):
            sinks.append(tm.Sink(tm.SinkKind.TICKET, k["key"]))
    return list(dict.fromkeys(sinks))


PR_STATUS = {"merged": "MERGED", "closed": "CLOSED", "open": "OPEN", "draft": "OPEN"}
DONE_TICKET = {"done", "closed", "resolved"}
_repos: dict[str, str] = {}


def github_repo(path: str) -> str:
    """owner/name of a repo: the name itself, or the GitHub origin of a local folder. '' when there is none."""
    if re.fullmatch(r"[\w.-]+/[\w.-]+", path or ""):
        return path
    if path not in _repos:
        slug = ""
        if path and os.path.isdir(path):
            r = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"], capture_output=True, text=True)
            m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", r.stdout.strip()) if r.returncode == 0 else None
            slug = m.group(1) if m else ""
        _repos[path] = slug
    return _repos[path]


def status_of_task(task: dict, sinks: list[tm.Sink]) -> dict:
    """What each sink reported, label -> status. PR states and ticket statuses come from the task's own data (GitHub and
    Jira, already read by the app). Branch states come from GitHub, cached. A sink with no status stays unknown."""
    status = {}
    for p in task.get("prs", []):
        key = pr_key(p.get("url", ""))
        if key and p.get("state") in PR_STATUS:
            status[f"pr {key}"] = PR_STATUS[p["state"]]
    for k in task.get("tickets", []):
        if k.get("key") and k.get("status"):
            status[f"ticket {k['key']}"] = "Done" if k["status"].lower() in DONE_TICKET else k["status"]
    status.update(statuses_for([s for s in sinks if s.kind == tm.SinkKind.BRANCH]))
    # a branch whose commits all have their patch in the default branch is merged, even when it was forked from another
    in_main = {p["branch"] for p in task.get("places", []) if p.get("commits") and not p.get("unmerged") and p.get("branch")}
    for s in sinks:
        if s.kind == tm.SinkKind.BRANCH and s.key.split("@", 1)[-1] in in_main and status.get(s.label) != "OPEN":
            status[s.label] = "MERGED"
    return status


def task_state(task: dict) -> tuple[tm.ThreadState | None, str, dict]:
    """The thread state from the task's sinks (thread_model.thread_state), its reason, and the status of each sink.
    None when the task has no sinks: other evidence decides for it."""
    sinks = sinks_of_task(task)
    if not sinks:
        return None, "", {}
    status = status_of_task(task, sinks)
    state, why = tm.thread_state(sinks, status, task.get("updated", 0))
    return state, why, status
