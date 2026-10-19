"""Storage v2 (docs/design/storage.md): the only package that runs SQL on store.db and index.db.

Layout under config.DATA_DIR / "v2": store.db (collected and user data; a reset keeps it), index.db (derived; a reset
deletes it), vectors/ (turbovec indexes). `store_db()` and `index_db()` open each file once per process.

The modules here are typed repository functions, one module per aggregate: sources, documents, cards, threads. Each
takes the Db as its first argument, reads with `db.read`/`db.query` and writes with `db.write`."""
import threading
from pathlib import Path

from pensieve import config
from pensieve.store import schema
from pensieve.store.db import Db

_lock = threading.Lock()
_open: dict[str, Db] = {}


def root() -> Path:
    return config.DATA_DIR / "v2"


def store_path() -> Path:
    return root() / "store.db"


def index_path() -> Path:
    return root() / "index.db"


def vectors_dir() -> Path:
    return root() / "vectors"


def avatars_dir() -> Path:
    """People's pictures, as files <hash>.<ext>; the `avatars` table of index.db lists them."""
    return root() / "avatars"


def _get(name: str, make) -> Db:
    db = _open.get(name)
    if db is None:
        with _lock:
            db = _open.get(name)
            if db is None:
                db = _open[name] = make()
    return db


def store_db() -> Db:
    """store.db of this process."""
    return _get("store", lambda: Db(store_path(), schema.script(schema.STORE), schema.STORE_VERSION,
                                    schema.STORE_MIGRATIONS))


def index_db() -> Db:
    """index.db of this process."""
    return _get("index", lambda: Db(index_path(), schema.script(schema.INDEX), schema.INDEX_VERSION,
                                    schema.INDEX_MIGRATIONS))


def close_all():
    """Commit and close both files (at exit, and in tests that open another DATA_DIR)."""
    with _lock:
        for db in _open.values():
            db.close()
        _open.clear()
