import asyncio
from collections import Counter
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from pensieve import actions, config, connections, content, llm, mcp_server, migrate, onboarding,places as places_mod, reembed, scopes, stale as stale_mod, search as search_mod, settings, sources, summarize, task_states, usage, work
from pensieve.datamap import DataMap
from pensieve.files import Files
from pensieve.indexer import Store, embed
from pensieve.repos import Repos, check as repo_check
from pensieve.watch import Watch

store: Store | None = None
repos: Repos | None = None
files: Files | None = None
datamap: DataMap | None = None
searcher: search_mod.Searcher | None = None
subscribers: set[asyncio.Queue] = set()
status = {"indexing": False, "message": "starting", "layout": False, "insights_running": False, "insights_scope": None}
wake_files = asyncio.Event()  # set (thread-safely) by the file watcher
wake_repos = asyncio.Event()
wake_sessions = asyncio.Event()
watch: "Watch | None" = None
inflight: set[str] = set()
failed_topics: set[str] = set()  # Data-map topics the AI couldn't name this run (by membership signature)  # background LLM jobs (scoped topic names, team summaries) already running

def broadcast(event: dict):
    for q in list(subscribers):
        q.put_nowait(event)


FULL_SWEEP_SECONDS = 300  # safety net behind the file watcher; also discovers new repos and folders
SESSION_BATCH_SECONDS = 30  # a session backlog is indexed in turns this long, with folders swept between them
SESSION_SWEEP_SECONDS = 60  # every session folder is swept this often; between sweeps, the watcher's transcripts only


async def _run(fn, *a):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *a)


async def _wait(ev: asyncio.Event, timeout):
    try:
        await asyncio.wait_for(ev.wait(), timeout)
    except asyncio.TimeoutError:
        pass
    ev.clear()


async def _quiet(ev: asyncio.Event, quiet=0.15, cap=1.0):
    """Wait until changes stop for `quiet` seconds (at most `cap` seconds), so a burst of saves is indexed once."""
    end = time.monotonic() + cap
    while (left := end - time.monotonic()) > 0:
        ev.clear()
        try:
            await asyncio.wait_for(ev.wait(), min(quiet, left))
        except asyncio.TimeoutError:
            return


MAP_IDLE_SECONDS = 45
_last_indexed = [0.0]
_layout_due = asyncio.Event()  # something was indexed since the last layout (set at start: lay out what's missing)
_layout_due.set()


def _indexed():
    _last_indexed[0] = time.time()
    _layout_due.set()


def _save_indexes():
    """Write each vector index (.tv) that changed since its last save. Indexing saves at most every
    vectors.SAVE_SECONDS; this runs when indexing is idle and at shutdown."""
    for corpus in (store, files, repos):
        try:
            corpus.save(force=True)
        except Exception:
            import traceback
            traceback.print_exc()


async def map_worker():
    """Map layouts (UMAP) of sessions, documents, code and the joint Data map, kept off the indexing path: they wait
    until nothing has been indexed for MAP_IDLE_SECONDS. A first index of a big repo is searchable as it goes and is
    laid out once, at the end, instead of being refit every time it grows by 15%."""
    while True:
        await _layout_due.wait()
        while status.get("repo_busy") or time.time() - _last_indexed[0] < MAP_IDLE_SECONDS:
            await asyncio.sleep(5)
        _layout_due.clear()
        status.update(layout=True)
        broadcast({"type": "status", **status})
        for corpus, event in ((store, "updated"), (files, "files"), (repos, "repos"), (datamap, "data")):
            try:
                if await _run(corpus.reproject):
                    broadcast({"type": event})
            except Exception:
                import traceback
                traceback.print_exc()
        _save_indexes()  # nothing was indexed for MAP_IDLE_SECONDS: the .tv files catch up
        status.update(layout=False)
        broadcast({"type": "status", **status})


async def watcher():
    """Sessions and documents: re-indexed as soon as the file watcher reports a change (and polled every few
    seconds). Repos run in their own loop so a long first index of a big repo doesn't hold these up."""
    loop = asyncio.get_running_loop()
    status.update(message="building exact-match index")
    broadcast({"type": "status", **status})
    try:
        await _run(search_mod.ensure_fts, store.db, store.writing, store.meta)
    except Exception:
        import traceback
        traceback.print_exc()
    asyncio.create_task(map_worker())
    # watching (and the repo sweep behind checkouts) reads the user's folders: not before they've chosen what to index,
    # so macOS asks for folder access at that step instead of at launch
    while not settings.get("setup_done"):
        status.update(indexing=False, message="waiting for setup")
        await _wait(wake_files, config.POLL_SECONDS)
    watch.configure(repos.checkouts())
    asyncio.create_task(repo_watcher())
    asyncio.create_task(upstream_watcher())
    asyncio.create_task(model_switcher())
    asyncio.create_task(connections_worker())
    asyncio.create_task(doc_worker())
    asyncio.create_task(session_watcher())
    work.listeners.append(lambda: loop.call_soon_threadsafe(broadcast, {"type": "threads"}))
    from pensieve import people
    people.listeners.append(lambda: loop.call_soon_threadsafe(broadcast, {"type": "people"}))
    people.blame_fn = repos.people  # each author's share of a repo's lines, for the org chart's ownership signal
    from pensieve import team_projects
    team_projects.listeners.append(lambda: loop.call_soon_threadsafe(broadcast, {"type": "projects"}))
    _start_cards(loop)
    last_full = 0
    while True:
        if not settings.get("setup_done"):  # nothing is indexed before the user picks the sources
            status.update(indexing=False, message="waiting for setup")
            await _wait(wake_files, config.POLL_SECONDS)
            continue
        try:
            d = watch.take()
            if sorted(str(p) for p in files.roots()) != sorted(watch.folders):  # a folder appeared or went away
                d["full_files"] = True
            n = 0  # sessions: session_watcher
            if d["full_files"] or time.time() - last_full > FULL_SWEEP_SECONDS:
                last_full = time.time()
                status.update(indexing=True, message="indexing files")
                broadcast({"type": "status", **status})
                if await _run(lambda: files.sync(force=True, on_progress=lambda k: loop.call_soon_threadsafe(broadcast, {"type": "files"}))):
                    broadcast({"type": "files"})
                await _run(migrate.mark_indexed, "files")
                watch.configure(repos.checkouts())
            elif d["files"]:
                changed, retry = await _run(files.sync_paths, d["files"])
                if changed:
                    broadcast({"type": "files"})
                if retry:  # still being written: look again once it settles
                    watch.requeue_files(retry)
                    loop.call_later(config.SETTLE_SECONDS + 0.5, wake_files.set)
            elif files.unsaved:  # a search indexed the change first
                await _run(files.save)
                broadcast({"type": "files"})
            if n or d["full_files"] or d["files"]:
                _indexed()
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
        await _quiet(wake_files)


async def session_watcher():
    """Agent transcripts, in a loop of their own: a sweep of the folders can take minutes, and a session's new turns
    should reach its card within seconds (cardbuild.py). A transcript that grows again within ACTIVE_SECONDS is
    indexed again after that time."""
    from pensieve import indexer
    loop = asyncio.get_running_loop()
    last_sweep = 0.0
    while True:
        if not settings.get("setup_done"):
            await _wait(wake_sessions, config.POLL_SECONDS)
            continue
        try:
            paths = watch.take_sessions() if watch else set()
            if not last_sweep or store.behind or time.time() - last_sweep > SESSION_SWEEP_SECONDS:
                last_sweep = time.time()  # every folder: new folders, and what the watcher missed
                n = await _run(lambda: store.sync_files(budget=SESSION_BATCH_SECONDS,
                    on_progress=lambda k: loop.call_soon_threadsafe(broadcast, {"type": "updated", "sessions": k})))
                if not store.behind:  # every session is indexed
                    await _run(migrate.mark_indexed, "sessions")
            elif paths or store.waiting:  # the transcripts that changed, at once
                n = await _run(lambda: store.sync_paths(paths))
            else:
                n = 0
            if n:
                broadcast({"type": "updated", "sessions": n})
                _indexed()
            if store.deferred:  # a transcript that grew again too soon: index its new turns in a moment
                loop.call_later(indexer.ACTIVE_SECONDS, wake_sessions.set)
        except Exception:
            import traceback
            traceback.print_exc()
        if store.behind:  # sessions left over: go on at once
            continue
        await _wait(wake_sessions, config.POLL_SECONDS)
        await _quiet(wake_sessions, quiet=0.1, cap=0.5)


def _start_cards(loop):
    """The card builder (cardbuild.py): fed by session indexing, document upserts and commit loads; it sends a
    `card` event with the ids of the cards that changed, and writes a card's note once the card is quiet."""
    from pensieve import cardbuild, indexer, repos as repos_mod
    b = cardbuild.Builder(store)

    def changed(ids, removed):
        loop.call_soon_threadsafe(broadcast, {"type": "thread", "ids": ids, "removed": removed})

    b.listeners.append(changed)
    indexer.session_listeners.append(lambda sid: cardbuild.emit(cardbuild.SESSION, [sid]))
    connections.listeners.append(lambda r: cardbuild.emit(cardbuild.DOCUMENT, [r.id]))
    repos_mod.commit_listeners.append(lambda root: cardbuild.emit(cardbuild.COMMIT, [root]))
    b.start()


wake_docs = asyncio.Event()
DOC_POLL_SECONDS = 60


async def doc_worker():
    """Chunk and embed the collected documents that changed (docindex.py): at once when a connection or an agent
    session stores one, else every DOC_POLL_SECONDS (a source switched on or off)."""
    from pensieve import docindex
    loop = asyncio.get_running_loop()
    connections.listeners.append(lambda r: loop.call_soon_threadsafe(wake_docs.set))
    while True:
        try:
            if await _run(lambda: docindex.get().sync(budget=60)):
                broadcast({"type": "docs"})
        except Exception:
            import traceback
            traceback.print_exc()
        await _wait(wake_docs, DOC_POLL_SECONDS)
        wake_docs.clear()
        await _quiet(wake_docs)


UPSTREAM_SECONDS = 1800


async def upstream_watcher():
    """Fetch origin's default branch of each repo every UPSTREAM_SECONDS, to show which repos are behind."""
    fetch = False  # the first pass reads the last fetch, so the arrows show without waiting for the network
    while True:
        try:
            await _run(repos.check_upstream, fetch)
            broadcast({"type": "upstream"})
        except Exception:
            import traceback
            traceback.print_exc()
        await asyncio.sleep(UPSTREAM_SECONDS if fetch else 5)
        fetch = True


async def repo_watcher():
    last_full = 0
    while True:
        if not settings.get("setup_done"):
            await _wait(wake_repos, 30)
            continue
        try:
            d_repos, asked = watch.take_repos()
            full = asked or time.time() - last_full > FULL_SWEEP_SECONDS
            if full or d_repos or repos.unsaved:
                status.update(indexing=True, repo_busy=True, message="indexing repos")
                broadcast({"type": "status", **status})
                if full:
                    last_full = time.time()
                    n = await _run(lambda: repos.sync(force=True))
                    await _run(migrate.mark_indexed, "repos")
                else:
                    n = await _run(lambda: repos.refresh(d_repos, wait=3600)) or 0
                    if repos.unsaved:  # this pass's edits, or ones a search indexed first
                        await _run(repos.save)
                        n += 1
                status.update(repo_busy=False, indexing=False, message="idle")
                watch.configure(repos.checkouts())
                if n:
                    broadcast({"type": "repos"})
                    _indexed()
                broadcast({"type": "status", **status})
        except Exception as e:
            import traceback
            traceback.print_exc()
            status.update(repo_busy=False, indexing=False, message=f"error: {e!r}")
        await _wait(wake_repos, 30)
        await _quiet(wake_repos)


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
    except llm.Queued:  # it waits for your agent
        pass
    except Exception as e:
        broadcast({"type": "toast", "message": f"Insight generation failed: {e!r}"[:200]})
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
    except llm.Queued:  # it waits for your agent
        pass
    except Exception as e:
        broadcast({"type": "toast", "message": f"Team summary failed: {e!r}"[:200]})
    finally:
        inflight.discard(key)


AGENT_BATCH = 10  # jobs of each kind that wait for the agent at a time


async def agent_pass(loop, pool):
    """Ask your agent for every kind of job at once. A job that the agent answered is used now; the others wait in
    its queue."""
    def ok(res):
        return any(not isinstance(r, BaseException) for r in res)
    run = lambda fn, *a: loop.run_in_executor(pool, fn, *a)  # noqa: E731
    sess, topics = summarize.pending(store)
    data_topics = [t for t in await _run(datamap.pending_topics) if t["sig"] not in failed_topics]
    res = await asyncio.gather(*[run(summarize.name_data_topic, datamap, t) for t in data_topics[:AGENT_BATCH]],
                               return_exceptions=True)
    failed_topics.update(t["sig"] for t, r in zip(data_topics, res) if isinstance(r, Exception) and not isinstance(r, llm.Offline))
    if ok(res):
        broadcast({"type": "data_topics"})
    if ok(await asyncio.gather(*[run(summarize.summarize_session, store, s) for s in sess[:AGENT_BATCH]], return_exceptions=True)):
        broadcast({"type": "summaries"})
    if ok(await asyncio.gather(*[run(summarize.name_topic, store, t) for t in topics[:AGENT_BATCH]], return_exceptions=True)):
        broadcast({"type": "topics"})
    if not status["insights_running"] and store.topics() and await _run(_insights_due):
        await run_insights()


async def engine_worker():
    """Answer the waiting jobs with the text engine (engine.py) one at a time, at most engine_rate a minute, and stop
    an idle built-in server. A failed job waits a minute before the next try."""
    from pensieve import engine
    while True:
        if not settings.get("setup_done"):
            await asyncio.sleep(15)
            continue
        try:
            await asyncio.to_thread(engine.sweep)
            n = await asyncio.to_thread(engine.run_once)
            await asyncio.sleep(60 / max(1, settings.get("engine_rate")) if n else 10)
        except Exception as e:
            status["message"] = f"text engine: {e}"[:200]
            await asyncio.sleep(60)


async def enricher():
    """Every 30 seconds, the jobs for your agent: session summaries, topic names, insights and task notes."""
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(3)
    while True:
        try:
            await agent_pass(loop, pool)
            await asyncio.sleep(30)
        except Exception as e:
            status["message"] = f"enricher: {e!r}"[:200]
            await asyncio.sleep(30)


async def card_keeper():
    """Every history.CHECK seconds: mark what later commits did (work.check_done), link the cards to threads
    (history.current) and save the ones that changed; once a day, prune the history."""
    from pensieve import history, work
    await asyncio.sleep(60)  # after the first index pass has settled
    history.current, pruned = history.Keeper(store), 0.0
    while True:
        try:
            saved = await asyncio.to_thread(history.current.run_once)
            if saved:
                print(f"Saved {len(saved)} context cards to their threads' history", flush=True)
            if time.time() - pruned > 86400:
                await asyncio.to_thread(history.prune)
                pruned = time.time()
        except Exception:
            import traceback
            traceback.print_exc()
        await asyncio.sleep(history.CHECK)


def _drop_plugin_duplicates():
    from pensieve import integrations
    try:
        gone = integrations.drop_plugin_duplicates(config.PORT)
        if gone:
            print(f"Removed hooks that the Pensieve plugin already has: {', '.join(gone)}", flush=True)
    except Exception:  # a settings file we can't read or write: leave it as it is
        import traceback
        traceback.print_exc()


@asynccontextmanager
async def lifespan(app):
    global store, repos, files, searcher, datamap
    store = Store()
    repos = Repos(store)
    files = Files(store)
    datamap = DataMap(store, repos, files)
    searcher = search_mod.Searcher(store, repos, files)
    actions.bind(store=store, repos=repos, files=files, searcher=searcher, datamap=datamap, status=status, port=config.PORT)
    loop = asyncio.get_running_loop()

    def changed(keys):  # settings edited from the UI or MCP: re-sweep promptly, tell the Mac app and the UI
        if "file_types" in keys or "embed_model" in keys:  # images, video or audio switched on: install their parts
            content.resume()
        repos._extra = None  # re-run the repo sweep with the new folders/roots
        watch.force_full()
        loop.call_soon_threadsafe(broadcast, {"type": "settings", "keys": sorted(keys)})
    settings.on_change(changed)

    def wake():
        loop.call_soon_threadsafe(wake_files.set)
        loop.call_soon_threadsafe(wake_repos.set)
        loop.call_soon_threadsafe(wake_sessions.set)
    global watch
    watch = Watch(wake)

    def installed():  # a model part is here: read the files that waited for it
        watch.force_full()
        wake()
        loop.call_soon_threadsafe(broadcast, {"type": "settings", "keys": ["content"]})
    content.on_installed.append(installed)
    content.resume()
    repos.pending = watch.take_changed_repos
    repos.requeue = watch.requeue_repos
    files.pending = watch.take_files
    files.requeue = watch.requeue_files
    migrate.listeners.append(lambda: loop.call_soon_threadsafe(broadcast, {"type": "status", **status}))
    migrate.start()  # v1 data here: import what v2 keeps, in the background; delete v1 after the first full index
    loop.run_in_executor(None, embed, ["warm up"])  # load the model now, not in the first search or edit
    loop.run_in_executor(None, _drop_plugin_duplicates)
    loop.run_in_executor(None, searcher._folder_index)  # starts its first build, so searches find folders sooner
    threading.Thread(target=store.backfill_usage, daemon=True, name="usage-backfill").start()
    tasks = [asyncio.create_task(watcher()), asyncio.create_task(enricher()), asyncio.create_task(card_keeper()),
             asyncio.create_task(engine_worker())]
    async with mcp_server.mcp.session_manager.run():
        yield
    for t in tasks:
        t.cancel()
    from pensieve import engine
    engine.stop_builtin()
    _save_indexes()


app = FastAPI(lifespan=lifespan)
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


@app.middleware("http")
async def local_only(request: Request, call_next):
    """Only this Mac's own pages and programs. The server listens on 127.0.0.1, but a web page in the browser can
    still reach it: through DNS rebinding (its own host name pointed at 127.0.0.1, which the Host header shows) or
    with a cross-site request (which carries the page's Origin)."""
    from urllib.parse import urlsplit

    def local(h):  # names under .localhost always resolve to this Mac (RFC 6761), e.g. pensieve.localhost
        return h in LOCAL_HOSTS or (h or "").endswith(".localhost")
    host = urlsplit("//" + request.headers.get("host", "")).hostname
    origin = request.headers.get("origin")
    if not local(host) or (origin is not None and not local(urlsplit(origin).hostname)):  # "null" too
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse("Pensieve only answers requests from this Mac", status_code=403)
    return await call_next(request)
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
            "embed": config.EMBED_MODEL, "setup_done": settings.get("setup_done"), "syncing": _syncing(),
            "migration": migrate.view()}


@app.get("/api/usage")
def get_usage(days: float = 7, recent: int = 0):
    """MCP tool calls over the last `days` (the log is in ~/.pensieve/v2/store.db); `recent`: also the latest calls."""
    return usage.summary(days, min(recent, 200))


@app.get("/api/syncing")
def get_syncing():
    """Just the running jobs, for the menu bar to poll: no database work, so it never waits on indexing."""
    return {"syncing": _syncing()}


def _syncing() -> list[dict]:
    """What is being indexed right now, with progress where the total is known."""
    out = [j for j in [migrate.job()] if j]
    for kind, src in (("sessions", store), ("files", files), ("repo", repos)):
        p = getattr(src, "progress", None)
        if p:
            out.append({"kind": kind, **p})
    if reembed.progress:
        p = reembed.progress
        out.append({"kind": "model", "name": config.EMBED_MODELS[p["model"]]["label"], **p})
    return out


wake_model = asyncio.Event()


async def model_switcher():
    """Embeds the index with the model chosen in Settings, in the background (see reembed). Ends in a restart."""
    loop = asyncio.get_running_loop()
    while True:
        if settings.get("setup_done") and reembed.wanted() != config.EMBED_MODEL or store.meta("next_model"):
            try:
                if reembed.switch_empty(store, [store, repos, files]):
                    broadcast({"type": "status", **status})
                    await _wait(wake_model, 600)
                    continue
                await _run(reembed.run, store, lambda: loop.call_soon_threadsafe(
                    broadcast, {"type": "status", **status, "syncing": _syncing()}))
            except Exception:
                import traceback
                traceback.print_exc()
        await _wait(wake_model, 600)


@app.get("/api/embed_models")
def embed_models():
    """The models the index can use: the index's own, the one chosen, and how long switching takes on this Mac."""
    n = sum(store.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in reembed.TABLES)
    return {"current": config.EMBED_MODEL, "wanted": reembed.wanted(), "chunks": n, "progress": reembed.progress,
            "models": [dict(id=k, label=m["label"], about=m["about"], dim=m["dim"],
                            switch_seconds=round(n / repos.embed_rate(k))) for k, m in config.EMBED_MODELS.items()]}


@app.get("/api/content")
def get_content():
    """What the search model reads: text always; images, video and audio when switched on, with their install."""
    return {"kinds": content.status(), "model": config.EMBED_MODEL, "needs_model": config.GEMMA}


class ContentReq(BaseModel):
    kind: str  # images | video | audio
    on: bool


@app.put("/api/content")
def put_content(req: ContentReq):
    """Switch a kind on (its model part downloads, then its files are read) or off (found by name again)."""
    try:
        content.switch(req.kind, req.on)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return get_content()


@app.delete("/api/content/{part}")
def delete_part(part: str):
    """Delete a downloaded model part (vision or audio) that no switched-on kind uses."""
    try:
        content.remove(part)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    return get_content()


@app.get("/api/session-projects")
def session_projects():
    """The session map's projects: canonical repos (clones and worktrees merged); non-repo folders stay as-is."""
    scope = repos.session_scope()
    by = {}
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


@app.get("/api/projects")
async def get_projects(q: str = "", refresh: bool = False):
    """The Projects tab: groups of tickets with the branches and pull requests tied to them (project_model.py).
    The user's projects first (`section` mine), then the others (`section` around); `stale` ones go under "Show
    older". Served from the last build; a build older than two minutes starts a new one in the background."""
    from pensieve import team_projects
    team_projects.rebuild(store, force=refresh)
    return await _run(team_projects.summary, q)


class ProjectMatchReq(BaseModel):
    ids: list[str]  # Map point ids (file:…, code:…, session:…)


@app.post("/api/projects/match")
def match_projects(req: ProjectMatchReq):
    """The projects whose context cards hold most of these Map items' sessions: the Map topic panel's "Open as
    project"."""
    from pensieve import team_projects
    return {"projects": team_projects.best_for(req.ids[:5000])}


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    """One project: its tickets with their states, pull requests, branches, people and the ties between them."""
    from pensieve import team_projects
    p = team_projects.project(pid)
    if p is None:
        raise HTTPException(404, f"unknown project {pid!r}")
    return p


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


STATS_TTL = 30  # seconds: the stats of the Insights tab are made again at most this often
_stats = {"value": {}, "busy": set(), "lock": threading.Lock()}  # value: repos key -> (time, stats)
_gaps = {"value": {}, "building": set(), "lock": threading.Lock()}


@app.get("/api/insights/stats")
def insights_stats_api(fresh: bool = False, names: str = Query("", alias="repos")):
    """Token use, the value score, deployed lines, peers and momentum (insights_stats.py), for all repos or the
    comma-separated `repos`. No AI. An answer older than STATS_TTL is returned, and made again in the background.
    A sync endpoint: it runs on the request thread pool, not behind the indexing jobs of the loop's executor."""
    from pensieve import insights_stats, people, store as v2
    sel = sorted({r for r in names.split(",") if r})
    key = ",".join(sel)

    def make():
        v = insights_stats.stats(v2.index_db(), v2.store_db(), people.graph(), repos=sel or None,
                                 deployed_fn=repos.deployed)
        _stats["value"][key] = (time.time(), v)
        return v

    def refresh():
        try:
            with _stats["lock"]:
                make()
        finally:
            _stats["busy"].discard(key)
    hit = _stats["value"].get(key)
    if hit and not fresh:  # an older answer is returned at once, and made again in the background
        if time.time() - hit[0] >= STATS_TTL and key not in _stats["busy"]:
            _stats["busy"].add(key)
            threading.Thread(target=refresh, daemon=True, name="insights-stats").start()
        return hit[1]
    with _stats["lock"]:
        return make()


@app.get("/api/insights/gaps")
def insights_gaps_api(stale_days: int = 90, inactive_days: int = 180, names: str = Query("", alias="repos")):
    """Knowledge gaps over every indexed repo, or the comma-separated `repos`. The first build reads all blame rows,
    so it runs in the background: the answer has `building` until it is done, and the last result while a new one
    builds. A result stays until a repo's head moves."""
    from pensieve import insights_stats, store as v2
    from pensieve.store import cache as cache_db, insights as q
    stale_days, inactive_days = max(7, min(stale_days, 3650)), max(30, min(inactive_days, 3650))
    key = f"insights:gaps:v6:{stale_days}:{inactive_days}"
    idx = v2.index_db()
    heads = [[r, h] for r, h, _ in q.repo_heads(idx)]
    sel = {r for r in names.split(",") if r}

    def only(res):
        return insights_stats.gaps_for(res, sel) if sel else res
    with _gaps["lock"]:
        hit = _gaps["value"].get(key) or cache_db.get_json(idx, key)
        if hit and hit.get("heads") == heads:
            _gaps["value"][key] = hit
            return dict(only(hit["result"]), building=False)
        if key not in _gaps["building"]:
            _gaps["building"].add(key)

            def build():
                try:
                    every = [n for _r, n, _rm in q.repos(idx)]
                    t0 = time.perf_counter()
                    res = insights_stats.gaps(repos, every, stale_days, inactive_days)
                    res["ms"] = round((time.perf_counter() - t0) * 1000)
                    v = {"heads": heads, "result": res}
                    cache_db.put_json(idx, key, v, 30 * 86400)
                    with _gaps["lock"]:
                        _gaps["value"][key] = v
                except Exception:
                    import traceback
                    traceback.print_exc()
                finally:
                    with _gaps["lock"]:
                        _gaps["building"].discard(key)
            threading.Thread(target=build, daemon=True, name="insights-gaps").start()
    last = (hit or {}).get("result")
    return dict(only(last) if last else {"areas": [], "counts": {}, "stale_days": stale_days,
                                         "inactive_days": inactive_days}, building=True)


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
    summarizing = raw is None and bool(t["people"])
    if summarizing:
        asyncio.create_task(summarize_team(t, key))
    return {**{k: v for k, v in t.items() if k != "area_activity"}, "summaries": json.loads(raw) if raw else None,
            "summarizing": summarizing}


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


@app.get("/api/repos/upstream")
def repos_upstream():
    return repos.upstream


@app.post("/api/repos/pull")
async def repos_pull(body: dict):
    root = body.get("root")
    if root not in repos.checkouts().values():
        raise HTTPException(404)
    r = await _run(repos.pull, root)
    broadcast({"type": "upstream"})
    wake_repos.set()
    return r


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
    if not folders:
        return {"topics": await loop.run_in_executor(None, datamap.topics)}
    home = str(Path.home())
    abs_ = [f if f.startswith("/") else f"{home}/{f}" for f in folders.split("|") if f]
    r = await loop.run_in_executor(None, datamap.scoped_topics, abs_)
    todo = [t for t in r["topics"] if not t["named"]]
    if todo:
        asyncio.create_task(name_data_scoped(todo))
    return r


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
    if req.name:
        size = Counter(r["of"].values())
        todo = [t for t in r["topics"] if not t["named"] and size[t["id"]] >= 25]
        if todo:
            asyncio.create_task(name_data_scoped(todo))
    return r


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


def _repo_check(cwd: str) -> dict:
    indexed = dict(store.db.execute("SELECT root, remote FROM repos").fetchall())
    return repo_check(cwd, indexed)


@app.get("/api/repo-check")
def get_repo_check(cwd: str):
    return _repo_check(cwd)


def _place_state(root: str, kind: str) -> dict:
    """indexed | pending | declined | unknown for a place the agents work in: a repo as repo-check sees it; a folder
    by whether an enabled folder source covers it."""
    if kind == "repo":
        return _repo_check(root)
    p = Path(root)
    on = settings.enabled("files")
    for f, fp in zip(settings.get("folders"), settings.paths("folders")):
        if p == fp or fp in p.parents:
            return {"state": "indexed" if on and settings.enabled(f"files/{f}") else "declined"}
    return {"state": "unknown"}


@app.get("/api/places")
def get_places(fresh: bool = False):
    """Where the agents work, busiest first: the repos and folders of their sessions and of the files they read and
    edited, with whether each one is indexed (ROADMAP B11)."""
    return {"places": places_mod.places(_place_rows, _place_state, fresh), "home": str(Path.home())}


def _place_rows():
    return store.db.execute(places_mod.ROWS).fetchall()


class PlaceReq(BaseModel):
    root: str
    kind: str  # repo | folder


@app.post("/api/places/index")
def index_place(req: PlaceReq):
    """Index a place the agents work in: a repo as `pensieve repo index` does, a folder as a new document folder."""
    if req.kind == "repo":
        repo_choice(RepoChoiceReq(cwd=req.root, index=True))
    else:
        try:
            sid = onboarding.add("files", req.root)
        except ValueError as e:
            raise HTTPException(400, str(e))
        settings.set_enabled("files", True)
        settings.set_enabled(sid, True)
    return get_places(fresh=True)


@app.post("/api/gate/{mode}")
async def hook_gate(mode: str, request: Request):
    """The search gate's three Claude Code hooks (gate.py): reset on a user prompt, mark after a Pensieve tool call,
    check before Grep, Glob or Bash. Only a denied check answers; every other answer is empty, so the call goes on."""
    from fastapi.responses import Response
    from pensieve import gate
    try:
        d = await request.json()
    except ValueError:
        return Response()
    sid = d.get("session_id") or ""
    if mode == "reset":
        gate.reset(sid, d.get("prompt") or "")
    elif mode == "mark":
        gate.mark(sid)
    elif mode == "check":
        cwd, tool, inp = d.get("cwd") or "", d.get("tool_name") or "", d.get("tool_input") or {}
        t = time.perf_counter()
        decision = await asyncio.to_thread(gate.check, sid, tool, inp, lambda: _repo_check(cwd).get("state") == "indexed")
        if decision != "not_search":
            usage.record(ts=time.time(), source="gate", tool=f"gate_{decision}", client="claude-code", conn=sid, cwd=cwd,
                         args=dict(tool=tool, arm=gate.arm(sid), input=str(inp.get("command") or inp.get("pattern") or "")[:300]),
                         ms=round((time.perf_counter() - t) * 1000, 2), ok=True)  # a denial is the gate working
        if decision == "deny":
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": gate.DENY}}
    return Response()


@app.post("/api/repo-check")
async def hook_repo_check(request: Request):
    """Claude Code's SessionStart hook posts its JSON here; the answer adds a note about the repo to the session."""
    from pensieve import integrations
    try:
        cwd = (await request.json()).get("cwd") or ""
    except ValueError:
        return {}
    c = await asyncio.to_thread(_repo_check, cwd)
    notes = [integrations.repo_note(c)]
    if c.get("state") == "indexed":  # a cleanup hint, from the last stale report only: the hook may wait 3 s at most
        items = stale_mod.cached(c["root"])
        if items is None:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, lambda: stale_mod.from_store(store, roots=[c["root"]]))
        notes.append(integrations.stale_note(c["name"], items or []))
        try:  # the threads come from the cards table: no build here
            notes.append(integrations.threads_note(c["name"], c["root"], await asyncio.to_thread(work.tasks, store, 30)))
        except Exception:
            pass
    note = "\n\n".join(n for n in notes if n)
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": note}} if note else {}


@app.post("/api/prompt-context")
async def hook_prompt_context(request: Request):
    """Claude Code's UserPromptSubmit hook posts its JSON here; the answer adds Pensieve's matches for the prompt."""
    try:
        body = await request.json()
        text = await asyncio.to_thread(mcp_server.prompt_context, body.get("prompt") or "", body.get("cwd") or "",
                                       request.query_params.get("card", "text"))
    except Exception:  # Claude Code adds whatever the hook prints to the prompt: never an error page
        import traceback
        traceback.print_exc()
        return {}
    if text:  # Pensieve searched for this prompt: that opens the search gate, so Claude doesn't search it again
        from pensieve import gate
        gate.mark(body.get("session_id") or "", by="prompt")
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}} if text else {}


class RepoChoiceReq(BaseModel):
    cwd: str
    index: bool


@app.post("/api/repo-choice")
def repo_choice(req: RepoChoiceReq):
    """`pensieve repo index|skip`: index the repo at cwd, or switch it off in Sources so no one asks again."""
    c = _repo_check(req.cwd)
    if c["state"] == "none":
        raise HTTPException(400, f"not in a git repo: {req.cwd}")
    sid = f"repos/{c['root']}"
    if not req.index:
        settings.set_enabled(sid, False)
    else:
        if not settings.enabled("repos"):
            settings.set_enabled("repos", True)
        settings.set_enabled(sid, True)
        if c["state"] != "indexed" and Path(c["root"]) not in settings.paths("repos"):
            settings.update({"repos": settings.get("repos") + [c["root"]]})
    return _repo_check(req.cwd)


@app.get("/api/sources")
def get_sources():
    return sources.catalog(store, repos, files)


class SourceReq(BaseModel):
    id: str
    enabled: bool | None = None
    map: bool | None = None  # on or off the map only; the source stays indexed


@app.put("/api/sources")
def put_source(req: SourceReq):
    try:
        if req.enabled is not None:
            sources.set_source(req.id, req.enabled, repos)
        if req.map is not None:
            sources.set_source(req.id, req.map, repos, map_only=True)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return sources.catalog(store, repos, files)


@app.get("/api/filetypes")
def get_filetypes():
    return files.types()


class FileTypeReq(BaseModel):
    id: str
    index: str | None = None  # content | name | off
    map: bool | None = None


@app.put("/api/filetypes")
def put_filetype(req: FileTypeReq):
    change = {k: v for k, v in (("index", req.index), ("map", req.map)) if v is not None}
    try:
        settings.update({"file_types": {req.id: change}})
    except ValueError as e:
        raise HTTPException(400, str(e))
    return files.types()


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


@app.get("/api/setup")
def get_setup(fresh: bool = False, categories: str | None = None, scan: bool = True):
    """The first-run (and after-reset) steps: what Pensieve is, the profiles, the AI engines and, with `scan`, what
    can be indexed in `categories` (comma-separated; all by default). Only the scan reads the user's folders."""
    out = onboarding.guide()
    if scan:
        out["candidates"] = onboarding.scan(categories.split(",") if categories else None, fresh)
    return out


class SetupReq(BaseModel):
    sources: list[str] | None = None  # ids to index; None with recommended=true takes the pre-checked ones
    recommended: bool = False
    embed_model: str | None = None  # the search model (config.EMBED_MODELS)
    content: list[str] | None = None  # images, video, audio: read by content (needs EmbeddingGemma 2)
    text_engine: str | None = None  # what writes notes and summaries (onboarding.engines)


@app.post("/api/setup")
async def post_setup(req: SetupReq):
    """Index exactly the chosen sources: every other candidate is switched off, then indexing starts."""
    try:
        res = await _run(lambda: onboarding.start(req.sources, req.recommended, req.embed_model, req.content,
                                                  req.text_engine))
    except ValueError as e:
        raise HTTPException(400, str(e))
    wake_files.set()
    wake_repos.set()
    wake_model.set()
    return res


class AddReq(BaseModel):
    kind: str  # files | repos | agents
    path: str


@app.post("/api/setup/add")
def setup_add(req: AddReq):
    """A source the setup scan didn't find. Returns its id, to choose in the next POST /api/setup."""
    try:
        return {"id": onboarding.add(req.kind, req.path)}
    except ValueError as e:
        raise HTTPException(400, str(e))


def _tilde(p: Path) -> str:
    return str(p).replace(str(Path.home()), "~", 1)


@app.post("/api/sources/agents")
def add_session_folder(req: FolderReq):
    """A folder of agent transcripts; which agent wrote them is read from the files."""
    p = Path(req.path).expanduser()
    if not p.is_dir():
        raise HTTPException(400, f"not a folder: {req.path}")
    found = config.detect_sessions(p)
    if not found:
        raise HTTPException(400, f"No Claude Code, Codex or Qwen Code sessions found in {req.path}")
    name, root = found
    entry = f"{name}:{_tilde(root)}"
    known = {(n, str(r)) for _, n, r, _ in config.session_sources()}
    if (name, str(root)) not in known:
        settings.update({"session_folders": settings.get("session_folders") + [entry]})
    return sources.catalog(store, repos, files)


@app.delete("/api/sources/agents")
def remove_session_folder(path: str):
    settings.update({"session_folders": [e for e in settings.get("session_folders") if e.partition(":")[2] != path],
                     "disabled": [d for d in settings.get("disabled") if not d.endswith(f"@{path}")]})
    return sources.catalog(store, repos, files)


@app.post("/api/sources/repos")
def add_repos(req: FolderReq):
    """A repo, or a folder whose repos are all indexed (sweep_depth levels deep), whatever their size."""
    p = Path(req.path).expanduser()
    if not p.is_dir():
        raise HTTPException(400, f"not a folder: {req.path}")
    cur = settings.get("repos")
    if not any(Path(r).expanduser() == p for r in cur):
        settings.update({"repos": cur + [_tilde(p)]})
    return sources.catalog(store, repos, files)


@app.delete("/api/sources/repos")
def remove_repos(path: str):
    """✕ on a repo: remove it from the index and stop finding it. On a folder of repos added by hand: stop indexing
    the folder (the sweep removes the repos in it that nothing else finds)."""
    root = str(Path(path).expanduser())
    settings.update({"repos": [r for r in settings.get("repos") if r != path]})
    indexed = store.db.execute("SELECT 1 FROM repos WHERE root=?", (root,)).fetchone()
    if indexed:
        sources.set_source(f"repos/{root}", False)
        threading.Thread(target=repos.remove, args=(root,), daemon=True).start()
    return sources.catalog(store, repos, files)


@app.get("/api/connections")
def get_connections():
    """The MCP servers that Pensieve can collect from, each with its tools and which of them are read-only."""
    return connections.servers()


@app.post("/api/connections/discover")
async def discover_connections():
    """Read the MCP servers of the insights engine again, and connect to the user's own servers."""
    await asyncio.to_thread(connections.discover)
    return sources.catalog(store, repos, files)


@app.post("/api/connections/{sid}/collect")
async def collect_connection(sid: str):
    """Collect from one connection now, with its read-only tools."""
    if sid not in {r["id"] for r in connections.servers()}:
        raise HTTPException(404, f"unknown connection {sid!r}")
    run = await asyncio.to_thread(connections.collect, sid)
    wake_files.set()
    return {"run": run, "sources": sources.catalog(store, repos, files)}


class McpServerReq(BaseModel):
    name: str
    url: str = ""
    headers: dict[str, str] = {}
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}


@app.post("/api/sources/connections")
async def add_mcp_server(req: McpServerReq):
    """Add or change one of the MCP servers that Pensieve connects to itself, then list its tools."""
    x = {"name": req.name, **({"url": req.url, "headers": req.headers} if req.url else
                               {"command": req.command, "args": req.args, "env": req.env})}
    cur = [c for c in settings.get("mcp_servers") if c["name"] != req.name.strip()]
    try:
        settings.update({"mcp_servers": cur + [x]})
    except ValueError as e:
        raise HTTPException(400, str(e))
    await asyncio.to_thread(connections.discover_own)
    return sources.catalog(store, repos, files)


class ConnectorReq(BaseModel):
    kind: str            # one of the profile's `add` kinds: oauth, header, env or gh
    secret: str = ""     # the token, for header and env
    secret2: str = ""    # a second value that the server needs (the Slack team id)


@app.post("/api/connectors/{pid}/add")
async def add_connector(pid: str, req: ConnectorReq):
    """Add a cloud connector (Notion, Linear …) as one of Pensieve's own servers, in the way the user chose. For
    OAuth, this starts the sign-in and returns the sign-in page as `signin_url`."""
    from pensieve import connectors
    p = connectors.PROFILES.get(pid)
    a = next((a for a in (p or {}).get("add", []) if a["kind"] == req.kind), None)
    if not a:
        raise HTTPException(404, f"no way to add {pid!r} with {req.kind!r}")
    if a["kind"] == "gh":
        await asyncio.to_thread(connections.discover_gh)
        return {"sources": sources.catalog(store, repos, files)}
    if a["kind"] in ("header", "env") and not req.secret.strip():
        raise HTTPException(400, "Paste the token first.")
    name = p["label"]
    if a["kind"] == "oauth":
        x = {"name": name, "url": a["url"], "headers": {}, "oauth": True, **({"scope": a["scope"]} if a.get("scope") else {})}
    elif a["kind"] == "header":
        x = {"name": name, "url": a["url"], "headers": {a["header"]: a.get("prefix", "") + req.secret.strip(), **a.get("extra", {})}}
    elif a["kind"] == "env":
        env = {a["env"]: req.secret.strip(), **({a["env_more"]: req.secret2.strip()} if a.get("env_more") and req.secret2.strip() else {})}
        x = {"name": name, "command": a["command"], "args": a["args"], "env": env}
    else:
        raise HTTPException(400, f"{p['label']} is added in another way: {a['help']}")
    try:
        settings.update({"mcp_servers": [c for c in settings.get("mcp_servers") if c["name"] != name] + [x]})
    except ValueError as e:
        raise HTTPException(400, str(e))
    sid = connections.slug(name)
    out = {}
    if a["kind"] == "oauth":
        r = await asyncio.to_thread(connections.sign_in, sid)
        out = {"signin_url": r.get("url"), "error": r.get("error")}
    else:
        await asyncio.to_thread(connections.discover_own)
    return {**out, "sid": sid, "sources": sources.catalog(store, repos, files)}


@app.post("/api/connectors/signin/{sid}")
async def connector_signin(sid: str):
    """Sign in again to one of Pensieve's own servers that uses OAuth: returns the sign-in page as `signin_url`."""
    try:
        r = await asyncio.to_thread(connections.sign_in, sid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"signin_url": r.get("url"), "error": r.get("error"), "sources": sources.catalog(store, repos, files)}


@app.get("/api/connectors/callback")
def connector_callback(state: str = "", code: str = "", error: str = "", error_description: str = "", iss: str = ""):
    """Where the browser comes back after an OAuth sign-in. The code goes to the sign-in that waits for it."""
    from fastapi.responses import HTMLResponse
    from pensieve import mcp_oauth
    ok = mcp_oauth.finish(state, code or None, (error_description or error) or None, iss or None)
    msg = ("Pensieve is finishing the sign-in. Go back to Pensieve: the connector shows as found when it is done, "
           "and you can switch it on there."
           if ok and code else f"The sign-in did not work: {error_description or error or 'no sign-in waits for this page'}.")
    import html
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>Pensieve</title>"
                        f"<body style='font:15px -apple-system,sans-serif;padding:40px'><p>{html.escape(msg)}</p>"
                        f"<p>You can close this tab.</p></body>")


@app.delete("/api/sources/connections")
def remove_mcp_server(path: str):
    """✕ on a server that Pensieve connects to itself: forget it and its collected items."""
    sid = connections.slug(path)
    settings.update({"mcp_servers": [c for c in settings.get("mcp_servers") if c["name"] != path],
                     "connections_on": [d for d in settings.get("connections_on") if d != f"connections/{sid}"]})
    connections.remove(sid)
    return sources.catalog(store, repos, files)


@app.get("/api/threads")
def get_threads(days: int = 30, ids: str = ""):
    """The Threads tab: the threads with their state, their data points from every source, each one's note, and its
    saved history (id and number of saved versions) once the keeper has linked it. With `ids` (comma-separated),
    only those threads: the page asks for them after a `thread` event."""
    from pensieve import history
    only = set(_csv(ids)) if ids else None
    ts = work.tasks(store, days, ids=only)
    have = work.notes()
    stale = {t["id"] for t in work.stale_notes(ts, limit=len(ts))}
    of = {t["id"]: history.thread_of(t["id"]) for t in ts}
    if only is None:
        work.warm_card_search(ts, have)
    # each note's core (problem, goal, state, next, question); notes in an older format show until written again
    return dict(threads=ts, notes={t["id"]: {**work.core(have[t["id"]]), "title": have[t["id"]].get("title", ""),
                                             "stale": t["id"] in stale}
                                   for t in ts if work.core(have.get(t["id"], {}))},
                history={k: dict(id=v, saves=history.saves(v)) for k, v in of.items() if v},
                states=task_states.models(), writing=status.get("work_notes") or [])


@app.get("/api/threads/search")
def search_threads(q: str, days: int = 30):
    """The threads of the last `days` days that match `q`, best first: {results: [{id, score}]}. Meaning words rank
    the threads, "exact phrases" and -words filter them."""
    ts = work.tasks(store, days)
    return dict(results=work.search_cards(ts, work.notes(), q))


@app.get("/api/thread")
def get_thread(id: str, format: str = "html", share: bool = False, days: int = 30, version: int | None = None):
    """One thread (pensieve-thread/2): the id of a thread on the Threads tab, or a th_ id. `format`: html (the page
    for people, which holds the JSON), agent (compact JSON with a line on how to read it) or json. `share`: the thread
    to send: no local part or path, and a summary of each local or restricted resource, read whole now (this takes a
    few model calls). `version`: a saved version instead of the thread now (-1 is the last)."""
    from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
    from pensieve import cards, history
    if version is not None:
        saves = history.history(id if id.startswith("th_") else history.thread_of(id) or "")
        if not saves:
            raise HTTPException(404, f"no saved versions of {id}")
        card = saves[version]["card"]
    else:
        card = _local_card(history.task_of(id) or id if id.startswith("th_") else id, days)
        card = cards.draft_share(card, store) if share else card
    if format == "html":
        return HTMLResponse(cards.render_html(card))
    return PlainTextResponse(cards.render_agent(card)) if format == "agent" else JSONResponse(card)


def _local_card(task_id: str, days: int) -> dict:
    from pensieve import cards
    t = next((x for x in work.tasks(store, days) if x["id"] == task_id), None)
    if not t:
        raise HTTPException(404, f"no thread {task_id}")
    return cards.build(t, work.notes().get(task_id, {}), task_states.history(store, task_id))


class CardDraftReq(BaseModel):
    id: str
    days: int = 30  # the Threads tab's period, where the id comes from
    include: list[int] | None = None  # the local thread's resources to keep, by position; default all


@app.post("/api/thread/share/draft")
def draft_share(req: CardDraftReq):
    """The Share button's preview: the local thread's resources (what the person can untick) and the shared thread
    drafted from them, with a summary of each local or restricted one. Nothing is saved."""
    from pensieve import cards
    card = _local_card(req.id, req.days)
    return {"resources": [{k: r.get(k) for k in ("kind", "title", "reach")} for r in card.get("resources", [])],
            "thread": cards.draft_share(card, store, req.include)}


def _repo_rows():
    return repos.db.execute("SELECT root, name, remote FROM repos").fetchall()


@app.get("/api/people")
def get_people(limit: int = 200, refresh: bool = False):
    """The People tab: the nearest `limit` people with their closeness to the user (0..1, the user is 1), and the
    weighted edges between them. Served from the last build; a build older than half an hour starts a new one."""
    from pensieve import people
    people.rebuild(store, _repo_rows, force=refresh)
    return people.summary(limit)


@app.get("/api/people/org")
def get_people_org():
    """The People tab's Org view, all inferred: teams, and each person's function, role and team with confidence
    and evidence."""
    from pensieve import people
    people.rebuild(store, _repo_rows)
    return people.org_chart()


@app.get("/api/people/find")
def find_person(who: str):
    """The person id for a name, an email or 'Name <email>' (as the Map shows authors)."""
    from pensieve import people
    pid = people.find(who)
    if not pid:
        raise HTTPException(404, f"no person for {who!r}")
    return {"id": pid}


@app.get("/api/people/search")
async def search_people(q: str, remote: bool = True, deep: bool = False):
    """Find a person, as lines of JSON. The first line has the local matches: the graph, then the people that earlier
    searches found. Then one line for each remote source as it answers: GitHub through gh, and the switched-on
    people connectors with their read-only user search tools. `deep` is accepted for older clients and does nothing.
    The last line is {"type": "done"}."""
    from pensieve import directory
    q = q.strip()[:100]

    async def gen():
        yield json.dumps({"type": "local", "hits": await asyncio.to_thread(directory.local, q)}) + "\n"
        if not remote or len(q) < 2:
            yield json.dumps({"type": "done"}) + "\n"
            return
        jobs = {}
        if await asyncio.to_thread(directory.gh_on):
            jobs["github"] = ({"label": "GitHub"}, lambda: directory.search_github(q))
        for r in await asyncio.to_thread(directory.user_servers):
            jobs[r["id"]] = (r, lambda r=r: directory.search_server(r, q))
        yield json.dumps({"type": "sources", "asking": [{"id": k, "label": v[0]["label"]} for k, v in jobs.items()],
                          "skipped": []}) + "\n"

        async def one(sid, r, fn):
            t0 = time.time()
            try:
                out = {"hits": await asyncio.to_thread(lambda: directory.hits(fn()))}
            except Exception as e:
                out = {"hits": [], "error": str(e)[:200]}
            return {"type": "source", "id": sid, "label": r["label"], "seconds": round(time.time() - t0, 1), **out}
        for f in asyncio.as_completed([one(k, *v) for k, v in jobs.items()]):
            yield json.dumps(await f) + "\n"
        yield json.dumps({"type": "done"}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})


@app.get("/api/people/pic/{h}")
def get_people_pic(h: str):
    """A picture from the picture folder, by the hash of its address (people found by a search)."""
    from pensieve import people
    if len(h) != 16 or set(h) - set("0123456789abcdef"):
        raise HTTPException(404)
    f = people.avatar_by_hash(h)
    if not f:
        raise HTTPException(404)
    return FileResponse(f, headers={"Cache-Control": "max-age=3600"})


@app.get("/api/people/{pid}")
def get_person(pid: str):
    """One person's card: identities, function, what they work on, and the user's relation to them."""
    from pensieve import people
    p = people.profile(pid)
    if p is None:
        raise HTTPException(404, f"unknown person {pid!r}")
    return p


@app.get("/api/people/{pid}/relations")
def get_person_relations(pid: str):
    """The people closest to the person, for the panel under the graph: peers (the same inferred team) and the
    strongest collaborators outside the team. Each link has a label, a confidence (0..1), its evidence and how the two
    work together (`about`). `evidence` says how much org evidence the builds have gathered."""
    from pensieve import people
    r = people.relations(pid)
    if r is None:
        raise HTTPException(404, f"unknown person {pid!r}")
    return r


@app.get("/api/people/{pid}/avatar")
def get_person_avatar(pid: str):
    """The person's picture, from the copy in the data folder (never fetched on each view)."""
    from pensieve import people
    f = people.avatar(pid)
    if not f:
        raise HTTPException(404)
    return FileResponse(f, headers={"Cache-Control": "max-age=3600"})


class PeopleMergeReq(BaseModel):
    ids: list[str]


class PeopleSplitReq(BaseModel):
    key: str


class PeopleMeReq(BaseModel):
    id: str


@app.post("/api/people/merge")
def merge_people(req: PeopleMergeReq):
    """These people are one person (kept on this Mac, in store.db)."""
    from pensieve import people
    try:
        return {"id": people.merge(req.ids)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/people/split")
def split_person(req: PeopleSplitReq):
    """This identity (email:…, gh:…, slack:…, name:…) is not the person it was joined to."""
    from pensieve import people
    people.split(req.key)
    return {"ok": True}


@app.post("/api/people/me")
def set_me_person(req: PeopleMeReq):
    """This person is the user."""
    from pensieve import people
    try:
        people.set_me(req.id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.delete("/api/people/overrides")
def reset_people_overrides():
    """Forget every merge, split and 'this is me' that the user made."""
    from pensieve import people
    people.reset_overrides()
    return {"ok": True}


@app.post("/api/people/profiles")
async def fetch_people_profiles():
    """Look up the Slack pictures and titles of the nearest people, with the Slack connection's read-only user
    tools, then build the graph again."""
    from pensieve import people
    res = await asyncio.to_thread(people.fetch_profiles)
    if not res.get("error"):
        people.rebuild(store, _repo_rows, force=True)
    return res


@app.get("/api/threads/history")
def get_thread_history_index():
    """The threads with saved versions: id, title, number of saves, first and last save; and which saved thread each
    current thread belongs to (`of`)."""
    from pensieve import history
    return {"threads": history.listing(), "of": history.current.of_task if history.current else {}}


@app.post("/api/threads/save")
async def save_threads():
    """Save the threads that changed now, as the keeper does every history.CHECK seconds. Returns what was saved."""
    from pensieve import history
    if history.current is None:
        raise HTTPException(503, "the thread keeper starts a minute after Pensieve")
    return {"saved": await asyncio.to_thread(history.current.run_once)}


@app.get("/api/thread/versions")
def get_thread_versions(id: str):
    """A thread's saved versions, oldest first: {at, reasons, snapshot, thread}. GET /api/thread?version=n gives one
    as a page."""
    from pensieve import history
    saves = history.history(id if id.startswith("th_") else history.thread_of(id) or "")
    if not saves:
        raise HTTPException(404, f"no saved versions of {id}")
    return [{**{k: v for k, v in s.items() if k != "card"}, "thread": s["card"]} for s in saves]


class CardRenderReq(BaseModel):
    thread: dict
    ask: str = ""


@app.post("/api/thread/share/render")
def render_share(req: CardRenderReq):
    """The Share dialog's preview: the edited thread's page exactly as saving would write it (made fit to send)."""
    from fastapi.responses import HTMLResponse
    from pensieve import cards
    return HTMLResponse(cards.render_html(cards.share(req.thread, req.ask)))


class CardSaveReq(BaseModel):
    id: str
    days: int = 30
    thread: dict | None = None  # the shared thread as the person edited it; default: a draft of the whole thread
    ask: str = ""


@app.post("/api/thread/share/save")
def save_share(req: CardSaveReq):
    """The Share button's save: the shared thread as an HTML page in ~/Downloads (the app's web view can't
    download). An edited thread is made fit to send again, and refused if it still holds anything local. Returns the
    file's path."""
    from pensieve import cards
    card = cards.share(req.thread if req.thread else cards.draft_share(_local_card(req.id, req.days), store), req.ask)
    if cards.local_parts(card):
        raise HTTPException(422, f"the thread still holds something local: {', '.join(cards.local_parts(card)[:5])}")
    return {"path": cards.save(card, Path.home() / "Downloads")}


class TaskStateReq(BaseModel):
    id: str
    state: str  # todo | open | loose_ends | complete | forgotten | auto
    reason: str = ""


@app.post("/api/thread/state")
def set_thread_state(req: TaskStateReq):
    """Set a thread's state by hand, or back to automatic with "auto"."""
    try:
        r = work.set_state(store, req.id, req.state, "user", req.reason)
    except ValueError as e:
        raise HTTPException(400, str(e))
    broadcast({"type": "thread", "ids": [req.id], "removed": []})
    return r


class ResumeReq(BaseModel):
    session: str
    run: bool = True  # false: only the command, to copy


@app.post("/api/thread/resume")
def resume_session(req: ResumeReq):
    """Open Terminal in the session's folder and resume the agent session there. Only the agent that wrote a
    session can resume it."""
    cmd = work.resume_command(store, req.session)
    if not cmd:
        raise HTTPException(404, "can't resume that session: its folder is gone, or its agent has no resume command")
    if req.run:
        _terminal(cmd)
    return {"ok": True, "command": cmd}


AGENT_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_BRIDGE_SESSION_ID",
                 "CLAUDE_PID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_ENTRYPOINT",
                 "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_EFFORT", "CLAUDE_CODE_EXECPATH")


def _outside_env() -> dict:
    """This process's environment without the marks of an agent session. A Pensieve server that an agent started
    (a dev server from a Claude Code session, say) carries CLAUDE_CODE_CHILD_SESSION and the session's id; a new
    agent that inherits them takes itself for a child of that session and does not save its transcript. The
    PENSIEVE_* variables of such a server (its data folder, its port) are its own, not the new session's."""
    return {k: v for k, v in os.environ.items() if k not in AGENT_MARKERS and not k.startswith("PENSIEVE_")}


def _terminal(cmd: str):
    """Run a shell command in a new Terminal window, as a session of its own: not a child of the agent that started
    this server (see _outside_env)."""
    import subprocess
    cmd = f"unset {' '.join(AGENT_MARKERS)}; {cmd}"
    script = cmd.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e", f'tell application "Terminal" to do script "{script}"',
                    "-e", 'tell application "Terminal" to activate'], check=False, capture_output=True, env=_outside_env())


class ReviveReq(BaseModel):
    id: str = ""  # a thread on the Threads tab, as it is now
    ids: list[str] = []  # or a cluster: the threads of one branch, newest first
    branch: str = ""  # the cluster's branch
    days: int = 30
    thread: str = ""  # or a saved version of a thread (th_ id), number `version` (default the last)
    version: int = -1
    run: bool = True  # false: only the prompt with the thread in it, to paste into any agent
    agent: str = ""  # the agent to start (agents.AGENTS); "" = the user's default agent


@app.get("/api/agents")
def list_agents():
    """The coding agents Pensieve can start: each with whether it is installed, and the user's default."""
    from pensieve import agents
    return {"agents": agents.listing(settings.get("default_agent")), "default": settings.get("default_agent") or ""}


@app.post("/api/thread/revive")
def revive_thread(req: ReviveReq):
    """Start a new agent session in Terminal with a thread as its first context: the thread now, or a version that
    the history saved, such as one from work that ended. With run=false, the prompt with the thread in it."""
    from pensieve import cards, history
    if req.ids:
        got = [_local_card(i, req.days) for i in req.ids]
        if not req.run:
            return {"ok": True, "prompt": cards.revive_cluster_prompt(got, req.branch)}
        cmd = cards.revive_cluster_command(got, req.branch, config.V2_DIR / "revive", req.agent)
        _terminal(cmd)
        return {"ok": True, "command": cmd}
    if req.thread:
        saves = history.history(req.thread)
        if not saves:
            raise HTTPException(404, f"no saved versions of {req.thread}")
        card = saves[req.version]["card"]
    else:
        card = _local_card(req.id, req.days)
    if not req.run:
        return {"ok": True, "prompt": cards.revive_prompt(card)}
    cmd = cards.revive_command(card, config.V2_DIR / "revive", req.agent)
    _terminal(cmd)
    return {"ok": True, "command": cmd}


@app.get("/api/thread/adjacent")
async def thread_adjacent(id: str, days: int = 30):
    """For one thread: coworkers' recent commits in the same folders, and tickets and pages on the same subject."""
    t = next((t for t in work.tasks(store, days) if t["id"] == id), None)
    if not t:
        raise HTTPException(404, f"unknown thread {id!r}")
    people, rel = await asyncio.gather(asyncio.to_thread(work.coworkers, t), asyncio.to_thread(work.related, t, searcher))
    return dict(coworkers=people, related=rel)


CONNECTIONS_CHECK_SECONDS = 300
DISCOVER_SECONDS = 6 * 3600


async def connections_worker():
    """Read the engine's MCP servers now and then, and collect from each connection that is on when it is due."""
    wait = 20  # soon after start, then every CONNECTIONS_CHECK_SECONDS
    while True:
        await asyncio.sleep(wait)
        wait = CONNECTIONS_CHECK_SECONDS
        if not settings.get("setup_done"):
            continue
        try:
            st = connections._load()
            if settings.enabled(f"connections/{connections.SESSIONS}") and not st.get("sessions_backfilled2"):
                if await asyncio.to_thread(connections.backfill_sessions):
                    wake_files.set()
                    broadcast({"type": "repos"})
            if (settings.get("mcp_servers") or connections.agent_configs()) and \
                    time.time() - (st.get("discovered") or 0) > DISCOVER_SECONDS:
                await asyncio.to_thread(connections.discover)
                broadcast({"type": "repos"})  # the Sources list changed
            for sid in connections.due():
                await asyncio.to_thread(connections.collect, sid)
                wake_files.set()
                broadcast({"type": "repos"})
            await asyncio.to_thread(migrate.mark_indexed, "documents")  # each connection that is due was collected
        except Exception:
            import traceback
            traceback.print_exc()


def _read_version():
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("pensieve")
    except PackageNotFoundError:
        return "dev"


# read once: an upgrade replaces the package files under a running backend, and the running code is still the old one
VERSION = _read_version()


def _version():
    return VERSION


class FeedbackReq(BaseModel):
    kind: str
    summary: str
    details: str = ""


@app.get("/api/feedback")
def get_feedback():
    from pensieve import feedback
    return {"items": feedback.items(), "new_issue": f"{feedback.REPO}/issues/new/choose"}


@app.post("/api/feedback")
def post_feedback(req: FeedbackReq):
    from pensieve import feedback
    try:
        x = feedback.add(req.kind, req.summary, req.details, source="user")
    except ValueError as e:
        raise HTTPException(400, str(e))
    broadcast({"type": "feedback"})
    return dict(x, issue_url=feedback.issue_url(x))


@app.put("/api/feedback/{fid}")
def put_feedback(fid: str, body: dict):
    from pensieve import feedback
    try:
        return feedback.set_status(fid, body.get("status", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(404)


@app.get("/api/about")
def about():
    from pensieve import reset
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
    from pensieve import reset
    reset.request(req.settings)
    asyncio.get_running_loop().call_later(0.6, reset.restart)  # after this response is sent
    return {"ok": True}


@app.post("/api/restart")
async def restart_server():
    """Start this server again in place. The index and the settings stay."""
    from pensieve import reset
    asyncio.get_running_loop().call_later(0.6, reset.restart)  # after this response is sent
    return {"ok": True}


@app.get("/api/integrations")
def get_integrations():
    from pensieve import integrations
    return integrations.status(config.PORT)


class IntegrationReq(BaseModel):
    on: bool


@app.put("/api/integrations/{item}")
def put_integration(item: str, req: IntegrationReq):
    from pensieve import integrations
    try:
        if item in integrations.TARGETS:
            integrations.set_instructions(item, req.on)
        elif item in integrations.MCP:
            integrations.set_mcp(item, req.on)
        elif item in integrations.HOOKS:
            integrations.set_hook(item, req.on, config.PORT)
        else:
            integrations.set_skill(item, req.on)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return integrations.status(config.PORT)


@app.post("/api/touched")
async def touched(req: Request):
    """Files an agent just changed: a Claude Code PostToolUse event ({tool_input: {file_path}, cwd}) or
    {paths: [...]}. They are queued for indexing as if the file watcher saw them, so the next search waits for
    them even when the file system event comes late."""
    try:
        body = await req.json()
    except ValueError:
        raise HTTPException(400, "expected JSON")
    ti = body.get("tool_input") or {}
    paths = [p for p in (ti.get("file_path"), ti.get("notebook_path"), *(body.get("paths") or [])) if isinstance(p, str) and p]
    cwd = body.get("cwd") or "/"
    for p in paths:
        watch.note(os.path.realpath(os.path.join(cwd, os.path.expanduser(p))))
    return {"queued": len(paths)}


@app.get("/api/engine")
def get_engine():
    """The text engine (engine.py): which one, how many jobs wait, its last answer and error, and the command
    templates with whether each is installed."""
    from pensieve import engine
    return engine.status()


class EngineTestReq(BaseModel):
    text_engine: str
    cli_command: str = ""
    llm_url: str = ""
    llm_model: str = ""
    llm_key: str | None = None  # None: the saved key, for the saved URL only


@app.post("/api/engine/test")
async def test_engine(req: EngineTestReq):
    """Ask an engine for one word before it is saved: {ok, reply, ms, error}."""
    from pensieve import engine
    s = {**settings.load(), **req.model_dump(exclude_none=True)}
    if req.llm_key is None and req.llm_url.rstrip("/") != settings.get("llm_url").rstrip("/"):
        s["llm_key"] = ""
    return await asyncio.to_thread(engine.test, s)


@app.get("/api/engine/models")
async def engine_models(url: str, key: str = ""):
    """The models that an OpenAI-compatible server lists; a saved key is used only for the saved URL."""
    from pensieve import engine
    if not key and url.rstrip("/") == settings.get("llm_url").rstrip("/"):
        key = settings.get("llm_key")
    try:
        return {"models": await asyncio.to_thread(engine.server_models, url, key)}
    except RuntimeError as e:
        return {"models": [], "error": str(e)}


@app.get("/api/settings")
def get_settings():
    return {"settings": settings.public(settings.load()), "defaults": settings.DEFAULTS, "descriptions": settings.DESCRIPTIONS}


@app.put("/api/settings")
def put_settings(changes: dict):
    try:
        out = {"settings": settings.public(settings.update(changes))}
    except ValueError as e:
        raise HTTPException(400, str(e))
    if "embed_model" in changes:
        wake_model.set()
    return out




def main():
    from pensieve.cli import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
