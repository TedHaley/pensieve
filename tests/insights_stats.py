"""Tests of the token use of transcripts (pensieve/tokens.py) and of the rules of the Insights score and badges
(pensieve/insights_stats.py). Run: python tests/insights_stats.py

The transcripts and numbers are made up."""
import json
import os
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

os.environ["PENSIEVE_DATA"] = tempfile.mkdtemp(prefix="pensieve-insights-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import insights_stats as ist, parsers, tokens  # noqa: E402

TS = "2026-03-02T10:15:00.000Z"


def claude_line(mid, model="claude-opus-5-5", out=10, ts=TS, **usage):
    u = dict(input_tokens=3, output_tokens=out, cache_creation_input_tokens=100, cache_read_input_tokens=1000, **usage)
    return {"type": "assistant", "timestamp": ts, "message": {"id": mid, "model": model, "usage": u,
                                                               "content": [{"type": "text", "text": "ok"}]}}


def write(path: Path, lines):
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))


def test_streamed_reply_counts_once():
    """A streamed reply repeats its message id on several lines: it is one call, and its last line wins."""
    acc = tokens.new()
    for d in (claude_line("m1", out=5), claude_line("m1", out=40), claude_line("m2", out=7)):
        tokens.claude_line(d, acc)
    rows = tokens.rows(acc)
    assert len(rows) == 1
    hour, model, calls, i, o, cw, cr, cw1 = rows[0]
    assert (model, calls, i, o, cw, cr) == ("claude-opus-5-5", 2, 6, 47, 200, 2000)
    assert hour % 3600 == 0


def test_missing_fields_count_as_zero():
    """A usage without cache fields, with a null, or with a wrong type counts the missing parts as zero; a line with
    no usage, no id or the synthetic model is skipped."""
    acc = tokens.new()
    tokens.claude_line({"type": "assistant", "message": {"id": "a", "model": "claude-sonnet-5-5",
                                                         "usage": {"input_tokens": 4, "output_tokens": None}}}, acc)
    tokens.claude_line({"type": "assistant", "message": {"id": "b", "model": "claude-sonnet-5-5",
                                                         "usage": {"input_tokens": "x", "output_tokens": 2}}}, acc)
    tokens.claude_line({"type": "assistant", "message": {"id": "c", "model": "<synthetic>", "usage": {"input_tokens": 9}}}, acc)
    tokens.claude_line({"type": "assistant", "message": {"model": "claude-sonnet-5-5", "usage": {"input_tokens": 9}}}, acc)
    tokens.claude_line({"type": "user", "message": {"id": "d", "usage": {"input_tokens": 9}}}, acc)
    tokens.claude_line({"type": "assistant", "message": "text"}, acc)
    rows = tokens.rows(acc)
    assert [(r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows] == [("claude-sonnet-5-5", 2, 4, 2, 0, 0)]
    assert rows[0][0] == 0  # no timestamp: hour 0


def test_parser_reads_usage_incrementally_with_subagents():
    """The Claude parser collects usage as it reads; a resumed parse adds only the new lines, and a subagent's
    transcript counts for its session."""
    d = Path(tempfile.mkdtemp())
    p = d / "s1.jsonl"
    write(p, [claude_line("m1"), {"type": "user", "timestamp": TS, "message": {"content": "hello"}}])
    s = parsers.parse("claude", p)
    assert tokens.rows(s.state["tok"])[0][2] == 1
    with p.open("a") as f:
        f.write(json.dumps(claude_line("m1", out=50)) + "\n" + json.dumps(claude_line("m2")) + "\n")
    (d / "s1" / "subagents").mkdir(parents=True)
    write(d / "s1" / "subagents" / "agent-1.jsonl", [claude_line("m1", model="claude-haiku-5-5", out=3)])
    s = parsers.parse("claude", p, resume=s)
    rows = {r[1]: r for r in tokens.rows(s.state["tok"])}
    assert rows["claude-opus-5-5"][2] == 2 and rows["claude-opus-5-5"][4] == 60  # m1's last line (50) + m2 (10)
    assert rows["claude-haiku-5-5"][2] == 1  # the subagent's "m1" is not the session's "m1"
    full = tokens.rows(tokens.scan("claude", p))
    assert sorted(full) == sorted(tokens.rows(s.state["tok"]))  # the backfill scan gives the same rows


def test_codex_and_qwen():
    """Codex: a token_count event that repeats the total is skipped; cached input is a cache read. Qwen: one call
    per line id; thoughts count as output."""
    acc = tokens.new()
    tc = lambda total, inp, cached, out: {"type": "event_msg", "timestamp": TS, "payload": {"type": "token_count", "info": {
        "total_token_usage": {"total_tokens": total},
        "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}}}}
    for d in ({"type": "turn_context", "payload": {"model": "gpt-x"}}, tc(100, 80, 30, 20), tc(100, 80, 30, 20),
              tc(150, 40, 0, 10), {"type": "event_msg", "payload": {"type": "token_count", "info": None}}):
        tokens.codex_line(d, acc)
    assert tokens.rows(acc) == [(tokens._hour(TS), "gpt-x", 2, 90, 30, 0, 30, 0)]
    acc = tokens.new()
    q = {"type": "assistant", "uuid": "u1", "timestamp": TS, "model": "qwen/q",
         "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 5, "thoughtsTokenCount": 7,
                           "cachedContentTokenCount": 40}}
    tokens.qwen_line(q, acc)
    tokens.qwen_line(q, acc)
    assert tokens.rows(acc) == [(tokens._hour(TS), "qwen/q", 1, 60, 12, 0, 40, 0)]


def test_cost_estimate():
    """Cost uses the model's list price; cache writes are 1.25x input (2x for the 1-hour cache); an unknown model has
    no cost."""
    c = tokens.cost("claude-opus-5-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000, cache_write_1h=0)
    assert abs(c - (4 + 20 + 5 + 0.2)) < 1e-9
    c1h = tokens.cost("claude-opus-5-5", 0, 0, 1_000_000, 0, cache_write_1h=1_000_000)
    assert abs(c1h - 8) < 1e-9
    assert tokens.price("claude-haiku-4-5-20251001") == (1.0, 5.0, 0.10)
    assert tokens.cost("qwen/qwen3", 10, 10, 0, 0) is None


def test_score():
    """Score = 100 x points per million weighted tokens, with at least 1 million; no tokens gives no score."""
    counts = dict(pr_merged=2, thread_closed=1, ticket_done=1, review=3, commit=40, item_closed=2)
    assert ist.points(counts) == 20 + 5 + 3 + 6 + 30 + 2  # commits stop at 30
    w = ist.weighted(input=1_000_000, output=1_000_000, cache_write=1_000_000, cache_read=10_000_000)
    assert w == 1e6 + 5e6 + 1.25e6 + 1e6
    assert ist.score(66, 8.25e6) == 800
    assert ist.score(10, 1000) == 1000  # floor: 1 million
    assert ist.score(10, 0) is None


def test_streaks():
    """A run counts days with something shipped; a workday without one breaks it, a weekend day does not, and today
    does not break it before it ends."""
    mon = date(2026, 3, 2)  # a Monday
    first = mon - timedelta(days=30)
    shipped = {mon - timedelta(days=d) for d in (3, 4, 7)}  # Fri, Thu, then Mon a week before
    cur, best = ist.streaks(shipped, mon, first)  # today (Mon) has nothing yet; Sat and Sun are skipped
    assert (cur, best) == (2, 2)
    shipped |= {mon - timedelta(days=d) for d in (5, 6)}  # Wed, Tue: Mon to Fri all shipped
    assert ist.streaks(shipped, mon, first) == (5, 5)
    assert ist.streaks(shipped | {mon - timedelta(days=1)}, mon, first) == (6, 6)  # a Sunday ship extends it
    assert ist.streaks(set(), mon, first) == (0, 0)


def test_badges():
    """Closer: 5 closed threads; Reviewer: 5 reviews; On a roll: a 5-day run; Lean: weighted tokens per merged PR
    below the median of the 4 windows before."""
    prev = [dict(pr_merged=2, wtok=40e6), dict(pr_merged=1, wtok=30e6), dict(wtok=10e6), dict(pr_merged=4, wtok=40e6)]
    cur = dict(thread_closed=5, review=4, pr_merged=2, wtok=30e6)  # 15M per PR; median before: 20M
    b = {x["id"]: x for x in ist.badges(cur, prev, streak=5)}
    assert b["closer"]["earned"] and b["streak"]["earned"] and b["lean"]["earned"]
    assert not b["reviewer"]["earned"] and b["reviewer"]["progress"] == 4
    b = {x["id"]: x for x in ist.badges(dict(pr_merged=1, wtok=50e6), prev, streak=0)}
    assert not b["lean"]["earned"] and not b["closer"]["earned"] and b["closer"]["progress"] == 0
    b = {x["id"]: x for x in ist.badges(dict(wtok=5e6), prev, streak=0)}
    assert not b["lean"]["earned"] and b["lean"]["detail"] == "no merged PR in the last 7 days"


def test_deployed_lines_count_only_the_default_branch():
    """Deployed lines are the lines added in commits on the default branch: a merged branch counts, an unmerged
    branch does not, a merge commit adds nothing of its own, and lock files do not count."""
    import subprocess
    from pensieve import repos
    root = tempfile.mkdtemp()
    env = dict(os.environ, GIT_AUTHOR_NAME="Sam", GIT_AUTHOR_EMAIL="Sam@example.com", GIT_COMMITTER_NAME="Sam",
               GIT_COMMITTER_EMAIL="sam@example.com")

    def run(*a):
        subprocess.run(["git", "-C", root, *a], check=True, capture_output=True, env=env)

    def commit(path, lines, msg):
        Path(root, path).parent.mkdir(parents=True, exist_ok=True)
        Path(root, path).write_text("".join(f"line {i}\n" for i in range(lines)))
        run("add", path)
        run("commit", "-m", msg)
    run("init", "-q", "-b", "main")
    commit("a.py", 3, "first")
    commit("uv.lock", 50, "lock")  # 0 counted lines
    run("checkout", "-q", "-b", "feature")
    commit("b.py", 5, "merged later")
    run("checkout", "-q", "main")
    run("merge", "-q", "--no-ff", "feature", "-m", "merge feature")
    run("checkout", "-q", "-b", "wip")
    commit("c.py", 7, "never merged")
    run("checkout", "-q", "main")
    ref = repos.deploy_ref(root)
    assert ref == "refs/heads/main"
    rows = repos.deployed_commits(root, ref, 30)
    assert sorted(n for _t, _e, n in rows) == [0, 3, 5]
    assert {e for _t, e, _n in rows} == {"sam@example.com"}
    assert ist.lines_per_m(8, 4e6) == 2.0 and ist.lines_per_m(8, 1000) == 8.0 and ist.lines_per_m(8, 0) is None


def test_risk():
    """Risk grows with the share by inactive authors, the age past the limit, and no active owner; it stays <= 1."""
    a = dict(departed_share=1.0, age_days=400, bus_factor=0)
    assert ist.risk(a, 90) == 1.0
    assert ist.risk(dict(departed_share=0.0, age_days=90, bus_factor=2), 90) == 0.125
    assert ist.risk(dict(departed_share=0.5, age_days=None, bus_factor=1), 90) == 0.3


def test_achievements():
    a = {x["id"]: x for x in ist.achievements(closed=3, last_week=2, n_open=1, close_days=3)}
    assert a["first"]["earned"] and not a["closer"]["earned"] and a["closer"]["progress"] == 3
    assert a["beat"]["earned"] and a["beat"]["goal"] == 3
    assert a["daily"]["earned"]
    assert not a["clean"]["earned"] and a["clean"]["goal"] == 4, "one thread is still in progress"
    a = {x["id"]: x for x in ist.achievements(closed=0, last_week=0, n_open=0, close_days=0)}
    assert not a["clean"]["earned"], "a clean slate needs a close"
    assert not a["beat"]["earned"] and a["beat"]["goal"] == 1


def test_week():
    """Closes since Monday count; open threads and loose ends come fewest steps first, with no known step last."""
    now = time.mktime((2026, 10, 21, 12, 0, 0, 0, 0, -1))  # a Wednesday
    monday = time.mktime((2026, 10, 19, 9, 0, 0, 0, 0, -1))
    step = lambda n: [dict(text=f"step {i}", how="agent") for i in range(n)]  # noqa: E731
    cards = [
        ("a", "complete", monday, dict(title="A")),
        ("b", "complete", monday + 86400, dict(title="B")),
        ("c", "complete", monday - 3 * 86400, dict(title="C")),  # last week
        ("d", "open", now - 60, dict(title="D", left=step(3))),
        ("e", "open", now - 60, dict(title="E", left=step(1))),
        ("f", "open", now - 60, dict(title="F")),
        ("g", "loose_ends", now - 120, dict(title="G", left=step(1))),
        ("h", "todo", now - 60, dict(title="H", left=step(1))),
    ]
    w = ist.week(cards, now)
    assert w["closed"] == 2 and w["last_week"] == 1 and w["best_week"] == 2
    assert [t["id"] for t in w["open"]] == ["e", "g", "d", "f"] and w["n_open"] == 4
    assert w["per_day"][:3] == [1, 1, 0]
    assert {x["id"]: x["earned"] for x in w["achievements"]}["beat"]


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t0 = time.time()
        t()
        print(f"ok   {t.__name__}  {round((time.time() - t0) * 1000)} ms")
    print(f"{len(tests)} passed")
