"""Where summaries, topic names, notes and insights get written. Pensieve runs no text model of its own: each
completion that it needs waits in a queue (agent_jobs) until the background text engine answers it (engine.py: a
command-line agent or a model server that the user runs), or an agent does over MCP (pending_work, submit_work)."""
import json


class Offline(Exception):
    """No answer is available now."""


class Queued(Offline):
    """The job waits in the queue for your agent. Callers that run several jobs go on to the next one."""


def _json_from(t: str):
    return json.loads(t[t.find("{"):t.rfind("}") + 1])


def _strip_think(t):
    if "</think>" in t:
        t = t.split("</think>", 1)[1]
    return t.strip()


def complete(prompt: str, system: str = "", max_tokens: int = 200, temperature: float = 0.2, schema: dict | None = None,
             kind: str = "text"):
    """The agent's answer to one completion. With `schema`, returns the parsed JSON object. Raises Queued while the
    job waits for your agent. `kind` names the job for the agent. `temperature` is not used: the agent picks its own."""
    from pensieve import agent_jobs
    t = agent_jobs.take(prompt, system, schema, kind, max_tokens)
    if t is None:
        raise Queued("Waiting for your agent")
    return _json_from(t) if schema else _strip_think(t)
