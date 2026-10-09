"""Reset Pensieve to a fresh install: delete the index (and optionally the settings) and rebuild from scratch.

The running server can't safely delete a database its threads have open, so a reset is two steps: `request()` leaves
a note and the server restarts itself in place (same process id, so the Mac app keeps tracking it); on the way up,
`apply_pending()` runs before anything opens the database.
"""
import os
import shutil
import sys

from . import config

MARK = config.DATA_DIR / "reset-pending"
INDEX = ["pensieve.db", "pensieve.db-wal", "pensieve.db-shm", "index.tv", "files.tv", "code.tv"]
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
    MARK.unlink(missing_ok=True)
    return what


def restart():
    """Replace this process with a fresh `pensieve` run (same arguments, same pid). Open sockets close on exec."""
    sys.stdout.flush()
    sys.stderr.flush()
    os.environ["PENSIEVE_RESTARTED"] = "1"  # don't open another browser tab
    os.execv(sys.executable, [sys.executable, "-m", "pensieve.cli", *sys.argv[1:]])
