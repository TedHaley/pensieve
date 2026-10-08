"""MCP server: lets any agent search, read, and configure Pensieve. Served at http://127.0.0.1:<port>/mcp
(streamable HTTP); `pensieve mcp` bridges stdio to it for agents that only speak stdio."""
import functools
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import actions, settings, sources

mcp = MCPServer(
    name="pensieve",
    instructions=(
        "Pensieve indexes this Mac's documents, git repos (with blame and history) and past AI agent sessions. "
        "Use search first: plain words search by meaning, \"double quotes\" require exact words, -word excludes, and "
        "kind:file|code|session, ext:pdf, in:<folder or repo> filter. Then read(id) for full text. "
        "who_knows finds the people who wrote the code about a topic; use it to suggest who to ask. "
        "Settings (indexed folders, repo sweep, hotkey, editor, LLM) can be read and changed with get_settings/update_settings."),
)


def _c():
    return actions.ctx


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
    r = _c()["searcher"].find(query, limit, [kind] if kind else None)
    return {"query": r["query"], "results": [{k: x[k] for k in ("id", "kind", "title", "subtitle", "path", "line", "snippet", "match", "score")}
                                             for x in r["results"]]}


@tool()
def read(id: str, max_chars: int = 12000) -> dict:
    """Full text of a search result: a document, a code chunk (with its owners, recent commits and the agent
    sessions that touched it), or an agent session transcript with its summary."""
    return actions.read(id, max_chars)


@tool()
def similar(id: str, limit: int = 8) -> list[dict]:
    """Items most similar in meaning to a file or session id."""
    kind, _, ref = id.partition(":")
    if kind == "file":
        return _c()["files"].similar(ref, limit)
    if kind == "session":
        return [dict(s, id=f"session:{s['id']}") for s in _c()["store"].similar(ref, limit)]
    if kind == "code":
        c = _c()["repos"].chunk(int(ref))
        hits = _c()["searcher"].find(c["text"][:1500], limit + 1, ["code"])["results"] if c else []
        return [h for h in hits if h["id"] != id][:limit]
    raise ValueError("id must start with file:, code: or session:")


@tool()
def who_knows(topic: str, repo: str | None = None) -> dict:
    """People who wrote the code most related to a topic (git blame weighted by relevance), with the files involved.
    Use this to suggest who to talk to. Optionally limit to one repo name (see list_repos)."""
    r = _c()["repos"].experts(q=topic, repo=repo)
    return {"people": r["experts"], "code": [dict(id=f"code:{c['id']}", repo=c["repo"], path=c["path"], line=c["start"],
                                                 score=round(c["score"], 3)) for c in r["chunks"]]}


@tool()
def list_repos() -> list[dict]:
    """Indexed git repos with their size and how many agent sessions ran in each."""
    return _c()["repos"].list()


@tool()
def team(repo: str, days: int = 90) -> dict:
    """What each person committed in a repo over the last `days`: commit counts, main areas, recent commit subjects."""
    t = _c()["repos"].team(repo, days)
    if t is None:
        raise ValueError(f"unknown repo {repo!r}")
    return {"repo": repo, "days": days, "people": [
        dict(name=p["name"], email=p["email"], me=p["me"], commits=p["commits"], areas=p["areas"],
             recent=[s["subject"] for s in p["subjects"][:5]]) for p in t["people"][:15]]}


@tool()
def unexplored(repo: str) -> dict:
    """Areas of a repo next to the user's own work that their agent sessions haven't touched yet, and who owns them."""
    r = _c()["repos"].territory(repo)
    if r is None:
        raise ValueError(f"unknown repo {repo!r}")
    return {"coverage": r["coverage"], "suggestions": r["suggestions"]}


@tool(name="open")
def open_item(id: str, action: str = "open") -> dict:
    """Open an item on the user's Mac. action: 'open' (default app, or the configured editor at the line for code),
    'reveal' (show in Finder) or 'visualize' (show it on the Pensieve map)."""
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


# a Starlette app with one route at /mcp; server.py adds that route to the FastAPI app (no mount, so no /mcp -> /mcp/ redirect)
app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True)
