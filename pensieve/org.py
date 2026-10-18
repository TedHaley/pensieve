"""The org chart of the People tab. All of it is inferred from what this Mac can see, and each part keeps its evidence.

- Team: people who work in the same repos and areas, talk in the same Slack channels and are in the same GitHub teams
  (from the CODEOWNERS files) form a cluster. A cluster takes the name of the GitHub team that most of its people are
  in, or else of the area that they share.
- Seniority: a Slack title's words (Senior, Staff, Lead, Manager, Director, VP, Head, CTO…) when there is a title.
  Otherwise signals: tenure (the first commit in the repos), breadth (repos and areas), review load (reviews given and
  approvals to other people), ownership (the top committer of areas, the git blame share, CODEOWNERS entries and
  GitHub team maintainers). Without a title the level is never above 'lead', and the confidence stays low.
- Reporting lines only with evidence: a manager's title that names a team ('Engineering Manager, Payments'), or one
  person who gives most of the approvals to a group of people. Otherwise the chart shows teams, not a tree."""
import math
import re
import time
from collections import Counter, defaultdict

LEVELS = ["IC", "senior IC", "lead", "manager", "director+"]
_TITLE = [
    (re.compile(r"\b(chief|c[etfpo]o|vp|svp|evp|vice president|head of|director|founder|president|partner)\b", re.I),
     "director+"),
    (re.compile(r"\b(product|program|project|account|customer success|community|office|marketing) manager\b", re.I),
     ""),
    (re.compile(r"\b(manager|management|em)\b", re.I), "manager"),
    (re.compile(r"\b(lead|tech lead|team lead)\b", re.I), "lead"),
    (re.compile(r"\b(staff|principal|architect|distinguished|fellow)\b", re.I), "senior IC"),
    (re.compile(r"\b(senior|sr\.?|iii|iv)\b", re.I), "senior IC"),
]
YEAR = 365 * 86400
MAX_PEOPLE = 300        # people placed in teams, nearest first
BIG_TEAM = 15          # a cluster larger than this is split again with a higher bar


def _title_level(title: str) -> tuple[str, str]:
    for rx, level in _TITLE:
        m = rx.search(title or "")
        if m:
            return (level or ("senior IC" if re.search(r"\b(senior|sr|lead|principal)\b", title, re.I) else "IC")), m.group(0)
    return "IC", ""


def _cos(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    return dot / (math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values())) or 1)


def infer(people: dict, profiles: dict, work_of: dict, edges: dict, raw: dict) -> dict:
    now = time.time()
    teams_gh = raw.get("teams") or {}
    pid_of_login = {}
    for pid, p in people.items():
        for k in p["keys"]:
            if k.startswith("gh:"):
                pid_of_login[k[3:]] = pid
    gh_of = defaultdict(set)        # person -> GitHub teams they are in
    maint_of = defaultdict(set)     # person -> GitHub teams they maintain
    for slug, t in teams_gh.items():
        for lg in t.get("members", []):
            if lg in pid_of_login:
                gh_of[pid_of_login[lg]].add(slug)
        for lg in t.get("maintainers", []):
            if lg in pid_of_login:
                maint_of[pid_of_login[lg]].add(slug)

    approvals = defaultdict(Counter)  # author -> approver -> approvals
    for pid, w in work_of.items():
        for author_login, n in w["approved"].items():
            a = pid_of_login.get(author_login)
            if a and a != pid:
                approvals[a][pid] += n
    top_approver = {}
    groups = defaultdict(list)
    for author, c in approvals.items():
        tot = sum(c.values())
        a, n = c.most_common(1)[0]
        if tot >= 4 and n / tot >= 0.5:
            top_approver[author] = (a, n, tot)
            groups[a].append(author)

    area_tot = defaultdict(Counter)   # "repo/area" -> person -> commits
    for pid, w in work_of.items():
        for r, v in w["repos"].items():
            for a, n in v["areas"].items():
                area_tot[f"{r}/{a}"][pid] += n
    owns_area = Counter()
    for area, c in area_tot.items():
        tot = sum(c.values())
        top, n = c.most_common(1)[0]
        if n >= 10 and n / tot >= 0.4 and len(c) >= 2:
            owns_area[top] += 1

    out_people = {}
    for pid, p in people.items():
        w = work_of[pid]
        out_people[pid] = dict(id=pid, name=p["name"], function=profiles[pid]["function"],
                               function_inferred=profiles[pid]["function_inferred"],
                               **_seniority(p, w, now, owns_area[pid], maint_of[pid], len(groups.get(pid, []))))

    active = sorted((pid for pid, w in work_of.items() if w["repos"] or w["channels"] or gh_of.get(pid)),
                    key=lambda q: people[q].get("rank", 1e9))[:MAX_PEOPLE]
    vec = {}
    for pid in active:
        w = work_of[pid]
        av = Counter()
        for r, v in w["repos"].items():
            for a, n in v["areas"].items():
                av[f"{r}/{a}"] += math.log1p(n)
        vec[pid] = (av, Counter({c: math.log1p(n) for c, n in w["channels"].items()}), gh_of.get(pid, set()))
    df = Counter(a for v in vec.values() for a in v[0]) + Counter(c for v in vec.values() for c in v[1])
    # an area or a channel that most people touch (a shared config folder, #general) says little about a team
    idf = {k: math.log(1 + len(vec) / n) for k, n in df.items()}
    vec = {pid: (Counter({a: x * idf[a] for a, x in v[0].items()}), Counter({c: x * idf[c] for c, x in v[1].items()}),
                 v[2]) for pid, v in vec.items()}
    clusters = _cluster(active, vec, 0.3)
    teams = []
    for i, members in enumerate(sorted(clusters, key=lambda m: -len(m))):
        teams.append(_name_team(f"t{i + 1}", members, vec, teams_gh, people, df))
    team_of = {m: t for t in teams for m in t["members"]}
    for pid, op in out_people.items():
        t = team_of.get(pid)
        if not t:
            op.update(team=None, team_confidence=0, team_evidence=["no cluster: too little shared work with others"]
                      if pid in vec else [])
            continue
        sims = sorted((_sim(vec[pid], vec[o]) for o in t["members"] if o != pid), reverse=True)[:3]
        avg = sum(sims) / len(sims) if sims else 0
        ev = list(t["evidence"][:2])
        mine = gh_of.get(pid, set()) & {t.get("github_team")}
        if mine:
            ev.insert(0, f"member of the GitHub team {teams_gh[next(iter(mine))]['name']}")
        op.update(team=t["id"], team_confidence=round(min(0.85, 0.3 + avg + (0.15 if mine else 0)), 2),
                  team_evidence=ev + [f"work most like {len(t['members']) - 1} others in this group "
                                      f"(similarity {avg:.2f})"])

    reports = _reporting(people, out_people, teams, team_of, groups, top_approver, approvals)
    for t in teams:
        t["managers"] = sorted({e["manager"] for e in reports if e["report"] in t["members"]
                                and sum(1 for x in reports if x["manager"] == e["manager"] and x["report"] in t["members"]) >= 2},
                               key=lambda q: people[q].get("rank", 1e9))
    leaders = sorted((pid for pid, op in out_people.items() if op["level"] == "director+"),
                     key=lambda q: people[q].get("rank", 1e9))
    return dict(built=now, teams=teams, people=out_people, edges=reports, leaders=leaders,
                note="Inferred from repos, pull requests, CODEOWNERS, GitHub teams and Slack. Not an HR record.")


def _seniority(p, w, now, owned_areas, maintains, approver_for) -> dict:
    ev, score = [], 0.0
    first = w["first"]
    if first:
        yrs = (now - first) / YEAR
        ev.append(f"first commit {yrs:.1f} years ago")
        score += 1.0 if yrs >= 4 else 0.5 if yrs >= 2 else 0
    areas = sum(1 for v in w["repos"].values() for a, n in v["areas"].items() if n >= 3)
    if len(w["repos"]) >= 3 or areas >= 10:
        score += 0.4
        ev.append(f"works across {len(w['repos'])} repos and {areas} areas")
    if w["n_reviews"] >= 20 and w["n_reviews"] >= 1.5 * max(1, w["n_prs"]):
        score += 0.6
        ev.append(f"reviews more than writes: {w['n_reviews']} reviews, {w['n_prs']} pull requests")
    elif w["n_reviews"]:
        ev.append(f"{w['n_reviews']} reviews given, {w['n_prs']} pull requests written")
    if len(w["approved"]) >= 5:
        score += 0.6
        ev.append(f"approves the pull requests of {len(w['approved'])} people")
    if owned_areas:
        score += 0.6 if owned_areas >= 3 else 0.3
        ev.append(f"top committer of {owned_areas} area{'s' if owned_areas > 1 else ''}")
    best = max(w["blame"].items(), key=lambda kv: kv[1], default=None)
    if best and best[1] >= 0.1:
        score += 0.4
        ev.append(f"wrote {best[1]:.0%} of the lines in {best[0]} (git blame)")
    if w["owns"]:
        score += 0.5
        ev.append("a code owner in " + ", ".join(r for r, _ in w["owns"].most_common(2)) + " (CODEOWNERS)")
    if maintains:
        score += 0.5
        ev.append(f"maintainer of {len(maintains)} GitHub team{'s' if len(maintains) > 1 else ''}")
    if approver_for >= 3:
        score += 0.8
        ev.append(f"gives most of the approvals to {approver_for} people")
    signals = sum(1 for _ in ev)
    if p["title"]:
        level, word = _title_level(p["title"])
        ev.insert(0, f"Slack title '{p['title']}'" + (f" ('{word}')" if word else ": no seniority word"))
        return dict(level=level, level_confidence=0.8 if word else 0.55, level_evidence=ev, level_from="title")
    if not (w["repos"] or w["n_prs"] or w["n_reviews"]):
        return dict(level=None, level_confidence=0, level_evidence=["not enough work seen to tell"], level_from="")
    level = "IC" if score < 1.0 else "senior IC" if score < 2.2 else "lead"
    return dict(level=level, level_confidence=round(min(0.55, 0.25 + 0.04 * signals), 2), level_evidence=ev,
                level_from="signals")


def _sim(a, b) -> float:
    """How alike two people's work is: areas count most, then Slack channels, then GitHub teams. A part that one of
    them has no data for is left out."""
    parts = [(0.6, _cos(a[0], b[0]), bool(a[0] and b[0])), (0.25, _cos(a[1], b[1]), bool(a[1] and b[1])),
             (0.15, len(a[2] & b[2]) / len(a[2] | b[2]) if a[2] | b[2] else 0, bool(a[2] and b[2]))]
    wsum = sum(w for w, _, ok in parts if ok)
    return sum(w * s for w, s, ok in parts if ok) / wsum if wsum else 0.0


def _cluster(members, vec, thr) -> list[list[str]]:
    """Label propagation over each person's nearest neighbours (similarity >= thr). A cluster that is too large is
    clustered again with a higher bar."""
    nbr = {}
    for a in members:
        sims = sorted(((_sim(vec[a], vec[b]), b) for b in members if b != a), reverse=True)
        nbr[a] = [(s, b) for s, b in sims[:6] if s >= thr]
    label = {a: a for a in members}
    for _ in range(15):
        changed = False
        for a in members:
            votes = Counter()
            for s, b in nbr[a]:
                votes[label[b]] += s
            if votes:
                best = max(votes.items(), key=lambda kv: (kv[1], kv[0]))[0]
                if best != label[a] and votes[best] > votes.get(label[a], 0):
                    label[a] = best
                    changed = True
        if not changed:
            break
    groups = defaultdict(list)
    for a in members:
        groups[label[a]].append(a)
    out = []
    for g in groups.values():
        if len(g) > BIG_TEAM and thr < 0.7:
            out += _cluster(g, vec, thr + 0.15)
        elif len(g) >= 2:
            out.append(g)
    return out


def _name_team(tid, members, vec, teams_gh, people, df) -> dict:
    """A cluster's name and evidence: the GitHub team that half or more of its people are in, else the area that sets
    it apart (shared by its people, rare outside it)."""
    gh = Counter(t for m in members for t in vec[m][2])
    areas, chans = Counter(), Counter()
    for m in members:
        for a in vec[m][0]:
            areas[a] += 1
        for c in vec[m][1]:
            chans[c] += 1
    ev, gt = [], None
    if gh:
        slug, n = max(gh.items(), key=lambda kv: (kv[1], -len(teams_gh[kv[0]].get("members", [])), kv[0]))
        if n >= 2 and n / len(members) >= 0.5:
            gt = slug
            ev.append(f"{n} of {len(members)} are in the GitHub team {teams_gh[slug]['name']}")
    half = max(2, len(members) / 2)
    distinct = lambda c: sorted(((a, n) for a, n in c.items() if n >= 2),
                                key=lambda kv: (kv[1] < half, -kv[1] / df[kv[0]], -kv[1]))
    shared = distinct(areas)[:3]
    if shared:
        ev.append("work in " + ", ".join(f"{a} ({n} of them)" for a, n in shared))
    ch = distinct(chans)[:2]
    if ch:
        ev.append("talk in " + ", ".join(f"{c} ({n})" for c, n in ch))
    name = teams_gh[gt]["name"] if gt else (shared[0][0] if shared else ch[0][0] if ch else "Group")
    members = sorted(members, key=lambda q: people[q].get("rank", 1e9))
    return dict(id=tid, name=name, inferred=True, github_team=gt, members=members, size=len(members), evidence=ev,
                managers=[])


def _reporting(people, out_people, teams, team_of, groups, top_approver, approvals) -> list[dict]:
    """Manager -> report edges, only with evidence: a manager's title, or one person who approves most of the pull
    requests of three or more people."""
    found = {}

    def add(m, r, conf, ev):
        if m == r:
            return
        cur = found.get((m, r))
        if cur:
            cur["confidence"] = round(min(0.85, 1 - (1 - cur["confidence"]) * (1 - conf)), 2)
            cur["evidence"] += ev
        else:
            found[(m, r)] = dict(manager=m, report=r, confidence=round(conf, 2), evidence=list(ev))

    for pid, op in out_people.items():
        title = people[pid]["title"]
        if op["level"] not in ("manager", "director+") or op["level_from"] != "title":
            continue
        m = re.search(r"(?:,|\s-\s|\bof\b|\bfor\b)\s*(.+)$", title)
        phrase = {w for w in re.findall(r"[a-z]{3,}", (m.group(1) if m else "").lower())
                  if w not in {"the", "team", "and", "engineering"}}
        target = None
        if phrase:
            target = next((t for t in teams if phrase & set(re.findall(r"[a-z]{3,}", t["name"].lower()))), None)
        if target:
            for r in target["members"]:
                add(pid, r, 0.65, [f"title '{title}' names the team {target['name']}"])
        elif team_of.get(pid):
            for r in team_of[pid]["members"]:
                add(pid, r, 0.45, [f"title '{title}'; in the same inferred team"])
    for a, authors in groups.items():
        if len(authors) < 3:
            continue
        for r in authors:
            _, n, tot = top_approver[r]
            add(a, r, min(0.55, 0.3 + 0.05 * len(authors)),
                [f"approved {n} of {tot} approvals of {people[r]['name']}'s pull requests",
                 f"top approver for {len(authors)} people"])
    return sorted(found.values(), key=lambda e: -e["confidence"])


def card(chart: dict, pid: str) -> dict:
    """The org part of one person's card: seniority, team, and inferred manager and reports."""
    op = (chart or {}).get("people", {}).get(pid)
    if not op:
        return {}
    teams = {t["id"]: t for t in chart["teams"]}
    names = {q: o["name"] for q, o in chart["people"].items()}
    t = teams.get(op.get("team"))
    return dict(
        seniority=dict(level=op["level"], confidence=op["level_confidence"], evidence=op["level_evidence"],
                       source=op["level_from"], inferred=True),
        team=dict(id=t["id"], name=t["name"], size=t["size"], confidence=op["team_confidence"],
                  evidence=op["team_evidence"], inferred=True,
                  members=[dict(id=m, name=names.get(m, "")) for m in t["members"] if m != pid][:8]) if t else None,
        managers=[dict(id=e["manager"], name=names.get(e["manager"], ""), confidence=e["confidence"],
                       evidence=e["evidence"]) for e in chart["edges"] if e["report"] == pid][:3],
        reports=[dict(id=e["report"], name=names.get(e["report"], ""), confidence=e["confidence"])
                 for e in chart["edges"] if e["manager"] == pid][:20])
