"""Writes to branches and PRs that a session made with git and gh, from its Bash tool calls and their results.

A push, a commit, a branch created, or a PR opened or merged is a write. Text that only names them is not checked here.
The repo of a branch is the origin remote of the folder the session ran in."""
import json
import re
import subprocess
from functools import lru_cache

from pensieve.extract import WRITE, Edge, Session, extractor

PUSH = re.compile(r"\bgit (?:-C \S+ )?push\b[^\n;&|]*?\borigin\s+(?:HEAD:)?([\w][\w./-]*)")
PUSH_BARE = re.compile(r"\bgit (?:-C \S+ )?push\b(?![^\n;&|]*\borigin\s+[\w][\w./-]*)")  # git push, git push -u origin HEAD
CHECKOUT_B = re.compile(r"\bgit (?:-C \S+ )?(?:checkout -[bB]|switch -[cC])\s+([\w][\w./-]*)")
CHECKOUT = re.compile(r"\bgit (?:-C \S+ )?(?:checkout|switch)\s+([\w][\w./-]*)\s*(?:$|[;&|])")
COMMIT = re.compile(r"\bgit (?:-C \S+ )?commit\b")
PR_CREATE = re.compile(r"\bgh pr create\b")
PR_MERGE = re.compile(r"\bgh pr merge\s+(\d+)")
PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
REPO_FLAG = re.compile(r"(?:-R|--repo)\s+([\w.-]+/[\w.-]+)")
CD = re.compile(r"\bcd\s+(\S+)")
GENERIC = {"main", "master", "develop", "dev", "HEAD"}


@lru_cache(maxsize=None)
def origin_repo(folder: str) -> str:
    """org/repo of a folder's origin remote on this Mac, or empty."""
    try:
        r = subprocess.run(["git", "-C", folder, "remote", "get-url", "origin"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", r.stdout.strip())
    return m.group(1) if m else ""


def _results(records: list) -> dict[str, str]:
    """tool_use id -> the text of its result."""
    out = {}
    for rec in records:
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                c = b.get("content")
                text = " ".join(x.get("text", "") for x in c if isinstance(x, dict)) if isinstance(c, list) else str(c or "")
                out[b.get("tool_use_id", "")] = text
    return out


def _bash_calls(records: list):
    """(tool_use id, command, cwd, branch) for each Bash call, in order. The branch is the one Claude Code recorded
    with the call (`gitBranch`), or empty."""
    for rec in records:
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Bash":
                yield (b.get("id", ""), (b.get("input") or {}).get("command", ""), rec.get("cwd", ""),
                       rec.get("gitBranch") or "")


@extractor("git_tools")
def git_tools(session: Session):
    results = _results(session.records)
    branch = ""  # the branch the session is on, as far as its checkouts show
    folder = ""
    for tool_id, cmd, cwd, recorded in _bash_calls(session.records):
        folder = cwd or folder
        repo = origin_repo(folder) if folder else ""
        cd = CD.search(cmd)
        if cd and not folder:
            folder = cd.group(1)
        out = results.get(tool_id, "")
        for m in CHECKOUT_B.finditer(cmd):
            branch = m.group(1)
            if branch not in GENERIC:
                yield Edge(session.id, "branch", _branch_key(repo, branch), WRITE, f"created {branch}", "git_tools")
        for m in CHECKOUT.finditer(cmd):
            if not CHECKOUT_B.search(cmd):
                branch = m.group(1)
        # a commit or a bare push goes to the branch that is checked out: the last checkout in this session, else the
        # branch the agent recorded with the call (a session that starts on a branch never checks it out)
        on = branch or recorded
        for m in PUSH.finditer(cmd):
            name = m.group(1)
            if name not in GENERIC:
                yield Edge(session.id, "branch", _branch_key(repo, name), WRITE, f"git push {name}", "git_tools")
        if PUSH_BARE.search(cmd) and on and on not in GENERIC and not PUSH.search(cmd) and "error" not in out.lower()[:200]:
            yield Edge(session.id, "branch", _branch_key(repo, on), WRITE, f"git push {on}", "git_tools")
        if COMMIT.search(cmd) and on and on not in GENERIC and "error" not in out.lower()[:200]:
            yield Edge(session.id, "branch", _branch_key(repo, on), WRITE, "git commit", "git_tools")
        if PR_CREATE.search(cmd):
            for org_repo, n in PR_URL.findall(out)[:1]:
                yield Edge(session.id, "pr", f"{org_repo}#{n}", WRITE, "gh pr create", "git_tools")
        for m in PR_MERGE.finditer(cmd):
            flag = REPO_FLAG.search(cmd)
            target = flag.group(1) if flag else repo
            if target:
                yield Edge(session.id, "pr", f"{target}#{m.group(1)}", WRITE, "gh pr merge", "git_tools")


def _branch_key(repo: str, branch: str) -> str:
    return f"{repo}@{branch}" if repo else branch
