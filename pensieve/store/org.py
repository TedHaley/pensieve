"""Org evidence that builds up over the People builds (store.db; v1: people/org_evidence.json).

orgstore.py merges each build into the evidence and decays it; this module reads the evidence as the dict that
orgstore and org.py use, and writes it back:
- org_pull_requests and org_reviews: each pull request once, with its reviewers ('commented') and approvers
  ('approved'). Logins are kept as `gh:<login>` keys.
- org_titles: each identity's titles, with the first and last build that saw it and the number of builds.
- org_first_commits: the first commit of each identity.
- org_manager_titles: the titles with a manager word, with the same counts (manager-title sightings).
- org_team_pairs: how often two people (by an identity key) were in the same inferred team. All weights decay
  together at org_state.teams_at; `updated_at` is that time when the pair was written.
- org_state: one row with the build counts and times."""
from pensieve.models import (OrgFirstCommit, OrgManagerTitle, OrgPullRequest, OrgReview, OrgState, OrgTeamPair,
                             OrgTitle, ReviewState)
from pensieve.store import rows
from pensieve.store.db import Db


def _login(key: str | None) -> str:
    return (key or "")[3:] if (key or "").startswith("gh:") else (key or "")


def load(db: Db) -> dict:
    """The evidence in orgstore's shape: {since, updated, builds, last_build, prs, titles, managers, first, teams,
    teams_at}."""
    ev = dict(v=1, since=0, updated=0, builds=0, last_build=0, prs={}, titles={}, managers={}, first={}, teams={},
              teams_at=0)
    c = db.conn
    st = rows.select_one(c, OrgState, "id = 1")
    if st:
        ev.update(since=st.since, updated=st.updated, builds=st.builds, last_build=st.last_build, teams_at=st.teams_at)
    prs = ev["prs"]
    for p in rows.select(c, OrgPullRequest):
        prs[p.url] = dict(repo=p.repo, ts=p.updated_at or 0, author=_login(p.author_key), reviewers=[], approvers=[])
    for r in rows.select(c, OrgReview, order="url, reviewer_key"):
        p = prs.get(r.url)
        if p is None:
            continue
        login = _login(r.reviewer_key)
        if login not in p["reviewers"]:
            p["reviewers"].append(login)
        if r.state == ReviewState.APPROVED:
            p["approvers"].append(login)
    for t in rows.select(c, OrgTitle, order="key, first_seen"):
        ev["titles"].setdefault(t.key, {})[t.title] = {"first": t.first_seen, "last": t.last_seen, "n": t.builds}
    for t in rows.select(c, OrgManagerTitle, order="key, first_seen"):
        ev["managers"].setdefault(t.key, {})[t.title] = {"first": t.first_seen, "last": t.last_seen, "n": t.builds}
    ev["first"] = {f.key: f.at for f in rows.select(c, OrgFirstCommit)}
    ev["teams"] = {f"{t.a_key}\t{t.b_key}": t.weight for t in rows.select(c, OrgTeamPair)}
    return ev


def _rows(ev: dict):
    prs, reviews = [], []
    for url, p in ev.get("prs", {}).items():
        ts = int(p.get("ts") or 0) or None
        prs.append(OrgPullRequest(url, p.get("repo") or "", "gh:" + p["author"] if p.get("author") else None, None, ts))
        approvers = set(p.get("approvers") or [])
        for x in sorted(set(p.get("reviewers") or []) | approvers):
            reviews.append(OrgReview(url, "gh:" + x, ReviewState.APPROVED if x in approvers else ReviewState.COMMENTED,
                                     None))
    titles = [OrgTitle(k, t, int(s["first"]), int(s["last"]), int(s["n"]))
              for k, seen in ev.get("titles", {}).items() for t, s in seen.items()]
    managers = [OrgManagerTitle(k, t, int(s["first"]), int(s["last"]), int(s["n"]))
                for k, seen in ev.get("managers", {}).items() for t, s in seen.items()]
    first = [OrgFirstCommit(k, int(at)) for k, at in ev.get("first", {}).items() if at]
    teams_at = int(ev.get("teams_at") or 0)
    pairs = []
    for k, n in ev.get("teams", {}).items():
        a, _, b = k.partition("\t")
        if a and b:
            pairs.append(OrgTeamPair(a, b, float(n), teams_at))
    st = OrgState(1, int(ev.get("since") or 0), int(ev.get("updated") or 0), int(ev.get("builds") or 0),
                  int(ev.get("last_build") or 0), teams_at)
    return prs, reviews, titles, managers, first, pairs, st


TABLES = ("org_reviews", "org_pull_requests", "org_titles", "org_manager_titles", "org_first_commits",
          "org_team_pairs", "org_state")


def save(db: Db, ev: dict):
    """Replace the stored evidence with `ev` (one build's merge of it, by orgstore.merge and add_teams)."""
    parts = _rows(ev)

    def run(c):
        for t in TABLES:
            c.execute(f"DELETE FROM {t}")
        for p in parts[:-1]:
            rows.insert_many(c, p)
        rows.insert(c, parts[-1])
    db.write(run)


def import_evidence(db: Db, ev: dict):
    """For the v1 import: the content of people/org_evidence.json (orgstore's shape, with managers and teams).
    It replaces what is stored."""
    save(db, ev)


def import_manager_titles(db: Db, managers: dict):
    """For the v1 import: org_evidence.json's `managers` ({key: {title: {first, last, n}}}), added or replaced."""
    ms = [OrgManagerTitle(k, t, int(s["first"]), int(s["last"]), int(s["n"]))
          for k, seen in managers.items() for t, s in seen.items()]
    db.write(lambda c: rows.insert_many(c, ms, or_="REPLACE"))


def import_team_pairs(db: Db, teams: dict, teams_at: int):
    """For the v1 import: org_evidence.json's `teams` ({"a<TAB>b": weight}) and `teams_at`, added or replaced."""
    ps = [OrgTeamPair(*k.split("\t", 1), float(n), int(teams_at)) for k, n in teams.items() if "\t" in k]
    db.write(lambda c: rows.insert_many(c, ps, or_="REPLACE"))
