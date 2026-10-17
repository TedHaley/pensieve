"""The generation model when it is your agent (`ai: agent`).

Pensieve queues each completion that it needs. Agents that are connected over MCP read the queue with `pending_work`
and answer each job with `submit_work`. The next time Pensieve asks for the same completion, it gets that answer.
Two completions are the same job when their system text, prompt and schema are the same, so a job whose data
changes is asked again as a new job, and the old one is dropped after KEEP seconds.

Several sessions can work at the same time: `pending_work` gives each session the jobs that no other session took
in the last CLAIM seconds. Each session that answers is kept in `sessions`; one answer shows that it can write jobs.

The queue is its own SQLite file, because the MCP server and the app are different processes."""
import hashlib
import json
import sqlite3
import time
from contextlib import closing

from pensieve import config

DB = config.DATA_DIR / "agent_jobs.db"
KEEP = 2 * 86400
CLAIM = 10 * 60
ACTIVE = 24 * 3600  # a session that answered in this long counts as active
TEST = ("Reply with the single word: ready", "This is Pensieve's test job. It shows that you can answer its jobs.")


def _db():
    c = sqlite3.connect(DB, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, kind TEXT, system TEXT, prompt TEXT, schema TEXT, "
              "max_tokens INT, asked REAL, answer TEXT, answered REAL, claimed REAL, claimer TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS sessions(conn TEXT PRIMARY KEY, client TEXT, at REAL, answers INT)")
    return c


def job_id(prompt: str, system: str, schema: dict | None) -> str:
    return hashlib.sha1(json.dumps([system, prompt, schema], sort_keys=True).encode()).hexdigest()[:16]


def take(prompt: str, system: str, schema: dict | None, kind: str, max_tokens: int) -> str | None:
    """The agent's answer to this completion, given once; else the job goes in the queue and this returns None."""
    jid, now = job_id(prompt, system, schema), time.time()
    with closing(_db()) as c, c:
        row = c.execute("SELECT answer FROM jobs WHERE id=?", (jid,)).fetchone()
        if row and row[0] is not None:
            c.execute("DELETE FROM jobs WHERE id=?", (jid,))
            return row[0]
        c.execute("INSERT INTO jobs(id, kind, system, prompt, schema, max_tokens, asked) VALUES(?,?,?,?,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET asked=excluded.asked",
                  (jid, kind, system, prompt, json.dumps(schema) if schema else None, max_tokens, now))
        c.execute("DELETE FROM jobs WHERE coalesce(answered, asked) < ?", (now - KEEP,))
    return None


def queue_test():
    """Queue the test job until a session has answered any job."""
    if not sessions(KEEP):
        take(TEST[0], TEST[1], None, "test", 5)


def pending(limit: int = 5, kind: str = "", conn: str = "") -> list[dict]:
    """Jobs without an answer that no other session took lately, the most recently asked first. They are this
    session's for CLAIM seconds."""
    now = time.time()
    q = ("SELECT id, kind, system, prompt, schema, max_tokens FROM jobs WHERE answer IS NULL "
         "AND (claimed IS NULL OR claimed < ? OR claimer = ?)")
    args = [now - CLAIM, conn]
    if kind:
        q += " AND kind=?"
        args.append(kind)
    with closing(_db()) as c, c:
        rows = c.execute(q + " ORDER BY kind = 'test' DESC, asked DESC LIMIT ?", (*args, limit)).fetchall()
        c.executemany("UPDATE jobs SET claimed=?, claimer=? WHERE id=?", [(now, conn, r[0]) for r in rows])
    return [dict(id=i, kind=k, system=s, prompt=p, schema=json.loads(sc) if sc else None, max_tokens=m)
            for i, k, s, p, sc, m in rows]


def counts() -> dict[str, int]:
    with closing(_db()) as c:
        return dict(c.execute("SELECT kind, count(*) FROM jobs WHERE answer IS NULL GROUP BY kind").fetchall())


def sessions(within: float = ACTIVE) -> list[dict]:
    """The sessions that answered a job in the last `within` seconds, the latest first: {conn, client, at, answers}."""
    with closing(_db()) as c:
        rows = c.execute("SELECT conn, client, at, answers FROM sessions WHERE at > ? ORDER BY at DESC",
                         (time.time() - within,)).fetchall()
    return [dict(conn=a, client=b, at=t, answers=n) for a, b, t, n in rows]


def submit(jid: str, answer: str, conn: str = "", client: str | None = None):
    """Keep the answer to a job. Raises ValueError when there is no such job without an answer, or when the job has
    a schema and the answer is not a JSON object with its required fields."""
    with closing(_db()) as c, c:
        row = c.execute("SELECT schema FROM jobs WHERE id=? AND answer IS NULL", (jid,)).fetchone()
        if not row:
            raise ValueError(f"no open job {jid!r}: it was answered already, or its data changed (see pending_work)")
        if row[0]:
            try:
                obj = json.loads(answer[answer.find("{"):answer.rfind("}") + 1])
            except ValueError as e:
                raise ValueError(f"the answer must be a JSON object that matches the schema ({e})") from e
            missing = [k for k in json.loads(row[0]).get("required", []) if k not in obj]
            if missing:
                raise ValueError(f"the answer has no {', '.join(missing)}")
        now = time.time()
        c.execute("UPDATE jobs SET answer=?, answered=? WHERE id=?", (answer, now, jid))
        c.execute("INSERT INTO sessions VALUES(?, ?, ?, 1) ON CONFLICT(conn) DO UPDATE SET client=excluded.client, "
                  "at=excluded.at, answers=answers + 1", (conn or client or "agent", client, now))
