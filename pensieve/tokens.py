"""Token use of agent sessions, from the usage that each agent writes into its transcripts.

- Claude Code: `message.usage` on assistant lines (input, output, cache write, cache read) and `message.model`. A
  streamed reply is written as several lines with the same message id: the last line of an id wins. A session's
  subagents write their own transcripts in `<session>/subagents/`; their tokens count for the session.
- Codex: `token_count` events. `last_token_usage` is the turn's use; an event that repeats the total is skipped.
  The model is in the `turn_context` line before it.
- Qwen Code: `usageMetadata` on assistant lines, one per line id.

The parsers (parsers.py) call `claude_line`, `codex_line` and `qwen_line` for each line they read, so a transcript
that grows is read from where it stopped. `scan` reads a whole transcript for usage only (the backfill). `rows`
gives a session's sums per hour and model, for the `session_usage` table.

Costs are estimates: list prices per million tokens of each model on the Anthropic API (PRICES). Cache writes count
at 1.25 times the input price (5-minute cache) or 2 times (1-hour cache). Other agents' models have no price here."""
import json
from datetime import datetime
from pathlib import Path

# (model id prefix, input, output, cache read) in US dollars per million tokens, Anthropic API list prices of
# 2026-10. The longest prefix that matches wins. Cache writes: 1.25 x input (5 minutes), 2 x input (1 hour).
PRICES = [
    ("claude-fable-5-1", 10.0, 50.0, 0.25),
    ("claude-mythos-5-1", 10.0, 50.0, 0.25),
    ("claude-fable-5", 10.0, 50.0, 1.0),
    ("claude-mythos-5", 10.0, 50.0, 1.0),
    ("claude-opus-5-5", 4.0, 20.0, 0.20),
    ("claude-opus-5", 5.0, 25.0, 0.50),
    ("claude-opus-4-1", 15.0, 75.0, 1.50),
    ("claude-opus-4-2", 15.0, 75.0, 1.50),  # claude-opus-4-20250514
    ("claude-opus-4", 5.0, 25.0, 0.50),  # 4.5 to 4.8
    ("claude-sonnet-5-5", 2.0, 10.0, 0.20),
    ("claude-sonnet-5", 2.0, 10.0, 0.20),
    ("claude-sonnet-4", 3.0, 15.0, 0.30),
    ("claude-haiku-5-5", 0.10, 0.50, 0.01),
    ("claude-haiku-4", 1.0, 5.0, 0.10),
    ("claude-3-5-haiku", 0.80, 4.0, 0.08),
]
PRICES_AS_OF = "2026-10"
_BY_LEN = sorted(PRICES, key=lambda p: -len(p[0]))


def price(model: str) -> tuple[float, float, float] | None:
    """(input, output, cache read) per million tokens of a model, or None when it has no price here."""
    m = (model or "").lower().removeprefix("anthropic.").removeprefix("us.anthropic.")
    for prefix, i, o, r in _BY_LEN:
        if m.startswith(prefix):
            return i, o, r
    return None


def cost(model: str, input: int, output: int, cache_write: int, cache_read: int, cache_write_1h: int = 0) -> float | None:
    """The estimated cost in US dollars, or None for a model with no price."""
    p = price(model)
    if p is None:
        return None
    i, o, r = p
    w5 = max(0, cache_write - cache_write_1h)
    return (input * i + output * o + w5 * i * 1.25 + cache_write_1h * i * 2 + cache_read * r) / 1e6


def _hour(ts) -> int:
    """The start of the hour of an ISO time (UTC), as Unix seconds; 0 when the time is missing."""
    if not ts:
        return 0
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0
    return int(t) // 3600 * 3600


def _int(v) -> int:
    return v if isinstance(v, int) and v > 0 else 0


def new() -> dict:
    """The usage state of one session: each model call by its id, the read offsets of subagent transcripts, and the
    Codex state (the current model and the last total)."""
    return {"calls": {}, "sub": {}, "model": "", "total": None}


def claude_line(d: dict, acc: dict):
    if d.get("type") != "assistant":
        return
    m = d.get("message")
    if not isinstance(m, dict):
        return
    u, model = m.get("usage"), m.get("model") or ""
    if not isinstance(u, dict) or not model or model.startswith("<"):  # "<synthetic>": no model call
        return
    key = m.get("id") or d.get("requestId") or d.get("uuid")
    if not key:
        return
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    acc["calls"][key] = (_hour(d.get("timestamp")), model, _int(u.get("input_tokens")), _int(u.get("output_tokens")),
                         _int(u.get("cache_creation_input_tokens")), _int(u.get("cache_read_input_tokens")),
                         _int(cc.get("ephemeral_1h_input_tokens")))


def qwen_line(d: dict, acc: dict):
    u = d.get("usageMetadata")
    if d.get("type") != "assistant" or not isinstance(u, dict) or not d.get("uuid"):
        return
    cached = _int(u.get("cachedContentTokenCount"))
    acc["calls"][d["uuid"]] = (_hour(d.get("timestamp")), d.get("model") or "qwen",
                               max(0, _int(u.get("promptTokenCount")) - cached),
                               _int(u.get("candidatesTokenCount")) + _int(u.get("thoughtsTokenCount")), 0, cached, 0)


def codex_line(d: dict, acc: dict):
    p = d.get("payload")
    if not isinstance(p, dict):
        return
    if d.get("type") == "turn_context" and p.get("model"):
        acc["model"] = p["model"]
        return
    if p.get("type") != "token_count" or not isinstance(p.get("info"), dict):
        return
    info = p["info"]
    total = (info.get("total_token_usage") or {}).get("total_tokens")
    last = info.get("last_token_usage")
    if not isinstance(last, dict) or total is None or total == acc.get("total"):
        return
    acc["total"] = total
    cached = _int(last.get("cached_input_tokens"))
    acc["calls"][f"t{total}"] = (_hour(d.get("timestamp")), acc.get("model") or "codex",
                                 max(0, _int(last.get("input_tokens")) - cached), _int(last.get("output_tokens")), 0,
                                 cached, 0)


LINE = {"claude": claude_line, "qwen": qwen_line, "codex": codex_line}
# a line without one of these bytes has no usage: the scan skips its JSON parse
_MARK = {"claude": (b'"usage"',), "qwen": (b'"usageMetadata"',), "codex": (b'"token_count"', b'"turn_context"')}


def _read(path: Path, offset: int, fn, marks, acc) -> int:
    """Read whole lines of a transcript from `offset`; returns the offset after the last whole line."""
    try:
        f = open(path, "rb")
    except OSError:
        return offset
    with f:
        f.seek(offset)
        for raw in f:
            if not raw.endswith(b"\n"):
                break
            offset += len(raw)
            if not any(m in raw for m in marks):
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if isinstance(d, dict):
                fn(d, acc)
    return offset


def subagents(path: Path, acc: dict):
    """The usage of a Claude Code session's subagents (`<session>/subagents/*.jsonl`), from where the last read
    stopped."""
    folder = path.with_suffix("") / "subagents"
    if not folder.is_dir():
        return
    for p in sorted(folder.glob("*.jsonl")):
        k = p.name
        try:
            size = p.stat().st_size
        except OSError:
            continue
        off = acc["sub"].get(k, 0)
        if size < off:  # written again: read it whole
            acc["calls"] = {c: v for c, v in acc["calls"].items() if not str(c).startswith(k + ":")}
            off = 0
        if size == off:
            continue
        sub = new()
        acc["sub"][k] = _read(p, off, claude_line, _MARK["claude"], sub)
        for c, v in sub["calls"].items():
            acc["calls"][f"{k}:{c}"] = v


def scan(source: str, path: Path) -> dict:
    """The usage of a whole transcript (and its subagents), without the rest of the parse."""
    acc = new()
    if source in LINE and path.suffix == ".jsonl":
        _read(path, 0, LINE[source], _MARK[source], acc)
        if source == "claude":
            subagents(path, acc)
    return acc


def rows(acc: dict | None) -> list[tuple]:
    """A session's usage as rows (hour, model, calls, input, output, cache_write, cache_read, cache_write_1h): sums
    per hour and model."""
    out: dict[tuple, list] = {}
    for hour, model, i, o, cw, cr, cw1 in (acc or {}).get("calls", {}).values():
        r = out.setdefault((hour, model), [0, 0, 0, 0, 0, 0])
        r[0] += 1
        r[1] += i
        r[2] += o
        r[3] += cw
        r[4] += cr
        r[5] += cw1
    return [(h, m, *v) for (h, m), v in sorted(out.items())]
