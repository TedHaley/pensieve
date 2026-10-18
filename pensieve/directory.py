"""Find a person who is not in the People graph yet: a search of the user's own services, read only, with a local cache.

`local(q)` gives the people in the graph, then the people that earlier searches found. The remote sources are asked
only when they are there and switched on:
- GitHub, through `gh` (GET calls only, no model): the members of the user's organizations, cached for a day, with
  their names cached for two weeks. Public GitHub users come last, and only when the members give few matches.
- Slack and the other people connectors that are switched on, with their read-only user search tool. Pensieve calls a
  server that it connects to itself (own or agent config) directly. A Claude Code server costs a headless model run,
  so it is asked only when the user asks for it (`deep`).

Each person found in the user's own services is kept in people/directory.json (name, emails, GitHub login, Slack id,
title, picture, source, found_at). Records join by the same rules as the graph: email, GitHub login, Slack id, then
full name. Public GitHub users are not kept. Pictures go into the graph's picture folder."""
import asyncio
import json
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from pensieve import connections, connectors, people, settings

FILE = people.DIR / "directory.json"
ORG_TTL = 86400            # the member list of an organization
NAME_TTL = 14 * 86400      # one GitHub user's name and picture
MAX_MEMBERS = 2000         # per organization
PUBLIC_MIN = 3             # public GitHub users are asked only when the members give fewer matches than this
LIMIT = 8
# Connectors whose user tools find coworkers. Other servers can have a 'user' tool for other things (analytics users).
PEOPLE_CONNECTORS = {"slack", "linear", "notion", "hubspot", "github"}
CHAT = {"slack", "teams"}  # messaging tools: the first place to look for a person, even through Claude Code

_lock = threading.RLock()
_filling = set()


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def score(q: str, name: str = "", login: str = "", emails=(), slack: str = "") -> int:
    """How well a query matches a person: 0 for no match. A name that starts with the query is best, then names whose
    words start with the query's words, then a login, an email or a Slack id that holds the query."""
    q = _fold(q).strip()
    if not q:
        return 0
    n = _fold(name)
    words = re.split(r"[^a-z0-9]+", n)
    qw = [w for w in re.split(r"[^a-z0-9]+", q) if w]
    flat = q.replace(" ", "")
    if n.startswith(q):
        return 100
    if qw and all(any(w.startswith(x) for w in words) for x in qw):
        return 80
    if login and _fold(login).startswith(flat):
        return 70
    if any(_fold(e).split("@")[0].startswith(q.replace(" ", ".")) for e in emails):
        return 60
    if len(flat) < 3:  # two letters are in too many names, logins and emails
        return 0
    if q in n:
        return 50
    if login and flat in _fold(login):
        return 40
    if any(flat in _fold(e).split("@")[0] for e in emails):
        return 35
    if slack and q.upper() == slack:
        return 30
    return 0


def _keys(r: dict) -> list[str]:
    out = []
    for e in r.get("emails") or []:
        out += people._keys(email=e)
    out += people._keys(name=r.get("name", ""), login=r.get("login", ""), slack=r.get("slack", ""))
    return list(dict.fromkeys(out))


def load() -> list[dict]:
    return people._read(FILE, {}).get("people", [])


def merge(found: list[dict]) -> list[dict]:
    """Add people to the cache, joined with the records that share an email, a login or a Slack id, else a full name.
    Returns the merged records, in the order of `found`."""
    with _lock:
        recs = load()
        out = []
        for f in found:
            ks = _keys(f)
            if not ks:
                continue
            strong = set(k for k in ks if not k.startswith("name:"))
            hit = next((r for r in recs if strong & set(r["keys"])), None) or \
                next((r for r in recs if set(ks) & set(r["keys"])), None)
            now = time.time()
            if hit is None:
                hit = dict(id="d" + people._hash(sorted(ks, key=people._key_rank)[0]), name="", emails=[], login="",
                           slack="", title="", avatar_url="", sources=[], found_at=now, keys=[])
                recs.append(hit)
            for k in ("name", "login", "slack", "title", "avatar_url"):
                v = f.get(k) or ""
                # a full name is better than a login; a Slack picture is better than a GitHub one
                if v and (not hit[k] or (k == "name" and len(v.split()) > len(hit[k].split()))
                          or (k == "avatar_url" and f.get("source") == "slack")):
                    hit[k] = v
            hit["emails"] = sorted(set(hit["emails"]) | {e.lower() for e in f.get("emails") or [] if "@" in e})
            if f.get("source") and f["source"] not in hit["sources"]:
                hit["sources"].append(f["source"])
            hit["seen_at"] = now
            hit["keys"] = _keys(hit)
            out.append({**hit, "source": f.get("source", "")})
        people._write(FILE, {"people": recs})
        return out


_index = {"at": None, "of": {}}


def _graph_index(g: dict) -> dict:
    """Identity key -> person id of the graph, built again when the graph changes."""
    if _index["at"] != g.get("resolved"):
        _index["of"] = {k: p["id"] for p in g["people"].values() for k in p["keys"]}
        _index["at"] = g.get("resolved")
    return _index["of"]


def _graph_hit(p: dict, s: int = 0) -> dict:
    return dict(kind="graph", id=p["id"], name=p["name"], title=p.get("function") or p.get("title") or "",
                me=p["me"], rank=p.get("rank"), sources=p["sources"], score=s,
                avatar=bool(p["avatar_url"] and people._avatar_file(p["avatar_url"])))


def _dir_hit(r: dict, s: int = 0) -> dict:
    f = people._avatar_file(r["avatar_url"]) if r.get("avatar_url") else None
    return dict(kind="directory", id=r["id"], name=r["name"] or r["login"] or (r["emails"] or [""])[0],
                title=r.get("title", ""), login=r.get("login", ""), slack=r.get("slack", ""),
                emails=r.get("emails", []), sources=r.get("sources", []), found_at=r.get("found_at"),
                pic=f.stem if f else "", score=s, public=bool(r.get("public")))


def in_graph(r: dict) -> str | None:
    """The graph person that a directory record is, by a shared identity key."""
    g = people.graph()
    if not g:
        return None
    of = _graph_index(g)
    return next((of[k] for k in _keys(r) if k in of), None)


def local(q: str, limit: int = LIMIT) -> list[dict]:
    """People in the graph, then people in the cache who are not in the graph, best match first."""
    g = people.graph() or {}
    hits = []
    for p in (g.get("people") or {}).values():
        ids = [k.split(":", 1) for k in p["keys"]]
        s = score(q, p["name"], next((v for k, v in ids if k == "gh"), ""), [v for k, v in ids if k == "email"],
                  next((v for k, v in ids if k == "slack"), ""))
        if s:
            hits.append(_graph_hit(p, s))
    hits.sort(key=lambda h: (-h["score"], h["rank"] if h["rank"] is not None else 1e9))
    more = []
    for r in load():
        s = score(q, r["name"], r["login"], r["emails"], r["slack"])
        if s and not in_graph(r):
            more.append(_dir_hit(r, s))
    more.sort(key=lambda h: (-h["score"], h["name"]))
    return (hits + more)[:limit]


def get(did: str) -> dict | None:
    r = next((r for r in load() if r["id"] == did), None)
    return _dir_hit(r) if r else None


def find(who: str) -> dict | None:
    """A cached person for a name, an email, a GitHub login or a Slack id (the `person` MCP tool's fallback)."""
    who = (who or "").strip()
    if not who:
        return None
    want = set(people._keys(name=who, email=who if "@" in who else "",
                            login=who if re.fullmatch(r"[\w-]+", who) else "",
                            slack=who if re.fullmatch(r"[UW][A-Z0-9]{6,}", who) else ""))
    recs = load()
    r = next((r for r in recs if want & set(r["keys"])), None) or \
        next((r for r in recs if _fold(r["name"]) == _fold(who)), None)
    return _dir_hit(r) if r else None


def _gh_get(path: str, paginate=False, timeout=30):
    """One GET call of the GitHub API through gh. Never -f, -F or -X: with fields, gh sends a POST."""
    args = ["gh", "api", "-H", "Accept: application/vnd.github+json"] + (["--paginate"] if paginate else []) + [path]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise OSError((r.stderr or r.stdout).strip()[:200])
    text = r.stdout.strip()
    if paginate and text.startswith("["):  # gh writes the pages of a JSON array one after the other: [..][..]
        text = re.sub(r"\]\s*\[", ",", text)
    return json.loads(text or "null")


def gh_on() -> bool:
    """GitHub search runs when gh is installed and signed in (as for the People graph's pull requests), unless the
    user switched the GitHub (gh) source off."""
    return bool(shutil.which("gh")) and "connections/" + connections.GH not in settings.get("disabled") \
        and people._gh_login() != ""


def _orgs() -> list[str]:
    cf = people.CACHE / "gh-orgs.json"
    hit = people._read(cf, {})
    if hit.get("at", 0) > time.time() - ORG_TTL:
        return hit.get("orgs", [])
    try:
        orgs = [o["login"] for o in _gh_get("user/orgs?per_page=100") or [] if o.get("login")]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        orgs = hit.get("orgs", [])
    people._write(cf, {"at": time.time(), "orgs": orgs})
    return orgs


def _members(org: str) -> list[dict]:
    """The members of an organization that the user can see: [{login, name, avatar_url}], cached for ORG_TTL. Names
    come from each user's profile, cached for NAME_TTL and filled in the background."""
    cf = people.CACHE / f"gh-org-{people._hash(org)}.json"
    hit = people._read(cf, {})
    if hit.get("at", 0) < time.time() - ORG_TTL:
        try:
            got = _gh_get(f"orgs/{quote(org)}/members?per_page=100", paginate=True, timeout=90) or []
            old = {m["login"]: m for m in hit.get("members", [])}
            hit = {"at": time.time(), "org": org, "members": [
                {**old.get(m["login"], {}), "login": m["login"], "avatar_url": m.get("avatar_url", "")}
                for m in got[:MAX_MEMBERS] if m.get("login") and m.get("type", "User") == "User"]}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            hit = {**hit, "at": time.time()}
        people._write(cf, hit)
    _fill_names(org, cf)
    return hit.get("members", [])


def _fill_names(org: str, cf):
    """Look up the names of an organization's members (GET users/<login>) in a background thread. Each name is kept
    for NAME_TTL."""
    with _lock:
        if org in _filling:
            return
        todo = [m for m in people._read(cf, {}).get("members", []) if m.get("named", 0) < time.time() - NAME_TTL]
        if not todo:
            return
        _filling.add(org)

    def one(m):
        try:
            u = _gh_get(f"users/{quote(m['login'])}", timeout=20) or {}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return m
        return {**m, "name": u.get("name") or "", "email": u.get("email") or "",
                "avatar_url": u.get("avatar_url") or m.get("avatar_url", ""), "named": time.time()}

    def run():
        try:
            with ThreadPoolExecutor(6) as pool:
                done = {m["login"]: m for m in pool.map(one, todo)}
            with _lock:
                d = people._read(cf, {})
                d["members"] = [done.get(m["login"], m) for m in d.get("members", [])]
                people._write(cf, d)
        finally:
            _filling.discard(org)
    threading.Thread(target=run, daemon=True, name="gh-names").start()


def _gh_person(m: dict) -> dict:
    return dict(name=m.get("name") or "", login=m["login"], emails=[m["email"]] if m.get("email") else [],
                avatar_url=m.get("avatar_url") or f"https://github.com/{m['login']}.png?size=128", source="github")


def search_github(q: str, limit: int = 6) -> list[dict]:
    """Members of the user's organizations who match, then public GitHub users when there are few."""
    if not gh_on():
        return []
    found = []
    for org in _orgs():
        for m in _members(org):
            s = score(q, m.get("name", ""), m["login"], [m["email"]] if m.get("email") else [])
            if s:
                found.append((s, _gh_person(m)))
    found = [p for _, p in sorted(found, key=lambda x: -x[0])][:limit]
    if len(found) < PUBLIC_MIN and len(q.strip()) >= 3:
        try:
            res = _gh_get(f"search/users?q={quote(q.strip())}+type:user&per_page=3", timeout=20) or {}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            res = {}
        have = {p["login"].lower() for p in found}
        found += [{**_gh_person(u), "public": True} for u in res.get("items", [])
                  if u.get("login") and u["login"].lower() not in have and not people.is_bot(login=u["login"])]
    return found


def _search_tool(names) -> str | None:
    """A read-only tool that searches users (slack_search_users), else one that lists them (list_users)."""
    users = [n for n in names if connections.user_tool(n)]
    return next((n for n in users if {"search", "find", "query"} & set(connections.words(n))), None) or \
        next((n for n in users if "list" in connections.words(n)), None)


def user_servers() -> list[dict]:
    """Switched-on, connected servers of a people connector with a read-only user search tool. A server that another
    switched-on server covers is left out."""
    cov = connections.covered()
    out = []
    for r in connections.servers():
        pid = connectors.match(r["name"], r["id"])
        if pid not in PEOPLE_CONNECTORS or r["origin"] == "cli" or r["id"] in cov:
            continue
        if r.get("status") != "connected" or not settings.enabled(f"connections/{r['id']}"):
            continue
        if _search_tool([t["name"] for t in r.get("tools", []) if t.get("read")]):
            out.append({**r, "connector": pid, "label": connectors.PROFILES[pid]["label"]})
    return out


SEARCH = """Find the people named "{q}" with the {name} tools only. Call the user search tool once with that query.
Make at most 2 tool calls. Do not summarize, and do not change anything: Pensieve keeps the tool results.
When you are done, reply DONE."""


def _text(d: dict, *keys) -> str:
    return next((d[k] for k in keys if isinstance(d.get(k), str) and d[k]), "")


def _dicts(x, depth=0):
    if depth > 5:
        return
    if isinstance(x, dict):
        if any(k in x for k in ("email", "emailAddress", "displayName", "real_name", "name")):
            yield x
        for v in x.values():
            yield from _dicts(v, depth + 1)
    elif isinstance(x, list):
        for v in x[:200]:
            yield from _dicts(v, depth + 1)


def _parse(results: list[tuple], service: str) -> list[dict]:
    """People in the results of user tools: Slack's text and JSON users, else generic user objects."""
    out = []
    for _tool, _args, text in results:
        data = connections._json(text)
        strings = [text] if data is None else list(connections._strings(data))
        su = [u for s in strings for u in people.parse_slack_users(s)]
        su += list(people._json_users(data)) if data is not None else []
        for u in su:
            if not u.get("bot"):
                out.append(dict(name=u["name"], slack=u["id"], title=u.get("title", ""), avatar_url=u.get("avatar", ""),
                                emails=[u["email"]] if u.get("email") else [], source=service))
        if su or data is None:
            continue
        for d in _dicts(data):
            p = connectors.person(d, service)
            if p and p.get("name") and p.get("email"):  # an id of another service joins nothing here
                out.append(dict(name=p["name"], emails=[p["email"]], source=service,
                                title=_text(d, "title", "jobTitle"), avatar_url=_text(d, "avatarUrl", "avatar_url")))
    return out


async def _search_own(cfg: dict, q: str) -> list[tuple]:
    async with connections._open(cfg) as s:
        tools = [t for t in (await s.list_tools()).tools
                 if connections.is_read(t.name, t.annotations) and connections.user_tool(t.name)]
        name = _search_tool([t.name for t in tools])
        t = next((t for t in tools if t.name == name), None)
        if not t:
            return []
        props = (connections._attr(t, "input_schema") or {}).get("properties", {})
        param = next((p for p in ("query", "q", "search", "name", "term", "filter") if p in props), None)
        args = {param: q} if param else {}
        if "limit" in props:
            args["limit"] = 10
        res = await s.call_tool(t.name, args)
        if connections._attr(res, "is_error"):
            return []
        text = "\n".join(getattr(b, "text", "") for b in res.content)
        return [(t.name, args, text or json.dumps(connections._attr(res, "structured_content") or {}))]


def search_server(r: dict, q: str) -> list[dict]:
    """Ask one server for the people who match: own and agent servers directly, a Claude Code server headless."""
    if r["origin"] == "engine":  # the run's cost goes into r["_cost"]
        results = connections._collect_engine(r, 2, SEARCH.format(q=q.replace('"', ""), name=r["name"]),
                                              only=lambda n: _search_tool([n]) == n)
    else:
        cfg = connections.agent_configs()[r["id"]]["cfg"] if r["origin"] == "agent" else \
            next(c for c in settings.get("mcp_servers") if connections.slug(c["name"]) == r["id"])
        results = asyncio.run(asyncio.wait_for(_search_own(cfg, q), 60))
    found = _parse(results, r["connector"])
    # a list tool gives everyone, and a search can match on other fields: keep the people whose name matches first
    found.sort(key=lambda p: -score(q, p["name"], p.get("login", ""), p["emails"], p.get("slack", "")))
    if not any({"search", "find", "query"} & set(connections.words(t)) for t, _, _ in results):
        found = [p for p in found if score(q, p["name"], p.get("login", ""), p["emails"], p.get("slack", ""))]
    return found[:LIMIT]


def _pictures(recs: list[dict]):
    with ThreadPoolExecutor(6) as pool:
        list(pool.map(lambda r: people._fetch_avatar(r["avatar_url"]), [r for r in recs if r.get("avatar_url")]))


def hits(found: list[dict]) -> list[dict]:
    """Keep what a source found, fetch the pictures, and give each person as a search hit: the graph person when
    there is one. Public GitHub users are not kept."""
    own = merge([f for f in found if not f.get("public")])
    pub = [dict(f, id="g" + people._hash("gh:" + f["login"].lower()), slack="", title="", sources=["github"],
                found_at=time.time()) for f in found if f.get("public")]
    _pictures(own + pub)
    g = people.graph() or {"people": {}}
    out, seen = [], set()
    for r in own + pub:
        pid = in_graph(r)
        h = _graph_hit(g["people"][pid]) if pid in g["people"] else {**_dir_hit(r), "source": r.get("source", "")}
        if h["id"] not in seen:
            seen.add(h["id"])
            out.append({**h, "remote": True})
    return out
