"""Other user data in store.db: feedback (v1: feedback.jsonl), the log of MCP tool calls (v1: usage.db) and the queue
of completions for the user's agent (v1: agent_jobs.db). The MCP command opens store.db from its own process; WAL
lets both read while one writes."""
import json

from pensieve.models import (AgentJob, AgentSession, Feedback, FeedbackBy, FeedbackKind, FeedbackStatus, UsageCall,
                             now)
from pensieve.store import rows
from pensieve.store.db import Db


def _feedback_dict(f: Feedback) -> dict:
    return dict(id=f.id, ts=f.at, kind=f.kind.value, summary=f.text, details=f.details, source=f.source,
                status=f.status.value, version=f.version, context=json.loads(f.context))


def _feedback_row(x: dict) -> Feedback:
    src = x.get("source") or "user"
    return Feedback(x["id"], int(x.get("ts") or now()), FeedbackBy.USER if src == "user" else FeedbackBy.AGENT,
                    FeedbackKind(x["kind"]), x.get("summary") or "", FeedbackStatus(x.get("status") or "new"),
                    x.get("details") or "", src, x.get("version") or "", json.dumps(x.get("context") or {}))


def add_feedback(db: Db, x: dict) -> dict:
    """Keep a report in the v1 shape {id, ts, kind, summary, details, source, status, version, context}."""
    f = _feedback_row(x)
    db.write(lambda c: rows.insert(c, f))
    return _feedback_dict(f)


def feedback(db: Db) -> list[dict]:
    """Every report, in the v1 shape, oldest first."""
    return [_feedback_dict(f) for f in rows.select(db.conn, Feedback, order="at, id")]


def set_feedback_status(db: Db, id: str, status: FeedbackStatus) -> dict | None:
    def run(c):
        c.execute("UPDATE feedback SET status = ? WHERE id = ?", (status.value, id))
        return rows.select_one(c, Feedback, "id = ?", (id,))
    f = db.write(run)
    return _feedback_dict(f) if f else None


def import_feedback(db: Db, items: list[dict]):
    """For the v1 import: the lines of feedback.jsonl. A report that is there already stays as it is."""
    fs = [_feedback_row(x) for x in items if x.get("id") and x.get("kind") in {k.value for k in FeedbackKind}]
    db.write(lambda c: rows.insert_many(c, fs, or_="IGNORE"))


USAGE_COLS = ("at", "source", "tool", "client", "conn", "cwd", "scope", "args", "ms", "ok", "error", "n_results",
              "results", "laps", "out_chars")


def usage_row(r: dict) -> UsageCall:
    """A UsageCall from a v1 `calls` row ({ts, ..., ok}); `args`, `results` and `laps` are JSON text or None."""
    def j(v):
        if v is None or v == "":
            return None
        try:
            json.loads(v)
            return v
        except (TypeError, ValueError):
            return json.dumps(str(v))
    return UsageCall(None, int(r.get("ts") or r.get("at") or now()), r.get("source"), r.get("tool") or "",
                     r.get("client"), r.get("conn"), r.get("cwd"), r.get("scope"), j(r.get("args")),
                     float(r["ms"]) if r.get("ms") is not None else None, bool(r["ok"]) if r.get("ok") is not None
                     else None, r.get("error"), r.get("n_results"), j(r.get("results")), j(r.get("laps")),
                     r.get("out_chars"))


def add_usage(db: Db, calls: list[UsageCall], wait: bool = False):
    """Log tool calls. Without `wait`, the writer thread commits them with the next batch."""
    return db.write(lambda c: rows.insert_many(c, calls), wait=wait)


def usage_since(db: Db, since: int) -> list[tuple]:
    """(tool, ms, ok, client, conn, n_results, at) of the calls since `since`, by ms."""
    return db.query("SELECT tool, ms, ok, client, conn, n_results, at FROM usage_calls WHERE at >= ? ORDER BY ms",
                    (since,))


def usage_recent(db: Db, since: int, limit: int) -> list[tuple]:
    """(at, tool, client, cwd, args, ms, ok, error, n_results, results) of the latest calls."""
    return db.query("SELECT at, tool, client, cwd, args, ms, ok, error, n_results, results FROM usage_calls "
                    "WHERE at >= ? ORDER BY at DESC, id DESC LIMIT ?", (since, limit))


def import_usage(db: Db, v1_rows: list[dict]):
    """For the v1 import: the rows of usage.db's `calls` table, as dicts."""
    db.write(lambda c: rows.insert_many(c, [usage_row(r) for r in v1_rows]))


# The agent's job queue. Every function does its reads and writes in one write job, so that two processes do not
# take the same answer twice.
def take_job(db: Db, job: AgentJob, keep: int) -> str | None:
    """The answer of `job.id`, given once (the row goes); else queue the job (or move its asked time) and drop jobs
    older than `keep` seconds."""
    def run(c):
        r = c.execute("SELECT answer FROM agent_jobs WHERE id = ?", (job.id,)).fetchone()
        if r and r[0] is not None:
            c.execute("DELETE FROM agent_jobs WHERE id = ?", (job.id,))
            return r[0]
        rows.upsert(c, job, ("id",), keep=tuple(k for k in ("kind", "system", "prompt", "schema", "max_tokens",
                                                             "answer", "answered_at", "claimed_at", "claimer")))
        c.execute("DELETE FROM agent_jobs WHERE coalesce(answered_at, asked_at) < ?", (job.asked_at - keep,))
        return None
    return db.write(run)


def claim_jobs(db: Db, limit: int, kind: str, conn: str, claim: int) -> list[AgentJob]:
    """Open jobs that no other session claimed in the last `claim` seconds, the test job and then the newest first.
    They are claimed for `conn`."""
    t = now()

    def run(c):
        q = "answer IS NULL AND (claimed_at IS NULL OR claimed_at < ? OR claimer = ?)"
        args = [t - claim, conn]
        if kind:
            q += " AND kind = ?"
            args.append(kind)
        js = rows.select(c, AgentJob, q + " ORDER BY kind = 'test' DESC, asked_at DESC LIMIT ?", (*args, limit))
        c.executemany("UPDATE agent_jobs SET claimed_at = ?, claimer = ? WHERE id = ?", [(t, conn, j.id) for j in js])
        return js
    return db.write(run)


def job_counts(db: Db) -> dict[str, int]:
    return dict(db.query("SELECT kind, count(*) FROM agent_jobs WHERE answer IS NULL GROUP BY kind"))


def open_job_schema(db: Db, id: str) -> tuple[bool, str | None]:
    """(the job is open, its schema)."""
    r = db.one("SELECT schema FROM agent_jobs WHERE id = ? AND answer IS NULL", (id,))
    return (True, r[0]) if r else (False, None)


def answer_job(db: Db, id: str, answer: str, conn: str, client: str | None) -> bool:
    """Keep an answer and count it for the session. False when the job is not open."""
    t = now()

    def run(c):
        cur = c.execute("UPDATE agent_jobs SET answer = ?, answered_at = ? WHERE id = ? AND answer IS NULL",
                        (answer, t, id))
        if not cur.rowcount:
            return False
        c.execute("INSERT INTO agent_sessions VALUES (?, ?, ?, 1) ON CONFLICT (conn) DO UPDATE SET "
                  "client = excluded.client, at = excluded.at, answers = answers + 1", (conn, client, t))
        return True
    return db.write(run)


def agent_sessions(db: Db, since: int) -> list[AgentSession]:
    return rows.select(db.conn, AgentSession, "at > ?", (since,), order="at DESC")
