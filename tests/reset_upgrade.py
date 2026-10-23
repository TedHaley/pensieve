"""Tests of the reset on an upgrade (pensieve/reset.py settle_version): a new version deletes the index and what the
threads derived, keeps the settings, and runs the setup again for data from before SETUP_SINCE.
Run: python tests/reset_upgrade.py"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

DATA = Path(tempfile.mkdtemp())
os.environ["PENSIEVE_DATA"] = str(DATA)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import reset  # noqa: E402


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))
    return ok


def data(version: str):
    (DATA / "v2" / "vectors").mkdir(parents=True, exist_ok=True)
    (DATA / "threads" / "facts").mkdir(parents=True, exist_ok=True)
    (DATA / "threads" / "facts" / "a.json").write_text("{}")
    (DATA / "v2" / "index.db").write_text("x")
    (DATA / "sink_status.json").write_text("{}")
    sqlite3.connect(DATA / "v2" / "store.db").execute("CREATE TABLE IF NOT EXISTS threads (id)").connection.commit()
    (DATA / "settings.json").write_text(json.dumps({"setup_done": True, "mode": "developer", "repos": ["~/code/app"]}))
    reset.VERSION.write_text(version)


def run():
    results = []
    reset.current_version = lambda: "0.16.0"
    data("0.15.0")
    results.append(check("an upgrade returns the old version", reset.settle_version(), "0.15.0"))
    s = json.loads((DATA / "settings.json").read_text())
    results.append(check("the index is gone", (DATA / "v2" / "index.db").exists(), False))
    results.append(check("the threads' transcript facts are gone", (DATA / "threads" / "facts").exists(), False))
    results.append(check("the gh status cache is gone", (DATA / "sink_status.json").exists(), False))
    tables = {r[0] for r in sqlite3.connect(DATA / "v2" / "store.db").execute("SELECT name FROM sqlite_master")}
    results.append(check("the thread history tables are gone", "threads" in tables, False))
    results.append(check("data from before 0.16 runs the setup again", s["setup_done"], False))
    results.append(check("the settings stay", (s["mode"], s["repos"]), ("developer", ["~/code/app"])))
    results.append(check("the same version again changes nothing", reset.settle_version(), None))

    reset.current_version = lambda: "0.16.1"
    data("0.16.0")
    reset.settle_version()
    results.append(check("an upgrade after 0.16 keeps the setup done",
                         json.loads((DATA / "settings.json").read_text())["setup_done"], True))
    results.append(check("and still resets the index", (DATA / "v2" / "index.db").exists(), False))

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
