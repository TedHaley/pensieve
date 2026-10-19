"""The Projects tab's build (index.db; v1: projects/projects.json and clusters.npz).

- projects: one row per project, with its scores and counts.
- project_members: the people of a project, by weight. Their names and closeness come from the People graph when
  the tab reads them.
- project_items: the lists of a project, in order: representative items, ticket prefixes, repos and areas (text),
  and its pull requests, tickets, cards, sessions, commits and files (each a JSON object, as the tab shows it).
- project_points: the project of each Map item (point id), for "Open as project".
- meta `projects:state`: the build's time and counts. cache `projects:clusters`: the cluster centers (npz bytes)."""
import json

from pensieve.models import Project, ProjectItem, ProjectItemKind, ProjectMember, ProjectRole
from pensieve.store import cache, rows
from pensieve.store.db import Db

STATE = "projects:state"
CLUSTERS = "projects:clusters"
TEXT_LISTS = ("reps", "prefixes", "repos", "areas")
OBJ_LISTS = ("prs", "tickets", "tasks", "sessions", "commits", "files")
_KIND = {"reps": "rep", "prefixes": "prefix", "repos": "repo", "areas": "area", "prs": "pr", "tickets": "ticket",
         "tasks": "task", "sessions": "session", "commits": "commit", "files": "file"}
_LIST = {v: k for k, v in _KIND.items()}
COUNTS = ("code", "docs", "sessions", "commits", "prs", "tickets", "tasks", "people")


def _project_rows(e: dict):
    c = e.get("counts") or {}
    p = Project(e["id"], int(e["rank"]), e["sig"], e.get("keywords") or "", e.get("name") or "",
                float(e.get("relevance") or 0), bool(e.get("near")), ProjectRole(e.get("role") or "none"),
                float(e.get("me_share") or 0), float(e.get("mine") or 0), float(e.get("team") or 0),
                int(e.get("last") or 0), e.get("topic"), *[int(c.get(k) or 0) for k in COUNTS])
    members = [ProjectMember(e["id"], m["id"], i, float(m.get("weight") or 0), float(m.get("n") or 0),
                             int(m.get("last") or 0)) for i, m in enumerate(e.get("members") or [])]
    items = []
    for lst in TEXT_LISTS + OBJ_LISTS:
        for i, v in enumerate(e.get(lst) or []):
            items.append(ProjectItem(e["id"], ProjectItemKind(_KIND[lst]), i,
                                     v if lst in TEXT_LISTS else json.dumps(v)))
    return p, members, items


def save(idx: Db, state: dict, projects: list[dict], of: dict[str, str]):
    """Replace the Projects build. `projects` in the v1 shape (members need only id, weight, n and last); `of`:
    point id -> project id."""
    ps, ms, its = [], [], []
    for e in projects:
        p, m, i = _project_rows(e)
        ps.append(p)
        ms += m
        its += i

    def run(c):
        for t in ("project_items", "project_members", "project_points", "projects"):
            c.execute(f"DELETE FROM {t}")
        rows.insert_many(c, ps)
        rows.insert_many(c, ms)
        rows.insert_many(c, its)
        c.executemany("INSERT INTO project_points (point_id, project_id) VALUES (?, ?)", sorted(of.items()))
    idx.write(run)
    cache.set_meta_json(idx, STATE, state)


def state(idx: Db) -> dict | None:
    return cache.meta_json(idx, STATE)


def load(idx: Db) -> list[dict]:
    """The projects in the v1 shape, by rank. Members are {id, weight, n, last}."""
    c = idx.conn
    out, by = [], {}
    for p in rows.select(c, Project, order="rank"):
        e = dict(id=p.id, key=f"dscope:{p.sig}", sig=p.sig, keywords=p.keywords, reps=[], name=p.name, description="",
                 named=False, team=p.team, mine=p.mine, me_share=p.me_share, role=p.role.value, last=p.last_at,
                 topic=p.topic, counts={k: getattr(p, "n_" + k) for k in COUNTS}, prefixes=[], repos=[], areas=[],
                 members=[], prs=[], tickets=[], tasks=[], sessions=[], commits=[], files=[], relevance=p.relevance,
                 rank=p.rank, near=p.near)
        out.append(e)
        by[p.id] = e
    for m in rows.select(c, ProjectMember, order="project_id, idx"):
        if m.project_id in by:
            by[m.project_id]["members"].append(dict(id=m.person_id, weight=m.weight, n=m.n, last=m.last_at))
    for it in rows.select(c, ProjectItem, order="project_id, kind, idx"):
        e = by.get(it.project_id)
        if e is not None:
            lst = _LIST[it.kind.value]
            e[lst].append(it.value if lst in TEXT_LISTS else json.loads(it.value))
    return out


def points(idx: Db, ids: list[str]) -> dict[str, str]:
    """point id -> project id, for these Map items."""
    out = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        out.update(idx.query(f"SELECT point_id, project_id FROM project_points WHERE point_id IN "
                             f"({rows.marks(len(part))})", part))
    return out


def clusters(idx: Db) -> bytes | None:
    return cache.get(idx, CLUSTERS)


def set_clusters(idx: Db, npz: bytes, expires_at: int):
    cache.put(idx, CLUSTERS, npz, expires_at)
