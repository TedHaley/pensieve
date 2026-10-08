"""Documents in the user's folders (settings `folders`): text, Markdown, code, PDF and Word files, chunked and
embedded like sessions. Other files are indexed by name only so they still turn up in search. Git repos inside these
folders are skipped here; repos.py indexes them with blame and history."""
import hashlib
import os
import re
import time
import zipfile
from collections import Counter
from pathlib import Path

import numpy as np

from . import apps, config, settings
from .indexer import LAYOUT_VERSION, embed, reconcile
from .layout import cluster, fit3d, place_new, unit

FILES_INDEX_PATH = config.DATA_DIR / "files.tv"
TEXT_EXT = {".txt", ".md", ".markdown", ".rst", ".org", ".tex", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml",
            ".ini", ".cfg", ".conf", ".xml", ".html", ".htm", ".css", ".scss", ".py", ".js", ".ts", ".tsx", ".jsx",
            ".java", ".kt", ".swift", ".go", ".rs", ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".cs",
            ".scala", ".sql", ".sh", ".zsh", ".bash", ".r", ".jl", ".lua", ".pl", ".ipynb", ".log", ".env.example"}
DOC_EXT = {".pdf", ".docx", ".pptx"}
FILES_VERSION = "files-v2"  # bump to re-extract every file on the next sweep
NAME_ONLY_MAX = 4000  # per folder sweep: name-only entries beyond this are skipped (e.g. huge Downloads)
CHUNK = 1500
MAX_CHUNKS = 200      # per file; long PDFs are truncated
KIND = {**{e: "doc" for e in (".md", ".markdown", ".rst", ".org", ".txt", ".tex")}, ".pdf": "pdf", ".docx": "doc",
        ".pptx": "slides", ".csv": "data", ".tsv": "data", ".json": "data", ".ipynb": "notebook"}


def kind_of(path: Path) -> str:
    e = path.suffix.lower()
    if e in KIND:
        return KIND[e]
    if e in TEXT_EXT:
        return "code"
    return "other"


def _xml_text(z: zipfile.ZipFile, names) -> str:
    out = []
    for n in names:
        x = z.read(n).decode("utf8", "replace")
        x = re.sub(r"</w:p>|</a:p>", "\n", x)
        out.append(re.sub(r"<[^>]+>", "", x))
    return "\n".join(out)


def extract(path: Path) -> str:
    """Plain text of a document, or '' if it has none we can read."""
    e = path.suffix.lower()
    try:
        if e == ".pdf":
            from pypdf import PdfReader
            r = PdfReader(str(path))
            return "\n\n".join((p.extract_text() or "") for p in r.pages[:300])
        if e == ".docx":
            with zipfile.ZipFile(path) as z:
                return _xml_text(z, ["word/document.xml"])
        if e == ".pptx":
            with zipfile.ZipFile(path) as z:
                slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                                key=lambda n: int(re.findall(r"\d+", n)[-1]))
                return _xml_text(z, slides)
        if e in TEXT_EXT or not e:
            b = path.read_bytes()[:2_000_000]
            if b"\x00" in b[:4000]:
                return ""
            return b.decode("utf8", "replace")
    except Exception:
        return ""
    return ""


def chunk_text(text: str):
    """(start_line, end_line, text) chunks broken on blank lines near CHUNK characters."""
    lines = text.split("\n")
    out, start, size = [], 0, 0
    for i, ln in enumerate(lines):
        size += len(ln) + 1
        if size >= CHUNK or (size > 600 and not ln.strip()):
            body = "\n".join(lines[start:i + 1])
            while len(body) > CHUNK * 2:  # one enormous line (minified, PDF without breaks)
                out.append((start + 1, i + 1, body[:CHUNK])); body = body[CHUNK:]
            out.append((start + 1, i + 1, body))
            start, size = i + 1, 0
    if start < len(lines):
        out.append((start + 1, len(lines), "\n".join(lines[start:])))
    return [c for c in out if c[2].strip()][:MAX_CHUNKS]


def secret(name: str, patterns) -> bool:
    from fnmatch import fnmatch
    n = name.lower()
    return any(fnmatch(n, p.lower()) for p in patterns)


def walk(root: Path, exclude: set, exclude_files=()):
    """Files under root, skipping hidden folders, excluded names, secret-looking files, and git repos (repos.py)."""
    for d, dirs, files in os.walk(root):
        if d != str(root) and (".git" in dirs or ".git" in files):
            dirs[:] = []
            continue
        dirs[:] = [x for x in dirs if not x.startswith(".") and x not in exclude
                   and not x.endswith((".app", ".photoslibrary", ".bundle", ".framework"))]
        for f in files:
            if not f.startswith(".") and not f.startswith("~$") and not secret(f, exclude_files):
                yield Path(d) / f


class Files:
    def __init__(self, store):
        from turbovec import IdMapIndex
        self.store, self.db, self.lock = store, store.db, store.lock
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS files(
          path TEXT PRIMARY KEY, root TEXT, name TEXT, ext TEXT, kind TEXT, mtime REAL, size INTEGER,
          n_chunks INTEGER, x REAL, y REAL, z REAL, cluster INTEGER);
        CREATE TABLE IF NOT EXISTS file_chunks(
          id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, idx INTEGER, start INTEGER, end INTEGER, text TEXT, vec BLOB);
        CREATE INDEX IF NOT EXISTS fc_path ON file_chunks(path);
        """)
        self.index = IdMapIndex.load(str(FILES_INDEX_PATH)) if FILES_INDEX_PATH.exists() else IdMapIndex(dim=config.EMBED_DIM, bit_width=4)
        if reconcile(self.index, self.db, "file_chunks"):
            self.index.sync(str(FILES_INDEX_PATH))
        self._last = 0
        self.progress = {}

    # ---- indexing ----------------------------------------------------------
    def sync(self, log=lambda m: print(m, flush=True), force=False, on_progress=None) -> int:
        """Index new/changed files in the configured folders and drop ones that are gone. Returns files touched."""
        if not force and time.time() - self._last < 30:
            return 0
        self._last = time.time()
        s = settings.load()
        apps.refresh(settings.enabled)
        roots = self.roots()
        exclude, max_bytes = set(s["exclude"]), s["max_file_mb"] * 1_000_000
        with self.lock:
            known = {r[0]: (r[1], r[2]) for r in self.db.execute("SELECT path, mtime, size FROM files")}
        stale = self.store.meta("files_v") != FILES_VERSION
        seen, touched, name_only = set(), 0, Counter()
        for root in roots:
            for p in walk(root, exclude, s["exclude_files"]):
                try:
                    st = p.stat()
                except OSError:
                    continue
                k = kind_of(p)
                if k == "other":
                    name_only[root] += 1
                    if name_only[root] > NAME_ONLY_MAX:
                        continue
                sp = str(p)
                seen.add(sp)
                if (not stale and known.get(sp) == (st.st_mtime, st.st_size)) or time.time() - st.st_mtime < config.SETTLE_SECONDS:
                    continue
                try:
                    self._index_file(p, root, st, k, max_bytes)
                    touched += 1
                except Exception as e:
                    log(f"skip {p}: {e!r}")
                if touched % 50 == 0 and touched:
                    log(f"files: {touched} indexed")
                    with self.lock:
                        self.index.sync(str(FILES_INDEX_PATH))
                    if on_progress:
                        on_progress(touched)
        if stale:
            self.store.meta("files_v", FILES_VERSION)
        gone = [p for p in known if p not in seen]
        for p in gone:
            self._drop(p)
        if gone:
            with self.lock:
                self.db.commit()
        if touched or gone:
            self.reproject()
            with self.lock:
                self.index.sync(str(FILES_INDEX_PATH))
        return touched + len(gone)

    @staticmethod
    def roots():
        """Folders to index: the enabled folders plus enabled apps' content; nested roots fold into their parent."""
        cand = [p.resolve() for f, p in zip(settings.get("folders"), settings.paths("folders"))
                if settings.enabled(f"files/{f}") and p.is_dir()]
        cand += [p.resolve() for p in apps.roots(settings.enabled)]
        return [p for p in dict.fromkeys(cand) if not any(p != q and p.is_relative_to(q) for q in cand)]

    def sync_paths(self, paths, log=lambda m: print(m, flush=True)):
        """Index or drop just these paths (from the file watcher). Returns (changed count, paths to retry later
        because they were still being written)."""
        s = settings.load()
        roots = self.roots()
        exclude, max_bytes = set(s["exclude"]), s["max_file_mb"] * 1_000_000
        changed, retry = 0, []
        for sp in paths:
            p = Path(sp)
            root = next((r for r in roots if p.is_relative_to(r)), None)
            if root is None:
                continue
            if not p.exists():
                with self.lock:
                    gone = [r[0] for r in self.db.execute("SELECT path FROM files WHERE path=? OR path LIKE ?", (sp, sp + "/%"))]
                for g in gone:
                    self._drop(g)
                changed += len(gone)
                continue
            if not p.is_file() or not self._eligible(p, root, exclude, s["exclude_files"]):
                continue
            st = p.stat()
            if time.time() - st.st_mtime < config.SETTLE_SECONDS:
                retry.append(sp)
                continue
            with self.lock:
                row = self.db.execute("SELECT mtime, size FROM files WHERE path=?", (sp,)).fetchone()
            if row == (st.st_mtime, st.st_size):
                continue
            try:
                self._index_file(p, root, st, kind_of(p), max_bytes)
                changed += 1
                log(f"file updated: {p.name}")
            except Exception as e:
                log(f"skip {p}: {e!r}")
        if changed:
            with self.lock:
                self.db.commit()
            self.reproject()
            with self.lock:
                self.index.sync(str(FILES_INDEX_PATH))
        return changed, retry

    @staticmethod
    def _eligible(p: Path, root: Path, exclude, exclude_files):
        rel = p.relative_to(root).parts
        if any(x.startswith(".") or x in exclude or x.endswith((".app", ".photoslibrary", ".bundle", ".framework"))
               for x in rel[:-1]):
            return False
        if p.name.startswith((".", "~$")) or secret(p.name, exclude_files):
            return False
        d = p.parent
        while d != root and d.is_relative_to(root):  # inside a git repo: repos.py owns it
            if (d / ".git").exists():
                return False
            d = d.parent
        return True

    def _index_file(self, p: Path, root: Path, st, kind, max_bytes):
        rel = p.relative_to(root.parent) if root.parent != p else p
        head = f"{rel}\n"
        text = extract(p) if kind != "other" and st.st_size <= max_bytes else ""
        chunks = chunk_text(text) if text.strip() else []
        if not chunks:  # name-only entry: still findable by name and folder
            chunks = [(0, 0, f"{p.name}\n{p.parent}")]
        vecs = embed([head + c[2] for c in chunks])
        with self.lock:
            self._drop(str(p))
            ids = []
            for i, ((a, b, t), v) in enumerate(zip(chunks, vecs)):
                cur = self.db.execute("INSERT INTO file_chunks(path, idx, start, end, text, vec) VALUES(?,?,?,?,?,?)",
                                      (str(p), i, a, b, t, v.astype(np.float16).tobytes()))
                ids.append(cur.lastrowid)
            self.index.add_with_ids(vecs, np.array(ids, dtype=np.uint64))
            self.db.execute("INSERT OR REPLACE INTO files(path, root, name, ext, kind, mtime, size, n_chunks) VALUES(?,?,?,?,?,?,?,?)",
                            (str(p), str(root), p.name, p.suffix.lower().lstrip("."), kind, st.st_mtime, st.st_size, len(chunks)))
            self.db.commit()

    def _drop(self, path):
        with self.lock:
            for (cid,) in self.db.execute("SELECT id FROM file_chunks WHERE path=?", (path,)).fetchall():
                try:
                    self.index.remove(cid)
                except Exception:
                    pass
            self.db.execute("DELETE FROM file_chunks WHERE path=?", (path,))
            self.db.execute("DELETE FROM files WHERE path=?", (path,))

    # ---- layout ------------------------------------------------------------
    def centroids(self):
        with self.lock:
            rows = self.db.execute("SELECT path, vec FROM file_chunks").fetchall()
        by = {}
        for p, v in rows:
            by.setdefault(p, []).append(np.frombuffer(v, np.float16).astype(np.float32))
        paths = list(by)
        return paths, (unit(np.stack([np.mean(by[p], axis=0) for p in paths])) if paths else np.zeros((0, config.EMBED_DIM)))

    def needs_layout(self):
        with self.lock:
            return bool(self.db.execute("SELECT 1 FROM files WHERE x IS NULL LIMIT 1").fetchone())

    def reproject(self, force=False):
        """One point per file. Full UMAP when the set grew meaningfully; otherwise new files go next to neighbours."""
        paths, C = self.centroids()
        if len(paths) < 4:
            return False
        with self.lock:
            placed = {r[0]: r[1:] for r in self.db.execute("SELECT path, x, y, z, cluster FROM files WHERE x IS NOT NULL")}
            fit_n = int(self.store.meta("files_fit_n") or 0)
            ok = self.store.meta("files_layout") == LAYOUT_VERSION
        n = len(paths)
        if force or not ok or len(placed) < 0.5 * n or n - fit_n > max(60, 0.15 * fit_n):
            P = fit3d(C, n_neighbors=12, min_dist=0.2)
            lab = cluster(C)
            with self.lock:
                self.db.executemany("UPDATE files SET x=?, y=?, z=?, cluster=? WHERE path=?",
                                    [(*map(float, q), int(c), p) for q, c, p in zip(P, lab, paths)])
                self.store.meta("files_fit_n", n)
                self.store.meta("files_layout", LAYOUT_VERSION)
                self.db.commit()
            return True
        missing = [i for i, p in enumerate(paths) if p not in placed]
        if not missing:
            return False
        known = [i for i, p in enumerate(paths) if p in placed]
        Pk = np.array([placed[paths[i]][:3] for i in known], dtype=np.float32)
        Pn = place_new(C[known], Pk, C[missing], k=3, jitter=0.04)
        near = (C[missing] @ C[known].T).argmax(1)
        with self.lock:
            for j, i in enumerate(missing):
                self.db.execute("UPDATE files SET x=?, y=?, z=?, cluster=? WHERE path=?",
                                (*map(float, Pn[j]), placed[paths[known[near[j]]]][3], paths[i]))
            self.db.commit()
        return True

    # ---- queries -----------------------------------------------------------
    def points(self):
        with self.lock:
            rows = self.db.execute("SELECT path, root, name, ext, kind, mtime, size, n_chunks, x, y, z, cluster "
                                   "FROM files WHERE x IS NOT NULL").fetchall()
        home = str(Path.home())
        out = []
        for r in rows:
            root = Path(r[1])
            rel = os.path.relpath(r[0], root.parent)
            out.append(dict(id=f"file:{r[0]}", path=r[0], rel=rel, folder=os.path.dirname(rel), root=root.name,
                            name=r[2], ext=r[3], kind=r[4], mtime=r[5], size=r[6], n=r[7], p=r[8:11], cluster=r[11],
                            display=r[0].replace(home, "~", 1)))
        return out

    def file(self, path):
        with self.lock:
            r = self.db.execute("SELECT path, root, name, ext, kind, mtime, size, n_chunks FROM files WHERE path=?", (path,)).fetchone()
            if not r:
                return None
            chunks = self.db.execute("SELECT idx, start, end, text FROM file_chunks WHERE path=? ORDER BY idx", (path,)).fetchall()
        return dict(id=f"file:{r[0]}", path=r[0], root=r[1], name=r[2], ext=r[3], kind=r[4], mtime=r[5], size=r[6],
                    n=r[7], display=r[0].replace(str(Path.home()), "~", 1),
                    chunks=[dict(idx=c[0], start=c[1], end=c[2], text=c[3]) for c in chunks if c[1]])

    def similar(self, path, k=8):
        paths, C = self.centroids()
        if path not in paths:
            return []
        sims = C @ C[paths.index(path)]
        out = []
        for j in np.argsort(-sims)[1:k + 1]:
            out.append(dict(id=f"file:{paths[j]}", path=paths[j], name=Path(paths[j]).name,
                            display=paths[j].replace(str(Path.home()), "~", 1), score=float(sims[j])))
        return out

    def stats(self):
        with self.lock:
            return dict(self.db.execute("SELECT kind, COUNT(*) FROM files GROUP BY kind").fetchall())
