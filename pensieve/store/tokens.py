"""index.db: `session_usage`, the tokens of each agent session per hour and model (pensieve/tokens.py). The session
indexer replaces a session's rows each time it reads the session; the backfill fills sessions indexed before the
table existed."""
import sqlite3

from pensieve.models import SessionUsage
from pensieve.store import rows as R
from pensieve.store.db import Db


def replace_session(c: sqlite3.Connection, session_id: str, usage: list[tuple]):
    """Write a session's rows (hour, model, calls, input, output, cache_write, cache_read, cache_write_1h), in place
    of the ones it had. Runs on a writer connection (a write job, or a hold)."""
    c.execute("DELETE FROM session_usage WHERE session_id = ?", (session_id,))
    R.insert_many(c, [SessionUsage(session_id, *r) for r in usage])


def write_sessions(db: Db, items: list[tuple[str, list[tuple]]]):
    """Replace the rows of several sessions in one write job."""
    def run(c):
        for sid, usage in items:
            replace_session(c, sid, usage)
    if items:
        db.write(run)


def delete_sessions(c: sqlite3.Connection, session_ids: list[str]):
    if session_ids:
        c.execute(f"DELETE FROM session_usage WHERE session_id IN ({R.marks(len(session_ids))})", session_ids)


def sessions_with_usage(db: Db) -> set[str]:
    return {r[0] for r in db.query("SELECT DISTINCT session_id FROM session_usage")}


def rows_since(db: Db, since: int = 0) -> list[tuple]:
    """(session_id, hour, model, calls, input, output, cache_write, cache_read, cache_write_1h) from `since` on: for
    sums over some sessions only (the sessions of some repos)."""
    return db.query("SELECT session_id, hour, model, calls, input, output, cache_write, cache_read, cache_write_1h "
                    "FROM session_usage WHERE hour >= ?", (since,))
