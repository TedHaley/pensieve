"""Map a working directory to a stable project name (git root basename; worktrees fold into their repo)."""
from functools import lru_cache
from pathlib import Path

from .config import HOME


@lru_cache(maxsize=None)
def project_name(cwd: str) -> str:
    if not cwd:
        return "(unknown)"
    p = Path(cwd)
    if p == HOME:
        return "(home)"
    parts = p.parts
    if ".claude" in parts and "worktrees" in parts:  # <repo>/.claude/worktrees/<name>
        return Path(*parts[:parts.index(".claude")]).name
    root = project_root(cwd)
    return Path(root).name if root else p.name


@lru_cache(maxsize=None)
def project_root(cwd: str) -> str | None:
    """Repo root for cwd; linked git worktrees and .claude/worktrees fold into the main repo."""
    p = Path(cwd or "/nonexistent")
    parts = p.parts
    if ".claude" in parts and "worktrees" in parts:
        p = Path(*parts[:parts.index(".claude")])
    for q in [p, *p.parents]:
        g = q / ".git"
        if g.is_file():  # linked worktree: "gitdir: <main>/.git/worktrees/<name>"
            try:
                gd = Path(g.read_text().split("gitdir:", 1)[1].strip())
                if "worktrees" in gd.parts:
                    return str(Path(*gd.parts[:gd.parts.index("worktrees")]).parent)
            except (OSError, IndexError):
                pass
            return str(q)
        if g.exists():
            return str(q)
        if q == HOME:
            break
    return None
