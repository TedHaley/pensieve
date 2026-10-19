"""A vector for each distinct text that was embedded, so that the same text is never embedded twice.

The key is the sha1 of the exact text that the model reads (after `indexer.model_texts`: the title and the format of
the model are part of it) and the model's name. `indexer.embed` looks here before it embeds and stores what it embeds.
Vectors are float16, as in the chunk tables.

v1 keeps the table in its own file (`embed_cache.db`), with its own connection: the indexing threads hold write
transactions on `pensieve.db` while they embed, and a second writer to that file would wait for them. In v2 the table
is in `index.db` (see docs/design/storage.md); `EmbedCache(path)` takes that path.

`fill_from_v1` reads a v1 database (read-only) and stores the vector of each text chunk in it, for the migration to v2.
"""
import hashlib
import sqlite3
import threading
from pathlib import Path

import numpy as np

from pensieve import config

PATH = config.DATA_DIR / "embed_cache.db"
SCHEMA = """CREATE TABLE IF NOT EXISTS embed_cache (
  hash   TEXT NOT NULL,
  model  TEXT NOT NULL,
  vec    BLOB NOT NULL,
  PRIMARY KEY (hash, model)
) STRICT"""
PART = 500  # keys per SELECT: below SQLite's limit on bound parameters


def key(text: str) -> str:
    return hashlib.sha1(text.encode("utf8", "surrogatepass")).hexdigest()


class EmbedCache:
    def __init__(self, path=PATH):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute(SCHEMA)
        self.db.commit()
        self.hits = self.misses = 0  # texts found and not found by `indexer.embed`, since start

    def get(self, model: str, hashes) -> dict:
        """hash -> float32 vector, for the hashes that are in the cache."""
        hashes = list(dict.fromkeys(hashes))
        out = {}
        with self._lock:
            for i in range(0, len(hashes), PART):
                part = hashes[i:i + PART]
                for h, v in self.db.execute(
                        f"SELECT hash, vec FROM embed_cache WHERE model=? AND hash IN ({','.join('?' * len(part))})",
                        (model, *part)):
                    out[h] = np.frombuffer(v, np.float16).astype(np.float32)
        return out

    def put(self, model: str, items):
        """Store (hash, vector) pairs. A hash that is there already keeps its vector."""
        rows = [(h, model, np.asarray(v).astype(np.float16).tobytes()) for h, v in items]
        if not rows:
            return
        with self._lock:
            self.db.executemany("INSERT OR IGNORE INTO embed_cache(hash, model, vec) VALUES(?,?,?)", rows)
            self.db.commit()

    def count(self, model=None) -> int:
        with self._lock:
            if model:
                return self.db.execute("SELECT COUNT(*) FROM embed_cache WHERE model=?", (model,)).fetchone()[0]
            return self.db.execute("SELECT COUNT(*) FROM embed_cache").fetchone()[0]

    def close(self):
        with self._lock:
            self.db.close()


_cache = None
_cache_lock = threading.Lock()


def get() -> EmbedCache | None:
    """The process's cache, opened at the first use. None when it can't be opened: embedding works without it."""
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                try:
                    _cache = EmbedCache()
                except sqlite3.Error as e:
                    print(f"embed cache {PATH}: not opened ({e!r})", flush=True)
                    _cache = False
    return _cache or None


def _v1_model(db_path: Path) -> str:
    """The model of a v1 index: the name in `embed_model` next to its database, Qwen for an index from before models
    could be chosen (as `config._index_model` reads it)."""
    try:
        name = (db_path.parent / "embed_model").read_text().strip()
        if name in config.EMBED_MODELS:
            return name
    except OSError:
        pass
    return config.QWEN


def _v1_rows(db):
    """(text, title, stored vector) of each v1 chunk whose vector was made from its text. Titles are what indexing
    passed to `embed` (as `reembed._rows` reads them). Left out: chunks whose vector came from an image, a video or a
    sound, because their text only names the file."""
    from pensieve import content
    from pensieve.files import READ_IMAGE_EXT, type_of
    for text, v in db.execute("SELECT text, vec FROM chunks WHERE vec IS NOT NULL"):
        yield text or "", None, v
    for text, path, v in db.execute("SELECT text, path, vec FROM code_chunks WHERE vec IS NOT NULL"):
        if Path(path).suffix.lower() in READ_IMAGE_EXT:
            continue
        yield text or "", path, v
    for text, path, root, kind, v in db.execute(
            "SELECT c.text, c.path, f.root, f.kind, c.vec FROM file_chunks c LEFT JOIN files f ON f.path=c.path "
            "WHERE c.vec IS NOT NULL"):
        title = None
        if kind != "folder" and root:
            if type_of(path) in content.KINDS:
                continue
            p, r = Path(path), Path(root)
            title = str(p.relative_to(r.parent)) if r.parent != p and p.is_relative_to(r.parent) else str(p)
        yield text or "", title, v


def fill_from_v1(v1_db_path, cache: EmbedCache | None = None, model: str | None = None, batch=2000) -> int:
    """Store the vectors of a v1 database (`code_chunks`, `file_chunks`, `chunks`) in the cache, by the hash of the
    text that the model read for each chunk. Opens the v1 database read-only. Returns the vectors read."""
    from pensieve.indexer import model_texts
    v1_db_path = Path(v1_db_path)
    model = model or _v1_model(v1_db_path)
    cache = cache or get()
    dim = config.EMBED_MODELS[model]["dim"]
    db = sqlite3.connect(f"file:{v1_db_path}?mode=ro", uri=True)
    n, todo = 0, []

    def flush():
        texts = model_texts([t for t, _, _ in todo], titles=[ti for _, ti, _ in todo], name=model)
        cache.put(model, [(key(m), np.frombuffer(v, np.float16)) for m, (_, _, v) in zip(texts, todo)])
        todo.clear()
    try:
        for text, title, v in _v1_rows(db):
            if len(v) != 2 * dim:  # a vector of another model (a switch that did not finish)
                continue
            todo.append((text, title, v))
            n += 1
            if len(todo) >= batch:
                flush()
        if todo:
            flush()
    finally:
        db.close()
    return n
