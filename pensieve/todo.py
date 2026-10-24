"""A to-do started: a ticket of the user's that nothing picked up yet gets a worktree on a branch named from its key,
and an agent session there that reads the ticket first.

The worktree sits in `<repo>/.claude/worktrees/<branch>`, made from the repo's default branch. Which repo a ticket
project's work starts in is remembered by the key's prefix (settings `ticket_repos`); the first start of a project
asks, with the repos this Mac knows as the choices."""
import re
import shlex
import time
from pathlib import Path

from pensieve import settings

TODO_PROMPT = ("Start the work on {key}: \"{title}\"{url}. This worktree is on branch {branch}, made from {base} in "
               "{repo}, for it. The ticket, as its tracker has it:\n\n{text}\n\n"
               "Read the ticket's page with your own tools when you can, for its comments and links. Then say the "
               "problem and the goal in your words, lay out a plan, and ask me before you change anything outside this "
               "worktree.")


def ticket_of(task: dict) -> dict | None:
    """The ticket a to-do is for: the task's first ticket with a key."""
    return next((k for k in task.get("tickets", []) if k.get("key")), None)


def repos_known() -> list[dict]:
    """The repos this Mac knows, newest work first: the repos in Settings, and the git roots the sessions of the
    last 90 days ran in. {path, name, last}."""
    from pensieve import transcripts, work
    out: dict[str, float] = {}
    for f in transcripts.sessions_since(time.time() - 90 * 86400):
        root = work._git_root(f.project) if f.project else ""
        if root and (Path(root) / ".git").is_dir():  # a linked worktree has a .git file: its repo is the choice
            out[root] = max(out.get(root, 0.0), f.updated_ts())
    for p in settings.get("repos"):
        root = work._git_root(str(Path(p).expanduser()))
        if root:
            out.setdefault(root, 0.0)
    return [dict(path=r, name=Path(r).name, last=at) for r, at in sorted(out.items(), key=lambda x: -x[1])]


def branch_name(key: str, title: str) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return f"{key}-{words}"[:60].rstrip("-") if words else key


def start(task: dict, repo: str | None = None, agent: str = "") -> dict:
    """Make the worktree and the command that starts the agent in it. Without a repo for the ticket's project, and
    none remembered, returns {choose: [repos]} so the caller can ask. Returns {command, path, branch, repo}."""
    from pensieve import agents, stale, work
    k = ticket_of(task)
    if not k:
        raise ValueError("this thread has no ticket to start from")
    key = k["key"]
    prefix = key.split("-", 1)[0]
    mapping = dict(settings.get("ticket_repos") or {})
    root = repo or mapping.get(prefix) or ""
    root = work._git_root(str(Path(root).expanduser())) if root else ""
    if not root:
        return {"choose": repos_known(), "key": key}
    if mapping.get(prefix) != root:
        settings.update({"ticket_repos": {**mapping, prefix: root}})
    branch = branch_name(key, k.get("title", ""))
    base = stale._default_branch(root) or "HEAD"
    path = Path(root) / ".claude" / "worktrees" / branch
    if not path.is_dir():
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = bool(work._git(root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", ttl=0))
        args = ["worktree", "add", str(path), branch] if exists else ["worktree", "add", "-b", branch, str(path), base]
        out = work._git(root, *args, ttl=0)
        if not path.is_dir():
            raise ValueError(f"git could not make the worktree: {out or 'no output'}")
    text = (k.get("text") or "").strip() or "(the tracker gave no description)"
    url = f" ({k['url']})" if k.get("url") else ""
    prompt = TODO_PROMPT.format(key=key, title=k.get("title") or "", url=url, branch=branch, base=base.rsplit("/", 1)[-1],
                                repo=Path(root).name, text=text[:4000])
    agent = agent if agents.get(agent) else agents.choose(settings.get("default_agent"))
    return {"command": f"cd {shlex.quote(str(path))} && {agents.start(agent, prompt)}", "path": str(path),
            "branch": branch, "repo": root, "key": key, "prompt": prompt}
