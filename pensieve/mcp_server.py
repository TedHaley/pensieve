"""MCP server: lets any agent search, read, and configure Pensieve. Served at http://127.0.0.1:<port>/mcp
(streamable HTTP); `pensieve mcp` bridges stdio to it for agents that only speak stdio."""
import contextvars
import functools
import json
import time
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from pensieve import actions, onboarding, scopes, settings, sources, usage

# Tool descriptions are sent to the agent on every turn, so connections get the tools for finding code and people by
# default; ?tools=all (or `pensieve mcp --all-tools`) adds the ones that manage Pensieve itself.
AGENT_TOOLS = {"search", "read", "similar", "who_knows", "team", "unexplored", "knowledge_gaps", "list_repos",
               "current_scope", "open", "index_path"}
# Until the user has chosen what to index, every connection also gets the setup tools, so the agent that installed
# Pensieve can walk the user through it in its conversation.
SETUP_TOOLS = {"setup_guide", "setup_scan", "setup_add_source", "setup_start"}
tool_profile = contextvars.ContextVar("tool_profile", default="agent")
agent_cwd = contextvars.ContextVar("agent_cwd", default=None)  # where the agent runs (?cwd=), for relative paths
agent_client = contextvars.ContextVar("agent_client", default=None)  # ?client=: the agent's name, e.g. claude-code
agent_conn = contextvars.ContextVar("agent_conn", default=None)  # ?conn=: one id per agent connection (stdio bridge)


class _Server(MCPServer):
    async def list_tools(self):
        tools = await super().list_tools()
        if tool_profile.get() == "all":
            return tools
        shown = AGENT_TOOLS | (set() if settings.get("setup_done") else SETUP_TOOLS)
        return [t for t in tools if t.name in shown]


mcp = _Server(
    name="pensieve",
    instructions=(
        "Pensieve indexes this repo, the Mac's documents and past agent sessions. To find code, call search before "
        "Grep/Glob, with what the code does plus names you'd guess it uses; the first hit is usually right. "
        "who_knows suggests who to ask about an area. If Pensieve isn't set up yet, it indexes nothing: call "
        "setup_guide and walk the user through the steps it lists."),
)


def _c():
    return actions.ctx


def _scope():
    return scopes.from_request()


def _check(id):
    if not scopes.contains(_scope(), id, _c()["store"], _c()["repos"]):
        raise ValueError(f"{id} is outside this connection's scope ({_scope().name})")


def _check_repo(repo):
    root = _c()["repos"]._root(repo)
    if root is None:
        raise ValueError(f"unknown repo {repo!r}")
    if not scopes.repo_allowed(_scope(), root):
        raise ValueError(f"repo {repo!r} is outside this connection's scope ({_scope().name})")


def tool(name=None):
    """Register an MCP tool; bad input (ValueError, unknown id) reaches the agent as a readable error. Every call is
    logged to usage.db (see usage.py)."""
    def deco(fn):
        @functools.wraps(fn)
        def wrap(*a, **k):
            info, t, res, err = {}, time.perf_counter(), None, None
            token = usage.current.set(info)
            try:
                res = fn(*a, **k)
                return res
            except KeyError as e:
                err = f"not found: {e.args[0] if e.args else e}"
                raise ToolError(err) from e
            except ValueError as e:
                err = str(e)
                raise ToolError(err) from e
            except Exception as e:
                err = repr(e)[:500]
                raise
            finally:
                usage.current.reset(token)
                sc = scopes.current.get(None)
                usage.record(ts=time.time(), source="mcp", tool=name or fn.__name__, client=agent_client.get(),
                             conn=agent_conn.get(), cwd=agent_cwd.get(), scope=getattr(sc, "name", None),
                             args={**dict(enumerate(a)), **k}, ms=round((time.perf_counter() - t) * 1000, 2),
                             ok=err is None, error=err, out_chars=len(res) if isinstance(res, str) else
                             len(json.dumps(res, default=str)) if res is not None else 0, **info)
        return mcp.tool(name=name)(wrap)
    return deco


@tool()
def search(query: str, limit: int = 8, kind: str | None = None) -> str:
    """Find code (or docs, past agent sessions) in one call; use before Grep/Glob. Best query: what the code does plus
    names you'd guess it uses ("retry failed webhook delivery with backoff retryWebhook"); a bare name or an error
    string also works. "quotes" = exact, -word excludes, ext:py, in:<path>, by:<person>,
    kind:code|doc|session|folder|repo|person. One line per hit, best first: path:line  enclosing definition ▸ the
    line that matched  [id for read/similar]. If those lines show what you need, use the hit: no grep to confirm."""
    sc = _scope()
    r = _c()["searcher"].find(f"{query} kind:{kind}" if kind else query, limit, scope=sc)
    cwd = (agent_cwd.get() or "").rstrip("/")
    # hits in linked worktrees (other branches' copies) rank below the checkout the agent runs in
    elsewhere = lambda x: " @ " in (x.get("subtitle") or "") and not (cwd and (x.get("path") or "").startswith(cwd + "/"))
    res = [x for x in r["results"] if not elsewhere(x)] + [x for x in r["results"] if elsewhere(x)]
    usage.note(n_results=len(res), results=[[x.get("id"), x.get("path")] for x in res], laps=r.get("timing"))
    found = _lines(res)
    if getattr(sc, "unindexed", False):  # only past sessions in this repo can match; its code isn't indexed
        return (f"{sc.description}. `pensieve repo index` indexes it." + (f"\n{found}" if found else ""))
    return found or "No matches."


def _lines(results):
    """Grep-sized results: what an agent re-reads on every later turn, so only what it needs to act on."""
    import os
    import re
    cwd, home, out = agent_cwd.get(), os.path.expanduser("~"), []
    for x in results:
        path = x.get("path") or ""
        if cwd and path.startswith(cwd.rstrip("/") + "/"):
            path = path[len(cwd.rstrip("/")) + 1:]
        elif path.startswith(home + "/"):
            path = "~" + path[len(home):]
        snip = re.sub(r"\s+", " ", x.get("snippet") or "").strip(" …")
        snip = snip[:140] + ("…" if len(snip) > 140 else "")
        kind = x.get("kind") or str(x.get("id", "")).partition(":")[0]
        if kind == "session":
            where = f'session "{x.get("title") or ""}" ({x.get("subtitle") or x.get("updated") or ""})'
        else:
            line = x.get("focus_line") or x.get("line")
            where = path + (f":{line}" if line else "") if path else x.get("title") or ""
            if x.get("stale"):  # edited after it was indexed: the snippet can be out of date
                where += " (edited since indexed)"
            if x.get("focus"):  # code: the line that matched, inside its definition
                cut = lambda s, n: s[:n] + ("…" if len(s) > n else "")
                snip = (f"{cut(x['definition'], 90)}  ▸ " if x.get("definition") else "") + cut(x["focus"], 140)
        out.append(f"{where}  {snip}  [{x.get('id')}]".replace("    ", "  "))
        if x.get("excerpt"):  # the top hits: a few numbered lines, as Read would show them
            out += [f"    {n}│ {t.rstrip()}" for n, t in x["excerpt"]]
    return "\n".join(out)


@tool()
def read(id: str, max_chars: int = 12000) -> dict:
    """Full text of a search result id: code with its owners and recent commits, a document, or a session."""
    _check(id)
    return actions.read(id, max_chars)


@tool()
def similar(id: str, limit: int = 8) -> str:
    """Items most similar in meaning to a search result id (e.g. other code that does the same thing)."""
    return _lines(_similar(id, limit)) or "Nothing similar."


def _similar(id, limit):
    _check(id)
    kind, _, ref = id.partition(":")
    sc, st, rp = _scope(), _c()["store"], _c()["repos"]
    if kind == "file":
        return [x for x in _c()["files"].similar(ref, limit * 3) if scopes.contains(sc, x["id"], st, rp)][:limit]
    if kind == "session":
        return [x for x in (dict(s, id=f"session:{s['id']}") for s in st.similar(ref, limit * 3))
                if scopes.contains(sc, x["id"], st, rp)][:limit]
    if kind == "code":
        c = rp.chunk(int(ref))
        import re
        # the chunk is the query: strip what the search box would read as quotes, exclusions or filters
        text = re.sub(r'["\']|(?<!\S)-(?=\S)|\b(kind|ext|in):', " ", c["text"][:1500]) if c else ""
        hits = _c()["searcher"].find(text, limit + 1, ["code"], scope=sc)["results"] if c else []
        return [h for h in hits if h["id"] != id][:limit]
    raise ValueError("id must start with file:, code: or session:")


@tool()
def who_knows(topic: str, repo: str | None = None) -> dict:
    """Who wrote the code most related to a topic (git blame), with their files: who to ask. repo: just one repo."""
    if repo:
        _check_repo(repo)
    sc = _scope()
    allow = scopes.allowlist(sc, "code", _c()["store"], _c()["repos"], _c()["repos"].index) if sc else None
    r = _c()["repos"].experts(q=topic, repo=repo, allow=allow)
    return {"people": r["experts"], "code": [dict(id=f"code:{c['id']}", repo=c["repo"], path=c["path"], line=c["start"],
                                                 score=round(c["score"], 3)) for c in r["chunks"]]}


@tool()
def list_repos() -> list[dict]:
    """Indexed git repos (in this connection's scope) with their size and how many agent sessions ran in each."""
    return [r for r in _c()["repos"].list() if scopes.repo_allowed(_scope(), r["root"])]


@tool()
def team(repo: str, days: int = 90) -> dict:
    """What each person committed in a repo over the last `days`: commit counts, main areas, recent commit subjects."""
    _check_repo(repo)
    t = _c()["repos"].team(repo, days)
    if t is None:
        raise ValueError(f"unknown repo {repo!r}")
    return {"repo": repo, "days": days, "people": [
        dict(name=p["name"], email=p["email"], me=p["me"], commits=p["commits"], areas=p["areas"],
             recent=[s["subject"] for s in p["subjects"][:5]]) for p in t["people"][:15]]}


@tool()
def unexplored(repo: str) -> dict:
    """Areas of a repo next to the user's own work that their agent sessions haven't touched yet, and who owns them."""
    _check_repo(repo)
    r = _c()["repos"].territory(repo)
    if r is None:
        raise ValueError(f"unknown repo {repo!r}")
    return {"coverage": r["coverage"], "suggestions": r["suggestions"]}


@tool()
def knowledge_gaps(repo: str, inactive_days: int = 180) -> dict:
    """Areas of a repo mostly written (git blame) by people with no commits in `inactive_days`: who wrote each, who
    active knows it best (`ask`), and its bus factor (active people holding >=10%)."""
    _check_repo(repo)
    r = _c()["repos"].knowledge_gaps(repo, inactive_days)
    if r is None:
        raise ValueError(f"unknown repo {repo!r}")
    return r


@tool(name="open")
def open_item(id: str, action: str = "open") -> dict:
    """Open an item on the user's Mac. action: 'open' (code opens in the editor at the line), 'reveal' (Finder) or
    'visualize' (the Pensieve map)."""
    _check(id)
    return actions.open_item(id, action)


@tool()
def show_visualizer(view: str | None = None) -> dict:
    """Open the Pensieve Map window. view: code, map, files, insights or settings."""
    return actions.visualize(view=view)


@tool()
def show_search(query: str = "") -> dict:
    """Pop up the Pensieve search panel on the user's screen, prefilled with a query."""
    return actions.show_search(query)


@tool()
def status() -> dict:
    """Index size (files, repos, code chunks, sessions) and whether indexing is running."""
    with _c()["store"].lock:
        db = _c()["store"].db
        n = lambda q: db.execute(q).fetchone()[0]
        return {**{k: v for k, v in _c()["status"].items()},
                "files": n("SELECT COUNT(*) FROM files"), "repos": n("SELECT COUNT(*) FROM repos"),
                "code_chunks": n("SELECT COUNT(*) FROM code_chunks"),
                "sessions": n("SELECT COUNT(*) FROM sessions WHERE n_chunks>0")}


@tool()
def current_scope() -> dict:
    """The slice of the index this connection searches: 'auto' = the repo the agent started in, 'all' = everything."""
    return scopes.describe(_scope())


@tool()
def list_scopes() -> dict:
    """Named scopes: each is a list of source ids (see list_sources) an agent can be limited to."""
    return {"scopes": scopes.named(), "current": scopes.describe(_scope())["name"]}


@tool()
def save_scope(name: str, sources: list[str], description: str = "") -> dict:
    """Create or replace a named scope, e.g. save_scope("payments", ["repos/<root>", "files/~/specs"]).
    Agents connect to it with ?scope=payments (HTTP) or `pensieve mcp --scope payments` (stdio). Agents started
    inside a repo that a scope includes get that scope automatically."""
    cur = dict(scopes.named())
    cur[name] = {"sources": sources, "description": description}
    return {"scopes": settings.update({"scopes": cur})["scopes"]}


@tool()
def delete_scope(name: str) -> dict:
    """Remove a named scope."""
    cur = dict(scopes.named())
    if name not in cur:
        raise KeyError(name)
    cur.pop(name)
    return {"scopes": settings.update({"scopes": cur})["scopes"]}


@tool()
def get_settings() -> dict:
    """Current settings with a description of each."""
    return {"settings": settings.public(settings.load()), "descriptions": settings.DESCRIPTIONS}


@tool()
def update_settings(changes: dict[str, Any]) -> dict:
    """Change settings, e.g. {"hotkey": "cmd+shift+space"}, {"editor": "cursor"}, {"max_repo_files": 20000},
    {"exclude_files": [...]}. To switch what gets indexed on or off, use set_source. Takes effect within seconds."""
    return {"settings": settings.public(settings.update(changes))}


@tool()
def list_sources() -> list[dict]:
    """What Pensieve indexes, by category (agents, files, repos, apps), with on/off state and item counts.
    Use the ids with set_source."""
    return [dict(id=c["id"], label=c["label"], enabled=c["enabled"], map=c["map"], count=c["count"],
                 items=[{k: i.get(k) for k in ("id", "label", "detail", "enabled", "map", "available", "count")}
                        for i in c["items"]])
            for c in sources.catalog(_c()["store"], _c()["repos"], _c()["files"])]


@tool()
def set_source(id: str, enabled: bool | None = None, map: bool | None = None) -> dict:
    """Switch a whole category ('agents', 'files', 'repos', 'apps') or one item ('agents/codex', 'files/~/Downloads',
    'repos/discover', 'repos/<repo root>', 'apps/apple_notes') on or off (`enabled`; off removes what it indexed),
    or only on or off the map (`map`; it stays searchable)."""
    if enabled is not None:
        sources.set_source(id, enabled, _c()["repos"])
    if map is not None:
        sources.set_source(id, map, _c()["repos"], map_only=True)
    return {k: settings.get(k) for k in ("disabled", "off_map")}


@tool()
def file_types() -> dict:
    """Each file type in Files & folders (text, pdf, office, data, code, media, other): whether its content is read
    (index: content | name | off), whether it shows on the map, how many files it has, and a histogram of their text
    length. Change one with update_settings({"file_types": {"pdf": {"index": "name", "map": false}}}); the chunk
    size and limit are the chunk_chars and max_chunks settings."""
    return _c()["files"].types()


@tool()
def add_folder(path: str) -> dict:
    """Index another folder (documents in it, and any git repos inside it)."""
    cur = settings.get("folders")
    return {"folders": settings.update({"folders": cur + [path]} if path not in cur else {})["folders"]}


@tool()
def remove_folder(path: str) -> dict:
    """Stop indexing a folder. Its documents drop out of search on the next sweep."""
    return {"folders": settings.update({"folders": [f for f in settings.get("folders") if f != path]})["folders"]}


@tool()
def index_path(path: str) -> dict:
    """Index a folder when the user asks you to: a git repo, a folder of repos, a folder of agent sessions, or a
    folder of documents (Pensieve works out which). Relative paths are from your working folder."""
    res = onboarding.index(path, agent_cwd.get())
    from pensieve import server
    server.watch.force_full()
    return res


@tool()
def usage_stats(days: float = 7) -> dict:
    """Pensieve's own MCP tool calls over the last `days`: per tool, the number of calls, errors, and p50/p95 time."""
    return usage.summary(days)


@tool()
def setup_guide() -> dict:
    """Start of setup (first run, or after a reset). Returns what Pensieve is (`about`), user profiles, the source
    categories with the folders scanning each one reads, the insights engines (`ai`) and the energy note. Reads no
    folders. Walk the user through it in order, one question at a time:
    1. Tell them what Pensieve is, from `about`.
    2. Ask which profiles fit them (several can).
    3. Tell them which folders the scan reads (`reads` of each profile's categories) and that macOS may ask them to
       allow access. When they agree, call setup_scan with those profiles.
    4. Show what the scan found, pre-checked items first, and ask what to index. Offer setup_add_source for folders
       or repos it missed.
    5. Ask which insights engine to use (`ai`; "none" is fine, it can change later in Settings).
    6. Tell them indexing takes a while and uses a lot of energy, so plug in a laptop, then call setup_start."""
    return onboarding.guide()


@tool()
def setup_scan(profiles: list[str] | None = None, categories: list[str] | None = None) -> list[dict]:
    """Look for what can be indexed, in the categories of the chosen profiles (ids from setup_guide), or in the
    named categories ('agents', 'files', 'repos', 'apps'). This reads the user's folders, so macOS may ask them for
    access. Returns each category's items: id (for setup_start), label, size, and `checked` = recommended."""
    cats = categories or (onboarding.categories_for(profiles) if profiles else None)
    return onboarding.scan(cats, fresh=True)


@tool()
def setup_add_source(kind: str, path: str) -> dict:
    """Add a folder the scan missed: kind 'files' (documents), 'repos' (a repo, or a folder of repos) or 'agents' (a
    folder of agent transcripts). Returns the source id to include in setup_start."""
    return {"id": onboarding.add(kind, path)}


@tool()
def setup_start(sources: list[str] | None = None, recommended: bool = False, ai: str | None = None) -> dict:
    """Finish setup: index exactly these source ids from setup_scan (or, with recommended=true, the pre-checked
    ones); everything else is switched off. `ai` sets the insights engine (an id from setup_guide's `ai`, or "none").
    Indexing starts at once and can take a while; search works meanwhile."""
    res = onboarding.start(sources, recommended, ai)
    from pensieve import server
    server.watch.force_full()
    return res


@tool()
def reindex() -> dict:
    """Rescan folders and repos now instead of waiting for the next sweep."""
    from pensieve import server
    server.watch.force_full()
    return {"ok": True}


_app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True)
_endpoint = _app.routes[0].app if hasattr(_app.routes[0], "app") else _app.routes[0].endpoint


class _Scoped:
    """ASGI app (a class, so Starlette doesn't treat it as a request/response function)."""
    async def __call__(self, scope, receive, send):
        await _scoped(scope, receive, send)


_http_clients = {}  # User-Agent -> the name the agent gave in `initialize` (HTTP is stateless: later calls don't carry it)


async def _note_client(scope, receive):
    """Read the request body; if it is `initialize`, remember the agent's name for its User-Agent. Returns a receive
    that replays the body to the MCP endpoint."""
    body, more = b"", True
    while more:
        msg = await receive()
        if msg["type"] != "http.request":
            return lambda: _done(msg)
        body += msg.get("body", b"")
        more = msg.get("more_body", False)
    try:
        req = json.loads(body)
        if isinstance(req, dict) and req.get("method") == "initialize":
            name = ((req.get("params") or {}).get("clientInfo") or {}).get("name")
            if name:
                ua = dict(scope.get("headers") or []).get(b"user-agent", b"").decode(errors="replace")
                _http_clients[ua] = str(name)[:80]
    except ValueError:
        pass
    sent = False

    async def replay():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()
    return replay


async def _done(msg):
    return msg


async def _scoped(scope, receive, send):
    """Every MCP request runs inside the connection's scope: ?scope=<name>|auto|all (&cwd=<dir> for auto)."""
    from urllib.parse import parse_qs
    q = {k: v[0] for k, v in parse_qs(scope.get("query_string", b"").decode()).items()}
    try:
        sc = scopes.resolve(q.get("scope", ""), q.get("cwd"), actions.ctx.get("repos"))
    except KeyError as e:
        from starlette.responses import JSONResponse
        await JSONResponse({"error": f"unknown Pensieve scope {e.args[0]!r}"}, status_code=404)(scope, receive, send)
        return
    if scope.get("method") == "POST":
        receive = await _note_client(scope, receive)
    ua = dict(scope.get("headers") or []).get(b"user-agent", b"").decode(errors="replace")
    token = scopes.current.set(sc)
    ptoken = tool_profile.set("all" if q.get("tools") == "all" else "agent")
    ctoken = agent_cwd.set(q.get("cwd"))
    ktoken = agent_client.set(q.get("client") or _http_clients.get(ua) or "http")
    ntoken = agent_conn.set(q.get("conn"))
    try:
        await _endpoint(scope, receive, send)
    finally:
        agent_conn.reset(ntoken)
        agent_client.reset(ktoken)
        agent_cwd.reset(ctoken)
        tool_profile.reset(ptoken)
        scopes.current.reset(token)


# server.py adds this route to the FastAPI app (no mount, so no /mcp -> /mcp/ redirect)
from starlette.routing import Route  # noqa: E402
app_routes = [Route("/mcp", endpoint=_Scoped(), methods=["GET", "POST", "DELETE"])]
