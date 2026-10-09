import asyncio
from collections import Counter
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import actions, config, llm, scopes, search as search_mod, settings, sources, summarize
from .datamap import DataMap
from .files import Files
from .watch import Watch
from .indexer import Store
from .repos import Repos
from . import mcp_server

store: Store | None = None
repos: Repos | None = None
files: Files | None = None
datamap: DataMap | None = None
searcher: search_mod.Searcher | None = None
subscribers: set[asyncio.Queue] = set()
status = {"indexing": False, "message": "starting", "layout": False, "insights_running": False, "insights_scope": None}
wake_files = asyncio.Event()  # set (thread-safely) by the file watcher
wake_repos = asyncio.Event()
watch: "Watch | None" = None
inflight: set[str] = set()
failed_topics: set[str] = set()  # Data-map topics the AI couldn't name this run (by membership signature)  # background LLM jobs (scoped topic names, team summaries) already running

def broadcast(event: dict):
    for q in list(subscribers):
        q.put_nowait(event)


FULL_SWEEP_SECONDS = 300  # safety net behind the file watcher; also discovers new repos and folders


async def _run(fn, *a):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *a)


async def _wait(ev: asyncio.Event, timeout):
    try:
        await asyncio.wait_for(ev.wait(), timeout)
    except asyncio.TimeoutError:
        pass
    ev.clear()


async def watcher():
    """Sessions and documents: re-indexed as soon as the file watcher reports a change (and polled every few
    seconds). Repos run in their own loop so a long first index of a big repo doesn't hold these up."""
    loop = asyncio.get_running_loop()
    status.update(layout=True, message="building exact-match index")
    broadcast({"type": "status", **status})
    try:
        await _run(search_mod.ensure_fts, store.db, store.lock, store.meta)
        status.update(message="laying out map")
        broadcast({"type": "status", **status})
        if await _run(store.reproject):
            broadcast({"type": "updated"})
        if await _run(repos.reproject):
            broadcast({"type": "repos"})
    except Exception:
        import traceback
        traceback.print_exc()
    status.update(layout=False)
    watch.configure(repos.checkouts())
    asyncio.create_task(repo_watcher())
    last_full = 0
    while True:
        try:
            d = watch.take()
            if sorted(str(p) for p in files.roots()) != sorted(watch.folders):  # a folder appeared or went away
                d["full_files"] = True
            n = await _run(lambda: store.sync_files(
                on_progress=lambda k: loop.call_soon_threadsafe(broadcast, {"type": "updated", "sessions": k})))
            if n:
                broadcast({"type": "updated", "sessions": n})
            if d["full_files"] or time.time() - last_full > FULL_SWEEP_SECONDS:
                last_full = time.time()
                status.update(indexing=True, message="indexing files")
                broadcast({"type": "status", **status})
                if await _run(lambda: files.sync(force=True, on_progress=lambda k: loop.call_soon_threadsafe(broadcast, {"type": "files"}))):
                    broadcast({"type": "files"})
                watch.configure(repos.checkouts())
            elif d["files"]:
                changed, retry = await _run(files.sync_paths, d["files"])
                if changed:
                    broadcast({"type": "files"})
                if retry:  # still being written: look again once it settles
                    watch.requeue_files(retry)
                    loop.call_later(config.SETTLE_SECONDS + 0.5, wake_files.set)
            # self-heal: if an earlier layout attempt failed, points without positions would never appear
            if await _run(store.needs_layout) and await _run(store.reproject):
                broadcast({"type": "updated"})
            if await _run(files.needs_layout) and await _run(files.reproject):
                broadcast({"type": "files"})
            if await _run(datamap.needs_layout) and await _run(datamap.reproject):  # the joint Data map
                broadcast({"type": "data"})
            if status.get("repo_busy"):
                status.update(message="indexing repos")
            else:
                status.update(indexing=False, message="idle")
            broadcast({"type": "status", **status})
        except Exception as e:
            import traceback
            traceback.print_exc()
            status.update(indexing=False, layout=False, message=f"error: {e!r}")
        await _wait(wake_files, config.POLL_SECONDS)
        await asyncio.sleep(0.8)  # let a burst of saves land together


async def repo_watcher():
    last_full = 0
    while True:
        try:
            d_repos, asked = watch.take_repos()
            full = asked or time.time() - last_full > FULL_SWEEP_SECONDS
            if full or d_repos:
                status.update(indexing=True, repo_busy=True, message="indexing repos")
                broadcast({"type": "status", **status})
                if full:
                    last_full = time.time()
                    n = await _run(lambda: repos.sync(force=True))
                else:
                    n = await _run(lambda: repos.sync(only=d_repos))
                status.update(repo_busy=False, indexing=False, message="idle")
                watch.configure(repos.checkouts())
                if n:
                    broadcast({"type": "repos"})
                if await _run(repos.needs_layout) and await _run(repos.reproject):
                    broadcast({"type": "repos"})
                broadcast({"type": "status", **status})
        except Exception as e:
            import traceback
            traceback.print_exc()
            status.update(repo_busy=False, indexing=False, message=f"error: {e!r}")
        await _wait(wake_repos, 30)
        await asyncio.sleep(1.5)


def _person(author):
    return author.split(" <")[0]


def _codebase(sids=None, limit=2):
    """Grounding text for insights: unexplored areas near the user's work and what teammates are doing."""
    scope = repos.session_scope()
    per = {}
    for sid, v in scope.items():
        if v["root"] and (sids is None or sid in sids):
            per[v["project"]] = per.get(v["project"], 0) + 1
    known = {r["name"] for r in repos.list()}
    out = []
    for name in sorted((n for n in per if n in known), key=lambda n: -per[n])[:limit]:
        team = repos.team(name)
        terr = repos.territory(name, sids)
        if not terr:
            continue
        cached = json.loads(store.meta(_team_key(team)) or "{}") if team else {}
        worked = [f for f in terr["footprint"] if f["sessions"]]
        out.append(f"Repository {name}: the user's {terr['sessions']} sessions touched {len(worked)} of "
                   f"{len(terr['footprint'])} code areas ({round(terr['coverage'] * 100)}% of the code).")
        if worked:
            out.append("Areas the user worked in: " + ", ".join(
                f"{f['area']} ({f['sessions']} sessions)" for f in sorted(worked, key=lambda f: -f["sessions"])[:8]))
        out.append("Unexplored areas closest to the user's work:")
        names = {p["email"]: p["name"] for p in team["people"]} if team else {}
        for x in terr["suggestions"][:6]:
            recent = [names[e] for e in x["recent_people"] if e in names]
            who = ", ".join((recent or [_person(o) for o in x["owners"]])[:2])
            out.append(f"- area {x['area']} ({x['chunks']} code chunks"
                       + (f"; next to {x['near']} which they worked on" if x["near"] else "")
                       + (f"; {x['recent_commits']} commits in the last 90 days" if x["recent_commits"] else "")
                       + (f"; people: {who}" if who else "") + ")")
        if team and team["people"]:
            out.append(f"What teammates did in the last {team['days']} days:")
            for p in [p for p in team["people"] if not p["me"]][:6]:
                focus = cached.get(p["email"], {}).get("focus")
                out.append(f"- {p['name']}: {p['commits']} commits" + (f", {focus}" if focus else "")
                           + "; areas " + ", ".join(a["area"] for a in p["areas"][:3]))
        out.append("")
    return "\n".join(out)


def _team_key(team):
    return f"team:{team['root']}:{team['days']}:{team['head']}"


def _insights_due():
    """The main insights: written once at first, then rewritten when out of date (if that's switched on)."""
    raw = store.meta("insights")
    if raw is None:
        return True
    return bool(settings.get("insights_auto")) and summarize.insights_stale(store, json.loads(raw), None, store.topics())


async def run_insights(scope=None, sids=None):
    if status["insights_running"]:
        return
    status.update(insights_running=True, insights_scope=scope)
    broadcast({"type": "status", **status})
    loop = asyncio.get_running_loop()
    try:
        def job():
            topics = store.scoped_topics(sids) if scope else None
            ground = _codebase(set(sids) if sids is not None else None)
            return summarize.generate_insights(store, scope, set(sids) if sids is not None else None, topics, ground)
        await loop.run_in_executor(None, job)
        broadcast({"type": "insights", "scope": scope})
    except Exception as e:
        msg = ("Choose an AI engine in Settings → AI & Insights to generate insights." if isinstance(e, llm.Offline)
               else f"Insight generation failed: {e!r}"[:200])
        broadcast({"type": "toast", "message": msg})
    finally:
        status.update(insights_running=False, insights_scope=None)
        broadcast({"type": "status", **status})


async def name_scoped(topics):
    loop = asyncio.get_running_loop()
    todo = [t for t in topics if t["key"] not in inflight]
    if not todo:
        return
    inflight.update(t["key"] for t in todo)
    try:
        await asyncio.gather(*[loop.run_in_executor(None, summarize.name_topic, store, t, t["key"]) for t in todo],
                             return_exceptions=True)
        broadcast({"type": "scoped_topics"})
    finally:
        inflight.difference_update(t["key"] for t in todo)


async def summarize_team(team, key):
    if key in inflight:
        return
    inflight.add(key)
    try:
        await asyncio.get_running_loop().run_in_executor(None, summarize.summarize_team, store, team, key)
        broadcast({"type": "team", "repo": team["repo"]})
    except Exception as e:
        broadcast({"type": "toast", "message": f"Team summary failed: {e!r}"[:200]})
    finally:
        inflight.discard(key)


async def enricher():
    """Low-priority LLM work: session summaries -> topic names -> first insights -> chunk summaries."""
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(3)
    while True:
        try:
            llm.sweep()  # unload the built-in model after 10 idle minutes
            if llm.paused():  # e.g. on battery: background AI work waits
                await asyncio.sleep(30)
                continue
            if llm.provider() == "none":  # not chosen yet, or off: nothing to do (and nothing to report as broken)
                if status.get("llm_offline"):
                    status["llm_offline"] = False
                    broadcast({"type": "status", **status})
                await asyncio.sleep(10)
                continue
            sess, topics, chunks = summarize.pending(store)
            if llm.provider() in ("claude", "codex"):
                chunks = []  # one CLI call per conversation chunk would be thousands of cloud calls
            data_topics = [t for t in await _run(datamap.pending_topics) if t["sig"] not in failed_topics]
            if data_topics:  # a handful of quick calls; first, so a session that keeps changing can't hold them up
                res = await asyncio.gather(*[loop.run_in_executor(pool, summarize.name_data_topic, datamap, t) for t in data_topics[:3]],
                                           return_exceptions=True)
                if any(isinstance(r, (httpx.ConnectError, llm.Offline)) for r in res):
                    raise next(r for r in res if isinstance(r, (httpx.ConnectError, llm.Offline)))
                failed_topics.update(t["sig"] for t, r in zip(data_topics, res) if isinstance(r, Exception))  # keep keyword names
                broadcast({"type": "data_topics"})
            elif sess:
                await asyncio.gather(*[loop.run_in_executor(pool, summarize.summarize_session, store, s) for s in sess[:3]])
                broadcast({"type": "summaries"})
            elif topics:
                await asyncio.gather(*[loop.run_in_executor(pool, summarize.name_topic, store, t) for t in topics[:3]])
                broadcast({"type": "topics"})
            elif not status["insights_running"] and store.topics() and await _run(_insights_due):
                await run_insights()
            elif chunks:
                await asyncio.gather(*[loop.run_in_executor(pool, summarize.summarize_chunk, store, c, t) for c, t in chunks[:3]])
            else:
                await asyncio.sleep(10)
            if status.get("llm_offline"):
                status["llm_offline"] = False
                broadcast({"type": "status", **status})
        except (httpx.ConnectError, llm.Offline):  # engine not running/available: everything but AI features still works
            if not status.get("llm_offline"):
                status["llm_offline"] = True
                broadcast({"type": "status", **status})
            await asyncio.sleep(30)
        except Exception as e:
            status["message"] = f"enricher: {e!r}"[:200]
            await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app):
    global store, repos, files, searcher, datamap
    store = Store()
    repos = Repos(store)
    files = Files(store)
    datamap = DataMap(store, repos, files)
    searcher = search_mod.Searcher(store, repos, files)
    actions.bind(store=store, repos=repos, files=files, searcher=searcher, status=status, port=config.PORT)
    loop = asyncio.get_running_loop()

    def changed(keys):  # settings edited from the UI or MCP: re-sweep promptly, tell the Mac app and the UI
        if "ai" in keys:
            if settings.get("ai") == "builtin":
                llm.start_download()  # in the background; the app stays usable
            else:
                llm.stop_builtin()
        repos._extra = None  # re-run the repo sweep with the new folders/roots
        watch.force_full()
        loop.call_soon_threadsafe(broadcast, {"type": "settings", "keys": sorted(keys)})
    settings.on_change(changed)

    def wake():
        loop.call_soon_threadsafe(wake_files.set)
        loop.call_soon_threadsafe(wake_repos.set)
    global watch
    watch = Watch(wake)
    repos.pending = watch.take_changed_repos
    repos.requeue = watch.requeue_repos
    if settings.get("ai") == "builtin":
        llm.start_download()
    tasks = [asyncio.create_task(watcher()), asyncio.create_task(enricher())]
    async with mcp_server.mcp.session_manager.run():
        yield
    for t in tasks:
        t.cancel()
    llm.stop_builtin()


app = FastAPI(lifespan=lifespan)
app.router.routes.extend(mcp_server.app_routes)  # MCP at /mcp
STATIC = Path(__file__).parent / "static"
NO_CACHE = {"Cache-Control": "no-store"}


@app.get("/")
def root():
    return FileResponse(STATIC / "index.html", headers=NO_CACHE)


@app.get("/static/{name}")
def static(name: str):
    f = (STATIC / name).resolve()
    if f.parent != STATIC.resolve() or not f.is_file():
        raise HTTPException(404)
    return FileResponse(f, headers=NO_CACHE)


@app.get("/api/status")
def get_status():
    with store.lock:
        return _status()


def _status():
    db = store.db
    n = db.execute("SELECT COUNT(*), COALESCE(SUM(n_chunks),0) FROM sessions WHERE n_chunks>0").fetchone()
    return {**status, "sessions": n[0], "chunks": n[1],
            "files": db.execute("SELECT COUNT(*) FROM files").fetchone()[0],
            "repos": db.execute("SELECT COUNT(*) FROM repos").fetchone()[0],
            "summarized_sessions": db.execute("SELECT COUNT(*) FROM sessions WHERE summary IS NOT NULL AND n_chunks>0").fetchone()[0],
            "summarized_chunks": db.execute("SELECT COUNT(*) FROM chunks WHERE summary IS NOT NULL").fetchone()[0],
            "topics_named": db.execute("SELECT COUNT(*) FROM topics WHERE name IS NOT NULL").fetchone()[0],
            "topics": db.execute("SELECT COUNT(*) FROM topics").fetchone()[0],
            "code_chunks": db.execute("SELECT COUNT(*) FROM code_chunks").fetchone()[0],
            "llm": settings.get("llm_model"), "llm_url": settings.get("llm_url"), "embed": config.EMBED_MODEL,
            "ai": settings.get("ai")}


@app.get("/api/projects")
def projects():
    """Projects are canonical repos (clones and worktrees merged); non-repo folders stay as-is."""
    scope = repos.session_scope()
    by = {}
    with store.lock:
        rows = store.db.execute("SELECT id, n_chunks, updated, project FROM sessions WHERE n_chunks>0").fetchall()
    home = str(Path.home())
    for sid, n, upd, cwd in rows:
        v = scope.get(sid)
        if not v:
            continue
        e = by.setdefault(v["project"], dict(name=v["project"], sessions=0, chunks=0, last="", repo=bool(v["root"]),
                                              root=v["root"], folders=set(), where=Counter()))
        e["sessions"] += 1; e["chunks"] += n; e["last"] = max(e["last"], upd or "")
        e["folders"].add(v["folder"])
        if cwd:
            e["where"][cwd.replace(home, "~", 1)] += 1
    # `path`: where most of a project's sessions ran (a folder that isn't an indexed repo shows where it is)
    return [{**{k: v for k, v in e.items() if k != "where"}, "folders": sorted(e["folders"]), "path":e["where"].most_common(1)[0][0] if e["where"] else None}
            for e in sorted(by.values(), key=lambda e: -e["sessions"])]


@app.get("/api/points")
def points(level: str = "session"):
    res = store.points(level)
    scope = repos.session_scope()
    for p in res["points"]:
        v = scope.get(p["session"])
        if v:
            p["folder"] = p["project"]
            p["project"] = v["project"]
            if level == "session":
                p["dirs"] = v["dirs"]
    return res


class ScopeReq(BaseModel):
    session_ids: list[str]
    scope: str | None = None


@app.post("/api/topics/scoped")
async def scoped_topics(req: ScopeReq):
    ts = await asyncio.get_running_loop().run_in_executor(None, store.scoped_topics, req.session_ids)
    unnamed = [t for t in ts if not t["named"]]
    if unnamed:
        asyncio.create_task(name_scoped(unnamed))
    return ts


@app.get("/api/topics")
def topics():
    return store.topics()


@app.get("/api/session/{sid:path}")
def session(sid: str):
    s = store.session(sid)
    if not s:
        raise HTTPException(404)
    return s


@app.get("/api/similar/{sid:path}")
def similar(sid: str):
    return store.similar(sid)


def _csv(v: str | None):
    return [x for x in (v or "").split(",") if x]


@app.get("/api/search")
def search(q: str, k: int = 12, projects: str = "", sources: str = "", since: str = "", until: str = ""):
    return store.search(q, k, projects=_csv(projects), sources=_csv(sources), since=since or None, until=until or None)


@app.get("/api/insights")
def get_insights(scope: str = ""):
    raw = store.meta(summarize.insight_key(scope or None))
    return {"running": status["insights_running"], "running_scope": status["insights_scope"],
            "data": json.loads(raw) if raw else None}


@app.post("/api/insights")
async def refresh_insights(req: ScopeReq | None = None):
    if status["insights_running"]:
        return {"running": True, "busy": True}
    scope = req.scope if req else None
    asyncio.create_task(run_insights(scope, req.session_ids if req and scope else None))
    return {"running": True}


class StaleReq(BaseModel):
    scope: str
    session_ids: list[str]


@app.post("/api/insights/stale")
def insights_stale(req: StaleReq):
    """Whether a scope's insights are out of date for the sessions it has now (the page then refreshes them)."""
    raw = store.meta(summarize.insight_key(req.scope))
    if not raw or not settings.get("insights_auto"):
        return {"stale": False}
    sids = set(req.session_ids)
    return {"stale": summarize.insights_stale(store, json.loads(raw), sids, store.scoped_topics(sids))}


@app.get("/api/team/{name}")
async def team(name: str, days: int = 90):
    loop = asyncio.get_running_loop()
    t = await loop.run_in_executor(None, repos.team, name, days)
    if t is None:
        raise HTTPException(404)
    key = _team_key(t)
    raw = store.meta(key)
    if raw is None and t["people"]:
        asyncio.create_task(summarize_team(t, key))
    return {**{k: v for k, v in t.items() if k != "area_activity"}, "summaries": json.loads(raw) if raw else None,
            "summarizing": raw is None and bool(t["people"])}


@app.post("/api/territory/{name}")
async def territory(name: str, req: ScopeReq | None = None):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, repos.team, name, 90)  # warms per-area recent activity
    sids = set(req.session_ids) if req and req.scope else None
    r = await loop.run_in_executor(None, repos.territory, name, sids)
    if r is None:
        raise HTTPException(404)
    return r


@app.get("/api/events")
async def events():
    q: asyncio.Queue = asyncio.Queue()
    subscribers.add(q)

    async def gen():
        try:
            yield f"data: {json.dumps({'type': 'status', **status})}\n\n"
            while True:
                try:
                    yield f"data: {json.dumps(await asyncio.wait_for(q.get(), 25))}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            subscribers.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/repos")
def list_repos():
    return repos.list()


@app.get("/api/repo/{name}/points")
def repo_points(name: str):
    r = repos.points(name)
    if r is None:
        raise HTTPException(404)
    return r


@app.get("/api/repo/{name}/overlaps")
def repo_overlaps(name: str):
    return repos.overlaps(name)


@app.get("/api/repo/{name}/owners")
def repo_owners(name: str):
    return repos.owners(name)


@app.get("/api/repo/{name}/gaps")
def repo_gaps(name: str, inactive_days: int = 180):
    r = repos.knowledge_gaps(name, inactive_days)
    if r is None:
        raise HTTPException(404)
    return r


@app.get("/api/repo/{name}/people")
def repo_people(name: str):
    return repos.people(name)


@app.get("/api/code/{cid}")
def code_chunk(cid: int):
    r = repos.chunk(cid)
    if r is None:
        raise HTTPException(404)
    return r


@app.get("/api/experts")
def experts(q: str, repo: str | None = None):
    return repos.experts(q=q, repo=repo)


@app.get("/api/sessioncode/{sid:path}")
def session_code(sid: str):
    r = repos.session_code(sid)
    if r is None:
        raise HTTPException(404)
    return r


# ---- unified search, files, settings, opening things -------------------------------
def _scope_param(scope: str | None, cwd: str | None = None):
    """?scope= on a request; absent -> the default_scope setting. 'all' -> everything."""
    name = settings.get("default_scope") if scope is None else scope
    try:
        return scopes.resolve(name, cwd, repos)
    except KeyError:
        raise HTTPException(404, f"unknown scope {name!r}")


@app.get("/api/find")
def find(q: str, limit: int = 20, kinds: str = "", scope: str | None = None, cwd: str | None = None):
    return searcher.find(q, limit, _csv(kinds) or None, scope=_scope_param(scope, cwd))


def _scope_info(name, sc):
    s = scopes.resolve(name, None, repos)
    n = {k: (len(a) if (a := scopes.allowlist(s, k, store, repos, idx)) is not None else None)
         for k, idx in (("file", files.index), ("code", repos.index), ("session", store.index))}
    return dict(name=name, description=sc.get("description", ""), sources=sc["sources"], chunks=n,
                sessions=len(scopes.session_ids(s, store, repos)))


@app.get("/api/scopes")
def list_scopes():
    return {"scopes": [_scope_info(n, sc) for n, sc in scopes.named().items()],
            "default_scope": settings.get("default_scope")}


class ScopeDef(BaseModel):
    sources: list[str]
    description: str = ""


@app.put("/api/scopes/{name}")
def save_scope(name: str, req: ScopeDef):
    cur = dict(scopes.named())
    cur[name] = {"sources": req.sources, "description": req.description}
    try:
        settings.update({"scopes": cur})
    except ValueError as e:
        raise HTTPException(400, str(e))
    return list_scopes()


@app.delete("/api/scopes/{name}")
def delete_scope(name: str):
    cur = dict(scopes.named())
    cur.pop(name, None)
    changes = {"scopes": cur}
    if settings.get("default_scope") == name:
        changes["default_scope"] = ""
    settings.update(changes)
    return list_scopes()


@app.get("/api/scopes/resolve")
def resolve_scope(scope: str = "auto", cwd: str | None = None):
    """What an agent would see: e.g. /api/scopes/resolve?scope=auto&cwd=/path/to/repo."""
    return scopes.describe(_scope_param(scope, cwd))


@app.get("/api/data/points")
def data_points():
    """Everything on one map: documents, code files (main checkout) and agent sessions."""
    return datamap.points()


@app.get("/api/data/topics")
async def data_topics(folders: str = ""):
    """Topics on the Data map: clusters of items about the same thing, with keyword or AI-written names. With
    `folders` ('|'-separated, relative to home), topics among just the items in those folders, plus which topic
    each item is in; their AI names are written in the background and announced with a 'data_topics' event."""
    loop = asyncio.get_running_loop()
    ai = await loop.run_in_executor(None, llm.provider)
    paused = await loop.run_in_executor(None, llm.paused)
    if not folders:
        return {"topics": await loop.run_in_executor(None, datamap.topics), "ai": ai != "none", "ai_paused": paused}
    home = str(Path.home())
    abs_ = [f if f.startswith("/") else f"{home}/{f}" for f in folders.split("|") if f]
    r = await loop.run_in_executor(None, datamap.scoped_topics, abs_)
    todo = [t for t in r["topics"] if not t["named"]]
    if todo and ai != "none" and not paused:
        asyncio.create_task(name_data_scoped(todo))
    return {**r, "ai": ai != "none", "ai_paused": paused}


class ViewTopicsReq(BaseModel):
    rev: str
    idx: list[int]
    name: bool = False  # also write AI names for the bigger topics (the page asks once the view has settled)


@app.post("/api/data/view_topics")
async def view_topics(req: ViewTopicsReq):
    """Topics among exactly the items on screen (indexes into /api/data/points, list `rev`): zooming in or filtering
    re-clusters what's left. 409 when the page's points are out of date."""
    loop = asyncio.get_running_loop()
    try:
        r = await loop.run_in_executor(None, datamap.view_topics, req.rev, req.idx)
    except ValueError:
        raise HTTPException(409, "points changed; reload them")
    ai = await loop.run_in_executor(None, llm.provider)
    paused = await loop.run_in_executor(None, llm.paused)
    if req.name and ai != "none" and not paused:
        size = Counter(r["of"].values())
        todo = [t for t in r["topics"] if not t["named"] and size[t["id"]] >= 25]
        if todo:
            asyncio.create_task(name_data_scoped(todo))
    return {**r, "ai": ai != "none", "ai_paused": paused}


async def name_data_scoped(topics):
    loop = asyncio.get_running_loop()
    todo = [t for t in topics if t["key"] not in inflight and t["sig"] not in failed_topics]
    if not todo:
        return
    inflight.update(t["key"] for t in todo)
    try:
        res = await asyncio.gather(*[loop.run_in_executor(None, summarize.name_data_topic, datamap, t) for t in todo],
                                   return_exceptions=True)
        failed_topics.update(t["sig"] for t, r in zip(todo, res) if isinstance(r, Exception))
        broadcast({"type": "data_topics"})
    finally:
        inflight.difference_update(t["key"] for t in todo)


@app.get("/api/files/points")
def file_points():
    return files.points()


@app.get("/api/file")
def file_detail(path: str):
    r = files.file(path)
    if r is None:
        raise HTTPException(404)
    return {**r, "similar": files.similar(path)}


@app.get("/api/item")
def item(id: str, max_chars: int = 20000):
    try:
        return actions.read(id, max_chars)
    except KeyError:
        raise HTTPException(404)


class OpenReq(BaseModel):
    id: str
    action: str = "open"  # open | reveal | visualize


@app.post("/api/open")
def open_item(req: OpenReq):
    try:
        return actions.open_item(req.id, req.action)
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/ai")
async def ai_status():
    return await asyncio.get_running_loop().run_in_executor(None, llm.status)


@app.get("/api/sources")
def get_sources():
    return sources.catalog(store, repos, files)


class SourceReq(BaseModel):
    id: str
    enabled: bool


@app.put("/api/sources")
def put_source(req: SourceReq):
    try:
        sources.set_source(req.id, req.enabled, repos)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return sources.catalog(store, repos, files)


class FolderReq(BaseModel):
    path: str


@app.post("/api/sources/folders")
def add_folder(req: FolderReq):
    p = Path(req.path).expanduser()
    if not p.is_dir():
        raise HTTPException(400, f"not a folder: {req.path}")
    cur = settings.get("folders")
    if req.path not in cur:
        settings.update({"folders": cur + [req.path]})
    return sources.catalog(store, repos, files)


@app.delete("/api/sources/folders")
def remove_folder(path: str):
    settings.update({"folders": [f for f in settings.get("folders") if f != path],
                     "disabled": [d for d in settings.get("disabled") if d != f"files/{path}"]})
    return sources.catalog(store, repos, files)


def _version():
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("pensieve")
    except PackageNotFoundError:
        return "dev"


@app.get("/api/about")
def about():
    from . import reset
    return {"version": _version(), "data_dir": str(config.DATA_DIR).replace(str(Path.home()), "~", 1),
            "index_bytes": reset.index_bytes()}


@app.get("/api/update/check")
async def update_check():
    """For the visualizer outside the Mac app (the app checks and installs updates itself)."""
    import httpx
    cur = _version()
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.github.com/repos/TedHaley/pensieve/releases/latest",
                            headers={"Accept": "application/vnd.github+json", "User-Agent": f"Pensieve/{cur}"})
        r.raise_for_status()
        j = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(502, f"Couldn't reach GitHub: {e}")
    latest = (j.get("tag_name") or "").lstrip("vV")
    num = lambda v: tuple(int(x) if x.isdigit() else 0 for x in v.split("-")[0].split("."))
    return {"current": cur, "latest": latest, "newer": cur != "dev" and num(latest) > num(cur), "url": j.get("html_url")}


class ResetReq(BaseModel):
    settings: bool = False


@app.post("/api/reset")
async def factory_reset(req: ResetReq):
    """Delete the index (and the settings too if asked) and restart, which rebuilds everything from scratch."""
    from . import reset
    reset.request(req.settings)
    asyncio.get_running_loop().call_later(0.6, reset.restart)  # after this response is sent
    return {"ok": True}


@app.get("/api/integrations")
def get_integrations():
    from . import integrations
    return integrations.status()


class IntegrationReq(BaseModel):
    on: bool


@app.put("/api/integrations/{item}")
def put_integration(item: str, req: IntegrationReq):
    from . import integrations
    try:
        if item in integrations.TARGETS:
            integrations.set_instructions(item, req.on)
        else:
            integrations.set_skill(item, req.on)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return integrations.status()


@app.get("/api/settings")
def get_settings():
    return {"settings": settings.public(settings.load()), "defaults": settings.DEFAULTS, "descriptions": settings.DESCRIPTIONS}


@app.put("/api/settings")
def put_settings(changes: dict):
    try:
        return {"settings": settings.public(settings.update(changes))}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/ai/models")
def ai_models(url: str, key: str = ""):
    """Test an OpenAI-compatible server and list its models. The saved key is only used for the saved URL."""
    if not key and url.rstrip("/") == settings.get("llm_url").rstrip("/"):
        key = settings.get("llm_key")
    return llm.server_models(url, key)


def main():
    from .cli import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
