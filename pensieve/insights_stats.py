"""The numbers of the Insights tab, made without AI: token use, the output it bought, a weekly value score with
streaks and badges, the user's output next to their nearest people, threads and repos with momentum or stalled, and
knowledge gaps for a manager.

Windows are rolling 7-day windows that end now (window 0 is the last 7 days), so a Monday is not compared with a
whole week. "Days" are local days of this Mac.

Value per token:
- Weighted tokens = input + 1.25 x cache writes + 0.1 x cache reads + 5 x output. These are the ratios of the list
  price of every current Claude model, so a cache read costs a tenth of a fresh input token, whatever the model.
- Points = 10 a merged pull request + 5 a closed thread + 3 a done ticket + 2 a review given + 1 a commit (at most
  30 a window) + 1 an item of a thread done by a commit.
- Score = 100 x points / weighted tokens in millions (at least 1 million, so a quiet week scores no more than its
  points). The rule functions (`weighted`, `points`, `score`, `streaks`, `badges`) take plain numbers, for tests."""
import json
import math
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from pensieve import tokens
from pensieve.store import insights as q
from pensieve.store import tokens as tokens_db

DAY, WEEK = 86400, 7 * 86400
WINDOWS = 8
WEIGHTS = {"input": 1.0, "cache_write": 1.25, "cache_read": 0.1, "output": 5.0}
POINTS = {"pr_merged": 10, "thread_closed": 5, "ticket_done": 3, "review": 2, "commit": 1, "item_closed": 1}
COMMIT_CAP = 30
SCORE_FLOOR = 1e6  # weighted tokens: a window that used less counts as 1 million
BEST_SHARE = 0.25  # a personal best counts only windows with at least this share of the median window's tokens
DONE = re.compile(r"\b(done|closed|resolved|complete|completed|shipped|released)\b", re.I)
PEERS = 10

FORMULA = ("Score = 100 × points ÷ weighted tokens (millions, at least 1). Points: merged PR 10, closed thread 5, "
           "done ticket 3, review given 2, commit 1 (at most 30), thread item done 1. Weighted tokens = input + "
           "1.25 × cache writes + 0.1 × cache reads + 5 × output (the ratios of Claude list prices). A personal "
           "best counts only weeks with at least a quarter of your usual tokens.")
LINES_FORMULA = ("Deployed lines per M tokens = lines you added in commits that reached the default branch (on "
                 "origin: merged PRs or commits pushed to main; not other branches) ÷ weighted tokens in millions (at "
                 "least 1). Lock files, minified files and generated folders don't count; merge commits don't either.")


def weighted(input: int = 0, output: int = 0, cache_write: int = 0, cache_read: int = 0) -> float:
    return (input * WEIGHTS["input"] + output * WEIGHTS["output"] + cache_write * WEIGHTS["cache_write"]
            + cache_read * WEIGHTS["cache_read"])


def points(counts: dict) -> int:
    """The value points of a window's output counts (keys of POINTS; commits count up to COMMIT_CAP)."""
    out = 0
    for k, w in POINTS.items():
        n = int(counts.get(k) or 0)
        out += w * (min(n, COMMIT_CAP) if k == "commit" else n)
    return out


def score(pts: float, wtok: float) -> int | None:
    """100 x points per million weighted tokens; None for a window with no tokens."""
    if not wtok or wtok <= 0:
        return None
    return round(100 * pts / (max(wtok, SCORE_FLOOR) / 1e6))


def lines_per_m(lines: int, wtok: float) -> float | None:
    """Deployed lines per million weighted tokens (at least 1 million, as for the score); None with no tokens."""
    if not wtok or wtok <= 0:
        return None
    return round(lines / (max(wtok, SCORE_FLOOR) / 1e6), 1)


def median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def streaks(days: set[date], today: date, first: date) -> tuple[int, int]:
    """(current, best) runs of days with a shipped item. Weekend days extend a run when something shipped and
    never break one; today does not break the current run before it ends."""
    def run_back(d):
        n = 0
        while d >= first:
            if d in days:
                n += 1
            elif d.weekday() < 5 and d != today:
                break
            d -= timedelta(days=1)
        return n
    cur = run_back(today)
    best, n, d = 0, 0, first
    while d <= today:
        if d in days:
            n += 1
        elif d.weekday() < 5 and d != today:
            n = 0
        best = max(best, n)
        d += timedelta(days=1)
    return cur, max(best, cur)


BADGES = [
    dict(id="closer", name="Closer", rule="Close 5 threads in the last 7 days.", goal=5),
    dict(id="lean", name="Lean", rule="Merge a PR, with fewer weighted tokens per merged PR than your median of "
                                      "the 4 weeks before.", goal=1),
    dict(id="reviewer", name="Reviewer", rule="Give 5 reviews in the last 7 days.", goal=5),
    dict(id="streak", name="On a roll", rule="Ship (a merged PR or a closed thread) 5 days in a row. Weekends "
                                             "don't break a run.", goal=5),
]


def badges(cur: dict, prev: list[dict], streak: int) -> list[dict]:
    """Each badge with whether it is earned and the progress to it. `cur` and each of `prev` (the 4 windows before)
    hold the counts of a window and `wtok`, its weighted tokens."""
    def per_pr(w):
        return w["wtok"] / w["pr_merged"] if w.get("pr_merged") else None
    base = median([per_pr(w) for w in prev])
    mine = per_pr(cur)
    lean = mine is not None and base is not None and mine < base
    have = {"closer": cur.get("thread_closed", 0), "reviewer": cur.get("review", 0), "streak": streak,
            "lean": 1 if lean else 0}
    out = []
    for b in BADGES:
        n = have[b["id"]]
        d = dict(b, earned=n >= b["goal"], progress=min(n, b["goal"]))
        if b["id"] == "lean":
            d["detail"] = (f"{_short(mine)} per PR vs {_short(base)}" if mine is not None and base is not None
                           else "no merged PR in the last 7 days" if mine is None else "no PRs in the 4 weeks before")
        out.append(d)
    return out


ACHIEVEMENTS = [
    dict(id="first", name="First close", rule="Close a thread this week."),
    dict(id="closer", name="Closer", rule="Close 5 threads this week."),
    dict(id="beat", name="Beat last week", rule="Close more threads than last week."),
    dict(id="daily", name="Daily closer", rule="Close a thread on 3 different days this week."),
    dict(id="clean", name="Clean slate", rule="Close a thread, and leave no thread in progress."),
]


def achievements(closed: int, last_week: int, n_open: int, close_days: int) -> list[dict]:
    """The weekly achievements, each with whether it is earned and the progress to it. They count threads closed
    since Monday: `close_days` is the number of days with a close, `n_open` the threads still in progress."""
    goal = {"first": 1, "closer": 5, "beat": last_week + 1, "daily": 3, "clean": max(1, closed + n_open)}
    have = {"first": closed, "closer": closed, "beat": closed, "daily": close_days, "clean": closed}
    out = []
    for a in ACHIEVEMENTS:
        g, n = goal[a["id"]], have[a["id"]]
        earned = n >= g and (a["id"] != "clean" or (n_open == 0 and closed > 0))
        out.append(dict(a, goal=g, progress=min(n, g), earned=earned))
    return out


def closest_first(n_left: int, updated: float) -> tuple:
    """The order of "Closest to done" (Insights, and the closest_to_done MCP tool): the fewest steps to a close first,
    then the newest. A thread with no known step is last, because Pensieve cannot tell what closes it."""
    return n_left == 0, n_left, -updated


def week(cards: list, now: float) -> dict:
    """This week (since Monday, local time): the threads closed, the threads in progress that are left to close
    (fewest items left first), and the achievements. `cards` are (id, state, updated, view) rows."""
    today = date.fromtimestamp(now)
    start = datetime.combine(today - timedelta(days=today.weekday()), datetime.min.time()).timestamp()
    weeks = Counter()  # weeks before this one -> threads closed
    closed, days, open_ = [], set(), []
    for cid, state, updated, v in cards:
        title = (v.get("anchor") or {}).get("title") or v.get("title") or cid  # the Threads tab's title
        if state in ("closed", "complete"):  # "complete" is the name before the rename
            if updated >= start:
                closed.append(dict(id=cid, title=title, at=updated))
                days.add(date.fromtimestamp(updated))
            else:
                weeks[int((start - updated) // WEEK) + 1] += 1
        elif state in ("open", "loose_ends"):
            left = v.get("left") or []
            open_.append(dict(id=cid, title=title, updated=updated, n_left=len(left),
                              left=[dict(text=x.get("text", ""), how=x.get("how", "")) for x in left][:3],
                              repos=[r.rsplit("/", 1)[-1] for r in v.get("repos", [])][:2]))
    open_.sort(key=lambda t: closest_first(t["n_left"], t["updated"]))
    closed.sort(key=lambda t: -t["at"])
    per_day = [0] * 7
    for c in closed:
        per_day[date.fromtimestamp(c["at"]).weekday()] += 1
    return dict(start=start, closed=len(closed), closed_threads=closed[:8], last_week=weeks[1],
                best_week=max([weeks[k] for k in (1, 2, 3)] + [len(closed)]), per_day=per_day,
                open=open_[:8], n_open=len(open_),
                achievements=achievements(len(closed), weeks[1], len(open_), len(days)))


def _short(n):
    if n is None:
        return "—"
    for div, s in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= div:
            return f"{n / div:.1f}{s}"
    return str(round(n))


def _iso(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() if ts else 0.0
    except ValueError:
        return 0.0


def _window(t: float, now: float) -> int | None:
    k = int((now - t) // WEEK)
    return k if 0 <= k < WINDOWS and t <= now else None


def _me(graph: dict | None):
    people = (graph or {}).get("people", {})
    me = next((p for p in people.values() if p.get("me")), None)
    keys = me["keys"] if me else []
    return me, {k[6:] for k in keys if k.startswith("email:")}, {k[3:].lower() for k in keys if k.startswith("gh:")}, keys


def _prs(idx) -> list[dict]:
    from pensieve import people
    seen, out = set(), []
    for _root, name, remote in q.repos(idx):
        for p in people.cached_prs(remote):
            if p.get("url") and p["url"] not in seen:
                seen.add(p["url"])
                out.append(dict(p, repo=name))
    return out


def _merged_at(p) -> float:
    return p.get("merged") or (p.get("ts") if p.get("state") == "merged" else 0) or 0


def token_stats(idx, now: float, sessions: set | None = None) -> dict:
    """Token use per window, per day and per model; with `sessions`, only those sessions' tokens."""
    since = int(now - WINDOWS * WEEK) // 3600 * 3600
    recent_since = int(now - 4 * WEEK)
    win = [Counter() for _ in range(WINDOWS)]
    days = defaultdict(Counter)
    by_model, recent_by_model, model_sessions = defaultdict(Counter), defaultdict(Counter), defaultdict(set)
    for sid, hour, model, calls, i, o, cw, cr, cw1 in tokens_db.rows_since(idx, 0):
        if sessions is not None and sid not in sessions:
            continue
        rec = dict(calls=calls, input=i, output=o, cache_write=cw, cache_read=cr, cache_write_1h=cw1)
        by_model[model].update(rec)
        model_sessions[model].add(sid)
        if hour >= recent_since:
            recent_by_model[model].update(rec)
        if hour < since:
            continue
        c = tokens.cost(model, i, o, cw, cr, cw1)
        rec.update(cost=c or 0.0, unpriced=(i + o + cw + cr) if c is None else 0)
        k = _window(hour + 1800, now)
        if k is not None:
            win[k].update(rec)
        days[datetime.fromtimestamp(hour).date().isoformat()].update(rec)

    def model_row(model, x):
        c = tokens.cost(model, x["input"], x["output"], x["cache_write"], x["cache_read"], x["cache_write_1h"])
        return dict(model=model, calls=x["calls"], input=x["input"], output=x["output"], cache_write=x["cache_write"],
                    cache_read=x["cache_read"], total=x["input"] + x["output"] + x["cache_write"] + x["cache_read"],
                    cost=None if c is None else round(c, 2))
    models = [dict(model_row(m, x), sessions=len(model_sessions[m])) for m, x in by_model.items()]
    models.sort(key=lambda m: (-(m["cost"] or 0), -m["total"]))
    recent = sorted((model_row(m, x) for m, x in recent_by_model.items()), key=lambda m: -m["total"])
    start = date.fromtimestamp(now) - timedelta(days=WINDOWS * 7 - 1)
    series = []
    for j in range(WINDOWS * 7):
        d = (start + timedelta(days=j)).isoformat()
        x = days.get(d, Counter())
        series.append(dict(day=d, input=x["input"], output=x["output"], cache_write=x["cache_write"],
                           cache_read=x["cache_read"], cost=round(x["cost"], 2)))
    out_w = []
    for w in win:
        total = w["input"] + w["output"] + w["cache_write"] + w["cache_read"]
        side = w["input"] + w["cache_write"] + w["cache_read"]
        out_w.append(dict(total=total, input=w["input"], output=w["output"], cache_write=w["cache_write"],
                          cache_read=w["cache_read"], calls=w["calls"], cost=round(w["cost"], 2),
                          unpriced=w["unpriced"], cache_share=round(w["cache_read"] / side, 4) if side else None,
                          wtok=weighted(w["input"], w["output"], w["cache_write"], w["cache_read"])))
    return dict(windows=out_w, days=series, models=models, recent_models=recent,
                all_total=sum(m["total"] for m in models), all_cost=round(sum(m["cost"] or 0 for m in models), 2),
                prices_as_of=tokens.PRICES_AS_OF,
                note="Estimated at Anthropic API list prices; a subscription bills differently. Other agents' "
                     "models have no price here.")


def output_events(idx, store, graph, now: float, prs: list[dict], roots: set | None = None,
                  deployed: dict | None = None) -> tuple[list, dict]:
    """The user's output as (kind, time, ref) events over the windows, and what each source had. With `roots`, only
    what belongs to those repos: their commits, and the threads (and their tickets and items) in them. `prs` are
    filtered by the caller. `deployed` (repo name -> repos.deployed rows) gives ("deployed", time, lines) events."""
    me, emails, logins, keys = _me(graph)
    since = now - WINDOWS * WEEK
    ev = []
    for author, repo, ts in q.commits(idx, int(since)):
        if author in emails and (roots is None or repo in roots):
            ev.append(("commit", ts, repo))
    for _name, rows in (deployed or {}).items():
        for ts, email, lines in rows:
            if email in emails and ts >= since and lines:
                ev.append(("deployed", ts, lines))
    for p in prs:
        login = (p.get("author") or {}).get("login", "").lower()
        if login in logins:
            m = _merged_at(p)
            if m and m >= since:
                ev.append(("pr_merged", m, p["url"]))
            if p.get("created") and p["created"] >= since:
                ev.append(("pr_opened", p["created"], p["url"]))
        else:
            at = next((t for who, t in (p.get("review_at") or {}).items() if who.lower() in logins), None)
            if at is None and "review_at" not in p and any(r.lower() in logins for r in p.get("reviewers") or []):
                at = p.get("ts")  # an older cache without review times: the PR's last update
            if at and at >= since:
                ev.append(("review", at, p["url"]))
    cards = []
    for cid, state, updated, view in q.cards(store):
        try:
            v = json.loads(view)
        except ValueError:
            continue
        if roots is not None and not roots & set(v.get("repos") or []):
            continue
        cards.append((cid, state, updated, v))
        if state in ("closed", "complete") and updated >= since:
            ev.append(("thread_closed", updated, cid))
    card_ids = {c[0] for c in cards}
    card_tickets = {k.get("key") for c in cards for k in c[3].get("tickets", [])}
    if not prs:  # no gh: the merged pull requests of the user's own threads
        seen = set()
        for _cid, _st, _u, v in cards:
            for p in v.get("prs", []):
                if p.get("own") and p.get("merged_at") and p["url"] not in seen and p["merged_at"] >= since:
                    seen.add(p["url"])
                    ev.append(("pr_merged", p["merged_at"], p["url"]))
    for key, status, at in q.tickets_done(store, list(keys), int(since)):
        if DONE.search(status) and (roots is None or key in card_tickets):
            ev.append(("ticket_done", at, key))
    for cid, at in q.items_closed(store, int(since)):
        if roots is None or cid in card_ids:
            ev.append(("item_closed", at, cid))
    return ev, dict(me=me, emails=emails, logins=logins, cards=cards, gh=bool(prs))


def game(tok: dict, ev: list, now: float) -> dict:
    wins = [Counter() for _ in range(WINDOWS)]
    for kind, t, ref in ev:
        k = _window(t, now)
        if k is not None:
            wins[k][kind] += ref if kind == "deployed" else 1
    rows = []
    for k in range(WINDOWS):
        c = dict(wins[k])
        wt = tok["windows"][k]["wtok"]
        pts = points(c)
        rows.append(dict(c, points=pts, wtok=wt, score=score(pts, wt), tokens=tok["windows"][k]["total"],
                         lines=c.get("deployed", 0), lines_per_m=lines_per_m(c.get("deployed", 0), wt)))
    today = date.fromtimestamp(now)
    first = today - timedelta(days=WINDOWS * 7 - 1)
    shipped = {date.fromtimestamp(t) for kind, t, _ in ev if kind in ("pr_merged", "thread_closed")}
    cur_streak, best_streak = streaks(shipped, today, first)
    cur, prev = rows[0], rows[1:5]
    usual = median([r["wtok"] for r in rows if r["wtok"]])
    for r in rows:  # a window with a sliver of the usual tokens has a score that no real week can reach
        r["counts"] = bool(r["score"] is not None and usual and r["wtok"] >= BEST_SHARE * usual)
    scores = [r["score"] for r in rows if r["counts"]]
    best = max(scores) if scores else None
    best_at = next((k for k, r in enumerate(rows) if r["counts"] and r["score"] == best), None)
    lpm = [r["lines_per_m"] for r in rows if r["counts"] and r["lines_per_m"] is not None]
    lines_best = max(lpm) if lpm else None
    return dict(windows=rows, score=cur["score"], prev_median=median([r["score"] for r in prev]), best=best,
                best_window=best_at, points=cur["points"], streak=cur_streak, best_streak=best_streak,
                badges=badges(cur, prev, cur_streak), formula=FORMULA, weights=WEIGHTS, point_values=POINTS,
                commit_cap=COMMIT_CAP,
                lines=dict(value=cur["lines_per_m"], lines=cur["lines"], prev_median=median([r["lines_per_m"] for r in prev]),
                           best=lines_best, best_window=next((k for k, r in enumerate(rows) if r["counts"]
                                                              and r["lines_per_m"] == lines_best), None),
                           formula=LINES_FORMULA))


def kpis(tok: dict, g: dict) -> list[dict]:
    w, rows = tok["windows"], g["windows"]

    def tile(key, label, cur, prev, better="up", fmt="n", hint=""):
        base = median(prev)
        delta = None if cur is None or not base else (cur - base) / base
        return dict(key=key, label=label, value=cur, base=base, delta=delta, better=better, fmt=fmt, hint=hint)

    def per(kind, k):
        n = rows[k].get(kind, 0)
        return rows[k]["wtok"] / n if n and rows[k]["wtok"] else None
    return [
        tile("tokens", "Tokens", w[0]["total"], [x["total"] for x in w[1:5]], "none", "tok",
             "All tokens of the last 7 days: input, output, cache writes and cache reads."),
        tile("cost", "Estimated cost", w[0]["cost"], [x["cost"] for x in w[1:5]], "down", "usd",
             "At Anthropic API list prices. A subscription bills differently."),
        tile("cache", "Cache-read share", w[0]["cache_share"], [x["cache_share"] for x in w[1:5]], "up", "pct",
             "Cache reads ÷ all input-side tokens. A cache read costs a tenth of a fresh input token."),
        tile("per_commit", "Weighted tokens per commit", per("commit", 0), [per("commit", k) for k in range(1, 5)],
             "down", "tok", ""),
        tile("per_thread", "Weighted tokens per closed thread", per("thread_closed", 0),
             [per("thread_closed", k) for k in range(1, 5)], "down", "tok", ""),
    ]


def peers(idx, graph, prs: list[dict], now: float, days: int = 28, names_sel: set | None = None,
          deployed: dict | None = None) -> dict:
    """Output (never tokens: Pensieve only sees this Mac's sessions) of the user and their nearest people in the
    People graph, over the same window: merged PRs, reviews given, commits, deployed lines, and the median time to
    a first review. With `names_sel`, only those repos. `deployed`: repo name -> repos.deployed rows."""
    people = (graph or {}).get("people", {})
    me = next((p for p in people.values() if p.get("me")), None)
    if not me:
        return dict(window_days=days, metrics=[], people=[])
    since = now - days * DAY
    near = sorted((p for p in people.values() if not p.get("me") and (p.get("closeness") or 0) > 0),
                  key=lambda p: -p["closeness"])[:PEERS]
    group = [me] + near
    by_email, by_login = {}, {}
    for p in group:
        for k in p["keys"]:
            if k.startswith("email:"):
                by_email[k[6:]] = p["id"]
            elif k.startswith("gh:"):
                by_login[k[3:].lower()] = p["id"]
    m = {p["id"]: Counter() for p in group}
    turn = defaultdict(list)
    names = {r: n for r, n, _ in q.repos(idx)}
    commits = [(by_email[a], names.get(r, r)) for a, r, _ts in q.commits(idx, int(since)) if a in by_email]
    recent = [p for p in prs if max(_merged_at(p), p.get("created") or 0, p.get("ts") or 0) >= since]
    # only the repos where a peer works too: the user's own side projects would skew every rank
    shared = {r for pid, r in commits if pid != me["id"]}
    shared |= {p["repo"] for p in recent
               if by_login.get((p.get("author") or {}).get("login", "").lower()) not in (None, me["id"])}
    shared |= {p["repo"] for p in recent if any(by_login.get(w.lower()) not in (None, me["id"])
                                                 for w in list(p.get("review_at") or {}) + (p.get("reviewers") or []))}
    if names_sel is not None:
        shared &= names_sel
    for pid, r in commits:
        if r in shared:
            m[pid]["commits"] += 1
    for name, rows in (deployed or {}).items():
        if name in shared:
            for ts, email, lines in rows:
                if ts >= since and email in by_email:
                    m[by_email[email]]["lines"] += lines
    for p in recent:
        if p["repo"] not in shared:
            continue
        a = by_login.get((p.get("author") or {}).get("login", "").lower())
        if a and _merged_at(p) >= since:
            m[a]["prs_merged"] += 1
        for who, at in (p.get("review_at") or {}).items():
            r = by_login.get(who.lower())
            if r and r != a and at >= since:
                m[r]["reviews"] += 1
                if p.get("created") and at >= p["created"]:
                    turn[r].append((at - p["created"]) / 3600)
        if "review_at" not in p:
            for who in p.get("reviewers") or []:
                r = by_login.get(who.lower())
                if r and r != a and (p.get("ts") or 0) >= since:
                    m[r]["reviews"] += 1
    defs = [("prs_merged", "PRs merged", "up"), ("reviews", "Reviews given", "up"), ("commits", "Commits", "up"),
            ("lines", "Deployed lines", "up"), ("turnaround", "Median hours to first review", "down")]
    vals = {pid: dict(m[pid]) for pid in m}
    for pid, hs in turn.items():
        vals[pid]["turnaround"] = round(statistics.median(hs), 1)
    metrics = []
    for key, label, better in defs:
        if key == "turnaround":
            have = [(pid, v[key]) for pid, v in vals.items() if v.get(key) is not None]
        else:
            have = [(pid, v.get(key, 0)) for pid, v in vals.items()]
        mine = vals[me["id"]].get(key, None if key == "turnaround" else 0)
        others = [v for pid, v in have if pid != me["id"]]
        if mine is None or not others:
            metrics.append(dict(key=key, label=label, better=better, me=mine, rank=None, of=len(have), pct=None,
                                values=sorted(others), median=median(others)))
            continue
        beat = sum(1 for v in others if (v < mine if better == "up" else v > mine))
        ties = sum(1 for v in others if v == mine)
        rank = 1 + sum(1 for v in others if (v > mine if better == "up" else v < mine))
        metrics.append(dict(key=key, label=label, better=better, me=mine, rank=rank, of=len(others) + 1,
                            pct=round(100 * (beat + 0.5 * ties) / len(others)), values=sorted(others),
                            median=median(others)))
    rows = [dict(id=p["id"], name=p["name"], me=bool(p.get("me")), closeness=round(p.get("closeness") or 0, 3),
                 **{k: vals[p["id"]].get(k) for k, _l, _b in defs}) for p in group]
    return dict(window_days=days, metrics=metrics, people=rows, gh=bool(prs), repos=sorted(shared),
                note=f"Output only, over the last {days} days, in the repos where these people work with you "
                     f"({', '.join(sorted(shared)) or 'none'}). Others' token use is not known: Pensieve reads only "
                     "this Mac.")


def momentum(cards: list, idx, ev: list, now: float, sel: set | None = None) -> dict:
    """Threads and repos by their weekly activity: momentum (2+ events this week, at or above the mean of the 3
    weeks before), stalled (quiet 7+ days with something left to do), cooling (some activity in the last 2 weeks),
    or quiet. With `sel` (repo roots), only those repos."""
    nw = 6

    def series(ts_list):
        s = [0] * nw
        for t in ts_list:
            k = int((now - t) // WEEK)
            if 0 <= k < nw and t <= now:
                s[nw - 1 - k] += 1
        return s

    def classify(s, quiet_days, left):
        if s[-1] >= 2 and s[-1] >= sum(s[-4:-1]) / 3:
            return "momentum"
        if quiet_days >= 7 and left:
            return "stalled"
        if s[-1] > 0 or s[-2] > 0:
            return "cooling"
        return "quiet"
    threads, left_by_repo = [], Counter()
    for cid, state, updated, v in cards:
        if state in ("closed", "complete", "closing", "forgotten"):
            continue
        ts = [e.get("ts") for e in v.get("events", []) if e.get("ts")]
        ts += [c.get("ts") for c in v.get("commits", []) if c.get("ts")]
        ts += [p.get("merged_at") for p in v.get("prs", []) if p.get("merged_at")]
        last = max(ts) if ts else updated
        left = v.get("left") or []
        for r in v.get("repos", []):
            left_by_repo[r] += len(left)
        s = series(ts)
        quiet = (now - last) / DAY
        anchor = v.get("anchor") or {}
        threads.append(dict(id=cid, title=v.get("title") or anchor.get("title") or cid, state=state, series=s,
                            status=classify(s, quiet, left), last=last, quiet_days=round(quiet, 1),
                            left=[x.get("text") for x in left][:3], n_left=len(left),
                            repos=[r.rsplit("/", 1)[-1] for r in v.get("repos", [])][:2]))
    roots = q.repos(idx)
    names = {r: n for r, n, _ in roots}
    act = defaultdict(list)  # repo root -> times of the user's commits, sessions and merged PRs there
    for kind, t, ref in ev:
        if kind == "commit":
            act[ref].append(t)
    for _sid, project, started, updated in q.sessions(idx):
        t = _iso(updated) or _iso(started)
        root = next((r for r in names if project == r or project.startswith(r + "/")), None)
        if root and t:
            act[root].append(t)
    url_root = {f"https://github.com/{rm.split('github.com/')[-1]}/": r for r, _n, rm in roots if "github.com/" in rm}
    for kind, t, ref in ev:
        if kind == "pr_merged":
            r = next((root for pre, root in url_root.items() if str(ref).startswith(pre)), None)
            if r:
                act[r].append(t)
    repos_out = []
    for r, ts in act.items():
        s = series(ts)
        if r not in names or not sum(s) or (sel is not None and r not in sel):
            continue
        quiet = (now - max(ts)) / DAY
        repos_out.append(dict(repo=names[r], series=s, status=classify(s, quiet, left_by_repo.get(r, 0)),
                              last=max(ts), quiet_days=round(quiet, 1), n_left=left_by_repo.get(r, 0)))
    order = {"momentum": 0, "stalled": 1, "cooling": 2, "quiet": 3}
    threads.sort(key=lambda x: (order[x["status"]], -x["series"][-1] if x["status"] == "momentum" else x["quiet_days"]))
    repos_out.sort(key=lambda x: (order[x["status"]], -sum(x["series"])))
    return dict(weeks=nw, threads=threads, repos=repos_out, counts=dict(Counter(t["status"] for t in threads)),
                repo_counts=dict(Counter(r["status"] for r in repos_out)))


def repo_sessions(idx, roots: set) -> set:
    """The ids of the sessions that ran in one of `roots` (their working folder is the repo or a folder in it)."""
    out = set()
    for sid, project, _s, _u in q.sessions(idx):
        if any(project == r or project.startswith(r + "/") for r in roots):
            out.add(sid)
    return out


def stats(idx, store, graph, now: float | None = None, repos: list[str] | None = None, deployed_fn=None) -> dict:
    """Every number of the page. `repos` (names) limits it to those repos: their sessions' tokens, their output,
    their threads and the peers' output in them. `deployed_fn(name)` gives a repo's deployed commits
    (Repos.deployed)."""
    now = now or time.time()
    t0 = time.perf_counter()
    all_repos = q.repos(idx)
    names = {n for _r, n, _rm in all_repos}
    sel_names = None if not repos else {n for n in repos if n in names}
    roots = None if sel_names is None else {r for r, n, _rm in all_repos if n in sel_names}
    tok = token_stats(idx, now, None if roots is None else repo_sessions(idx, roots))
    prs = [p for p in _prs(idx) if sel_names is None or p["repo"] in sel_names]
    deployed = {}
    if deployed_fn:
        for n in sorted(names if sel_names is None else sel_names):
            try:
                deployed[n] = deployed_fn(n)
            except Exception:  # a repo git can't read: no deployed lines from it
                deployed[n] = []
    ev, ctx = output_events(idx, store, graph, now, prs, roots, deployed)
    g = game(tok, ev, now)
    out = dict(now=now, tokens=tok, game=g, kpis=kpis(tok, g),
               peers=peers(idx, graph, prs, now, names_sel=sel_names, deployed=deployed),
               momentum=momentum(ctx["cards"], idx, ev, now, roots), week=week(ctx["cards"], now),
               repos=sorted(names), selected=sorted(sel_names) if sel_names is not None else None,
               sources=dict(gh=ctx["gh"], me=bool(ctx["me"]), cards=len(ctx["cards"]), deployed=bool(deployed_fn)))
    out["ms"] = round((time.perf_counter() - t0) * 1000)
    return out


def risk(a: dict, stale_days: int) -> float:
    """0 to 1: the share of an area's lines by inactive authors, its age past `stale_days`, and no active author
    with 10% of it."""
    age = a.get("age_days") or 0
    return round(min(1.0, 0.6 * a["departed_share"] + 0.25 * min(1.0, age / (2 * stale_days))
                     + (0.15 if a["bus_factor"] == 0 else 0)), 3)


def gaps(repos, names: list[str], stale_days: int, inactive_days: int) -> dict:
    """Knowledge gaps over the repos `names`: areas that are stale or whose authors stopped committing, by risk
    (weighted by the area's size), with who still knows each one."""
    out = []
    for name in names:
        h = repos.area_health(name, stale_days, inactive_days)
        for a in (h or {}).get("areas", []):
            a = dict(a, repo=name, risk=risk(a, stale_days))
            a["rank"] = round(a["risk"] * math.log10(max(10, a["lines"])), 4)
            out.append(a)
    out.sort(key=lambda a: -a["rank"])
    return dict(stale_days=stale_days, inactive_days=inactive_days, areas=out[:GAPS_KEEP], counts=_gap_counts(out),
                repos=names)


GAPS_KEEP = 400  # areas kept per build, by rank: a page shows 60 at most


def _gap_counts(areas: list[dict]) -> dict:
    return dict(total=len(areas), stale=sum(1 for a in areas if a["stale"]),
                departed=sum(1 for a in areas if a["departed_share"] >= 0.5),
                no_owner=sum(1 for a in areas if a["bus_factor"] == 0))


def gaps_for(res: dict, names: set) -> dict:
    """A gaps build limited to the repos `names` (the counts are of the kept areas)."""
    areas = [a for a in res.get("areas", []) if a["repo"] in names]
    return dict(res, areas=areas, counts=_gap_counts(areas), repos=[n for n in res.get("repos", []) if n in names])
