"""A local log of every MCP tool call: which agent made it, from where, with what arguments, how long it took, and what
it returned (for a search, the result ids in order). It lives in its own database, ~/.pensieve/usage.db, so resetting
the index keeps the history. Nothing leaves the Mac.

Calls are written by a background thread: logging never adds to a tool call's time, and a failure to log is dropped
rather than failing the call."""
import contextvars
import json
import queue
import sqlite3
import threading
import time

from pensieve import config

PATH = config.DATA_DIR / "usage.db"
# The call being handled, so a tool can add what only it knows (search: result ids, per-phase times).
current = contextvars.ContextVar("usage_call", default=None)
_q = queue.Queue(maxsize=10000)
_started = False
_lock = threading.Lock()


def _db():
    db = sqlite3.connect(PATH, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS calls(
            id INTEGER PRIMARY KEY, ts REAL, source TEXT, tool TEXT, client TEXT, conn TEXT, cwd TEXT, scope TEXT,
            args TEXT, ms REAL, ok INTEGER, error TEXT, n_results INTEGER, results TEXT, laps TEXT, out_chars INTEGER);
        CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts);
        CREATE INDEX IF NOT EXISTS calls_tool ON calls(tool, ts);
    """)
    return db


def _writer():
    db = _db()
    while True:
        rows = [_q.get()]
        while not _q.empty() and len(rows) < 200:
            rows.append(_q.get_nowait())
        try:
            db.executemany("INSERT INTO calls(ts, source, tool, client, conn, cwd, scope, args, ms, ok, error, n_results, "
                           "results, laps, out_chars) VALUES (:ts, :source, :tool, :client, :conn, :cwd, :scope, :args, "
                           ":ms, :ok, :error, :n_results, :results, :laps, :out_chars)", rows)
            db.commit()
        except sqlite3.Error:
            pass


def record(**row):
    global _started
    if not _started:
        with _lock:
            if not _started:
                threading.Thread(target=_writer, daemon=True, name="usage-log").start()
                _started = True
    for k in ("args", "results", "laps"):
        if row.get(k) is not None and not isinstance(row[k], str):
            row[k] = json.dumps(row[k])
    row = {k: row.get(k) for k in ("ts", "source", "tool", "client", "conn", "cwd", "scope", "args", "ms", "ok", "error",
                                   "n_results", "results", "laps", "out_chars")}
    try:
        _q.put_nowait(row)
    except queue.Full:
        pass


def note(**fields):
    """Add details to the call being logged (no-op outside one)."""
    c = current.get()
    if c is not None:
        c.update(fields)


def summary(days: float = 7) -> dict:
    """Calls per tool over the last `days`: count, errors, p50/p95 milliseconds."""
    since = time.time() - days * 86400
    db = _db()
    out = {}
    for tool, ms, ok in db.execute("SELECT tool, ms, ok FROM calls WHERE ts >= ? ORDER BY ms", (since,)):
        d = out.setdefault(tool, {"calls": 0, "errors": 0, "ms": []})
        d["calls"] += 1
        d["errors"] += not ok
        d["ms"].append(ms)
    for d in out.values():
        ms = d.pop("ms")
        d["p50_ms"] = round(ms[len(ms) // 2], 1)
        d["p95_ms"] = round(ms[min(len(ms) - 1, int(len(ms) * 0.95))], 1)
    return {"days": days, "tools": out}
