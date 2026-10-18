"""Org evidence that builds up over time, in people/org_evidence.json.

One build of the people graph sees only a part of the org: the last pull requests of each repo, the Slack titles that
the connection returns now, the teams that the clustering finds this time. This store keeps what each build saw, so
that the org chart can use all of it:

- prs: each pull request once, by its URL (author, reviewers, approvers, time). A pull request that a later build reads
  again adds no count. Its weight halves each HALF_LIFE_DAYS, and pull requests older than KEEP_DAYS go.
- titles: each identity's titles, with the first and last build that saw it and the number of builds.
- managers: the titles with a manager word, with the same counts (manager-title sightings).
- first: the first commit of each identity (the earliest that any build saw).
- teams: how often two people were in the same inferred team. Each build adds 1 to each pair, and the old counts
  decay with the same half-life.

A build counts once: building again from the same raw data (after a merge or a split) does not add sightings or team
counts. Pull requests and first commits are idempotent anyway."""
import json
from pathlib import Path

HALF_LIFE_DAYS = 365
KEEP_DAYS = 3 * 365
MAX_PRS = 20000
MAX_PAIRS = 60000
DAY = 86400


def decay(ts: float, now: float) -> float:
    return 0.5 ** (max(0.0, now - (ts or now)) / DAY / HALF_LIFE_DAYS)


def empty() -> dict:
    return dict(v=1, since=0, updated=0, builds=0, last_build=0, prs={}, titles={}, managers={}, first={}, teams={},
                teams_at=0)


def load(path: Path) -> dict:
    try:
        ev = json.loads(Path(path).read_text())
        if ev.get("v") == 1:
            return {**empty(), **ev}
    except (OSError, ValueError):
        pass
    return empty()


def save(path: Path, ev: dict):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(ev))
    tmp.replace(path)


def _title_key(keys: list[str]) -> str:
    return next((k for k in keys if k.startswith("slack:")), keys[0] if keys else "")


def merge(ev: dict, raw: dict, now: float, is_manager_title) -> bool:
    """Add one raw build's evidence to the store. True when this raw build is new to the store (then sightings
    count)."""
    built = raw.get("built") or now
    new = built > ev.get("last_build", 0) + 1
    prs = ev["prs"]
    for p in raw.get("pr_log") or []:
        url = p.get("url")
        if not url:
            continue
        rec = dict(repo=p.get("repo", ""), ts=p.get("ts") or 0, author=(p.get("author") or "").lower(),
                   reviewers=sorted({x.lower() for x in p.get("reviewers") or []}),
                   approvers=sorted({x.lower() for x in p.get("approvers") or []}))
        cur = prs.get(url)
        if cur:
            cur["ts"] = max(cur["ts"], rec["ts"])
            cur["reviewers"] = sorted(set(cur["reviewers"]) | set(rec["reviewers"]))
            cur["approvers"] = sorted(set(cur["approvers"]) | set(rec["approvers"]))
        else:
            prs[url] = rec
    if new:
        for ob in raw.get("ids") or []:
            t = (ob.get("title") or "").strip()
            k = _title_key(ob.get("keys") or [])
            if not t or not k:
                continue
            seen = ev["titles"].setdefault(k, {})
            s = seen.setdefault(t, {"first": built, "last": 0, "n": 0})
            if s["last"] < built:
                s["last"] = built
                s["n"] += 1
            if is_manager_title(t):
                m = ev["managers"].setdefault(k, {})
                ms = m.setdefault(t, {"first": built, "last": 0, "n": 0})
                if ms["last"] < built:
                    ms["last"] = built
                    ms["n"] += 1
    for k, w in (raw.get("work") or {}).items():
        for v in (w.get("repos") or {}).values():
            f = v.get("first") or 0
            if f and (not ev["first"].get(k) or f < ev["first"][k]):
                ev["first"][k] = f
    old = now - KEEP_DAYS * DAY
    keep = [(u, p) for u, p in prs.items() if p["ts"] >= old or not p["ts"]]
    if len(keep) > MAX_PRS:
        keep = sorted(keep, key=lambda up: -up[1]["ts"])[:MAX_PRS]
    ev["prs"] = dict(keep)
    if new:
        ev["builds"] = ev.get("builds", 0) + 1
        ev["last_build"] = built
    ev["since"] = min(ev.get("since") or built, built)
    ev["updated"] = now
    return new


def add_teams(ev: dict, teams: list[dict], anchor, now: float, new: bool):
    """Count, once for each new build, the pairs of people in the same inferred team. `anchor(pid)` gives a stable
    key for a person (an identity key, not the person id, which can change)."""
    if not new:
        return
    f = decay(ev.get("teams_at") or now, now)
    pairs = {k: round(v * f, 3) for k, v in ev["teams"].items() if v * f >= 0.05}
    for t in teams:
        ms = sorted({anchor(m) for m in t["members"]} - {""})
        for i, a in enumerate(ms):
            for b in ms[i + 1:]:
                k = a + "\t" + b
                pairs[k] = round(pairs.get(k, 0) + 1, 3)
    if len(pairs) > MAX_PAIRS:
        pairs = dict(sorted(pairs.items(), key=lambda kv: -kv[1])[:MAX_PAIRS])
    ev["teams"] = pairs
    ev["teams_at"] = now


def summary(ev: dict) -> dict:
    """How much evidence the org chart has: for the Org view and the relationship panel."""
    approvals = sum(len(p["approvers"]) for p in ev["prs"].values())
    reviews = sum(len(p["reviewers"]) for p in ev["prs"].values())
    return dict(prs=len(ev["prs"]), approvals=approvals, reviews=reviews,
                titles=sum(len(v) for v in ev["titles"].values()), people_with_titles=len(ev["titles"]),
                manager_titles=sum(len(v) for v in ev["managers"].values()), first_commits=len(ev["first"]),
                team_pairs=len(ev["teams"]), builds=ev.get("builds", 0), since=ev.get("since") or 0,
                updated=ev.get("updated") or 0)
