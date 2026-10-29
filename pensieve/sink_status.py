"""The status of a thread's sinks: the PRs and branches it writes to, and the tickets it names.

A sink is verified done when its system says so (thread_model.sink_complete). This module gathers the statuses:
  pr      from the task's own PR state (the state the app already reads through gh)
  branch  from GitHub, with gh (bench/sink_status.py is the same check)
  ticket  from the user's agent: the agent answers a job with the Atlassian connector it already has (agent_jobs)

Statuses are cached in DATA_DIR/sink_status.json with the time they were read. A cached branch status is used for
BRANCH_TTL seconds, and whatever its age while a build is offline. A status that is not in the cache yet is UNKNOWN,
and an unknown sink does not close a thread."""
import json
import os
import re
import subprocess
import time
import urllib.parse

from pensieve import config
from pensieve import thread_model as tm

CACHE = config.DATA_DIR / "sink_status.json"
BRANCH_TTL = 5 * 60
PR_URL = re.compile(r"https?://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")


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


_other: list = []  # the gh env of the user's other GitHub account, found once (repos the active account cannot read)
# (session id, label) -> status of the sinks this Mac answers for itself, per session (session_threads.on_default_branch)
local: dict[tuple[str, str], str] = {}


def _fallback_env() -> dict | None:
    """The environment with the token of another account that gh is logged in to on github.com: for a repo that the
    active account cannot see. None when gh has one account only."""
    if not _other:
        env = None
        try:
            r = subprocess.run(["gh", "auth", "status", "--json", "hosts"], capture_output=True, text=True, timeout=10)
            accounts = json.loads(r.stdout or "{}").get("hosts", {}).get("github.com", [])
            other = next((a["login"] for a in accounts if not a.get("active") and a.get("state") == "success"), "")
            if other:
                tok = subprocess.run(["gh", "auth", "token", "--user", other], capture_output=True, text=True, timeout=10)
                env = {**os.environ, "GH_TOKEN": tok.stdout.strip()} if tok.returncode == 0 and tok.stdout.strip() else None
        except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
            env = None
        _other.append(env)
    return _other[0]


def gh(args: list[str], cwd: str | None = None, timeout: int = 15) -> subprocess.CompletedProcess:
    """Run gh as the active account, and again as the other account when it fails: a work repo that only a second
    account can read."""
    r = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if r.returncode and _fallback_env():
        r = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=_fallback_env())
    return r


def pr_info(key: str) -> dict:
    """{state, head} of a PR, from gh, cached for BRANCH_TTL (and whatever its age while the build is offline,
    work._net). state is MERGED, CLOSED or OPEN; {} when gh cannot answer, or offline with nothing cached."""
    from pensieve import work
    cache = _load()
    entry = cache.get(f"pr {key}")
    if entry and (time.time() - entry["at"] < BRANCH_TTL or work._net["off"]):
        return entry["value"]
    if work._net["off"]:
        return {}
    repo, _, num = key.partition("#")
    value = {}
    r = gh(["pr", "view", num, "-R", repo, "--json", "state,headRefName"])
    if r.returncode == 0 and r.stdout.strip():
        d = json.loads(r.stdout)
        value = {"state": d.get("state", ""), "head": d.get("headRefName", "")}
    cache = _load()
    cache[f"pr {key}"] = dict(value=value, at=time.time())
    _save(cache)
    return value


def statuses_for(sinks: list[tm.Sink], session: str = "", pushed: dict | None = None) -> dict[str, str]:
    """The status of each sink, label -> status. A branch's state comes from GitHub, cached for BRANCH_TTL; while
    the build is offline (work._net, the first build after a start) the cached state serves whatever its age, and
    a branch with none stays unknown until the next build. `pushed`: label -> the commit that the branch's checkout
    last pushed; a cached state from before another push is stale (a branch pushed the first time was not found
    just before). A main checkout's state is `session`'s own (local). A ticket's status comes from the task's own
    data (status_of_task) or the cache; nothing asks a model."""
    from pensieve import work
    mine = {label: v for (sid, label), v in local.items() if sid == session}
    cache = _load()
    now = time.time()
    out, dirty = {}, False
    for s in sinks:
        entry = cache.get(s.label)
        if s.label in mine:
            out[s.label] = mine[s.label]
        elif any(label == s.label for _, label in local):
            continue  # another session's main: this one wrote nothing there, so it has no status
        elif s.kind == tm.SinkKind.PR:
            if entry and now - entry["at"] < BRANCH_TTL:
                out[s.label] = entry["value"]
        elif s.kind == tm.SinkKind.BRANCH:
            seen = (pushed or {}).get(s.label)
            if entry and seen is not None and entry.get("pushed") != seen:
                entry = None
            if entry and (now - entry["at"] < BRANCH_TTL or work._net["off"]):
                out[s.label] = entry["value"]
            elif not work._net["off"]:
                repo, _, branch = s.key.partition("@")
                value = branch_status(github_repo(repo), branch)
                if value == "UNKNOWN" and _fallback_env():  # the active account cannot read this repo
                    value = branch_status(github_repo(repo), branch, env=_fallback_env())
                cache[s.label] = dict(value=value, at=now, **({"pushed": seen} if seen is not None else {}))
                out[s.label] = value
                dirty = True
        elif s.kind == tm.SinkKind.TICKET:
            if entry:
                out[s.label] = entry["value"]
    if dirty:
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
_repos: dict[str, tuple[str, float]] = {}  # folder -> (owner/name or "", when git was asked)
NO_ORIGIN_TTL = 60


def github_repo(path: str) -> str:
    """owner/name of a repo: the name itself, or the GitHub origin of a local folder. '' when there is none."""
    if re.fullmatch(r"[\w.-]+/[\w.-]+", path or ""):
        return path
    slug, at = _repos.get(path, ("", 0.0))
    if not slug and time.time() - at > NO_ORIGIN_TTL:  # an origin added later is found
        if path and os.path.isdir(path):
            r = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"], capture_output=True, text=True)
            m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", r.stdout.strip()) if r.returncode == 0 else None
            slug = m.group(1) if m else ""
        _repos[path] = (slug, time.time())
    return slug


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
    session = (task.get("_sessions") or [{}])[0].get("id", "")
    branches = [s for s in sinks if s.kind == tm.SinkKind.BRANCH]
    # the commit each branch's checkout last pushed: "" before its first push
    pushed = {s.label: p.get("pushed") or "" for s in branches for p in task.get("places", [])
              if p.get("git") and p.get("branch") == s.key.split("@", 1)[-1] and not p.get("head")}
    status.update(statuses_for(branches, session, pushed))
    # a branch whose commits all have their patch in the default branch is merged, even when it was forked from another
    in_main = {p["branch"] for p in task.get("places", []) if p.get("commits") and not p.get("unmerged") and p.get("branch")}
    ours = {label for _, label in local}
    for s in sinks:
        if s.kind == tm.SinkKind.BRANCH and s.key.split("@", 1)[-1] in in_main and status.get(s.label) != "OPEN" \
                and s.label not in ours:
            status[s.label] = "MERGED"
    # GitHub says DELETED both for a branch never pushed and for one deleted after its merge. A branch with no PR,
    # whose upstream in git config is none or the default branch it was cut from, was never pushed: its work in the
    # checkout keeps it open.
    from pensieve import work
    heads = {p.get("head") for p in task.get("prs", []) + (task.get("contributed") or {}).get("prs", [])}
    upstream = lambda p: work._git(p.get("path", ""), "config", f"branch.{p['branch']}.merge", ttl=60)
    local_work = {p["branch"] for p in task.get("places", []) if p.get("branch") and not p.get("head")
                  and p["branch"] not in heads and (p.get("changed") or p.get("unmerged"))
                  and upstream(p).removeprefix("refs/heads/") in ("", *work.DEFAULT_BRANCHES)}
    for s in sinks:
        if s.kind == tm.SinkKind.BRANCH and s.key.split("@", 1)[-1] in local_work \
                and status.get(s.label) in ("DELETED", "UNKNOWN", None) and s.label not in ours:
            status[s.label] = "OPEN"
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
