"""MCP server: lets any agent search, read, and configure Pensieve. Served at http://127.0.0.1:<port>/mcp
(streamable HTTP); `pensieve mcp` bridges stdio to it for agents that only speak stdio."""
import functools
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import actions, scopes, settings, sources

mcp = MCPServer(
    name="pensieve",
    instructions=(
        "Pensieve indexes this Mac's documents, git repos (with blame and history) and past AI agent sessions. "
        "Use search first: plain words search by meaning, \"double quotes\" require exact words, -word excludes, and "
        "kind:file|code|session, ext:pdf, in:<folder or repo> filter. Then read(id) for full text. "
        "who_knows finds the people who wrote the code about a topic; use it to suggest who to ask. "
        "Everything is limited to this connection's scope (see current_scope): e.g. the repo the agent runs in. "
        "Settings (indexed folders, repo sweep, hotkey, editor, LLM) can be read and changed with get_settings/update_settings."),
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
    """Register an MCP tool; bad input (ValueError, unknown id) reaches the agent as a readable error."""
    def deco(fn):
        @functools.wraps(fn)
        def wrap(*a, **k):
            try:
                return fn(*a, **k)
            except KeyError as e:
                raise ToolError(f"not found: {e.args[0] if e.args else e}") from e
            except ValueError as e:
                raise ToolError(str(e)) from e
        return mcp.tool(name=name)(wrap)
    return deco


@tool()
def search(query: str, limit: int = 10, kind: str | None = None) -> dict:
    """Search files, code and agent sessions. Plain words match by meaning; "quoted phrases" must appear exactly;
    -word excludes; filters: kind:file|code|session, ext:<extension>, in:<path or repo substring>.
    Returns ranked results with an id to pass to read/open/similar."""
    r = _c()["searcher"].find(query, limit, [kind] if kind else None, scope=_scope())
    return {"query": r["query"], "scope": r["scope"]["name"], "results": [{k: x[k] for k in ("id", "kind", "title", "subtitle", "path", "line", "snippet", "match", "score")}
                                             for x in r["results"]]}


@tool()
def read(id: str, max_chars: int = 12000) -> dict:
    """Full text of a search result: a document, a code chunk (with its owners, recent commits and the agent
    sessions that touched it), or an agent session transcript with its summary."""
    _check(id)
    return actions.read(id, max_chars)


@tool()
def similar(id: str, limit: int = 8) -> list[dict]:
    """Items most similar in meaning to a file, code or session id (within this connection's scope)."""
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
        hits = _c()["searcher"].find(c["text"][:1500], limit + 1, ["code"], scope=sc)["results"] if c else []
        return [h for h in hits if h["id"] != id][:limit]
    raise ValueError("id must start with file:, code: or session:")


@tool()
def who_knows(topic: str, repo: str | None = None) -> dict:
    """People who wrote the code most related to a topic (git blame weighted by relevance), with the files involved.
    Use this to suggest who to talk to. Optionally limit to one repo name (see list_repos)."""
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


@tool(name="open")
def open_item(id: str, action: str = "open") -> dict:
    """Open an item on the user's Mac. action: 'open' (default app, or the configured editor at the line for code),
    'reveal' (show in Finder) or 'visualize' (show it on the Pensieve map)."""
    _check(id)
    return actions.open_item(id, action)


@tool()
def show_visualizer(view: str | None = None) -> dict:
    """Open the Pensieve visualizer. view: code, map, files, insights or settings."""
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
    """The slice of the index this connection works within (set by the agent's MCP config: ?scope=<name>, or 'auto'
    = the repo the agent was started in). 'all' means everything Pensieve indexes."""
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
    return {"settings": settings.load(), "descriptions": settings.DESCRIPTIONS}


@tool()
def update_settings(changes: dict[str, Any]) -> dict:
    """Change settings, e.g. {"hotkey": "cmd+shift+space"}, {"editor": "cursor"}, {"max_repo_files": 20000},
    {"exclude_files": [...]}. To switch what gets indexed on or off, use set_source. Takes effect within seconds."""
    return {"settings": settings.update(changes)}


@tool()
def list_sources() -> list[dict]:
    """What Pensieve indexes, by category (agents, files, repos, apps), with on/off state and item counts.
    Use the ids with set_source."""
    return [dict(id=c["id"], label=c["label"], enabled=c["enabled"], count=c["count"],
                 items=[{k: i.get(k) for k in ("id", "label", "detail", "enabled", "available", "count")} for i in c["items"]])
            for c in sources.catalog(_c()["store"], _c()["repos"], _c()["files"])]


@tool()
def set_source(id: str, enabled: bool) -> dict:
    """Switch a whole category ('agents', 'files', 'repos', 'apps') or one item ('agents/codex', 'files/~/Downloads',
    'repos/sweep', 'repos/<repo root>', 'apps/apple_notes') on or off. Switching off removes what it indexed."""
    return {"disabled": sources.set_source(id, enabled, _c()["repos"])["disabled"]}


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
def reindex() -> dict:
    """Rescan folders and repos now instead of waiting for the next sweep."""
    from . import server
    server.watch.force_full()
    return {"ok": True}


_app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True)
_endpoint = _app.routes[0].app if hasattr(_app.routes[0], "app") else _app.routes[0].endpoint


class _Scoped:
    """ASGI app (a class, so Starlette doesn't treat it as a request/response function)."""
    async def __call__(self, scope, receive, send):
        await _scoped(scope, receive, send)


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
    token = scopes.current.set(sc)
    try:
        await _endpoint(scope, receive, send)
    finally:
        scopes.current.reset(token)


# server.py adds this route to the FastAPI app (no mount, so no /mcp -> /mcp/ redirect)
from starlette.routing import Route  # noqa: E402
app_routes = [Route("/mcp", endpoint=_Scoped(), methods=["GET", "POST", "DELETE"])]
