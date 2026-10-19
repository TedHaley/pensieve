"""What on this Mac looks abandoned, for the user's agent to offer to clean up: linked git worktrees that no one has
committed to or worked in for a while, local branches already merged into the default branch, and indexed repos that
no agent has used in months. Pensieve only reports. The agent asks the user, and the user removes them; the next
sweep then drops them from the index, so the disk stays the source of truth and the index stays lean."""
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from pensieve.repos import git

DAYS = 14  # a worktree without a commit or an agent session in this many days is stale
# untracked folders that tools make and that hold no work: removing a worktree may delete them
THROWAWAY = (".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
             ".next", ".turbo", ".cache", ".DS_Store")
REPO_MONTHS = 3  # an indexed repo with no agent session and no local git activity in this many months is unused
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


def _worktree_list(root: str) -> list[dict]:
    """`git worktree list`: the path, head, branch, locked and prunable of each; the main checkout first."""
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
    return out


def _last_session(sessions, paths) -> str | None:
    """The last update of the sessions that ran in one of `paths`, or below one."""
    return max((u for c, u in sessions if c and any(c == p or c.startswith(p + "/") for p in paths)), default=None)


def worktrees(root: str, sessions: list[tuple[str, str]], chunks: dict, days: int = DAYS, listed=None) -> list[dict]:
    """The linked worktrees of the repo at `root`, with the evidence that each one is stale or not. `sessions` are
    (working folder, last update) pairs; `chunks` maps a worktree path to its indexed chunk count."""
    out = listed if listed is not None else _worktree_list(root)
    base = _default_branch(root)
    res = []
    for w in out[1:]:  # the first is the main checkout
        p = w["path"]
        here = Path(p).is_dir()
        last_commit = git(p, "log", "-1", "--format=%cI").strip() if here else None
        last_session = _last_session(sessions, [p])
        ages = [a for a in (_age_days(last_commit), _age_days(last_session)) if a is not None]
        idle = min(ages) if ages else None
        changes = [x for x in git(p, "status", "--porcelain").split("\n") if x] if here else []
        junk = [x for x in changes if x.startswith("?? ") and Path(x[3:].rstrip("/")).name in THROWAWAY]
        dirty = len(changes) - len(junk)
        force = " --force" if junk else ""  # git refuses to remove a worktree with untracked files
        ahead = int(git(root, "rev-list", "--count", f"{base}..{w['head']}").strip() or 0) if base and w.get("head") else None
        pushed = bool(git(root, "branch", "-r", "--contains", w["head"]).strip()) if w.get("head") and ahead else True
        o = dict(kind="worktree", path=p, branch=w.get("branch"), last_commit=last_commit, last_session=last_session,
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


def _git_dir(root) -> Path:
    """The repo's .git folder (shared by all its worktrees)."""
    d = git(root, "rev-parse", "--git-common-dir").strip()
    return Path(root, d) if d else Path(root, ".git")


def _mtime_days(path: Path) -> float | None:
    try:
        return (time.time() - path.stat().st_mtime) / 86400
    except OSError:
        return None


def _is_ancestor(root, a, b) -> bool:
    return subprocess.run(["git", "-C", root, "merge-base", "--is-ancestor", a, b], capture_output=True).returncode == 0


def _patch_ids(root, args, stdin=None) -> list[tuple[str, str]]:
    """(patch id, commit) for each commit that `git <args>` prints with its patch. `git patch-id --stable` gives the
    same change the same id at other line numbers."""
    out = subprocess.run(["git", "-C", root, *args], input=stdin, capture_output=True).stdout
    r = subprocess.run(["git", "-C", root, "patch-id", "--stable"], input=out, capture_output=True)
    return [tuple(x.split()[:2]) for x in r.stdout.decode(errors="replace").split("\n") if x]


SQUASH_COMMITS = 2000  # how far back in the default branch to look for a branch's squashed commit


def _squashed(root, base, names) -> list[str]:
    """The branches in `names` whose whole change (from the merge base to the tip, as one diff) is one commit in
    `base`: squash-merged. A branch that changed again after the squash, or whose squash git had to adjust, is not
    found."""
    from concurrent.futures import ThreadPoolExecutor
    if not names:  # for-each-ref with no pattern lists every ref
        return []
    tips = dict(x.split() for x in git(root, "for-each-ref", "--format=%(refname:short) %(objectname)",
                                       *(f"refs/heads/{n}" for n in names)).split("\n") if x)
    with ThreadPoolExecutor(8) as pool:  # one git process per branch: in parallel
        mbs = dict(zip(tips, pool.map(lambda n: git(root, "merge-base", base, tips[n]).strip(), tips)))
    mbs = {n: m for n, m in mbs.items() if m}
    if not mbs:
        return []
    since = min(int(git(root, "show", "-s", "--format=%ct", m).strip() or 0) for m in mbs.values())
    # --no-renames on both sides: log finds renames by default and diff-tree doesn't, so a rename gave two ids
    ids = {p for p, _ in _patch_ids(root, ["log", "-p", "--no-renames", "--no-merges", f"-{SQUASH_COMMITS}",
                                           f"--since={since}", base])}
    # one diff per branch, tip against merge base, in one process: diff-tree reads "<commit> <parent>" lines
    pairs = "".join(f"{tips[n]} {m}\n" for n, m in mbs.items()).encode()
    done = {c for p, c in _patch_ids(root, ["diff-tree", "-p", "--no-renames", "--stdin"], pairs) if p in ids}
    return [n for n in mbs if tips[n] in done]


def branches(root: str, listed: list[dict], days: int = DAYS) -> list[dict]:
    """Local branches whose commits are all in the default branch, with no commit and no move in `days` days, and
    not checked out in any worktree. One item for the ones `git branch -d` deletes; one more, without a command, for
    those it refuses because the branch it compares with (its upstream, else the checked-out branch) lacks them.
    Squash-merged branches (see _squashed) get an item of their own, also without a command: their commits are not
    in the default branch, so `git branch -d` refuses them and only `git branch -D` deletes them."""
    base = _default_branch(root)
    if not base:
        return []
    base_name = base.removeprefix("refs/heads/").removeprefix("refs/remotes/")
    local = base.removeprefix("refs/heads/") if base.startswith("refs/heads/") else base.split("/", 3)[-1]
    skip = {local} | {w["branch"] for w in listed if w.get("branch")}
    rows = [x.split(" ", 1) + [""] for x in git(root, "for-each-ref", "--merged", base,
                                                "--format=%(refname:short) %(upstream)", "refs/heads").split("\n") if x]
    merged_head = set(git(root, "for-each-ref", "--merged", "HEAD", "--format=%(refname:short)", "refs/heads").split())
    logs = _git_dir(root) / "logs" / "refs" / "heads"

    def idle(name):  # no commit on it, and the branch not created or moved (its reflog file), in `days` days
        ages = [a for a in (_age_days(git(root, "log", "-1", "--format=%cI", f"refs/heads/{name}").strip()),
                            _mtime_days(logs / name)) if a is not None]
        return not ages or min(ages) >= days
    ok, refused = [], []
    for name, up, *_ in rows:
        if name in skip or not idle(name):
            continue
        up = up.strip()
        if up and git(root, "rev-parse", "-q", "--verify", up).strip():
            deletable = _is_ancestor(root, f"refs/heads/{name}", up)
        else:
            deletable = name in merged_head
        (ok if deletable else refused).append(name)
    res = []
    if ok:
        res.append(dict(kind="branches", branches=ok, verdict="remove", chunks=0,
                        why=f"merged into {base_name}, idle {days}+ days, not checked out",
                        command=f"git -C {root} branch -d {' '.join(ok)}"))
    if refused:
        cur = git(root, "branch", "--show-current").strip() or "the detached HEAD"
        res.append(dict(kind="branches", branches=refused, verdict="review", chunks=0,
                        why=f"merged into {base_name}, but git branch -d refuses them: their upstream or the "
                            f"checked-out branch ({cur}) lacks those commits. Bring {cur} up to date first"))
    unmerged = git(root, "for-each-ref", "--no-merged", base, "--format=%(refname:short)", "refs/heads").split()
    squashed = _squashed(root, base, [n for n in unmerged if n not in skip and idle(n)])
    if squashed:
        res.append(dict(kind="branches", branches=squashed, verdict="review", chunks=0,
                        why=f"squash-merged into {base_name}: each one's changes are one commit there, idle {days}+ "
                            f"days, not checked out. git branch -d refuses them, because their own commits are not in "
                            f"{base_name}. If the user agrees, `git -C {root} branch -D {' '.join(squashed)}` deletes "
                            f"them"))
    return res


def unused_repo(root: str, listed: list[dict], sessions, n_chunks: int, history_days: float | None,
                touched=None, months: int = REPO_MONTHS) -> dict | None:
    """The repo as an item when no agent session used it in `months` months (ran in it or one of its worktrees, or,
    with `touched`, read or edited a file in it) and nothing happened in git there either (a commit, a checkout, a
    pull). None when the indexed sessions don't reach back that far (agents delete old transcripts), or when the
    clone is younger than that."""
    limit = months * 30
    if history_days is None or history_days < limit:
        return None
    gd = _git_dir(root)
    try:
        st = gd.stat()
    except OSError:
        return None
    if (time.time() - getattr(st, "st_birthtime", st.st_mtime)) / 86400 < limit:
        return None
    paths = [w["path"] for w in listed] or [root]
    recent = lambda x: x and (_age_days(x) or 0) < limit
    last = _last_session(sessions, paths)
    if recent(last):
        return None
    last = max((x for x in (last, touched(paths) if touched else None) if x), default=None)  # a slower query
    if recent(last):
        return None
    heads = gd / "logs" / "refs" / "heads"
    logs = [gd / "logs" / "HEAD", *(gd / "worktrees").glob("*/logs/HEAD"),
            *(Path(d, f) for d, _, fs in os.walk(heads) for f in fs)]
    if any(a < limit for a in map(_mtime_days, logs) if a is not None):
        return None
    seen = (f"last agent session {_age_days(last):.0f} days ago" if last
            else f"no agent session in the {history_days:.0f} days of sessions indexed")
    return dict(kind="repo", verdict="unindex", chunks=n_chunks, last_session=last,
                why=f"{seen}, and no git activity in {months} months. This only removes it from Pensieve's index; "
                    f"the repo stays on disk",
                command=f"pensieve repo remove {root}")


def from_store(store, days: int = DAYS, roots=None, fresh=False) -> list[dict]:
    """report() for the indexed repos (or `roots`), with the sessions and chunk counts from the index."""
    from collections import defaultdict
    db = store.db
    roots = roots or [r for (r,) in db.execute("SELECT root FROM repos")]
    sessions = db.execute("SELECT project, updated FROM sessions WHERE n_chunks>0").fetchall()
    first = db.execute("SELECT MIN(updated) FROM sessions WHERE n_chunks>0").fetchone()[0]
    chunks, totals = defaultdict(dict), defaultdict(int)
    for repo, wt, n in db.execute("SELECT repo, wt, COUNT(*) FROM code_chunks GROUP BY 1, 2"):
        totals[repo] += n
        if wt:
            chunks[repo][wt] = n

    def touched(paths):
        """The last session that read or edited a file below one of `paths`."""
        return max((u for p in paths for (u,) in store.db.execute(
            "SELECT MAX(s.updated) FROM session_files f JOIN sessions s ON s.id=f.session_id "
            "WHERE f.path>=? AND f.path<?", (p + "/", p + "0")) if u), default=None)
    return report(roots, sessions, chunks, days, fresh, totals, _age_days(first), touched)


def cached(root: str, days: int = DAYS) -> list[dict] | None:
    """What is stale in `root` from the last report, if it is recent; None when there is none yet."""
    hit = _cache.get((root, days))
    if not hit or time.time() - hit[0] > TTL:
        return None
    return [w for w in hit[1] if w["verdict"] != "keep"]


def report(roots: list[str], sessions, chunks, days: int = DAYS, fresh=False, totals=None, history_days=None,
           touched=None) -> list[dict]:
    """For each repo root: its worktrees, merged branches and, when unused, the repo itself. Reused for TTL seconds
    per repo; stale ones only (verdict remove, review or unindex)."""
    out = []
    for r in roots:
        hit = _cache.get((r, days))
        if fresh or not hit or time.time() - hit[0] > TTL:
            listed = _worktree_list(r)
            items = worktrees(r, sessions, chunks.get(r, {}), days, listed) + branches(r, listed, days)
            repo = unused_repo(r, listed, sessions, (totals or {}).get(r, 0), history_days, touched)
            hit = (time.time(), items + ([repo] if repo else []))
            _cache[(r, days)] = hit
        out += [dict(w, repo=Path(r).name, repo_root=r) for w in hit[1] if w["verdict"] != "keep"]
    return out
