"""The queue of text jobs for the user's agent, which writes all of Pensieve's text (llm.complete).

Pensieve queues each completion that it needs. Agents that are connected over MCP read the queue with `pending_work`
and answer each job with `submit_work`. The next time Pensieve asks for the same completion, it gets that answer.
Two completions are the same job when their system text, prompt and schema are the same, so a job whose data
changes is asked again as a new job, and the old one is dropped after KEEP seconds.

Several sessions can work at the same time: `pending_work` gives each session the jobs that no other session took
in the last CLAIM seconds. Each session that answers is kept in `agent_sessions`; one answer shows that it can write
jobs.

The queue is in store.db (`agent_jobs`), which the MCP server and the app open from their own processes."""
import hashlib
import json
import time

from pensieve import store
from pensieve.models import AgentJob, now
from pensieve.store import misc

KEEP = 2 * 86400
CLAIM = 10 * 60
ACTIVE = 24 * 3600  # a session that answered in this long counts as active


def job_id(prompt: str, system: str, schema: dict | None) -> str:
    return hashlib.sha1(json.dumps([system, prompt, schema], sort_keys=True).encode()).hexdigest()[:16]


def take(prompt: str, system: str, schema: dict | None, kind: str, max_tokens: int) -> str | None:
    """The agent's answer to this completion, given once; else the job goes in the queue and this returns None."""
    job = AgentJob(job_id(prompt, system, schema), kind, system, prompt, json.dumps(schema) if schema else None,
                   max_tokens, now(), None, None, None, None)
    return misc.take_job(store.store_db(), job, KEEP)


def pending(limit: int = 5, kind: str = "", conn: str = "") -> list[dict]:
    """Jobs without an answer that no other session took lately, the most recently asked first. They are this
    session's for CLAIM seconds."""
    js = misc.claim_jobs(store.store_db(), limit, kind, conn, CLAIM)
    return [dict(id=j.id, kind=j.kind, system=j.system, prompt=j.prompt,
                 schema=json.loads(j.schema) if j.schema else None, max_tokens=j.max_tokens) for j in js]


def counts() -> dict[str, int]:
    return misc.job_counts(store.store_db())


def sessions(within: float = ACTIVE) -> list[dict]:
    """The sessions that answered a job in the last `within` seconds, the latest first: {conn, client, at, answers}."""
    return [dict(conn=s.conn, client=s.client, at=s.at, answers=s.answers)
            for s in misc.agent_sessions(store.store_db(), int(time.time() - within))]


def submit(jid: str, answer: str, conn: str = "", client: str | None = None):
    """Keep the answer to a job. Raises ValueError when there is no such job without an answer, or when the job has
    a schema and the answer is not a JSON object with its required fields."""
    db = store.store_db()
    open_, schema = misc.open_job_schema(db, jid)
    if not open_:
        raise ValueError(f"no open job {jid!r}: it was answered already, or its data changed (see pending_work)")
    if schema:
        try:
            obj = json.loads(answer[answer.find("{"):answer.rfind("}") + 1])
        except ValueError as e:
            raise ValueError(f"the answer must be a JSON object that matches the schema ({e})") from e
        missing = [k for k in json.loads(schema).get("required", []) if k not in obj]
        if missing:
            raise ValueError(f"the answer has no {', '.join(missing)}")
    if not misc.answer_job(db, jid, answer, conn or client or "agent", client):
        raise ValueError(f"no open job {jid!r}: it was answered already, or its data changed (see pending_work)")
