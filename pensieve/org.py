"""The org chart of the People tab. All of it is inferred from what this Mac can see, and each part keeps its evidence.

- Team: people who work in the same repos and areas, talk in the same Slack channels and are in the same GitHub teams
  (from the CODEOWNERS files) form a cluster. A cluster takes the name of the GitHub team that most of its people are
  in, or else of the area that they share.
- Role: from the depth and breadth of a person's contributions, not from a title. Depth: areas where the person is
  the top committer, a large git blame share, CODEOWNERS entries, many commits in one area, and tenure. Breadth: the
  number of repos and areas, the people whose pull requests they approve, and GitHub teams they maintain. Deep and
  broad: lead. Deep: domain expert. Otherwise: contributor.
- Reporting lines only with evidence: a manager's title that names a team ('Engineering Manager, Payments'), one
  person who gives most of the approvals to a group of people, or a person who reviews and writes almost none and
  approves someone's pull requests. Otherwise the chart shows teams, not a tree.
- The user can say who manages whom (people.set_manager). That edge wins: the report's inferred managers drop out.

The evidence builds up over time (orgstore.py): pull requests, titles, first commits and team co-membership from all
builds so far, not only from the last one. More evidence gives more confidence."""
import math
import re
import time
from collections import Counter, defaultdict

from pensieve import orgstore

ROLES = ["contributor", "domain expert", "lead"]
# Slack title words: only for reporting lines (a manager's title) and the Leadership group, not for the role
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


def is_manager_title(title: str) -> bool:
    return _title_level(title)[0] in ("manager", "director+")


def _from_store(people: dict, pid_of_login: dict, ev: dict, now: float) -> dict:
    """The accumulated evidence (orgstore.py), by person: approvals (decayed), reviews, pull request counts, the
    first commit and the titles seen."""
    approvals = defaultdict(Counter)   # author -> approver -> approvals, weighted by age
    reviews = defaultdict(Counter)     # reviewer -> author -> pull requests reviewed
    approved = defaultdict(set)        # approver -> the authors they approved
    n_prs, n_rev = Counter(), Counter()
    for p in ev["prs"].values():
        a = pid_of_login.get(p["author"])
        if not a:
            continue
        n_prs[a] += 1
        w = orgstore.decay(p["ts"], now)
        for r in p["reviewers"]:
            q = pid_of_login.get(r)
            if q and q != a:
                n_rev[q] += 1
                reviews[q][a] += 1
        for r in p["approvers"]:
            q = pid_of_login.get(r)
            if q and q != a:
                approvals[a][q] += w
                approved[q].add(a)
    pid_of_key = {k: pid for pid, p in people.items() for k in p["keys"]}
    first = {}
    for k, ts in ev["first"].items():
        q = pid_of_key.get(k)
        if q and ts:
            first[q] = min(first.get(q) or ts, ts)
    titles = {}   # person -> (title, sightings) of the title seen last
    for k, seen in ev["titles"].items():
        q = pid_of_key.get(k)
        for t, s in seen.items():
            if q and (q not in titles or (s["last"], s["n"]) > (titles[q][1]["last"], titles[q][1]["n"])):
                titles[q] = (t, s)
    return dict(approvals=approvals, reviews=reviews, approved=approved, n_prs=n_prs, n_reviews=n_rev, first=first,
                titles=titles, has_prs=bool(ev["prs"]))


def _cos(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    return dot / (math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values())) or 1)


def infer(people: dict, profiles: dict, work_of: dict, edges: dict, raw: dict, store: dict | None = None,
          managers=()) -> dict:
    """`managers`: the (manager, report) pairs that the user set."""
    now = time.time()
    store = store or orgstore.empty()  # the evidence of all builds (orgstore.py)
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

    acc = _from_store(people, pid_of_login, store, now)
    if acc["has_prs"]:   # all the pull requests that any build saw, each once
        approvals = acc["approvals"]
    else:                # an older raw file without the pull request log: this build's counts
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
        if not tot:
            continue
        a, n = c.most_common(1)[0]
        if tot >= 3.5 and n / tot >= 0.5:
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

    out_people, title_of = {}, {}
    for pid, p in people.items():
        w = work_of[pid]
        seen = acc["titles"].get(pid)
        # no title in this build: the last one that an earlier build saw
        title_of[pid] = p["title"] or (seen[0] if seen else "")
        s = dict(first=min([x for x in (w["first"], acc["first"].get(pid)) if x] or [0]),
                 n_prs=max(w["n_prs"], acc["n_prs"][pid]), n_reviews=max(w["n_reviews"], acc["n_reviews"][pid]),
                 approved=max(len(w["approved"]), len(acc["approved"].get(pid, ()))))
        out_people[pid] = dict(id=pid, name=p["name"], function=profiles[pid]["function"],
                               function_inferred=profiles[pid]["function_inferred"],
                               **_role(s, w, now, owns_area[pid], maint_of[pid], len(groups.get(pid, []))))

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
    ev_teams = store["teams"]
    anchor = lambda q: people[q]["keys"][0] if people[q]["keys"] else ""
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
        # the same people in the same group in earlier builds: a steadier team
        me_a = anchor(pid)
        co = [ev_teams.get("\t".join(sorted((me_a, anchor(o)))), 0) for o in t["members"] if o != pid]
        stay = sum(co) / len(co) if co else 0
        bonus = min(0.1, 0.03 * stay) if stay >= 1 else 0
        if bonus:
            ev.append(f"in a group with these people in about {stay:.0f} earlier builds")
        op.update(team=t["id"], team_confidence=round(min(0.9, 0.3 + avg + (0.15 if mine else 0) + bonus), 2),
                  team_evidence=ev + [f"work most like {len(t['members']) - 1} others in this group "
                                      f"(similarity {avg:.2f})"])

    reports = _reporting(people, out_people, teams, team_of, groups, top_approver, approvals, title_of, acc)
    reports = _user_set(reports, managers, people)
    for t in teams:
        t["managers"] = sorted({e["manager"] for e in reports if e["report"] in t["members"]
                                and sum(1 for x in reports if x["manager"] == e["manager"] and x["report"] in t["members"]) >= 2},
                               key=lambda q: people[q].get("rank", 1e9))
    leaders = sorted((pid for pid in out_people if _title_level(title_of[pid])[0] == "director+"),
                     key=lambda q: people[q].get("rank", 1e9))
    review_map = {r: dict(c) for r, c in acc["reviews"].items()}  # reviewer -> author -> pull requests
    return dict(built=now, teams=teams, people=out_people, edges=reports, leaders=leaders, reviews=review_map,
                note="Inferred from repos, pull requests, CODEOWNERS, GitHub teams and Slack. Not an HR record.")


DEEP = BROAD = 1.2  # the depth and breadth scores at which a person counts as deep or broad


def _role(s, w, now, owned_areas, maintains, approver_for) -> dict:
    """The role from the depth and breadth of a person's contributions: a lead is deep and broad, a domain expert is
    deep, anyone else is a contributor. `s`: this build's work and the accumulated evidence together (first commit,
    pull requests, reviews, the number of people approved).

    `stamps` marks the pattern of a person who reviews other people's pull requests and writes almost none, and
    maintains a GitHub team or approves for several people: it is no role, but _reporting uses it."""
    depth, breadth, dev, bev = 0.0, 0.0, [], []
    if owned_areas:
        depth += 1.0 if owned_areas >= 3 else 0.6
        dev.append(f"top committer of {owned_areas} area{'s' if owned_areas > 1 else ''}")
    best = max(w["blame"].items(), key=lambda kv: kv[1], default=None)
    if best and best[1] >= 0.1:
        depth += 0.8 if best[1] >= 0.25 else 0.4
        dev.append(f"wrote {best[1]:.0%} of the lines in {best[0]} (git blame)")
    if w["owns"]:
        depth += 0.6
        dev.append("a code owner in " + ", ".join(r for r, _ in w["owns"].most_common(2)) + " (CODEOWNERS)")
    top = max(((f"{r}/{a}", n) for r, v in w["repos"].items() for a, n in v["areas"].items()), key=lambda x: x[1],
              default=None)
    if top and top[1] >= 30:
        depth += 0.4
        dev.append(f"{top[1]} commits in {top[0]}")
    first = s["first"]
    if first and (now - first) / YEAR >= 2:
        yrs = (now - first) / YEAR
        depth += 0.5 if yrs >= 4 else 0.3
        dev.append(f"first commit {yrs:.1f} years ago")
    repos = sum(1 for v in w["repos"].values() if any(n >= 3 for n in v["areas"].values()))
    areas = sum(1 for v in w["repos"].values() for n in v["areas"].values() if n >= 3)
    if repos >= 3:
        breadth += 0.8 if repos >= 5 else 0.5
    if areas >= 10:
        breadth += 0.6 if areas >= 20 else 0.3
    if repos >= 3 or areas >= 10:
        bev.append(f"works in {repos} repos and {areas} areas")
    if s["approved"] >= 3:
        breadth += 0.6 if s["approved"] >= 5 else 0.3
        bev.append(f"approves the pull requests of {s['approved']} people")
    if approver_for >= 3:
        breadth += 0.4
        bev.append(f"gives most of the approvals to {approver_for} people")
    if maintains:
        breadth += 0.4
        bev.append(f"maintainer of {len(maintains)} GitHub team{'s' if len(maintains) > 1 else ''}")
    n_rev, n_prs = s["n_reviews"], s["n_prs"]
    stamps = n_rev >= 3 and n_prs <= 0.2 * n_rev and bool(maintains or s["approved"] >= 2)
    if stamps:
        bev.append(f"reviews other people's pull requests and writes almost none ({n_rev} reviews, {n_prs} pull "
                   f"requests)")
    if not (w["repos"] or n_prs or n_rev or first):
        return dict(level=None, level_confidence=0, level_evidence=["not enough work seen to tell"], level_from="",
                    depth=0, breadth=0, stamps=False)
    deep, broad = depth >= DEEP, breadth >= BROAD
    level = "lead" if deep and broad else "domain expert" if deep else "contributor"
    ev = [f"Depth {depth:.1f}" + (f": {'; '.join(dev)}" if dev else ": no area of their own seen"),
          f"Breadth {breadth:.1f}" + (f": {'; '.join(bev)}" if bev else ": works in few repos and areas")]
    more = min(0.08, 0.02 * math.log2(1 + (n_prs + n_rev) / 10))  # more pull requests seen: a surer guess
    return dict(level=level, level_confidence=round(min(0.7, 0.3 + 0.04 * (len(dev) + len(bev)) + more), 2),
                level_evidence=ev, level_from="signals", depth=round(depth, 1), breadth=round(breadth, 1),
                stamps=stamps)


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


def _reporting(people, out_people, teams, team_of, groups, top_approver, approvals, title_of, acc) -> list[dict]:
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

    for pid in out_people:
        title = title_of.get(pid) or ""
        if not is_manager_title(title):
            continue
        seen = acc["titles"].get(pid)
        steady = 0.05 if seen and seen[0] == title and seen[1]["n"] >= 3 else 0  # the same title in several builds
        m = re.search(r"(?:,|\s-\s|\bof\b|\bfor\b)\s*(.+)$", title)
        phrase = {w for w in re.findall(r"[a-z]{3,}", (m.group(1) if m else "").lower())
                  if w not in {"the", "team", "and", "engineering"}}
        target = None
        if phrase:
            target = next((t for t in teams if phrase & set(re.findall(r"[a-z]{3,}", t["name"].lower()))), None)
        if target:
            for r in target["members"]:
                add(pid, r, 0.65 + steady, [f"title '{title}' names the team {target['name']}"])
        elif team_of.get(pid):
            for r in team_of[pid]["members"]:
                add(pid, r, 0.45 + steady, [f"title '{title}'; in the same inferred team"])
    for a, authors in groups.items():
        if len(authors) < 3:
            continue
        given = sum(top_approver[r][1] for r in authors)
        for r in authors:
            _, n, tot = top_approver[r]
            # more approvals seen over the builds: more confidence, up to 0.7
            add(a, r, min(0.7, 0.3 + 0.05 * len(authors) + 0.03 * math.log2(1 + given / 5)),
                [f"approved {n:.0f} of {tot:.0f} approvals of {people[r]['name']}'s pull requests (weighted by age)",
                 f"top approver for {len(authors)} people"])
    # A person who reviews and writes almost none (`stamps`) and approves someone's pull requests: a manager stamps
    # their reports' work, so one approver is enough. More approvals: more confidence, up to 0.6.
    for m, op in out_people.items():
        if not op.get("stamps"):
            continue
        for r, c in approvals.items():
            n, tot = c.get(m, 0), sum(c.values())
            if n >= 1 and not out_people.get(r, {}).get("stamps"):
                add(m, r, min(0.6, 0.3 + 0.1 * math.log2(1 + n)),
                    [f"approved {n:.0f} of {tot:.0f} approvals of {people[r]['name']}'s pull requests (weighted by age)",
                     "reviews other people's pull requests and writes almost none"])
    return sorted(found.values(), key=lambda e: -e["confidence"])


def _user_set(reports: list[dict], managers, people: dict) -> list[dict]:
    """The inferred edges with the user's (manager, report) pairs on top: each pair is an edge of confidence 1, and
    the report's other inferred managers drop out."""
    managers = [(m, r) for m, r in managers if m in people and r in people and m != r]
    if not managers:
        return reports
    told = {r for _, r in managers}
    out = [e for e in reports if e["report"] not in told]
    for m, r in managers:
        out.append(dict(manager=m, report=r, confidence=1.0, evidence=[f"set by you: {people[m]['name']} manages "
                                                                         f"{people[r]['name']}"], set_by_user=True))
    return sorted(out, key=lambda e: -e["confidence"])


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
                       source=op["level_from"], inferred=True, depth=op.get("depth", 0), breadth=op.get("breadth", 0)),
        team=dict(id=t["id"], name=t["name"], size=t["size"], confidence=op["team_confidence"],
                  evidence=op["team_evidence"], inferred=True,
                  members=[dict(id=m, name=names.get(m, "")) for m in t["members"] if m != pid][:8]) if t else None,
        managers=[dict(id=e["manager"], name=names.get(e["manager"], ""), confidence=e["confidence"],
                       evidence=e["evidence"], set_by_user=bool(e.get("set_by_user")))
                  for e in chart["edges"] if e["report"] == pid][:3],
        reports=[dict(id=e["report"], name=names.get(e["report"], ""), confidence=e["confidence"],
                      set_by_user=bool(e.get("set_by_user")))
                 for e in chart["edges"] if e["manager"] == pid][:20])
