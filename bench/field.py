"""Field test of the search gate: does making Claude Code search Pensieve before grep help in real work?

The gate (pensieve/gate.py) blocks grep-like searches in a turn until a Pensieve tool is called, except in the
control arm (one session in `search_gate_holdout`). This report reads the Claude Code transcripts of a period and
compares three groups of sessions: gate, control (the same days, no blocking) and baseline (the days before the test
started, when there was no gate at all).

    python bench/field.py --start 2026-10-20            # the test's first day; baseline = the 7 days before it
    python bench/field.py --start 2026-10-20 --days 14  # a longer baseline

For each group, per lookup (a turn with at least one code search, by Pensieve or grep):
- search calls: Pensieve searches and grep-like calls (Grep, Glob, a shell grep/rg/find)
- calls to the first file read: search calls before the first Read of a file in the turn
- grep after Pensieve: a grep-like call after a Pensieve search in the same turn (Pensieve likely missed)
- denied: grep-like calls that the gate blocked
- tokens and seconds per lookup turn
The report goes to ~/.pensieve/field-test/report-<date>.md: it names your repos, so it stays on your Mac."""
import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import gate  # noqa: E402

PROJECTS = Path.home() / ".claude" / "projects"
OUT = Path.home() / ".pensieve" / "field-test"
PENSIEVE = re.compile(r"^mcp__(plugin_pensieve_)?pensieve__")


def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else 0.0


def is_grep(name: str, inp: dict) -> bool:
    return gate.is_search(name, inp or {})


def turns(path: Path) -> list[dict]:
    """The user turns of one transcript: {start, end, cwd, calls: [(kind, name)], tokens, denied}. A turn starts at
    a prompt the user typed (not a tool result)."""
    out, cur = [], None
    for line in path.open(errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        msg, ts = d.get("message") or {}, _ts(d.get("timestamp", ""))
        content = msg.get("content")
        if d.get("type") == "user" and not d.get("isSidechain"):
            blocks = content if isinstance(content, list) else []
            typed = isinstance(content, str) or any(b.get("type") == "text" for b in blocks if isinstance(b, dict))
            if typed and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
                cur = dict(start=ts, end=ts, cwd=d.get("cwd") or "", calls=[], tokens=0, denied=0)
                out.append(cur)
                continue
        if cur is None:
            continue
        cur["end"] = max(cur["end"], ts)
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                name, inp = b.get("name", ""), b.get("input") or {}
                kind = ("pensieve_search" if PENSIEVE.match(name) and name.endswith("__search") else
                        "pensieve" if PENSIEVE.match(name) else "grep" if is_grep(name, inp) else
                        "read" if name == "Read" else "other")
                cur["calls"].append((kind, name))
            elif b.get("type") == "tool_result" and b.get("is_error") and "Search with Pensieve first" in str(b.get("content")):
                cur["denied"] += 1
        u = msg.get("usage") or {}
        if d.get("type") == "assistant":
            cur["tokens"] += sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "output_tokens")) \
                + 0.1 * (u.get("cache_read_input_tokens") or 0)
    return out


def lookup(t: dict) -> dict | None:
    """The measures of a turn with a code search, or None."""
    kinds = [k for k, _ in t["calls"]]
    if not any(k in ("grep", "pensieve_search") for k in kinds):
        return None
    first_read = kinds.index("read") if "read" in kinds else len(kinds)
    seen_pensieve, grep_after = False, 0
    for k in kinds:
        seen_pensieve |= k == "pensieve_search"
        grep_after += k == "grep" and seen_pensieve
    return dict(search=sum(k in ("grep", "pensieve_search") for k in kinds), pensieve=kinds.count("pensieve_search"),
                grep=kinds.count("grep"), to_read=sum(k in ("grep", "pensieve_search") for k in kinds[:first_read]),
                grep_after=grep_after, denied=t["denied"], tokens=t["tokens"], secs=t["end"] - t["start"],
                read=first_read < len(kinds))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="when the gate went on: YYYY-MM-DD or YYYY-MM-DDTHH:MM (local)")
    ap.add_argument("--days", type=int, default=7, help="days of baseline before --start")
    ap.add_argument("--until", help="last day to include (default today)")
    a = ap.parse_args()
    start = datetime.fromisoformat(a.start).timestamp()  # a day (its midnight) or the exact time the gate went on
    first = start - a.days * 86400
    until = datetime.combine(date.fromisoformat(a.until) + timedelta(days=1), datetime.min.time()).timestamp() \
        if a.until else float("inf")
    groups, repos = defaultdict(list), defaultdict(lambda: defaultdict(int))
    for path in PROJECTS.glob("*/*.jsonl"):
        if path.stat().st_mtime < first:
            continue
        sid = path.stem
        for t in turns(path):
            if not first <= t["start"] < until:
                continue
            m = lookup(t)
            if not m:
                continue
            g = "baseline" if t["start"] < start else gate.arm(sid)
            groups[g].append(m)
            repos[g][Path(t["cwd"]).name or "?"] += 1
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"report-{date.today().isoformat()}.md"
    out.write_text(render(groups, repos, a))
    print(out.read_text())
    print(f"\nSaved to {out}")


def render(groups, repos, a) -> str:
    def mean(xs):
        return statistics.fmean(xs) if xs else 0.0

    def med(xs):
        return statistics.median(xs) if xs else 0.0
    rows = [("Lookups (turns with a code search)", lambda g: str(len(g))),
            ("Search calls per lookup", lambda g: f"{mean([m['search'] for m in g]):.2f}"),
            ("Pensieve searches per lookup", lambda g: f"{mean([m['pensieve'] for m in g]):.2f}"),
            ("Grep-like calls per lookup", lambda g: f"{mean([m['grep'] for m in g]):.2f}"),
            ("Search calls before the first file read", lambda g: f"{mean([m['to_read'] for m in g if m['read']]):.2f}"),
            ("Lookups with grep after a Pensieve search", lambda g: f"{100 * mean([m['grep_after'] > 0 for m in g]):.0f}%"),
            ("Denied calls per lookup", lambda g: f"{mean([m['denied'] for m in g]):.2f}"),
            ("Tokens per lookup turn (median, cache reads at 10%)", lambda g: f"{med([m['tokens'] for m in g]):,.0f}"),
            ("Seconds per lookup turn (median)", lambda g: f"{med([m['secs'] for m in g]):.0f}")]
    names = [g for g in ("gate", "control", "baseline") if g in groups]
    lines = [f"# Search gate field test: report of {date.today().isoformat()}", "",
             f"Test start {a.start}; baseline: the {a.days} days before it. Holdout: 1 session in "
             f"{gate.settings.get('search_gate_holdout') or 'none'} is control.", "",
             "| Measure | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    lines += [f"| {label} | " + " | ".join(f(groups[g]) for g in names) + " |" for label, f in rows]
    lines += ["", "Lookups by repo:"]
    for g in names:
        top = sorted(repos[g].items(), key=lambda x: -x[1])[:8]
        lines.append(f"- {g}: " + ", ".join(f"{r} {n}" for r, n in top))
    lines += ["", "Caveats: the groups do different work, so compare rates, not totals. A small control arm is noisy "
              "until it has a few dozen lookups."]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
