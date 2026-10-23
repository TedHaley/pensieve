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
INDEX = ["v2/index.db", "v2/index.db-wal", "v2/index.db-shm", "v2/embed_model", "v2/vectors/docs.tv",
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
    config.use_model(config._index_model())  # the new index's model: the one chosen in Settings, else the default
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


def settle_version() -> str | None:
    """Before anything opens the database: when this build's version is not the data's, delete the index and the
    thread history, keep the settings, and stamp the new version. Returns the old version ('new' for none) or None."""
    now = current_version()
    had = VERSION.read_text().strip() if VERSION.exists() else None
    if had == now:
        return None
    store = config.DATA_DIR / "v2" / "store.db"
    if store.exists():
        for f in INDEX:
            (config.DATA_DIR / f).unlink(missing_ok=True)
        shutil.rmtree(config.DATA_DIR / EXPORTS, ignore_errors=True)
        import sqlite3
        con = sqlite3.connect(store)
        for t in THREAD_TABLES:
            con.execute(f"DROP TABLE IF EXISTS {t}")
        con.commit()
        con.close()
    VERSION.write_text(now)
    return had or "new"


def restart():
    """Replace this process with a fresh `pensieve` run (same arguments, same pid). Open sockets close on exec."""
    sys.stdout.flush()
    sys.stderr.flush()
    os.environ["PENSIEVE_RESTARTED"] = "1"  # don't open another browser tab
    os.execv(sys.executable, [sys.executable, "-m", "pensieve.cli", *sys.argv[1:]])
