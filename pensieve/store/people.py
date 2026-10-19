"""People: identities, persons and their keys, the user's overrides, Slack profiles and the people that Find a person
kept (store.db), and the People graph's edges and derived views (index.db).

- store.db holds what cannot be built again: each identity's first sighting, the person ids (stable from one build
  to the next, and kept across a reset), the older ids of each person, and the user's merges, splits and "This is me".
- index.db holds what a build makes: `person_edges`, and two views in `meta` that the People tab reads whole:
  `people:raw` (what the collectors found) and `people:graph` (profiles, the org chart and the people's derived
  fields)."""
import json

from pensieve.models import (DirectoryEmail, DirectoryPerson, DirectorySource, EdgeKind, Identity, IdentityKind,
                             KeyReason, OverrideAction, Person, PersonAlias, PersonEdge, PersonKey, PersonOverride,
                             SlackUser, now)
from pensieve.store import cache, rows
from pensieve.store.db import Db

RAW = "people:raw"
GRAPH = "people:graph"
KINDS = {k.value for k in IdentityKind}


def key_kind(key: str) -> IdentityKind | None:
    k = key.split(":", 1)[0]
    return IdentityKind(k) if k in KINDS and ":" in key else None


# The user's overrides. The shape {merge: [[a, b]], split: [k], me: [k], manager: [[manager, report]]} is the API;
# each item is one row.
def overrides(db: Db) -> dict:
    out = {"merge": [], "split": [], "me": [], "manager": []}
    for o in rows.select(db.conn, PersonOverride, order="id"):
        if o.action in (OverrideAction.MERGE, OverrideAction.MANAGER):
            out[o.action.value].append([o.a, o.b])
        else:
            out[o.action.value].append(o.a)
    return out


def _override_rows(o: dict) -> list[tuple[OverrideAction, str, str | None]]:
    return [(OverrideAction.MERGE, a, b) for a, b in o.get("merge", [])] + \
        [(OverrideAction.SPLIT, k, None) for k in o.get("split", [])] + \
        [(OverrideAction.ME, k, None) for k in o.get("me", [])] + \
        [(OverrideAction.MANAGER, a, b) for a, b in o.get("manager", [])]


def set_overrides(db: Db, o: dict, at: int | None = None):
    """Replace the overrides with `o` (the v1 shape). A row that stays keeps its time; a new row gets `at`."""
    at = at or now()

    def run(c):
        old = {(r.action, r.a, r.b): r.at for r in rows.select(c, PersonOverride)}
        c.execute("DELETE FROM person_overrides")
        rows.insert_many(c, [PersonOverride(None, act, a, b, old.get((act, a, b), at))
                             for act, a, b in _override_rows(o)])
    db.write(run)


def import_overrides(db: Db, o: dict, at: int | None = None):
    """For the v1 import: the content of people/overrides.json, added to the overrides that are there."""
    cur = overrides(db)
    merged = {k: cur[k] + [x for x in o.get(k, []) if x not in cur[k]] for k in cur}
    set_overrides(db, merged, at)


# Identities: one row per key, with its first and last sighting.
def put_identities(db: Db, ids: list[Identity]):
    """Add identities, or update their name, title, picture and last sighting. `first_seen` stays."""
    def run(c):
        for i in ids:
            rows.upsert(c, i, ("key",), keep=("first_seen",))
    db.write(run)


def identities(db: Db, keys=None) -> list[Identity]:
    if keys is None:
        return rows.select(db.conn, Identity, order="key")
    keys = list(keys)
    out = []
    for i in range(0, len(keys), 500):
        part = keys[i:i + 500]
        out += rows.select(db.conn, Identity, f"key IN ({rows.marks(len(part))})", part)
    return out


# Persons, their keys and aliases: written whole by each build.
def save_persons(db: Db, persons: list[Person], keys: list[PersonKey], aliases: list[PersonAlias],
                 identities_: list[Identity] = ()):
    """Replace the persons, their keys and aliases with one build's. `identities_` are added first for keys that
    have no identity row yet (a key from the git config or an override)."""
    def run(c):
        for i in identities_:
            rows.insert(c, i, or_="IGNORE")
        c.execute("DELETE FROM person_aliases")
        c.execute("DELETE FROM person_keys")
        c.execute("DELETE FROM persons")
        rows.insert_many(c, persons)
        rows.insert_many(c, keys)
        rows.insert_many(c, aliases, or_="IGNORE")
    db.write(run)


def import_persons(db: Db, v1_people: dict, at: int | None = None):
    """For the v1 import: the `people` of people/graph.json ({id: {name, me, keys, aliases, since}}), so that the
    first v2 build keeps the person ids (links to people stay the same). Replaces the stored persons."""
    at = at or now()
    ps, ks, als, ids = [], [], [], []
    for pid, p in v1_people.items():
        ps.append(Person(pid, p.get("name") or "", bool(p.get("me")), int(p.get("since") or at)))
        for k in p.get("keys", []):
            kind = key_kind(k)
            if kind:
                ks.append(PersonKey(k, pid, KeyReason.FACT))
                ids.append(Identity(k, kind, None, None, None, "import", at, at))
        als += [PersonAlias(a, pid) for a in p.get("aliases", []) if a not in v1_people]
    save_persons(db, ps, ks, als, ids)


def persons(db: Db) -> list[Person]:
    return rows.select(db.conn, Person, order="id")


def person_keys(db: Db) -> list[PersonKey]:
    return rows.select(db.conn, PersonKey, order="person_id, key")


def aliases(db: Db) -> list[PersonAlias]:
    return rows.select(db.conn, PersonAlias, order="old_id")


def previous(db: Db) -> dict:
    """The last build's people as `_stable_ids` reads them: {people: {id: {keys, aliases, since}}}."""
    out = {p.id: {"keys": [], "aliases": [], "since": p.created_at} for p in persons(db)}
    for k in person_keys(db):
        if k.person_id in out:
            out[k.person_id]["keys"].append(k.key)
    for a in aliases(db):
        if a.person_id in out:
            out[a.person_id]["aliases"].append(a.old_id)
    return {"people": out}


def current_id(db: Db, pid: str) -> str | None:
    """The id that a person has now, for an id that was theirs before."""
    if db.one("SELECT 1 FROM persons WHERE id = ?", (pid,)):
        return pid
    r = db.one("SELECT person_id FROM person_aliases WHERE old_id = ?", (pid,))
    return r[0] if r else None


# Slack profiles that a lookup returned (v1: people/slack_profiles.json).
def slack_users(db: Db) -> dict:
    """The v1 shape: {users: {id: {id, name, title, email, avatar, bot, display}}, me: id, at}."""
    users, me, at = {}, None, 0
    for u in rows.select(db.conn, SlackUser, order="id"):
        users[u.id] = {k: v for k, v in dict(id=u.id, name=u.name, title=u.title, email=u.email, avatar=u.avatar_url,
                                             bot=u.bot, display=u.display).items() if v not in ("", None)}
        users[u.id]["bot"] = u.bot
        if u.is_me:
            me = u.id
        at = max(at, u.seen_at)
    return {"users": users, **({"me": me} if me else {}), "at": at}


def put_slack_users(db: Db, users: list[dict], me: str | None = None, at: int | None = None):
    """Add or update Slack users ({id, name, title, email, avatar, bot, display}). A value that is empty keeps the
    stored one. `me`: the user's own Slack id."""
    at = at or now()

    def run(c):
        for u in users:
            cur = rows.select_one(c, SlackUser, "id = ?", (u["id"],))
            def pick(k, col):
                v = u.get(k)
                return v if v not in ("", None) else (getattr(cur, col) if cur else "")
            rows.insert(c, SlackUser(u["id"], pick("name", "name") or "", pick("title", "title") or "",
                                     (pick("email", "email") or "").lower(), pick("avatar", "avatar_url") or "",
                                     pick("display", "display") or "", bool(u.get("bot") or (cur and cur.bot)),
                                     bool(cur and cur.is_me), at), or_="REPLACE")
        if me:
            c.execute("UPDATE slack_users SET is_me = (id = ?)", (me,))
    db.write(run)


# The people that Find a person found and kept (v1: people/directory.json).
def directory(db: Db) -> list[dict]:
    """Each record in the v1 shape: {id, name, emails, login, slack, title, avatar_url, sources, found_at,
    seen_at}, in the order they were found."""
    emails, sources = {}, {}
    for e in rows.select(db.conn, DirectoryEmail, order="person_id, email"):
        emails.setdefault(e.person_id, []).append(e.email)
    for s in rows.select(db.conn, DirectorySource, order="person_id, idx"):
        sources.setdefault(s.person_id, []).append(s.source)
    return [dict(id=r.id, name=r.name, emails=emails.get(r.id, []), login=r.login, slack=r.slack, title=r.title,
                 avatar_url=r.avatar_url, sources=sources.get(r.id, []), found_at=r.found_at, seen_at=r.seen_at)
            for r in rows.select(db.conn, DirectoryPerson, order="found_at, id")]


def put_directory(db: Db, recs: list[dict]):
    """Add or replace directory records (the v1 shape)."""
    def run(c):
        for r in recs:
            rows.insert(c, DirectoryPerson(r["id"], r.get("name") or "", r.get("login") or "", r.get("slack") or "",
                                           r.get("title") or "", r.get("avatar_url") or "",
                                           int(r.get("found_at") or now()), int(r.get("seen_at") or r.get("found_at")
                                                                                or now())), or_="REPLACE")
            c.execute("DELETE FROM directory_emails WHERE person_id = ?", (r["id"],))
            c.execute("DELETE FROM directory_sources WHERE person_id = ?", (r["id"],))
            rows.insert_many(c, [DirectoryEmail(r["id"], e) for e in sorted(set(r.get("emails") or []))])
            rows.insert_many(c, [DirectorySource(r["id"], i, s) for i, s in enumerate(r.get("sources") or [])])
    db.write(run)


# index.db: the graph's edges, one row per kind of work between two people.
def save_edges(idx: Db, edges: list[PersonEdge]):
    def run(c):
        c.execute("DELETE FROM person_edges")
        rows.insert_many(c, edges)
    idx.write(run)


def edges(idx: Db) -> list[PersonEdge]:
    return rows.select(idx.conn, PersonEdge)


def edge_rows(e: dict, kind_weight: dict, decay: float) -> list[PersonEdge]:
    """The rows of one graph edge {a, b, kinds: {kind: n}, last}. A kind's weight is its share of the edge's weight
    (the decay is the edge's own, from its newest work), so the weights of an edge's rows add up to `w`."""
    import math
    return [PersonEdge(e["a"], e["b"], EdgeKind(k), int(n), int(e["last"]) if e["last"] else None,
                       kind_weight[k] * math.log1p(n) * decay) for k, n in e["kinds"].items()]


def edges_view(idx: Db, limit: int = 5000) -> list[dict]:
    """The graph's edges in the v1 shape {a, b, w, kinds, last}, the strongest first."""
    by = {}
    for r in edges(idx):
        e = by.setdefault((r.a, r.b), {"a": r.a, "b": r.b, "w": 0.0, "kinds": {}, "last": 0})
        e["w"] += r.weight
        e["kinds"][r.kind.value] = r.n
        e["last"] = max(e["last"], r.last_at or 0)
    out = sorted(by.values(), key=lambda e: -e["w"])[:limit]
    for e in out:
        e["w"] = round(e["w"], 4)
    return out


def raw(idx: Db) -> dict | None:
    return cache.meta_json(idx, RAW)


def set_raw(idx: Db, d: dict):
    cache.set_meta_json(idx, RAW, d)


def graph_view(idx: Db) -> dict | None:
    return cache.meta_json(idx, GRAPH)


def set_graph_view(idx: Db, d: dict):
    cache.set_meta(idx, GRAPH, json.dumps(d))


def key_reason(kind: str) -> KeyReason:
    return KeyReason(kind) if kind in {r.value for r in KeyReason} else KeyReason.FACT
