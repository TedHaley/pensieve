"""What on this Mac looks abandoned, for the user's agent to offer to clean up: linked git worktrees that no one has
committed to or worked in for a while. Pensieve only reports. The agent asks the user, and the user removes them;
the next sweep then drops them from the index, so the disk stays the source of truth and the index stays lean."""
import time
from datetime import datetime, timezone
from pathlib import Path

from pensieve.repos import git

DAYS = 14  # a worktree without a commit or an agent session in this many days is stale
# untracked folders that tools make and that hold no work: removing a worktree may delete them
THROWAWAY = (".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
             ".next", ".turbo", ".cache", ".DS_Store")
TTL = 600  # seconds a repo's report is reused (the SessionStart hook asks for it)
_cache = {}


def _default_branch(root) -> str | None:
    """origin/HEAD's branch, else main or master, as a ref that exists."""
    head = git(root, "symbolic-ref", "-q", "refs/remotes/origin/HEAD").strip()
    for ref in ([head] if head else []) + ["refs/heads/main", "refs/heads/master"]:
        if git(root, "rev-parse", "-q", "--verify", ref).strip():
            return ref
    return None


def _age_days(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - (t if t.tzinfo else t.replace(tzinfo=timezone.utc))).total_seconds() / 86400


def worktrees(root: str, sessions: list[tuple[str, str]], chunks: dict, days: int = DAYS) -> list[dict]:
    """The linked worktrees of the repo at `root`, with the evidence that each one is stale or not. `sessions` are
    (working folder, last update) pairs; `chunks` maps a worktree path to its indexed chunk count."""
    out, cur = [], {}
    listed = git(root, "worktree", "list", "--porcelain")
    for line in listed.split("\n") + [""]:
        if line.startswith("worktree "):
            cur = {"path": line[9:]}
        elif line.startswith("HEAD "):
            cur["head"] = line[5:]
        elif line.startswith("branch "):
            cur["branch"] = line[7:].removeprefix("refs/heads/")
        elif line in ("locked", "prunable") or line.startswith(("locked ", "prunable ")):
            cur[line.split()[0]] = True
        elif not line and cur:
            out.append(cur)
            cur = {}
    base = _default_branch(root)
    res = []
    for w in out[1:]:  # the first is the main checkout
        p = w["path"]
        here = Path(p).is_dir()
        last_commit = git(p, "log", "-1", "--format=%cI").strip() if here else None
        last_session = max((u for c, u in sessions if c and (c == p or c.startswith(p + "/"))), default=None)
        ages = [a for a in (_age_days(last_commit), _age_days(last_session)) if a is not None]
        idle = min(ages) if ages else None
        changes = [x for x in git(p, "status", "--porcelain").split("\n") if x] if here else []
        junk = [x for x in changes if x.startswith("?? ") and Path(x[3:].rstrip("/")).name in THROWAWAY]
        dirty = len(changes) - len(junk)
        force = " --force" if junk else ""  # git refuses to remove a worktree with untracked files
        ahead = int(git(root, "rev-list", "--count", f"{base}..{w['head']}").strip() or 0) if base and w.get("head") else None
        pushed = bool(git(root, "branch", "-r", "--contains", w["head"]).strip()) if w.get("head") and ahead else True
        o = dict(path=p, branch=w.get("branch"), last_commit=last_commit, last_session=last_session,
                 idle_days=round(idle, 1) if idle is not None else None, uncommitted=dirty, unmerged_commits=ahead,
                 unmerged_pushed=pushed, locked=bool(w.get("locked")), chunks=chunks.get(p, 0))
        if not here or w.get("prunable"):
            o.update(verdict="remove", why="its folder is gone", command=f"git -C {root} worktree prune")
        elif idle is not None and idle < days:
            o.update(verdict="keep", why=f"used {idle:.0f} days ago")
        elif dirty or (ahead and not pushed) or w.get("locked"):
            why = ", ".join(x for x in (f"{dirty} uncommitted change{'s' if dirty != 1 else ''}" if dirty else "",
                                        f"{ahead} commit{'s' if ahead != 1 else ''} not merged or pushed" if ahead and not pushed else "",
                                        "locked" if w.get("locked") else "") if x)
            # no removal command: the user decides about the work first; pushing the branch keeps it
            o.update(verdict="review", why=f"idle {idle:.0f} days, but has {why}" if idle else f"has {why}",
                     keep_work=f"git -C {p} push -u origin {o['branch']}" if o["branch"] and ahead and not pushed else None)
        else:
            merged = "its commits are in the default branch" if not ahead else "its commits are pushed"
            o.update(verdict="remove", why=f"idle {idle:.0f} days, clean, and {merged}" if idle else f"clean, and {merged}",
                     command=f"git -C {root} worktree remove{force} {p}" + (
                         f" && git -C {root} branch -d {o['branch']}" if o["branch"] and not ahead else ""))
        if junk:
            o["deletes"] = [x[3:].rstrip("/") for x in junk]  # what --force removes with the worktree
        res.append(o)
    return res


def from_store(store, days: int = DAYS, roots=None, fresh=False) -> list[dict]:
    """report() for the indexed repos (or `roots`), with the sessions and chunk counts from the index."""
    from collections import defaultdict
    with store.lock:
        db = store.db
        roots = roots or [r for (r,) in db.execute("SELECT root FROM repos")]
        sessions = db.execute("SELECT project, updated FROM sessions WHERE n_chunks>0").fetchall()
        chunks = defaultdict(dict)
        for repo, wt, n in db.execute("SELECT repo, wt, COUNT(*) FROM code_chunks WHERE wt!='' GROUP BY 1, 2"):
            chunks[repo][wt] = n
    return report(roots, sessions, chunks, days, fresh)


def cached(root: str, days: int = DAYS) -> list[dict] | None:
    """The stale worktrees of `root` from the last report, if it is recent; None when there is none yet."""
    hit = _cache.get((root, days))
    if not hit or time.time() - hit[0] > TTL:
        return None
    return [w for w in hit[1] if w["verdict"] != "keep"]


def report(roots: list[str], sessions, chunks, days: int = DAYS, fresh=False) -> list[dict]:
    """worktrees() for each repo root, reused for TTL seconds per repo; stale ones only (verdict remove or review)."""
    out = []
    for r in roots:
        hit = _cache.get((r, days))
        if fresh or not hit or time.time() - hit[0] > TTL:
            hit = (time.time(), worktrees(r, sessions, chunks.get(r, {}), days))
            _cache[(r, days)] = hit
        out += [dict(w, repo=Path(r).name, repo_root=r) for w in hit[1] if w["verdict"] != "keep"]
    return out
