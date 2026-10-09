"""Git repo indexing: code chunks + blame, session<->code links, and 'who knows this area' experts."""
import hashlib
import json
import re
import subprocess
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from . import config, settings
from .files import secret
from .indexer import CODE_QUERY, LAYOUT_VERSION, embed, reconcile
from .layout import fit3d, place_new, unit
from .projects import project_root

CODE_INDEX_PATH = config.DATA_DIR / "code.tv"
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".zip", ".gz", ".lock", ".woff", ".woff2",
            ".ttf", ".mp4", ".mov", ".parquet", ".pkl", ".csv", ".ipynb", ".map", ".snap"}
SKIP_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock", "Cargo.lock"}
MAX_BYTES = 200_000
GENERATED_DIRS = {"dist", "build", "vendor", "third_party", "node_modules", "out", "target", ".venv"}
CODE_CHUNK_CHARS = 1500
ALL = "__all__"  # pseudo-repo: every indexed repo in one joint layout, paths prefixed with '<repo>/' 


def git(root, *args, check=False) -> str:
    r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, errors="replace")
    return r.stdout


def norm_remote(url: str) -> str:
    u = re.sub(r"^(git@|https?://|ssh://git@)", "", url.strip()).replace(":", "/")
    return re.sub(r"\.git$", "", u).lower()


def area_of(path: str) -> str:
    """Two-level area key for a repo-relative file path ('core/pipelines/x.py' -> 'core/pipelines')."""
    parts = path.split("/")
    return "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else ".")


def locked(fn):
    """Serialize use of the shared SQLite connection (FastAPI runs sync endpoints on a thread pool)."""
    import functools

    @functools.wraps(fn)
    def wrap(self, *a, **k):
        with self.lock:
            return fn(self, *a, **k)
    return wrap


def is_bot(author: str) -> bool:
    a = author.lower()
    return "[bot]" in a or a.startswith(("dependabot", "renovate", "github-actions"))


def chunk_code(text: str):
    """Line-based chunks (start, end, body), preferring to break on blank lines."""
    lines = text.split("\n")
    out, start, size = [], 0, 0
    for i, ln in enumerate(lines):
        size += len(ln) + 1
        if size >= CODE_CHUNK_CHARS or (size > 700 and not ln.strip()):
            out.append((start + 1, i + 1, "\n".join(lines[start:i + 1])))
            start, size = i + 1, 0
    if start < len(lines) and "\n".join(lines[start:]).strip():
        out.append((start + 1, len(lines), "\n".join(lines[start:])))
    return [c for c in out if c[2].strip()]


def find_repos(base: Path, depth: int, exclude: set) -> list[str]:
    """Git repo roots at or below base (not descending into a repo once found, nor hidden/excluded folders)."""
    import os
    if not base.is_dir():
        return []
    if (base / ".git").exists():
        return [str(base.resolve())]
    out, depth0 = [], len(base.parts)
    for d, dirs, files in os.walk(base):
        if ".git" in dirs or ".git" in files:
            out.append(str(Path(d).resolve()))
            dirs[:] = []
            continue
        dirs[:] = [x for x in dirs if not x.startswith(".") and x not in exclude] if len(Path(d).parts) - depth0 < depth else []
    return out


def _sig(state: dict) -> str:
    return hashlib.sha1("\n".join(f"{p} {b}" for p, b in sorted(state.items())).encode()).hexdigest()


def tree_state(checkout) -> dict:
    """repo-relative path -> blob hash of what's on disk now: tracked files (modified ones re-hashed) plus
    untracked files that aren't ignored. Deleted files are left out."""
    state = {}
    for ln in git(checkout, "ls-files", "-s", "-z").split("\0"):
        m = re.match(r"\d+ (\w+) \d+\t(.+)", ln)
        if m:
            state[m.group(2)] = m.group(1)
    deleted = set(x for x in git(checkout, "ls-files", "-d", "-z").split("\0") if x)
    changed = [x for x in git(checkout, "ls-files", "-m", "-o", "--exclude-standard", "-z").split("\0")
               if x and x not in deleted and Path(Path(checkout) / x).is_file()
               and Path(x).suffix.lower() not in SKIP_EXT][:5000]
    for p in deleted:
        state.pop(p, None)
    if changed:
        r = subprocess.run(["git", "-C", checkout, "hash-object", "--stdin-paths"], input="\n".join(changed),
                           capture_output=True, text=True)
        for p, h in zip(changed, r.stdout.split()):
            state[p] = h
    return state


def worktree_list(root):
    """[(path, branch)] for the repo's checkouts, from `git worktree list` (includes .claude/worktrees)."""
    out, cur = [], {}
    for ln in git(root, "worktree", "list", "--porcelain").split("\n") + [""]:
        if ln.startswith("worktree "):
            cur = {"path": ln[9:]}
        elif ln.startswith("branch "):
            cur["branch"] = ln[7:].removeprefix("refs/heads/")
        elif not ln and cur:
            out.append((cur["path"], cur.get("branch", "detached")))
            cur = {}
    return out


def blame_lines(root, path, me=None):
    """line number (1-based) -> (author 'Name <email>', epoch) for the file as it is on disk; lines not committed
    yet are credited to `me` (name, email)."""
    out = git(root, "blame", "-w", "--line-porcelain", "--", path)
    res, cur_a, cur_m, cur_t, n = {}, "", "", 0, 0
    for ln in out.split("\n"):
        if ln.startswith("author "):
            cur_a = ln[7:]
        elif ln.startswith("author-mail "):
            cur_m = ln[12:].strip("<>")
        elif ln.startswith("author-time "):
            cur_t = int(ln[12:])
        elif ln.startswith("\t"):
            n += 1
            if cur_a == "Not Committed Yet" and me:
                res[n] = (f"{me[0]} <{me[1]}>", cur_t)
            else:
                res[n] = (f"{cur_a} <{cur_m}>", cur_t)
    return res


class Repos:
    def __init__(self, store):
        from turbovec import IdMapIndex
        self.store = store
        self.db = store.db
        self.lock = store.lock
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS repos(root TEXT PRIMARY KEY, name TEXT, remote TEXT, head TEXT, indexed REAL);
        CREATE TABLE IF NOT EXISTS code_files(repo TEXT, path TEXT, blob TEXT, PRIMARY KEY(repo, path));
        CREATE TABLE IF NOT EXISTS code_chunks(
          id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT, path TEXT, dir TEXT, start INTEGER, end INTEGER,
          text TEXT, vec BLOB, authors TEXT, last_ts INTEGER, x REAL, y REAL, z REAL);
        CREATE INDEX IF NOT EXISTS cc_repo ON code_chunks(repo, path);
        CREATE TABLE IF NOT EXISTS commits(repo TEXT, sha TEXT, ts INTEGER, author TEXT, subject TEXT, PRIMARY KEY(repo, sha));
        """)
        for col in ("gx REAL", "gy REAL", "gz REAL", "wt TEXT DEFAULT ''"):  # joint layout; linked worktree path
            try:
                self.db.execute(f"ALTER TABLE code_chunks ADD COLUMN {col}")
            except Exception:
                pass
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS checkout_files(repo TEXT, wt TEXT, path TEXT, blob TEXT, PRIMARY KEY(repo, wt, path));
        CREATE TABLE IF NOT EXISTS worktrees(path TEXT PRIMARY KEY, repo TEXT, branch TEXT);
        """)
        if not self.db.execute("SELECT 1 FROM checkout_files LIMIT 1").fetchone():  # carry over the old per-repo table
            self.db.execute("INSERT OR IGNORE INTO checkout_files SELECT repo, '', path, blob FROM code_files")
        self.db.commit()
        self.index = IdMapIndex.load(str(CODE_INDEX_PATH)) if CODE_INDEX_PATH.exists() else IdMapIndex(dim=config.EMBED_DIM, bit_width=4)
        if reconcile(self.index, self.db, "code_chunks"):
            self.index.sync(str(CODE_INDEX_PATH))
        self._cache = {}
        self.pending = None   # callable -> set of repo roots with changes waiting (set by the server's file watcher)
        self.requeue = None   # callable(set) to put them back
        self._servicing = False
        self._recheck = set()
        self._last = 0
        self._scope = (None, None)
        self._team = {}

    # ---- discovery ---------------------------------------------------------
    def _extra_roots(self):
        """Repos beyond the ones sessions ran in: settings `repos`, repos inside the indexed `folders`, and (with
        the repos/discover source) repos found under `sweep_roots`. Folders are searched `sweep_depth` levels deep."""
        hit = getattr(self, "_extra", None)
        if hit and hit[0] == settings.load() and time.time() - hit[1] < 600:
            return hit[2]
        s = settings.load()
        ex = set(s["exclude"])
        out = [r for base in settings.paths("repos") for r in find_repos(base, s["sweep_depth"], ex)]
        swept = [r for base in (settings.paths("folders") + settings.paths("sweep_roots") if settings.enabled("repos/discover") else [])
                 for r in find_repos(base, s["sweep_depth"], ex)]
        self.skipped = []
        for r in dict.fromkeys(swept):  # found by the sweep: skip giant repos unless named explicitly or used by sessions
            n = git(r, "ls-files").count("\n")
            if n > s["max_repo_files"]:
                self.skipped.append(dict(root=r, files=n))
            else:
                out.append(r)
        out = list(dict.fromkeys(out))
        self._extra = (s, time.time(), out)
        return out

    @locked
    def discover(self):
        """Repos your sessions ran in (plus any --repos); clones of the same remote collapse to the most-used root."""
        if not settings.enabled("repos"):
            return []
        use = Counter()
        for (p,) in (self.db.execute("SELECT project FROM sessions") if settings.enabled("repos/discover") else []):
            r = project_root(p or "")
            if r and Path(r).exists():
                use[r] += 1
        for r in self._extra_roots():
            r = project_root(r) or r
            use[r] += 0
        by_remote = {}
        for r, n in use.most_common():
            remote = norm_remote(git(r, "config", "--get", "remote.origin.url")) or r
            by_remote.setdefault(remote, (r, remote))
        return [v for v in by_remote.values() if settings.enabled(f"repos/{v[0]}")]

    @locked
    def canon_map(self):
        """any session repo root -> canonical repo root (handles duplicate clones). Cached for a minute."""
        if getattr(self, "_canon", None) and time.time() - self._canon[0] < 60:
            return self._canon[1]
        m = {}
        canon = {remote: r for r, remote in self.discover()}
        for (p,) in self.db.execute("SELECT DISTINCT project FROM sessions"):
            r = project_root(p or "")
            if r:
                remote = norm_remote(git(r, "config", "--get", "remote.origin.url")) or r
                if remote in canon:
                    m[r] = canon[remote]
        self._canon = (time.time(), m)
        al = {}
        for r, c in m.items():
            al[r] = c
        for c in set(m.values()):
            for ln in git(c, "worktree", "list", "--porcelain").split("\n"):
                if ln.startswith("worktree "):
                    al[ln[9:]] = c
        self._alias = sorted(al.items(), key=lambda kv: -len(kv[0]))
        return m

    def locate(self, path: str):
        """Absolute path -> (canonical repo root, repo-relative path). Folds clones, linked worktrees,
        .claude/worktrees checkouts, and deleted sibling clones like '<repo>-feature/' into their repo."""
        self.canon_map()
        p = re.sub(r"/\.claude/worktrees/[^/]+(?=/|$)", "", path.rstrip("/"))
        for a, c in self._alias:
            if p == a or p.startswith(a + "/"):
                return c, p[len(a) + 1:]
        for a, c in self._alias:
            m = re.match(re.escape(c) + r"-[^/]+(?:/(.*))?$", p)
            if m:
                return c, m.group(1) or ""
        return None, None

    def session_scope(self):
        """sid -> {project, folder, dirs}: the canonical repo a session belongs to and the repo directories it
        touched (as 'repo/dir/sub' keys). Cached until sessions or touched files change."""
        with self.lock:
            fp = self.db.execute("SELECT (SELECT COUNT(*) FROM sessions), (SELECT MAX(mtime) FROM sessions), "
                                 "(SELECT COUNT(*) FROM session_files)").fetchone()
            file_rows = self.db.execute("SELECT session_id, path FROM session_files WHERE path!=''").fetchall()
            sess_rows = self.db.execute("SELECT id, project, project_name FROM sessions").fetchall()
        if self._scope[0] == fp and time.time() - getattr(self, "_scope_t", 0) < 120:
            return self._scope[1]
        files = defaultdict(list)
        for sid, path in file_rows:
            files[sid].append(path)
        out = {}
        for sid, cwd, folder in sess_rows:
            root, rel_cwd = self.locate(cwd) if cwd else (None, None)
            dirs, per_repo = set(), Counter()
            if root and rel_cwd:
                dirs.add(f"{Path(root).name}/{rel_cwd}")
            for f in files.get(sid, []):
                r, rel = self.locate(f)
                if r is None or not rel:
                    continue
                per_repo[r] += 1
                d = rel.rsplit("/", 1)[0] if "/" in rel else ""
                dirs.add(f"{Path(r).name}/{d}" if d else Path(r).name)
            if root is None and per_repo:  # e.g. a session started in ~/Desktop that worked inside one repo
                r, n = per_repo.most_common(1)[0]
                if n >= 2 and n >= 0.5 * sum(per_repo.values()):
                    root = r
            out[sid] = dict(project=Path(root).name if root else folder, root=root, folder=folder,
                            dirs=sorted(d for d in dirs if "/" in d))
        self._scope, self._scope_t = (fp, out), time.time()
        return out

    # ---- indexing ----------------------------------------------------------
    def sync(self, log=lambda m: print(m, flush=True), force=False, only=None):
        """Bring every repo (or just `only`: canonical roots) up to date with what's on disk: committed, staged,
        modified and untracked files in the main checkout, plus files that differ from it in each linked worktree."""
        if not force and only is None and time.time() - self._last < 30:
            return 0
        if only is None:
            self._last = time.time()
            self.backfill_session_files()
        touched = 0
        found = self.discover()
        if only is None:  # repos no longer found or switched off: remove them
            keep = {r for r, _ in found}
            with self.lock:
                stale = [r for (r,) in self.db.execute("SELECT root FROM repos") if r not in keep]
            for r in stale:
                self.purge(r)
                log(f"repo {Path(r).name}: removed from the index")
                touched += 1
        for root, remote in found:
            if only is not None and root not in only:
                continue
            touched += self._service(root, log)
            try:
                touched += self._sync_repo(root, remote, log)
            except Exception as e:  # one broken repo shouldn't stop the rest
                log(f"repo {root}: {e!r}")
        if self._recheck and self.requeue:  # repos edited while they were being swept: one more pass
            self.requeue(self._recheck)
            self._recheck = set()
        if touched:
            self.reproject()
            with self.lock:
                self.index.sync(str(CODE_INDEX_PATH))
        return touched

    def _service(self, current, log):
        """During a long sweep, sync other repos that changed meanwhile so edits don't wait for the sweep to end."""
        if not self.pending or self._servicing:
            return 0
        roots = self.pending()
        if not roots:
            return 0
        self._servicing, n = True, 0
        try:
            if current in roots:  # the repo being swept: refresh its main checkout now, worktrees after the sweep
                self._recheck.add(current)
                state = tree_state(current)
                sig = _sig(state)
                if self.store.meta(f"tree:{current}") != sig:
                    n += self._index_checkout(current, "", state, None, log)
                    self.store.meta(f"tree:{current}", sig)
            with self.lock:
                remotes = dict(self.db.execute("SELECT root, remote FROM repos").fetchall())
            for r in roots - {current}:
                if r in remotes:
                    n += self._sync_repo(r, remotes[r], log)
        finally:
            self._servicing = False
        return n

    def checkouts(self):
        """Every checkout path we index -> canonical repo root (main checkouts and their linked worktrees)."""
        with self.lock:
            out = {r[0]: r[0] for r in self.db.execute("SELECT root FROM repos")}
            out.update({r[0]: r[1] for r in self.db.execute("SELECT path, repo FROM worktrees")})
        return out

    def _sync_repo(self, root, remote, log):
        name = Path(root).name
        head = git(root, "rev-parse", "HEAD").strip()
        with self.lock:
            row = self.db.execute("SELECT head FROM repos WHERE root=?", (root,)).fetchone()
            if not row:
                self.db.execute("INSERT OR REPLACE INTO repos VALUES(?,?,?,?,?)", (root, name, remote, None, time.time()))
                self.db.commit()
        if not row or row[0] != head:
            self._load_commits(root)
        state = tree_state(root)
        n = 0
        main_sig = _sig(state)
        main_changed = self.store.meta(f"tree:{root}") != main_sig
        if main_changed:
            n += self._index_checkout(root, "", state, None, log)
            self.store.meta(f"tree:{root}", main_sig)
        live = {}
        for wt, branch in worktree_list(root):
            if wt == root or not Path(wt).is_dir():
                continue
            n += self._service(root, log)
            live[wt] = branch
            ws = tree_state(wt)
            sig = _sig(ws) + main_sig
            if self.store.meta(f"tree:{wt}") != sig:
                n += self._index_checkout(root, wt, ws, state, log, branch)
                self.store.meta(f"tree:{wt}", sig)
        with self.lock:
            gone = [r[0] for r in self.db.execute("SELECT path FROM worktrees WHERE repo=?", (root,)) if r[0] not in live]
            for wt in gone:  # worktree removed: its branch-only chunks go too
                for (path,) in self.db.execute("SELECT path FROM checkout_files WHERE repo=? AND wt=?", (root, wt)).fetchall():
                    self._drop_file(root, path, wt)
                self.db.execute("DELETE FROM worktrees WHERE path=?", (wt,))
                n += 1
            self.db.executemany("INSERT OR REPLACE INTO worktrees VALUES(?,?,?)", [(w, root, b) for w, b in live.items()])
            self.db.execute("INSERT OR REPLACE INTO repos VALUES(?,?,?,?,?)", (root, name, remote, head, time.time()))
            self.db.commit()
        if n:
            self._cache.clear()
            self._team.clear()
        return n

    def _index_checkout(self, root, wt, state, base, log, branch=""):
        """Index one checkout. For a linked worktree (`wt`), only files that differ from the main checkout (`base`)."""
        target = state if base is None else {p: b for p, b in state.items() if base.get(p) != b}
        with self.lock:
            have = {r[0]: r[1] for r in self.db.execute("SELECT path, blob FROM checkout_files WHERE repo=? AND wt=?", (root, wt))}
        for path in list(have):
            if target.get(path) != have[path]:
                self._drop_file(root, path, wt)
        checkout = wt or root
        me = git(checkout, "config", "user.name").strip(), git(checkout, "config", "user.email").strip()
        secrets = settings.get("exclude_files")
        label = f"{Path(root).name}" + (f" @ {branch or Path(wt).name}" if wt else "")
        n_new = 0
        for path, blob in target.items():
            if have.get(path) == blob:
                continue
            self._service(root, log)  # keep other repos' edits flowing during a long index
            p = Path(path)
            if p.suffix.lower() in SKIP_EXT or p.name in SKIP_NAMES or ".min." in p.name or secret(p.name, secrets):
                continue
            try:
                raw = (Path(checkout) / path).read_bytes()
            except OSError:
                continue
            if len(raw) > MAX_BYTES or b"\x00" in raw[:2000] or not raw.strip():
                continue
            chunks = chunk_code(raw.decode("utf8", "replace"))
            if not chunks:
                continue
            bl = blame_lines(checkout, path, me)
            now = int(time.time())
            vecs = embed([f"{path}\n{c[2]}" for c in chunks])
            ids = []
            with self.lock:
                for (s, e, text), v in zip(chunks, vecs):
                    # lines without blame (untracked file) are the user's own, written just now
                    au = Counter(bl[i][0] if i in bl else f"{me[0]} <{me[1]}>" for i in range(s, e + 1))
                    ts = max((bl[i][1] if i in bl else now for i in range(s, e + 1)), default=0)
                    d = p.parts[0] if len(p.parts) > 1 else "."
                    cur_ = self.db.execute(
                        "INSERT INTO code_chunks(repo,path,dir,start,end,text,vec,authors,last_ts,wt) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (root, path, d, s, e, text, v.astype(np.float16).tobytes(), json.dumps(au), ts, wt))
                    ids.append(cur_.lastrowid)
                self.index.add_with_ids(vecs, np.array(ids, dtype=np.uint64))
                self.db.execute("INSERT OR REPLACE INTO checkout_files VALUES(?,?,?,?)", (root, wt, path, blob))
                self.db.commit()
            n_new += 1
            if n_new % 100 == 0:
                log(f"  {label}: {n_new} files")
                with self.lock:
                    self.index.sync(str(CODE_INDEX_PATH))
        if n_new:
            log(f"repo {label}: {n_new} files updated")
        return n_new

    def purge(self, root):
        with self.lock:
            for (cid,) in self.db.execute("SELECT id FROM code_chunks WHERE repo=?", (root,)).fetchall():
                try:
                    self.index.remove(cid)
                except Exception:
                    pass
            for t, col in (("code_chunks", "repo"), ("checkout_files", "repo"), ("commits", "repo"), ("worktrees", "repo"), ("repos", "root")):
                self.db.execute(f"DELETE FROM {t} WHERE {col}=?", (root,))
            self.db.execute("DELETE FROM meta WHERE key LIKE ?", (f"tree:{root}%",))
            self.db.commit()
        self._cache.clear()
        self._team.clear()

    def _drop_file(self, root, path, wt=""):
        with self.lock:
            for (cid,) in self.db.execute("SELECT id FROM code_chunks WHERE repo=? AND path=? AND wt=?", (root, path, wt)).fetchall():
                try:
                    self.index.remove(cid)
                except Exception:
                    pass
            self.db.execute("DELETE FROM code_chunks WHERE repo=? AND path=? AND wt=?", (root, path, wt))
            self.db.execute("DELETE FROM checkout_files WHERE repo=? AND wt=? AND path=?", (root, wt, path))

    @locked
    def _load_commits(self, root):
        out = git(root, "log", "--format=%H%x1f%at%x1f%ae%x1f%s", "-n", "5000")
        rows = [l.split("\x1f") for l in out.strip().split("\n") if l]
        self.db.executemany("INSERT OR REPLACE INTO commits VALUES(?,?,?,?,?)",
                            [(root, r[0], int(r[1]), r[2], r[3]) for r in rows if len(r) == 4])
        self.db.commit()

    @locked
    def backfill_session_files(self):
        """Sessions indexed before file tracking existed; a '' sentinel row marks 'parsed, nothing touched'."""
        from . import parsers
        have = {r[0] for r in self.db.execute("SELECT DISTINCT session_id FROM session_files")}
        for sid, src, path in self.db.execute("SELECT id, source, path FROM sessions").fetchall():
            if sid in have:
                continue
            try:
                s = parsers.parse(src, Path(path))
            except Exception:
                continue
            self.store.save_session_files(sid, s.touched)

    # ---- projection --------------------------------------------------------
    def reproject(self, force=False):
        """Per-repo UMAP plus one joint layout across repos. A layout is refit when new or grown >15%;
        otherwise new chunks are placed next to their nearest neighbours."""
        changed = False
        with self.lock:
            roots = [r[0] for r in self.db.execute("SELECT root FROM repos").fetchall()]
            multi = self.db.execute("SELECT COUNT(DISTINCT repo) FROM code_chunks").fetchone()[0] > 1
        for root in roots:
            changed |= self._layout(root, "WHERE repo=?", (root,), ("x", "y", "z"), force)
        if multi:
            changed |= self._layout(ALL, "", (), ("gx", "gy", "gz"), force)
        return changed

    @locked
    def needs_layout(self):
        """True when some code chunks have no map position yet (e.g. a layout attempt failed)."""
        q = "SELECT 1 FROM code_chunks WHERE x IS NULL LIMIT 1"
        multi = self.db.execute("SELECT COUNT(DISTINCT repo) FROM code_chunks").fetchone()[0] > 1
        return bool(self.db.execute(q).fetchone() or (multi and self.db.execute(
            "SELECT 1 FROM code_chunks WHERE gx IS NULL LIMIT 1").fetchone()))

    def _layout(self, key, where, args, cols, force):
        """UMAP over the main checkout's code only. Worktree versions of files are near-copies; fitting on them
        makes each point's neighbours mostly other versions of itself and dissolves the clusters. They are
        placed next to their nearest main-checkout code instead (usually the same file on the main branch)."""
        cx, cy, cz = cols
        version = LAYOUT_VERSION + "+mainfit"
        with self.lock:
            rows = self.db.execute(f"SELECT id, vec, {cx}, {cy}, {cz}, wt FROM code_chunks {where}", args).fetchall()
            fit_n = int(self.store.meta(f"code_fit_n:{key}") or 0)
            ok = self.store.meta(f"code_layout:{key}") == version
        if len(rows) < 4:
            return False
        X = unit(np.stack([np.frombuffer(r[1], np.float16) for r in rows]).astype(np.float32))
        have = np.array([r[2] is not None for r in rows])
        main = np.array([not r[5] for r in rows])
        if main.sum() < 4:  # nothing on the main checkout yet: lay out what there is
            main[:] = True
        ids = np.array([r[0] for r in rows])
        P = np.array([r[2:5] if r[2] is not None else (0, 0, 0) for r in rows], dtype=np.float32)
        n_main = int(main.sum())
        if force or not ok or n_main - fit_n > max(200, 0.15 * fit_n):
            P[main] = fit3d(X[main], n_neighbors=20, min_dist=0.08)
            P[~main] = place_new(X[main], P[main], X[~main], k=3, jitter=0.01) if (~main).any() else P[~main]
            todo = np.ones(len(rows), bool)
            with self.lock:
                self.store.meta(f"code_fit_n:{key}", n_main)
                self.store.meta(f"code_layout:{key}", version)
        elif not have.all():
            anchor = have & main
            todo = ~have
            if anchor.any():
                P[todo] = place_new(X[anchor], P[anchor], X[todo], k=3, jitter=0.01)
            else:
                P[todo] = place_new(X[have], P[have], X[todo])
        else:
            return False
        with self.lock:
            self.db.executemany(f"UPDATE code_chunks SET {cx}=?, {cy}=?, {cz}=? WHERE id=?",
                                [(*map(float, P[i]), int(ids[i])) for i in np.flatnonzero(todo)])
            self.db.commit()
        self._cache.clear()
        return True

    # ---- queries -----------------------------------------------------------
    def list(self):
        with self.lock:
            return self._list()

    def _list(self):
        rows = self.db.execute(
            "SELECT r.root, r.name, COUNT(c.id), COUNT(DISTINCT c.path) FROM repos r "
            "LEFT JOIN code_chunks c ON c.repo=r.root GROUP BY r.root ORDER BY 3 DESC").fetchall()
        live = {r[0] for r in self.db.execute("SELECT id FROM sessions WHERE n_chunks>0")}
        per = Counter(v["root"] for k, v in self.session_scope().items() if k in live)
        return [dict(root=r[0], name=r[1], chunks=r[2], files=r[3], sessions=per.get(r[0], 0)) for r in rows]

    def _sel(self, name, cols, extra=""):
        """Rows for one repo, or for every repo when name == ALL. `cols` must start with path; under ALL the
        path comes back prefixed with the repo name ('anastomo/docs/x.md') so trees and areas span repos."""
        with self.lock:
            if name == ALL:
                names = dict(self.db.execute("SELECT root, name FROM repos").fetchall())
                rows = self.db.execute(f"SELECT repo, {cols} FROM code_chunks WHERE 1=1 {extra}").fetchall()
                return [(f"{names.get(r[0], Path(r[0]).name)}/{r[1]}", *r[2:]) for r in rows]
            root = self._root(name)
            return self.db.execute(f"SELECT {cols} FROM code_chunks WHERE repo=? {extra}", (root,)).fetchall()

    def _root(self, name):
        if name == ALL:
            return ALL
        with self.lock:
            r = self.db.execute("SELECT root FROM repos WHERE name=? OR root=?", (name, name)).fetchone()
        return r[0] if r else None

    @locked
    def _session_links(self, root):
        """repo-relative path -> [(session_id, edited)] using session_files absolute paths."""
        aliases = [k for k, v in self.canon_map().items() if v == root] or [root]
        links = defaultdict(list)
        for sid, p, e in self.db.execute("SELECT session_id, path, edited FROM session_files WHERE path!=''").fetchall():
            for a in aliases:
                if p.startswith(a + "/"):
                    links[p[len(a) + 1:]].append((sid, bool(e)))
                    break
        return links

    @locked
    def points(self, name):
        root = self._root(name)
        if not root:
            return None
        if name == ALL:
            links = {}
            for r, n in self.db.execute("SELECT root, name FROM repos").fetchall():
                links.update({f"{n}/{k}": v for k, v in self._session_links(r).items()})
            rows = self._sel(ALL, "path, id, dir, start, end, authors, last_ts, gx, gy, gz", "AND gx IS NOT NULL")
        else:
            links = self._session_links(root)
            rows = self._sel(name, "path, id, dir, start, end, authors, last_ts, x, y, z", "AND x IS NOT NULL")
        pts = []
        for r in rows:
            au = json.loads(r[5] or "{}")
            top = max(au, key=au.get) if au else ""
            pts.append(dict(id=r[1], path=r[0], dir=r[2], start=r[3], end=r[4], author=top, ts=r[6], p=r[7:10],
                            sessions=sorted({s for s, _ in links.get(r[0], [])})))
        return dict(repo=name, points=pts)

    def team(self, name, days=90):
        """Per-person commit activity over the last `days`: volume, weekly rhythm, areas, recent commit subjects."""
        root = self._root(name)
        if not root:
            return None
        hit = self._team.get((root, days))
        if hit and time.time() - hit[0] < 300:
            return hit[1]
        out = git(root, "log", f"--since={days}.days", "--no-merges", "--numstat",
                  "--format=%x1e%H%x1f%at%x1f%an%x1f%ae%x1f%s")
        me = git(root, "config", "user.email").strip().lower()
        now, nw = time.time(), max(1, days // 7)
        people, head, area_activity, file_activity = {}, None, defaultdict(Counter), defaultdict(Counter)
        for rec in out.split("\x1e")[1:]:
            lines = rec.strip("\n").split("\n")
            bits = lines[0].split("\x1f", 4)
            if len(bits) < 5:
                continue
            sha, ts, an, ae, subj = bits[0], int(bits[1]), bits[2], bits[3].lower(), bits[4]
            head = head or sha
            if is_bot(an) or is_bot(ae):
                continue
            p = people.setdefault(ae, dict(name=an, email=ae, me=ae == me, commits=0, added=0, deleted=0,
                                           weeks=[0] * nw, areas=Counter(), files=set(), subjects=[], last=0))
            p["commits"] += 1
            p["last"] = max(p["last"], ts)
            p["weeks"][nw - 1 - min(nw - 1, int((now - ts) / (7 * 86400)))] += 1
            if len(p["subjects"]) < 15:
                p["subjects"].append(dict(sha=sha[:8], ts=ts, subject=subj))
            touched = set()
            for ln in lines[1:]:
                m = re.match(r"(\d+|-)\t(\d+|-)\t(.+)", ln)
                if not m:
                    continue
                path = re.sub(r"\{[^}]* => ([^}]*)\}", r"\1", m.group(3)).replace("//", "/")
                path = path.split(" => ")[-1]
                p["added"] += int(m.group(1)) if m.group(1) != "-" else 0
                p["deleted"] += int(m.group(2)) if m.group(2) != "-" else 0
                p["files"].add(path)
                file_activity[path][ae] += 1
                touched.add(area_of(path))
            for a in touched:
                p["areas"][a] += 1
                area_activity[a][ae] += 1
        ppl = sorted(people.values(), key=lambda p: -p["commits"])
        for p in ppl:
            p["author"] = f"{p['name']} <{p['email']}>"
            p["areas"] = [dict(area=a, commits=n) for a, n in p["areas"].most_common(5)]
            p["files"] = len(p["files"])
        res = dict(repo=name, root=root, days=days, head=head, people=ppl, me=me,
                   area_activity={a: dict(c) for a, c in area_activity.items()},
                   file_activity={f: dict(c) for f, c in file_activity.items()})
        self._team[(root, days)] = (time.time(), res)
        return res

    def _area_vectors(self, root):
        """Adaptive code areas: start from top-level directories and split any area holding more than ~6% of the
        repo into its subdirectories (up to 4 levels), so big trees like core/pipelines get meaningful parts.
        Returns area -> (centroid, n_chunks, owners Counter, last_ts). Cached until the next reindex."""
        key = ("areas", root)
        if key in self._cache:
            return self._cache[key]
        with self.lock:
            rows = self.db.execute("SELECT path, vec, authors, last_ts FROM code_chunks WHERE repo=?", (root,)).fetchall()
        dirs = [r[0].rsplit("/", 1)[0] if "/" in r[0] else "." for r in rows]
        limit = max(120, 0.06 * len(rows))
        areas = {"."}

        def split(prefix, depth):
            kids = Counter(d.split("/")[depth] for d in dirs
                           if d != "." and (not prefix or d.startswith(prefix + "/")) and len(d.split("/")) > depth)
            for k, n in kids.items():
                a = f"{prefix}/{k}" if prefix else k
                areas.add(a)
                if n > limit and depth < 3:
                    split(a, depth + 1)
        split("", 0)
        acc = {}
        for (path, vec, au, ts), d in zip(rows, dirs):
            a = self._area_for(d, areas)
            e = acc.setdefault(a, [np.zeros(config.EMBED_DIM, np.float32), 0, Counter(), 0])
            e[0] += np.frombuffer(vec, np.float16).astype(np.float32)
            e[1] += 1
            e[3] = max(e[3], ts or 0)
            for k, n in json.loads(au or "{}").items():
                if not is_bot(k):
                    e[2][k] += n
        out = {a: (e[0] / (np.linalg.norm(e[0]) + 1e-9), e[1], e[2], e[3]) for a, e in acc.items()}
        self._cache[key] = out
        return out

    @staticmethod
    def _area_for(d, areas):
        """Deepest area that contains directory d."""
        while d not in areas and "/" in d:
            d = d.rsplit("/", 1)[0]
        return d if d in areas else "."

    def territory(self, name, sids=None, top=8):
        """Explored vs unexplored code areas. Untouched areas are ranked by how close they sit to the areas the
        user's sessions worked in (code-embedding adjacency) and to what those sessions talked about."""
        root = self._root(name)
        if not root:
            return None
        areas = self._area_vectors(root)
        names = set(areas) | {a.rsplit("/", 1)[0] for a in areas if "/" in a}
        scope = self.session_scope()
        mine = [s for s, v in scope.items() if (sids is None or s in sids)
                and (v["project"] == name or any(d.startswith(name + "/") for d in v["dirs"]))]
        touched = Counter()
        for s in mine:
            hit = set()
            for d in scope[s]["dirs"]:
                if d.startswith(name + "/"):
                    a = self._area_for(d[len(name) + 1:], names)
                    if a in areas:
                        hit.add(a)
                    else:  # a directory above the area split (e.g. 'core'): count the areas below it as touched
                        hit.update(x for x in areas if x.startswith(a + "/") and a != ".")
            touched.update(hit)
        keys = [a for a in areas if areas[a][1] >= 3]
        if not keys:
            return dict(repo=name, footprint=[], suggestions=[], coverage=0, sessions=len(mine))
        A = np.stack([areas[a][0] for a in keys])
        T = [i for i, a in enumerate(keys) if a in touched]
        adj = (A @ A[T].T) if T else np.zeros((len(keys), 1))
        interest = np.zeros(len(keys))
        ids, C = self.store.centroids()
        sel = [i for i, s in enumerate(ids) if s in set(mine)]
        if sel:
            v = C[sel].mean(0)
            interest = A @ (v / (np.linalg.norm(v) + 1e-9))
        z = lambda x: (x - x.mean()) / (x.std() + 1e-9)
        score = 0.55 * z(adj.max(1)) + 0.45 * z(interest)
        team = self._team.get((root, 90), (0, {}))[1]
        me = (team.get("me") or git(root, "config", "user.email").strip()).lower()
        not_me = lambda a: f"<{me}>" not in a.lower()
        recent = defaultdict(Counter)
        for f, c in team.get("file_activity", {}).items():
            a = self._area_for(f.rsplit("/", 1)[0] if "/" in f else ".", set(areas))
            for e, n in c.items():
                if e != me:
                    recent[a][e] += n
        sugg = []
        for i in np.argsort(-score):
            a = keys[i]
            if a in touched or a == "." or a.split("/")[0] in GENERATED_DIRS:
                continue
            near = keys[T[int(adj[i].argmax())]] if T else None
            act = recent.get(a, Counter())
            sugg.append(dict(area=a, chunks=areas[a][1], score=round(float(score[i]), 3),
                             near=near, near_sim=round(float(adj[i].max()), 3) if T else None,
                             owners=[k for k, _ in areas[a][2].most_common(4) if not_me(k)][:3], last_ts=areas[a][3],
                             recent_commits=sum(act.values()), recent_people=[e for e, _ in act.most_common(3)]))
            if len(sugg) >= top:
                break
        total = sum(areas[a][1] for a in keys) or 1
        foot = sorted(([dict(area=a, chunks=areas[a][1], sessions=touched.get(a, 0), recent_commits=sum(recent.get(a, {}).values()),
                             owners=[k for k, _ in areas[a][2].most_common(3) if not_me(k)][:2]) for a in keys]),
                      key=lambda x: -x["chunks"])
        return dict(repo=name, footprint=foot, suggestions=sugg, sessions=len(mine),
                    coverage=round(sum(areas[a][1] for a in touched if a in areas) / total, 4))

    def _agg(self, root):
        """Blame aggregates for a repo, cached until the next reindex."""
        if root in self._cache:
            return self._cache[root]
        dirs, people = defaultdict(Counter), defaultdict(lambda: dict(lines=0, files=Counter(), dirs=Counter(), last=0))
        dir_chunks = Counter()
        cols = "path, authors, last_ts"
        agg_rows = self._sel(ALL, cols) if root == ALL else self._rows_for_root(root, cols)
        depth = 3 if root == ALL else 2
        for path, au, ts in agg_rows:
            parts = path.split("/")
            key = "/".join(parts[:depth]) if len(parts) > depth else ("/".join(parts[:-1]) or ".")
            dir_chunks[key] += 1
            for a, n in json.loads(au or "{}").items():
                if is_bot(a):
                    continue
                dirs[key][a] += n
                p = people[a]
                p["lines"] += n; p["files"][path] += n; p["dirs"][key] += n; p["last"] = max(p["last"], ts or 0)
        total = sum(p["lines"] for p in people.values()) or 1
        owners = [dict(dir=d, chunks=dir_chunks[d], lines=sum(c.values()),
                       owners=[dict(author=a, lines=n) for a, n in c.most_common(4)])
                  for d, c in sorted(dirs.items(), key=lambda kv: -dir_chunks[kv[0]])]
        ppl = [dict(author=a, lines=p["lines"], share=round(p["lines"] / total, 4), files=len(p["files"]),
                    last_active=p["last"], dirs=[d for d, _ in p["dirs"].most_common(4)])
               for a, p in sorted(people.items(), key=lambda kv: -kv[1]["lines"])]
        self._cache[root] = (owners, ppl)
        return owners, ppl

    def _rows_for_root(self, root, cols):
        with self.lock:
            return self.db.execute(f"SELECT {cols} FROM code_chunks WHERE repo=?", (root,)).fetchall()

    def knowledge_gaps(self, name, inactive_days=180, min_lines=150):
        """Areas whose knowledge left with their authors: most of the code (by git blame) was written by people who
        haven't committed for `inactive_days` (measured from the repo's latest commit, so a quiet repo doesn't flag
        everyone). For each area: how much of it the departed wrote, who they were, who still active knows it best,
        and how many active people hold a real share of it (bus factor)."""
        root = self._root(name)
        if not root or root == ALL:
            return None
        with self.lock:
            rows = self.db.execute("SELECT path, authors FROM code_chunks WHERE repo=? AND wt=''", (root,)).fetchall()
            last_by_email = dict(self.db.execute("SELECT lower(author), MAX(ts) FROM commits WHERE repo=? GROUP BY lower(author)", (root,)).fetchall())
            ref = self.db.execute("SELECT MAX(ts) FROM commits WHERE repo=?", (root,)).fetchone()[0] or 0
        cutoff = ref - inactive_days * 86400

        def who(a):  # "Name <email>" -> (name, email)
            m = re.match(r"(.*?)\s*<([^>]*)>", a)
            return (m.group(1).strip(), m.group(2).strip().lower()) if m else (a.strip(), "")
        # one person can commit under several emails: they're active if any email with their name is
        emails_by_name = defaultdict(set)
        area_lines, lines_by = defaultdict(Counter), Counter()
        for path, au in rows:
            area = area_of(path)
            for a, n in json.loads(au or "{}").items():
                if is_bot(a):
                    continue
                nm, em = who(a)
                emails_by_name[nm.lower()].add(em)
                area_lines[area][nm] += n
                lines_by[nm] += n
        canon = {}
        for nm in lines_by:
            canon[nm] = nm
        last = {nm: max((last_by_email.get(e, 0) for e in emails_by_name[nm.lower()]), default=0) for nm in lines_by}
        departed = {nm for nm, t in last.items() if t and t < cutoff}
        unknown = {nm for nm, t in last.items() if not t}  # never seen in the last 5000 commits: long gone
        gone = departed | unknown
        people = [dict(name=nm, lines=lines_by[nm], last_active=last[nm] or None, departed=nm in gone)
                  for nm, _ in lines_by.most_common(40)]
        gaps = []
        for area, c in area_lines.items():
            total = sum(c.values())
            if total < min_lines or area == "." or area.split("/")[0] in GENERATED_DIRS:
                continue
            gone_lines = sum(n for nm, n in c.items() if nm in gone)
            share = gone_lines / total
            active = [(nm, n) for nm, n in c.most_common() if nm not in gone]
            bus = sum(1 for _, n in active if n / total >= 0.10)
            if share < 0.5 and bus >= 1:
                continue
            parent = area.split("/")[0]
            near = Counter()
            for a2, c2 in area_lines.items():
                if a2 != area and a2.split("/")[0] == parent:
                    for nm, n in c2.items():
                        if nm not in gone:
                            near[nm] += n
            ask = (active[0][0] if active else (near.most_common(1)[0][0] if near else None))
            gaps.append(dict(area=area, lines=total, departed_share=round(share, 3), bus_factor=bus,
                             departed=[dict(name=nm, share=round(n / total, 3), last_active=last.get(nm) or None)
                                       for nm, n in c.most_common() if nm in gone][:4],
                             active=[dict(name=nm, share=round(n / total, 3)) for nm, n in active[:4]],
                             ask=ask, ask_reason=("knows the most of what's left" if active else
                                                  f"most active nearby in {parent}/" if near else None)))
        gaps.sort(key=lambda g: -(g["lines"] * g["departed_share"]))
        return dict(repo=name, as_of=ref, inactive_days=inactive_days, people=people, gaps=gaps[:40],
                    departed=sorted(gone, key=lambda nm: -lines_by[nm])[:20])

    def owners(self, name):
        root = self._root(name)
        return self._agg(root)[0][:40] if root else []

    def people(self, name):
        root = self._root(name)
        return self._agg(root)[1][:60] if root else []

    @locked
    def chunk(self, cid):
        r = self.db.execute("SELECT repo, path, start, end, text, authors, last_ts, wt FROM code_chunks WHERE id=?", (cid,)).fetchone()
        if not r:
            return None
        links = self._session_links(r[0]).get(r[1], [])
        titles = {sid: (t, src) for sid, t, src in self.db.execute(
            f"SELECT id, title, source FROM sessions WHERE id IN ({','.join('?' * len(links)) or 'NULL'})", [s for s, _ in links])}
        sess = [dict(id=s, edited=e, title=titles.get(s, ("", ""))[0], source=titles.get(s, ("", ""))[1]) for s, e in links]
        commits = [dict(sha=c[0][:8], ts=c[1], author=c[2], subject=c[3]) for c in self._file_commits(r[0], r[1])]
        br = self.db.execute("SELECT branch FROM worktrees WHERE path=?", (r[7],)).fetchone() if r[7] else None
        return dict(id=cid, repo=Path(r[0]).name, path=r[1], start=r[2], end=r[3], text=r[4],
                    authors=json.loads(r[5] or "{}"), last_ts=r[6], sessions=sess, commits=commits,
                    worktree=r[7] or None, branch=br[0] if br else None, checkout=r[7] or r[0])

    def _file_commits(self, root, path):
        out = git(root, "log", "--format=%H%x1f%at%x1f%ae%x1f%s", "-n", "8", "--", path)
        return [l.split("\x1f") for l in out.strip().split("\n") if l.count("\x1f") == 3]

    @locked
    def _experts_from(self, hits):
        """hits: [(chunk_id, similarity)] -> ranked people by similarity-weighted blamed lines."""
        score, files, last = Counter(), defaultdict(Counter), {}
        for cid, sim in hits:
            r = self.db.execute("SELECT path, authors, last_ts FROM code_chunks WHERE id=?", (cid,)).fetchone()
            if not r:
                continue
            for a, n in json.loads(r[1] or "{}").items():
                if is_bot(a):
                    continue
                score[a] += max(sim, 0) * n
                files[a][r[0]] += n
                last[a] = max(last.get(a, 0), r[2])
        tot = sum(score.values()) or 1
        return [dict(author=a, share=round(s / tot, 3), last_active=last[a],
                     files=[f for f, _ in files[a].most_common(4)]) for a, s in score.most_common(8)]

    def experts(self, q=None, vec=None, repo=None, k=40, allow=None):
        """`allow`: optional chunk-id allowlist (a scope); `repo` narrows further to one repo."""
        if vec is None:
            vec = embed([q], query=CODE_QUERY)
        qv = vec.reshape(1, -1).astype(np.float32)
        if allow is not None and not len(allow):
            return dict(chunks=[], experts=[], hit_ids=[])
        with self.lock:  # DB rows are inserted before the index add; hold the lock so we only allow indexed ids
            if repo and repo != ALL:
                root = self._root(repo)
                ids = [r[0] for r in self.db.execute("SELECT id FROM code_chunks WHERE repo=?", (root,))
                       if self.index.contains(r[0])]
                if not ids:
                    return dict(chunks=[], experts=[], hit_ids=[])
                allow = np.array(ids, dtype=np.uint64)
            sc, ids = self.index.search(qv, k=k, allowlist=allow) if allow is not None else self.index.search(qv, k=k)
        hits = [(int(i), float(s)) for s, i in zip(sc[0], ids[0])]
        chunks = []
        for cid, s in hits[:10]:
            r = self.db.execute("SELECT repo, path, start, end FROM code_chunks WHERE id=?", (cid,)).fetchone()
            if r:
                chunks.append(dict(id=cid, repo=Path(r[0]).name, path=r[1], start=r[2], end=r[3], score=s))
        return dict(chunks=chunks, experts=self._experts_from(hits), hit_ids=[h[0] for h in hits])

    @locked
    def session_code(self, sid):
        """Files the session touched, commits in its time window, and semantically nearest code + experts."""
        s = self.db.execute("SELECT project, started, updated FROM sessions WHERE id=?", (sid,)).fetchone()
        if not s:
            return None
        canon = self.canon_map()
        root = canon.get(project_root(s[0] or "") or "")
        files = []
        for p, e in self.db.execute("SELECT path, edited FROM session_files WHERE session_id=? AND path!=''", (sid,)).fetchall():
            files.append(dict(path=p, edited=bool(e)))
        commits = []
        if root and s[1] and s[2]:
            from datetime import datetime
            f = lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
            lo, hi = f(s[1]) - 300, f(s[2]) + 1800
            me = git(root, "config", "user.email").strip().lower()  # only the user's own commits count as "delivered"
            commits = [dict(sha=r[0][:8], ts=r[1], author=r[2], subject=r[3]) for r in self.db.execute(
                "SELECT sha, ts, author, subject FROM commits WHERE repo=? AND ts BETWEEN ? AND ? AND lower(author)=? ORDER BY ts",
                (root, lo, hi, me))]
        rows = self.db.execute("SELECT vec FROM chunks WHERE session_id=?", (sid,)).fetchall()
        near = dict(chunks=[], experts=[])
        if rows and self.db.execute("SELECT 1 FROM code_chunks LIMIT 1").fetchone():
            c = np.mean([np.frombuffer(r[0], np.float16).astype(np.float32) for r in rows], axis=0)
            c /= np.linalg.norm(c) + 1e-9
            near = self.experts(vec=c, repo=Path(root).name if root else None)
        return dict(repo=Path(root).name if root else None, files=files, commits=commits,
                    nearest=near["chunks"], experts=near["experts"])

    def overlaps(self, name, top=15):
        """Conceptual overlap: directory pairs (2 levels deep) whose embedding centroids are closest."""
        cross = name == ALL  # across repos: only pairs from different repos are interesting
        rows = self._sel(name, "path, vec, authors")
        depth = 3 if cross else 2
        groups, owners = defaultdict(list), defaultdict(Counter)
        for path, vec, au in rows:
            parts = path.split("/")
            key = "/".join(parts[:depth]) if len(parts) > depth else "/".join(parts[:-1]) or "."
            groups[key].append(np.frombuffer(vec, np.float16).astype(np.float32))
            for a, n in json.loads(au or "{}").items():
                if not is_bot(a):
                    owners[key][a] += n
        keys = [k for k, v in groups.items() if len(v) >= 3 and (not cross or "/" in k)]
        if len(keys) < 2:
            return []
        C = np.stack([np.mean(groups[k], axis=0) for k in keys])
        C /= np.linalg.norm(C, axis=1, keepdims=True) + 1e-9
        S = C @ C.T
        pairs = []
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                if keys[i].startswith(keys[j] + "/") or keys[j].startswith(keys[i] + "/"):
                    continue
                if cross and keys[i].split("/")[0] == keys[j].split("/")[0]:
                    continue
                pairs.append((float(S[i, j]), keys[i], keys[j]))
        pairs.sort(reverse=True)
        top_owner = lambda k: [a for a, _ in owners[k].most_common(2)]
        return [dict(sim=round(s, 3), a=a, b=b, owners_a=top_owner(a), owners_b=top_owner(b)) for s, a, b in pairs[:top]]
