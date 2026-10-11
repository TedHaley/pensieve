"""Documents in the user's folders (settings `folders`), chunked and embedded like sessions. Each file type (TYPES)
is read for its content, indexed by name only, or skipped, and shown on the map or not, as `file_types` says. Git
repos inside these folders are skipped here; repos.py indexes them with blame and history."""
import email
import email.policy
import hashlib
import os
import re
import subprocess
import threading
import time
import zipfile
from collections import Counter
from pathlib import Path

import numpy as np

from pensieve import apps, config, settings
from pensieve.indexer import LAYOUT_VERSION, embed, reconcile
from pensieve.layout import cluster, fit3d, nearest, place_new, unit

FILES_INDEX_PATH = config.DATA_DIR / "files.tv"
TEXT_EXT = {".txt", ".md", ".markdown", ".rst", ".org", ".tex", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml",
            ".ini", ".cfg", ".conf", ".xml", ".html", ".htm", ".css", ".scss", ".py", ".js", ".ts", ".tsx", ".jsx",
            ".java", ".kt", ".swift", ".go", ".rs", ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".cs",
            ".scala", ".sql", ".sh", ".zsh", ".bash", ".r", ".jl", ".lua", ".pl", ".ipynb", ".log", ".env.example",
            ".srt", ".vtt"}
TEXTUTIL_EXT = {".rtf", ".doc", ".odt"}  # macOS textutil converts these to plain text
SPOTLIGHT_EXT = {".pages", ".key", ".numbers"}  # read through Spotlight's importer (mdimport)
FILES_VERSION = "files-v3"  # part of every content file's mode: bump to re-extract every file on the next sweep
NAME_ONLY_MAX = 4000  # per folder sweep: name-only entries beyond this are skipped (e.g. huge Downloads)
KIND = {**{e: "doc" for e in (".md", ".markdown", ".rst", ".org", ".txt", ".tex", ".rtf", ".doc", ".odt", ".pages",
                              ".eml", ".srt", ".vtt", ".docx")},
        ".pdf": "pdf", ".pptx": "slides", ".key": "slides", ".csv": "data", ".tsv": "data", ".json": "data",
        ".xlsx": "data", ".numbers": "data", ".ipynb": "notebook"}
MEDIA_EXT = {".png", ".jpg", ".jpeg", ".gif", ".heic", ".heif", ".webp", ".tif", ".tiff", ".bmp", ".svg", ".psd",
             ".raw", ".cr2", ".nef", ".dng", ".mp3", ".m4a", ".wav", ".aiff", ".aif", ".flac", ".ogg", ".mp4", ".mov",
             ".m4v", ".avi", ".mkv", ".webm"}
# id -> (label, what it covers, extensions). Order is the order shown in Settings.
TYPES = {
    "text": ("Text and notes", "Plain text, Markdown, RTF, Word 97, OpenDocument, mail, subtitles",
             {".txt", ".md", ".markdown", ".rst", ".org", ".tex", ".rtf", ".doc", ".odt", ".eml", ".srt", ".vtt"}),
    "pdf": ("PDFs", "PDF documents (text only, no scanned images)", {".pdf"}),
    "office": ("Office and iWork", "Word, PowerPoint, Excel, Pages, Keynote, Numbers",
               {".docx", ".pptx", ".xlsx", ".pages", ".key", ".numbers"}),
    "data": ("Data", "CSV, TSV, JSON, notebooks", {".csv", ".tsv", ".json", ".ipynb"}),
    "code": ("Code and config", "Source and config files outside git repos (repos are indexed as repos)", set()),
    "media": ("Images, audio, video", "Found by name only; their content is not read", MEDIA_EXT),
    "other": ("Other files", "Archives, apps, installers and everything else, by name only", set()),
}
_TYPE_OF = {e: t for t, (_, _, exts) in TYPES.items() for e in exts}
# folders macOS shows as one file: never walked into (old iWork documents and RTFD are such folders)
PACKAGES = (".app", ".photoslibrary", ".bundle", ".framework", ".pages", ".key", ".numbers", ".rtfd")


def kind_of(path: Path) -> str:
    e = path.suffix.lower()
    if e in KIND:
        return KIND[e]
    if e in TEXT_EXT:
        return "code"
    return "other"


def type_of(path) -> str:
    """The TYPES id for a path (or a bare extension like 'md')."""
    s = str(path).lower()
    e = Path(s).suffix if "/" in s or "." in s else "." + s
    return _TYPE_OF.get(e) or ("code" if e in TEXT_EXT else "other")


def level_of(path: Path) -> str:
    """content | name | off for this file under the current `file_types` settings."""
    t = type_of(path)
    lv = settings.file_type(t)["index"]
    return "name" if lv == "content" and t in ("media", "other") else lv


def mode_of(level: str) -> str:
    """What a file was indexed with; a file whose stored mode differs is indexed again."""
    if level != "content":
        return level
    return f"content/{settings.get('chunk_chars')}/{settings.get('max_chunks')}/{FILES_VERSION}"


def _xml_text(z: zipfile.ZipFile, names) -> str:
    out = []
    for n in names:
        x = z.read(n).decode("utf8", "replace")
        x = re.sub(r"</w:p>|</a:p>|</si>", "\n", x)  # paragraphs; spreadsheet strings
        out.append(re.sub(r"<[^>]+>", "", x))
    return "\n".join(out)


def _spotlight_text(path: Path) -> str:
    """The text Spotlight's importer reads from a file (iWork and anything else it has an importer for)."""
    r = subprocess.run(["mdimport", "-t", "-d3", str(path)], capture_output=True, text=True, timeout=60)
    m = re.search(r'kMDItemTextContent = "((?:[^"\\]|\\.)*)"', r.stdout + r.stderr)
    if not m:
        return ""
    s = re.sub(r"\\U([0-9a-fA-F]{4})", lambda u: chr(int(u.group(1), 16)), m.group(1))
    return re.sub(r"\\(.)", lambda c: {"n": "\n", "t": "\t", "r": ""}.get(c.group(1), c.group(1)), s)


def _mail_text(path: Path) -> str:
    with open(path, "rb") as f:
        msg = email.message_from_binary_file(f, policy=email.policy.default)
    head = "\n".join(f"{h}: {msg[h]}" for h in ("Subject", "From", "To", "Date") if msg[h])
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body else ""
    if body is not None and body.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
    return head + "\n\n" + text


def extract(path: Path, max_bytes: int = 20_000_000) -> str:
    """Plain text of a document, or '' if it has none we can read."""
    e = path.suffix.lower()
    try:
        if e == ".pdf":
            from pypdf import PdfReader
            r = PdfReader(str(path))
            return "\n\n".join((p.extract_text() or "") for p in r.pages)
        if e == ".docx":
            with zipfile.ZipFile(path) as z:
                return _xml_text(z, ["word/document.xml"])
        if e == ".pptx":
            with zipfile.ZipFile(path) as z:
                slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                                key=lambda n: int(re.findall(r"\d+", n)[-1]))
                return _xml_text(z, slides)
        if e == ".xlsx":  # the cell text; numbers alone say little about what a sheet is
            with zipfile.ZipFile(path) as z:
                return _xml_text(z, ["xl/sharedStrings.xml"]) if "xl/sharedStrings.xml" in z.namelist() else ""
        if e in TEXTUTIL_EXT:
            r = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)], capture_output=True, timeout=60)
            return r.stdout.decode("utf8", "replace")
        if e == ".eml":
            return _mail_text(path)
        if e in SPOTLIGHT_EXT:
            return _spotlight_text(path)
        if e in TEXT_EXT or not e:
            b = path.read_bytes()[:max_bytes]
            if b"\x00" in b[:4000]:
                return ""
            return b.decode("utf8", "replace")
    except Exception:
        return ""
    return ""


def chunk_text(text: str, size: int = 1500, cap: int = 0):
    """(start_line, end_line, text) chunks broken on blank lines near `size` characters; at most `cap` (0 = all)."""
    lines = text.split("\n")
    out, start, n = [], 0, 0
    soft = size * 2 // 5  # a blank line after this much text ends a chunk early
    for i, ln in enumerate(lines):
        n += len(ln) + 1
        if n >= size or (n > soft and not ln.strip()):
            body = "\n".join(lines[start:i + 1])
            while len(body) > size * 2:  # one enormous line (minified, PDF without breaks)
                out.append((start + 1, i + 1, body[:size])); body = body[size:]
            out.append((start + 1, i + 1, body))
            start, n = i + 1, 0
            if cap and len(out) >= cap * 2:
                break
    else:
        if start < len(lines):
            out.append((start + 1, len(lines), "\n".join(lines[start:])))
    out = [c for c in out if c[2].strip()]
    return out[:cap] if cap else out


def secret(name: str, patterns) -> bool:
    from fnmatch import fnmatch
    n = name.lower()
    return any(fnmatch(n, p.lower()) for p in patterns)


def walk(root: Path, exclude: set, exclude_files=()):
    """(path, is_dir) under root: every folder and file, skipping hidden folders, excluded names, secret-looking
    files, and git repos (repos.py indexes those)."""
    for d, dirs, files in os.walk(root):
        if d != str(root) and (".git" in dirs or ".git" in files):
            dirs[:] = []
            continue
        dirs[:] = [x for x in dirs if not x.startswith(".") and x not in exclude
                   and not x.endswith(PACKAGES)]
        files = [f for f in files if not f.startswith(".") and not f.startswith("~$") and not secret(f, exclude_files)]
        yield Path(d), sorted(dirs) + files  # the folder itself, with what it contains
        for f in files:
            yield Path(d) / f, None


def author_of(p: Path, kind: str):
    """(author, source) for a document: embedded metadata (PDF, Office), then Spotlight, then the file's owner."""
    e = p.suffix.lower()
    try:
        if e == ".pdf":
            from pypdf import PdfReader
            a = (PdfReader(str(p)).metadata or {}).get("/Author")
            a = " ".join(str(a or "").split())
            if a:
                return a, "pdf"
        elif e in (".docx", ".pptx", ".xlsx"):
            with zipfile.ZipFile(p) as z:
                core = z.read("docProps/core.xml").decode("utf8", "replace")
            for tag in ("dc:creator", "cp:lastModifiedBy"):
                m = re.search(rf"<{tag}>([^<]+)</{tag}>", core)
                if m and m.group(1).strip():
                    return m.group(1).strip(), "office"
    except Exception:
        pass
    if kind in ("doc", "pdf", "slides", "data"):
        try:
            import subprocess
            r = subprocess.run(["mdls", "-raw", "-name", "kMDItemAuthors", str(p)], capture_output=True, text=True, timeout=5)
            out = r.stdout.strip()
            names = [] if out in ("", "(null)") else (re.findall(r'"([^"]+)"', out) or
                                                    [x.strip() for x in out.strip("()").split(",") if x.strip()])
            if names:
                return ", ".join(names[:3]), "spotlight"
        except Exception:
            pass
    try:
        import pwd
        pw = pwd.getpwuid(p.stat().st_uid)
        return (pw.pw_gecos.split(",")[0] or pw.pw_name), "owner"
    except (KeyError, OSError):
        return None, None


class Files:
    def __init__(self, store):
        from turbovec import IdMapIndex
        self.store, self.db, self.lock = store, store.db, store.lock
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS files(
          path TEXT PRIMARY KEY, root TEXT, name TEXT, ext TEXT, kind TEXT, mtime REAL, size INTEGER,
          n_chunks INTEGER, x REAL, y REAL, z REAL, cluster INTEGER, author TEXT, author_source TEXT);
        CREATE TABLE IF NOT EXISTS file_chunks(
          id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, idx INTEGER, start INTEGER, end INTEGER, text TEXT, vec BLOB);
        CREATE INDEX IF NOT EXISTS fc_path ON file_chunks(path);
        """)
        # chars: the document's full text length (before max_chunks cut it), for the size chart in Settings
        for col in ("author TEXT", "author_source TEXT", "chars INTEGER", "mode TEXT"):
            try:
                self.db.execute(f"ALTER TABLE files ADD COLUMN {col}")
            except Exception:
                pass
        self.index = IdMapIndex.load(str(FILES_INDEX_PATH)) if FILES_INDEX_PATH.exists() else IdMapIndex(dim=config.EMBED_DIM, bit_width=4)
        if reconcile(self.index, self.db, "file_chunks"):
            self.index.sync(str(FILES_INDEX_PATH))
        self._last = 0
        self.progress = {}
        self.sync_lock = threading.RLock()  # one indexing pass at a time: the watcher's, or a search catching up
        self.pending = None   # callable -> set of changed paths (set by the server's file watcher)
        self.requeue = None   # callable(set) to put them back
        self.unsaved = False  # indexed by a search catching up: map layout and saved index still to do
        self._refreshing = False
        self._sweeping = False  # a full sync holds sync_lock

    # ---- indexing ----------------------------------------------------------
    def sync(self, log=lambda m: print(m, flush=True), force=False, on_progress=None) -> int:
        """Index new/changed files in the configured folders and drop ones that are gone. Returns files touched."""
        with self.sync_lock:
            self._sweeping = True
            try:
                return self._sync(log, force, on_progress)
            finally:
                self._sweeping = False

    def refresh(self, paths=(), wait=1.5):
        """Index `paths` and the file watcher's queue now, even files saved a moment ago. Returns the files
        changed, or None when a sweep holds the index (the paths go back in the queue): a search doesn't wait
        for a sweep."""
        paths = set(paths) | (self.pending() if self.pending else set())
        if not paths:
            if self._refreshing and self.sync_lock.acquire(timeout=wait):  # the watcher is indexing an edit now
                self.sync_lock.release()
            return 0
        if self._sweeping or not self.sync_lock.acquire(timeout=wait):
            if self.requeue:
                self.requeue(paths)
            return None
        try:
            n, _ = self.sync_paths(paths, settle=0, layout=False)
            self.unsaved |= bool(n)
            return n
        finally:
            self.sync_lock.release()

    def save(self):
        """After `refresh`: place the new chunks on the map and write the vector index to disk."""
        self.unsaved = False
        self.reproject()
        with self.lock:
            self.index.sync(str(FILES_INDEX_PATH))

    def _sync(self, log, force, on_progress):
        if not force and time.time() - self._last < 30:
            return 0
        self._last = time.time()
        s = settings.load()
        apps.refresh(settings.enabled)
        roots = self.roots()
        exclude, max_bytes = set(s["exclude"]), s["max_file_mb"] * 1_000_000
        with self.lock:
            known = {r[0]: (r[1], r[2]) for r in self.db.execute("SELECT path, mtime, size FROM files")}
            modes = dict(self.db.execute("SELECT path, mode FROM files"))
        stale = self.store.meta("files_v") != FILES_VERSION
        seen, touched, name_only = set(), 0, Counter()
        levels = {}  # extension -> content | name | off
        for root in roots:
            for p, children in walk(root, exclude, s["exclude_files"]):
                if children is not None:  # a folder: indexed by its name, place and what's inside it
                    sp = str(p)
                    seen.add(sp)
                    try:
                        mt = p.stat().st_mtime
                    except OSError:
                        continue
                    if stale or known.get(sp) != (mt, len(children)):
                        try:
                            self._index_folder(p, root, mt, children)
                            touched += 1
                        except Exception as e:
                            log(f"skip folder {p}: {e!r}")
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                k = kind_of(p)
                ext = p.suffix.lower()
                lv = levels[ext] if ext in levels else levels.setdefault(ext, level_of(p))
                if lv == "off":
                    continue  # unseen: an earlier entry for it is dropped below
                if lv == "name":
                    name_only[root] += 1
                    if name_only[root] > NAME_ONLY_MAX:
                        continue
                sp = str(p)
                seen.add(sp)
                same = known.get(sp) == (st.st_mtime, st.st_size) and modes.get(sp) == mode_of(lv)
                if (not stale and same) or time.time() - st.st_mtime < config.SETTLE_SECONDS:
                    continue
                try:
                    self._index_file(p, root, st, k, max_bytes, lv)
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
        self.backfill_authors()
        gone = [p for p in known if p not in seen]
        for p in gone:
            self._drop(p)
        if gone:
            with self.lock:
                self.db.commit()
        if touched or gone or self.unsaved:
            self.save()
        return touched + len(gone)

    @staticmethod
    def roots():
        """Folders to index: the enabled folders plus enabled apps' content; nested roots fold into their parent."""
        cand = [p.resolve() for f, p in zip(settings.get("folders"), settings.paths("folders"))
                if settings.enabled(f"files/{f}") and p.is_dir()]
        cand += [p.resolve() for p in apps.roots(settings.enabled)]
        return [p for p in dict.fromkeys(cand) if not any(p != q and p.is_relative_to(q) for q in cand)]

    def sync_paths(self, paths, log=lambda m: print(m, flush=True), settle=config.SETTLE_SECONDS, layout=True):
        """Index or drop just these paths (from the file watcher). Returns (changed count, paths to retry later
        because they were changed less than `settle` seconds ago and can still be in the middle of a write)."""
        with self.sync_lock:
            self._refreshing = True
            try:
                return self._sync_paths(paths, log, settle, layout)
            finally:
                self._refreshing = False

    def _sync_paths(self, paths, log, settle, layout):
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
            if time.time() - st.st_mtime < settle:
                retry.append(sp)
                continue
            lv = level_of(p)
            with self.lock:
                row = self.db.execute("SELECT mtime, size, mode FROM files WHERE path=?", (sp,)).fetchone()
            if lv == "off":
                if row:
                    self._drop(sp)
                    changed += 1
                continue
            if row == (st.st_mtime, st.st_size, mode_of(lv)):
                continue
            try:
                self._index_file(p, root, st, kind_of(p), max_bytes, lv)
                changed += 1
                log(f"file updated: {p.name}")
            except Exception as e:
                log(f"skip {p}: {e!r}")
        if changed:
            with self.lock:
                self.db.commit()
            if layout:
                self.save()
        return changed, retry

    @staticmethod
    def _eligible(p: Path, root: Path, exclude, exclude_files):
        rel = p.relative_to(root).parts
        if any(x.startswith(".") or x in exclude or x.endswith(PACKAGES)
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

    def _index_folder(self, p: Path, root: Path, mtime, children):
        rel = p.relative_to(root.parent)
        text = f"{p.name}\nFolder {rel}\nContains: " + ", ".join(children[:80])
        vec = embed([text])
        with self.lock:
            self._drop(str(p))
            cur = self.db.execute("INSERT INTO file_chunks(path, idx, start, end, text, vec) VALUES(?,?,?,?,?,?)",
                                  (str(p), 0, 0, 0, text, vec[0].astype(np.float16).tobytes()))
            self.index.add_with_ids(vec, np.array([cur.lastrowid], dtype=np.uint64))
            self.db.execute("INSERT OR REPLACE INTO files(path, root, name, ext, kind, mtime, size, n_chunks) VALUES(?,?,?,?,?,?,?,?)",
                            (str(p), str(root), p.name, "", "folder", mtime, len(children), 1))
            self.db.commit()

    def backfill_authors(self, limit=400):
        """Fill in who wrote documents (a bounded batch per sweep, so the first run doesn't stall indexing)."""
        with self.lock:
            todo = self.db.execute("SELECT path, kind FROM files WHERE author_source IS NULL AND kind!='folder' LIMIT ?", (limit,)).fetchall()
        rows = []
        for path, kind in todo:
            a, src = author_of(Path(path), kind)
            rows.append((a, src or "none", path))
        if rows:
            with self.lock:
                self.db.executemany("UPDATE files SET author=?, author_source=? WHERE path=?", rows)
                self.db.commit()

    def _index_file(self, p: Path, root: Path, st, kind, max_bytes, level="content"):
        rel = p.relative_to(root.parent) if root.parent != p else p
        head = f"{rel}\n"
        text = extract(p, max_bytes) if level == "content" and st.st_size <= max_bytes else ""
        chunks = chunk_text(text, settings.get("chunk_chars"), settings.get("max_chunks")) if text.strip() else []
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
            self.db.execute("INSERT OR REPLACE INTO files(path, root, name, ext, kind, mtime, size, n_chunks, chars, mode) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (str(p), str(root), p.name, p.suffix.lower().lstrip("."), kind, st.st_mtime, st.st_size,
                             len(chunks), len(text) if text.strip() else None, mode_of(level)))
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

    @staticmethod
    def map_sig() -> str:
        """Changes whenever a map switch changes, so layouts built under other switches are redone."""
        import json
        s = settings.load()
        return json.dumps([sorted((t, settings.file_type(t)["map"], settings.file_type(t)["index"]) for t in TYPES),
                           sorted(s["off_map"])])

    def map_filter(self):
        """(path, root) -> whether the file is a map point: its type has content and is switched onto the map, and
        its folder (or app) is not in `off_map`. Folders and name-only files are searchable but never points."""
        shown = {t: settings.file_type(t)["map"] and settings.file_type(t)["index"] == "content"
                 and t not in ("media", "other") for t in TYPES}
        folders = {str(p.resolve()): f"files/{f}" for f, p in zip(settings.get("folders"), settings.paths("folders"))}
        for k, a in apps.APPS.items():
            try:
                folders.update({str(Path(r).resolve()): f"apps/{k}" for r in (a["roots"]() if a["available"]() else [])})
            except Exception:
                pass
        src = {}

        def ok(path, root):
            if not shown[type_of(path)]:
                return False
            if root not in src:
                src[root] = settings.on_map(folders.get(root, "files"))
            return src[root]
        return ok

    # ---- layout ------------------------------------------------------------
    def centroids(self):
        with self.lock:
            rows = self.db.execute("SELECT c.path, f.root, c.vec FROM file_chunks c JOIN files f ON f.path=c.path "
                                   "WHERE f.kind!='folder'").fetchall()
        ok = self.map_filter()
        by = {}
        for p, root, v in rows:
            if ok(p, root):
                by.setdefault(p, []).append(np.frombuffer(v, np.float16).astype(np.float32))
        paths = list(by)
        return paths, (unit(np.stack([np.mean(by[p], axis=0) for p in paths])) if paths else np.zeros((0, config.EMBED_DIM)))

    def needs_layout(self):
        if self.store.meta("files_map") != self.map_sig():
            return True
        ok = self.map_filter()
        with self.lock:
            rows = self.db.execute("SELECT path, root FROM files WHERE x IS NULL AND kind!='folder'").fetchall()
        return any(ok(p, r) for p, r in rows)

    def reproject(self, force=False):
        """One point per file. Full UMAP when the set grew meaningfully; otherwise new files go next to neighbours."""
        sig = self.map_sig()
        if self.store.meta("files_map") != sig:  # files left the map or joined it: drop old positions, fit again
            force = True
            with self.lock:
                self.db.execute("UPDATE files SET x=NULL, y=NULL, z=NULL")
                self.store.meta("files_map", sig)
                self.db.commit()
        paths, C = self.centroids()
        if len(paths) < 4:
            return False
        with self.lock:
            placed = {r[0]: r[1:] for r in self.db.execute("SELECT path, x, y, z, cluster FROM files WHERE x IS NOT NULL")}
            fit_n = int(self.store.meta("files_fit_n") or 0)
            ok = self.store.meta("files_layout") == LAYOUT_VERSION
        n = len(paths)
        if force or not ok or len(placed) < 0.5 * n or abs(n - fit_n) > max(60, 0.15 * fit_n):
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
        near = nearest(C[missing], C[known])
        with self.lock:
            for j, i in enumerate(missing):
                self.db.execute("UPDATE files SET x=?, y=?, z=?, cluster=? WHERE path=?",
                                (*map(float, Pn[j]), placed[paths[known[near[j]]]][3], paths[i]))
            self.db.commit()
        return True

    # ---- queries -----------------------------------------------------------
    def points(self):
        with self.lock:
            rows = self.db.execute("SELECT path, root, name, ext, kind, mtime, size, n_chunks, x, y, z, cluster, author, author_source "
                                   "FROM files WHERE x IS NOT NULL AND kind!='folder'").fetchall()
        home = str(Path.home())
        out = []
        ok = self.map_filter()
        for r in rows:
            if not ok(r[0], r[1]):
                continue
            root = Path(r[1])
            rel = os.path.relpath(r[0], root.parent)
            out.append(dict(id=f"file:{r[0]}", path=r[0], rel=rel, folder=os.path.dirname(rel), root=root.name,
                            name=r[2], ext=r[3], kind=r[4], mtime=r[5], size=r[6], n=r[7], p=r[8:11], cluster=r[11],
                            author=r[12], author_source=r[13] if r[13] != "none" else None,
                            display=r[0].replace(home, "~", 1)))
        return out

    def file(self, path):
        with self.lock:
            r = self.db.execute("SELECT path, root, name, ext, kind, mtime, size, n_chunks, author, author_source FROM files WHERE path=?", (path,)).fetchone()
            if not r:
                return None
            chunks = self.db.execute("SELECT idx, start, end, text FROM file_chunks WHERE path=? ORDER BY idx", (path,)).fetchall()
        return dict(id=f"file:{r[0]}", path=r[0], root=r[1], name=r[2], ext=r[3], kind=r[4], mtime=r[5], size=r[6],
                    n=r[7], display=r[0].replace(str(Path.home()), "~", 1), author=r[8],
                    author_source=r[9] if r[9] != "none" else None, is_dir=r[4] == "folder",
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

    SIZE_BINS = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000]  # upper edges, in default 1500-char chunks

    def types(self):
        """Each file type with its settings, how many files it has, and how long their text is: counts per size bin
        (bin i holds texts up to SIZE_BINS[i] chunks of 1500 characters; the last bin holds the rest)."""
        with self.lock:
            rows = self.db.execute("SELECT path, chars FROM files WHERE kind!='folder'").fetchall()
        size = settings.get("chunk_chars")
        out = {t: dict(id=t, label=label, description=desc, exts=sorted(e.lstrip(".") for e in exts)[:12],
                       readable=t not in ("media", "other"), **settings.file_type(t), files=0, read=0,
                       bins=[0] * (len(self.SIZE_BINS) + 1), cut=0)
               for t, (label, desc, exts) in TYPES.items()}
        cap = settings.get("max_chunks")
        for path, chars in rows:
            o = out[type_of(path)]
            o["files"] += 1
            if not chars:
                continue
            o["read"] += 1
            n = chars / 1500
            o["bins"][next((i for i, b in enumerate(self.SIZE_BINS) if n <= b), len(self.SIZE_BINS))] += 1
            if cap and chars > cap * size:
                o["cut"] += 1
        return dict(types=list(out.values()), bin_edges=self.SIZE_BINS, unit_chars=1500,
                    chunk_chars=size, max_chunks=cap)

    def stats(self):
        with self.lock:
            return dict(self.db.execute("SELECT kind, COUNT(*) FROM files GROUP BY kind").fetchall())
