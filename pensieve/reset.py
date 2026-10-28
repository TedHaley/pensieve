"""Reset Pensieve to a fresh install: delete the index (and optionally the settings) and rebuild from scratch.

The running server can't safely delete a database its threads have open, so a reset is two steps: `request()` leaves
a note and the server restarts itself in place (same process id, so the Mac app keeps tracking it); on the way up,
`apply_pending()` runs before anything opens the database.
"""
import json
import os
import shutil
import sys

from pensieve import config

MARK = config.DATA_DIR / "reset-pending"
INDEX = ["v2/index.db", "v2/index.db-wal", "v2/index.db-shm", "v2/vectors/docs.tv",
         "v2/vectors/sessions.tv", "v2/vectors/files.tv", "v2/vectors/code.tv"]  # v2: store.db stays
EXPORTS = "apps"  # copies of app content made for indexing


def index_bytes() -> int:
    total = sum((config.DATA_DIR / f).stat().st_size for f in INDEX if (config.DATA_DIR / f).exists())
    exp = config.DATA_DIR / EXPORTS
    if exp.is_dir():
        total += sum(p.stat().st_size for p in exp.rglob("*") if p.is_file())
    return total


def request(settings_too: bool):
    MARK.write_text("all" if settings_too else "index")


def apply_pending() -> str | None:
    """Delete what a requested reset covers. Returns 'index' or 'all' if a reset ran."""
    if not MARK.exists():
        return None
    what = MARK.read_text().strip() or "index"
    for f in INDEX:
        (config.DATA_DIR / f).unlink(missing_ok=True)
    shutil.rmtree(config.DATA_DIR / EXPORTS, ignore_errors=True)
    if what == "all":
        (config.DATA_DIR / "settings.json").unlink(missing_ok=True)
    else:  # the sources are asked again before anything is re-indexed
        f = config.DATA_DIR / "settings.json"
        try:
            s = json.loads(f.read_text())
            f.write_text(json.dumps({**s, "setup_done": False}, indent=2))
        except (OSError, ValueError):
            pass
    MARK.unlink(missing_ok=True)
    return what


VERSION = config.DATA_DIR / "data-version"  # the version whose index and threads these are
THREAD_TABLES = ["thread_snapshot_reasons", "thread_snapshots", "thread_events", "thread_cards", "thread_members",
                 "thread_repos", "threads"]  # children first: the saved versions of threads are not kept


def current_version() -> str:
    """The version of this build: the source tree's pyproject.toml when run from the repo, else the installed one."""
    import re
    from pathlib import Path
    project = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if project.exists():
        m = re.search(r'^version\s*=\s*"([^"]+)"', project.read_text(), re.M)
        if m:
            return m.group(1)
    from importlib.metadata import version
    return version("pensieve")


SETUP_SINCE = "0.16.0"  # data from before it runs the setup again: modes and the issue tracker step came in it
DERIVED = ["threads/facts", "sink_status.json"]  # what the threads read from transcripts and gh, made again on start


def _older(a: str, b: str) -> bool:
    num = lambda v: tuple(int(x) if x.isdigit() else 0 for x in v.split("-")[0].split("."))
    return num(a) < num(b)


def _schema_version(path) -> int | None:
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT version FROM schema_version").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def settle_schema() -> str | None:
    """Before anything opens the database: a database of another schema version is not migrated. Its store.db
    holds the schema of another version: everything in v2/ (store, index, vectors, connection state) and what the
    threads derived goes, and Pensieve builds it again from the sources. An index.db of another version: the index
    goes. The settings, sign-ins and Keychain tokens stay. Returns 'store', 'index' or None."""
    from pensieve.store import schema
    v2 = config.DATA_DIR / "v2"
    if (v2 / "store.db").exists() and _schema_version(v2 / "store.db") != schema.STORE_VERSION:
        shutil.rmtree(v2, ignore_errors=True)
        shutil.rmtree(config.DATA_DIR / EXPORTS, ignore_errors=True)
        for d in DERIVED:
            p = config.DATA_DIR / d
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
        return "store"
    if (v2 / "index.db").exists() and _schema_version(v2 / "index.db") != schema.INDEX_VERSION:
        for f in INDEX:
            (config.DATA_DIR / f).unlink(missing_ok=True)
        return "index"
    return None


VECTOR_TABLES = ("chunks", "code_chunks", "file_chunks", "doc_chunks")  # the tables of index.db that hold vectors


def _vector_sizes(path) -> set[int]:
    """The byte sizes of the vectors in an index.db."""
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            have = {n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {n for t in VECTOR_TABLES if t in have
                    for (n,) in con.execute(f"SELECT DISTINCT length(vec) FROM {t} WHERE vec IS NOT NULL")}
        finally:
            con.close()
    except sqlite3.Error:
        return set()


def settle_embeddings() -> bool:
    """Before anything opens the database: an index with a vector of another model than EmbeddingGemma 2 (not
    EMBED_DIM float16 values) goes, and Pensieve indexes again with EmbeddingGemma 2. The sources and settings stay.
    Returns whether the index went."""
    (config.V2_DIR / "embed_model").unlink(missing_ok=True)  # older versions recorded the model here
    if not config.DB_PATH.exists() or not _vector_sizes(config.DB_PATH) - {2 * config.EMBED_DIM}:
        return False
    for f in INDEX:
        (config.DATA_DIR / f).unlink(missing_ok=True)
    return True


def settle_version() -> str | None:
    """Before anything opens the database: when this build's version is not the data's, delete the index, the
    thread history and what the threads derived from transcripts, keep the settings, and stamp the new version. Data
    from before SETUP_SINCE also runs the setup again. Returns the old version ('new' for none) or None."""
    settle_schema()
    now = current_version()
    had = VERSION.read_text().strip() if VERSION.exists() else None
    if had == now:
        return None
    store = config.DATA_DIR / "v2" / "store.db"
    if store.exists():
        for f in INDEX:
            (config.DATA_DIR / f).unlink(missing_ok=True)
        shutil.rmtree(config.DATA_DIR / EXPORTS, ignore_errors=True)
        for d in DERIVED:
            p = config.DATA_DIR / d
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
        import sqlite3
        con = sqlite3.connect(store)
        for t in THREAD_TABLES:
            con.execute(f"DROP TABLE IF EXISTS {t}")
        con.commit()
        con.close()
        if had and _older(had, SETUP_SINCE) and not _older(now, SETUP_SINCE):
            f = config.DATA_DIR / "settings.json"
            try:
                s = json.loads(f.read_text())
                f.write_text(json.dumps({**s, "setup_done": False}, indent=2))
            except (OSError, ValueError):
                pass
    VERSION.write_text(now)
    return had or "new"


def restart():
    """Replace this process with a fresh `pensieve` run (same arguments, same pid). Open sockets close on exec."""
    sys.stdout.flush()
    sys.stderr.flush()
    os.environ["PENSIEVE_RESTARTED"] = "1"  # don't open another browser tab
    os.execv(sys.executable, [sys.executable, "-m", "pensieve.cli", *sys.argv[1:]])
