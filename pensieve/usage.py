"""A local log of every MCP tool call: which agent made it, from where, with what arguments, how long it took, and what
it returned (for a search, the result ids in order). It lives in the `usage_calls` table of store.db, so resetting
the index keeps the history. Nothing leaves the Mac.

Calls go to store.db's writer thread without a wait: logging never adds to a tool call's time, and a failure to log
is dropped rather than failing the call."""
import contextvars
import json
import time

from pensieve import store
from pensieve.store import misc

# The call being handled, so a tool can add what only it knows (search: result ids, per-phase times).
current = contextvars.ContextVar("usage_call", default=None)


def record(**row):
    for k in ("args", "results", "laps"):
        if row.get(k) is not None and not isinstance(row[k], str):
            row[k] = json.dumps(row[k])
    try:
        misc.add_usage(store.store_db(), [misc.usage_row(row)])
    except Exception:
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
    if not isinstance(a, dict):
        return ""
    for k in ("query", "topic", "q", "id", "path", "repo", "0"):
        if a.get(k):
            return str(a[k])[:200]
    return ""


def summary(days: float = 7, recent: int = 0) -> dict:
    """MCP tool calls over the last `days`: per tool (count, errors, p50/p95 ms), per agent, per day, and with
    `recent`, the latest calls with what they asked for and the top results."""
    db = store.store_db()
    since = int(time.time() - days * 86400)
    tools, clients, per_day, conns, all_ms = {}, {}, {}, set(), []
    for tool, ms, ok, client, conn, n, ts in misc.usage_since(db, since):
        ms = ms or 0.0
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
                         for ts, tool, client, cwd, args, ms, ok, error, n, results in misc.usage_recent(db, since, recent)]
    return out
