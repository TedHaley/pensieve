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


def _pct(ms, p):
    return round(ms[min(len(ms) - 1, int(len(ms) * p))], 1) if ms else None


def _query(args):
    """The text an agent asked for, from a call's arguments (search query, who_knows topic, read id...)."""
    try:
        a = json.loads(args or "{}")
    except ValueError:
        return ""
    for k in ("query", "topic", "q", "id", "path", "repo", "0"):
        if a.get(k):
            return str(a[k])[:200]
    return ""


def summary(days: float = 7, recent: int = 0) -> dict:
    """MCP tool calls over the last `days`: per tool (count, errors, p50/p95 ms), per agent, per day, and with
    `recent`, the latest calls with what they asked for and the top results."""
    db = _db()
    try:
        return _summary(db, time.time() - days * 86400, days, recent)
    finally:
        db.close()


def _summary(db, since, days, recent):
    tools, clients, per_day, conns, all_ms = {}, {}, {}, set(), []
    for tool, ms, ok, client, conn, n, ts in db.execute(
            "SELECT tool, ms, ok, client, conn, n_results, ts FROM calls WHERE ts >= ? ORDER BY ms", (since,)):
        d = tools.setdefault(tool, {"calls": 0, "errors": 0, "empty": 0, "ms": []})
        d["calls"] += 1
        d["errors"] += not ok
        d["empty"] += n == 0
        d["ms"].append(ms)
        all_ms.append(ms)
        clients[client or "unknown"] = clients.get(client or "unknown", 0) + 1
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        per_day[day] = per_day.get(day, 0) + 1
        conns.add(conn)
    for d in tools.values():
        ms = d.pop("ms")
        d["p50_ms"], d["p95_ms"] = _pct(ms, .5), _pct(ms, .95)
    out = {"days": days, "tools": tools, "calls": len(all_ms), "errors": sum(d["errors"] for d in tools.values()),
           "connections": len(conns - {None}), "p50_ms": _pct(all_ms, .5), "p95_ms": _pct(all_ms, .95),
           "clients": dict(sorted(clients.items(), key=lambda x: -x[1])), "per_day": dict(sorted(per_day.items()))}
    if recent:
        out["recent"] = [dict(ts=ts, tool=tool, client=client, cwd=cwd, query=_query(args), ms=ms, ok=bool(ok), error=error,
                              n_results=n, top=[r[1] or r[0] for r in json.loads(results or "[]")[:3]])
                         for ts, tool, client, cwd, args, ms, ok, error, n, results in db.execute(
                             "SELECT ts, tool, client, cwd, args, ms, ok, error, n_results, results FROM calls "
                             "WHERE ts >= ? ORDER BY ts DESC LIMIT ?", (since, recent))]
    return out
