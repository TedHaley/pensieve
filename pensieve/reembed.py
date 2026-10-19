"""Switch the index to another embedding model without indexing again: the text the index holds (session chunks,
code chunks, document chunks) is embedded with the new model into a second vector column while Pensieve keeps
working with the old one. When every chunk has its new vector, the backend restarts, and before anything loads,
`finish` moves the new vectors into place and deletes the vector index files, which rebuild from the database.
Nothing is chunked, blamed or read from disk again."""
import os
import sys
import time
from pathlib import Path

import numpy as np

from pensieve import config, settings

COLUMN = "vec_next"
TABLES = ("chunks", "code_chunks", "file_chunks")
INDEX_FILES = ("index.tv", "code.tv", "files.tv")
LAYOUT_KEYS = ("layout", "files_layout", "data_layout")  # and code_layout:<repo>: every map refits on new vectors
BATCH = 64

progress = None  # {model, done, total, seconds_left} while new vectors are embedded


def wanted() -> str:
    """The model the user chose; the index's own model when none is chosen."""
    name = settings.get("embed_model") or config.EMBED_MODEL
    return name if name in config.EMBED_MODELS else config.EMBED_MODEL


def _columns(db):
    for t in TABLES:
        try:
            db.execute(f"ALTER TABLE {t} ADD COLUMN {COLUMN} BLOB")
        except Exception:
            pass  # already there


def _rows(db, table, limit):
    """Chunks without a new vector yet: (id, text, title). A title is the path, as indexing embeds it."""
    if table == "chunks":
        return [(i, t, None) for i, t in db.execute(
            f"SELECT id, text FROM chunks WHERE {COLUMN} IS NULL LIMIT ?", (limit,))]
    if table == "code_chunks":
        return db.execute(f"SELECT id, text, path FROM code_chunks WHERE {COLUMN} IS NULL LIMIT ?", (limit,)).fetchall()
    out = []
    for i, text, path, root, kind in db.execute(
            f"SELECT c.id, c.text, c.path, f.root, f.kind FROM file_chunks c LEFT JOIN files f ON f.path=c.path "
            f"WHERE c.{COLUMN} IS NULL LIMIT ?", (limit,)):
        title = None
        if kind != "folder" and root:
            p, r = Path(path), Path(root)
            title = str(p.relative_to(r.parent)) if r.parent != p and p.is_relative_to(r.parent) else str(p)
        out.append((i, text, title))
    return out


def _left(db):
    return sum(db.execute(f"SELECT COUNT(*) FROM {t} WHERE {COLUMN} IS NULL").fetchone()[0] for t in TABLES)


def _fill(db, lock, target, model, stop=lambda: False, on_batch=lambda n: None):
    """Embed every chunk that has no new vector yet, with `model` (name, loaded). False if stopped first."""
    from pensieve.indexer import embed
    for table in TABLES:
        while True:
            if stop():
                return False
            with lock:
                rows = _rows(db, table, BATCH)
            if not rows:
                break
            vecs = embed([r[1] or "" for r in rows], titles=[r[2] for r in rows], model=model)
            with lock:
                db.executemany(f"UPDATE {table} SET {COLUMN}=? WHERE id=?",
                               [(v.astype(np.float16).tobytes(), r[0]) for r, v in zip(rows, vecs)])
                db.commit()
            on_batch(len(rows))
    return True


def _clear(db, lock):
    with lock:
        for t in TABLES:
            db.execute(f"UPDATE {t} SET {COLUMN}=NULL WHERE {COLUMN} IS NOT NULL")
        db.execute("DELETE FROM meta WHERE key IN ('next_model', 'next_ready')")
        db.commit()


def switch_empty(store, corpora) -> bool:
    """Switch at once, without embedding or a restart, while the index holds no vectors (a first setup).
    `corpora`: the objects that hold a vector index (Store, Repos, Files)."""
    target = wanted()
    if target == config.EMBED_MODEL:
        return False
    with store.lock:
        if any(store.db.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() for t in TABLES):
            return False
        from pensieve import vectors
        config.use_model(target)
        for c in corpora:
            c.index = vectors.Index(c.index.path, config.EMBED_DIM, fresh=True)
        store.db.execute("DELETE FROM meta WHERE key IN ('next_model', 'next_ready')")
        store.db.commit()
    return True


def run(store, notify=lambda: None):
    """In a background thread: embed everything with the chosen model, then restart the backend to switch.
    Returns early when the choice goes back to the index's model."""
    global progress
    target = wanted()
    db, lock = store.db, store.lock
    with lock:
        _columns(db)
        db.commit()
    if store.meta("next_model") not in (None, target):  # another model's vectors, from an earlier choice
        _clear(db, lock)
    if target == config.EMBED_MODEL:
        if store.meta("next_model"):
            _clear(db, lock)
        return False
    with lock:
        store.meta("next_model", target)
        db.commit()
        total = sum(db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES)
        done = total - _left(db)
    from pensieve.indexer import load_embedder
    model = (target, load_embedder(target))
    t0, start, batches = time.time(), done, 0
    progress = dict(model=target, done=done, total=total, seconds_left=None)

    def step(n):
        nonlocal done, total, batches
        done, batches = done + n, batches + 1
        if batches % 20 == 0:  # indexing goes on meanwhile: what it adds needs new vectors too
            with lock:
                total = done + _left(db)
        total = max(total, done)
        rate = (done - start) / max(time.time() - t0, 1e-3)
        progress.update(done=done, total=total, seconds_left=round((total - done) / rate) if rate else None)
        notify()

    finished = _fill(db, lock, target, model, stop=lambda: wanted() != target, on_batch=step)
    progress = None
    if not finished:
        if wanted() == config.EMBED_MODEL:
            _clear(db, lock)
        notify()
        return False
    with lock:
        store.meta("next_ready", target)  # `finish` swaps only then; what is indexed until the restart is little
        db.commit()
    restart()
    return True


def restart():
    """Start this backend again in the same process (same pid, so the Mac app keeps it); `finish` runs first."""
    sys.stdout.flush()
    sys.stderr.flush()
    os.environ["PENSIEVE_RESTARTED"] = "1"  # don't open another browser tab
    os.execv(sys.executable, [sys.executable, "-m", "pensieve.cli", *sys.argv[1:]])


def finish(log=lambda m: print(m, flush=True)):
    """At backend start, before the index loads: when the new vectors are ready for the chosen model, embed what was
    added since, move them into place and switch. Then the vector indexes and the maps rebuild from them."""
    import sqlite3
    if not config.DB_PATH.exists():
        return False
    db = sqlite3.connect(config.DB_PATH)
    try:
        row = db.execute("SELECT value FROM meta WHERE key='next_ready'").fetchone()
    except sqlite3.OperationalError:
        return False
    target = row[0] if row else None
    if not target or target not in config.EMBED_MODELS or target != wanted() or target == config.EMBED_MODEL:
        return False  # an unfinished switch goes on in the background (run)
    import threading
    from pensieve.indexer import load_embedder
    lock = threading.Lock()
    if _left(db):  # chunks indexed after the job finished
        log(f"embedding model: {_left(db)} chunks still to embed with {target}")
        _fill(db, lock, target, (target, load_embedder(target)))
    log(f"embedding model: switching the index to {target}")
    for t in TABLES:
        db.execute(f"UPDATE {t} SET vec={COLUMN}, {COLUMN}=NULL")
    db.execute("DELETE FROM meta WHERE key IN ('next_model', 'next_ready')")
    db.execute(f"DELETE FROM meta WHERE key IN ({','.join('?' * len(LAYOUT_KEYS))}) OR key LIKE 'code_layout:%'",
               LAYOUT_KEYS)
    db.commit()
    db.close()
    for f in INDEX_FILES:
        (config.DATA_DIR / f).unlink(missing_ok=True)
    config.use_model(target)
    return True
