import asyncio
import json
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import config, summarize
from .indexer import Store
from .repos import Repos

store: Store | None = None
repos: Repos | None = None
subscribers: set[asyncio.Queue] = set()
status = {"indexing": False, "message": "starting", "layout": False, "insights_running": False, "insights_scope": None}
inflight: set[str] = set()  # background LLM jobs (scoped topic names, team summaries) already running

SYSTEM = (
    "You are Pensieve, a sharp research assistant over the user's past conversations with AI coding agents "
    "(Claude Code, Qwen Code, Codex). Answer from the numbered excerpts and session summaries below; cite them "
    "inline like [1] or [2][5]. Write in concise Markdown: lead with the direct answer, then short bullets. "
    "Mention dates and projects when they matter. If the material doesn't contain the answer, say what is missing "
    "instead of guessing."
)
CODE_SYS = (
    "You are Pensieve answering questions about a codebase. Use the numbered code excerpts and cite them like [1]. "
    "Write concise Markdown. When asked who to talk to, recommend people from the git-blame list and say what they wrote."
)


def broadcast(event: dict):
    for q in list(subscribers):
        q.put_nowait(event)


async def watcher():
    loop = asyncio.get_running_loop()
    first = True
    while True:
        try:
            if first:  # bring layouts up to the current algorithm (UMAP) without blocking startup
                status.update(layout=True, message="laying out map")
                broadcast({"type": "status", **status})
                if await loop.run_in_executor(None, store.reproject):
                    broadcast({"type": "updated"})
                if await loop.run_in_executor(None, repos.reproject):
                    broadcast({"type": "repos"})
                status.update(layout=False)
                first = False
            status.update(indexing=True, message="indexing")
            broadcast({"type": "status", **status})
            n = await loop.run_in_executor(
                None, lambda: store.sync_files(
                    on_progress=lambda k: loop.call_soon_threadsafe(broadcast, {"type": "updated", "sessions": k})))
            status.update(indexing=False, message="idle")
            if n:
                broadcast({"type": "updated", "sessions": n})
            if await loop.run_in_executor(None, repos.sync):
                broadcast({"type": "repos"})
            broadcast({"type": "status", **status})
        except Exception as e:
            status.update(indexing=False, layout=False, message=f"error: {e!r}")
        await asyncio.sleep(config.POLL_SECONDS)


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
            sess, topics, chunks = summarize.pending(store)
            if sess:
                await asyncio.gather(*[loop.run_in_executor(pool, summarize.summarize_session, store, s) for s in sess[:3]])
                broadcast({"type": "summaries"})
            elif topics:
                await asyncio.gather(*[loop.run_in_executor(pool, summarize.name_topic, store, t) for t in topics[:3]])
                broadcast({"type": "topics"})
            elif store.meta("insights") is None and not status["insights_running"] and store.topics():
                await run_insights()
            elif chunks:
                await asyncio.gather(*[loop.run_in_executor(pool, summarize.summarize_chunk, store, c, t) for c, t in chunks[:3]])
            else:
                await asyncio.sleep(10)
            if status.get("llm_offline"):
                status["llm_offline"] = False
                broadcast({"type": "status", **status})
        except httpx.ConnectError:  # LM Studio (or other server) not running: everything but LLM features still works
            if not status.get("llm_offline"):
                status["llm_offline"] = True
                broadcast({"type": "status", **status})
            await asyncio.sleep(30)
        except Exception as e:
            status["message"] = f"enricher: {e!r}"[:200]
            await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app):
    global store, repos
    store = Store()
    repos = Repos(store)
    tasks = [asyncio.create_task(watcher()), asyncio.create_task(enricher())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)
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
            "summarized_sessions": db.execute("SELECT COUNT(*) FROM sessions WHERE summary IS NOT NULL AND n_chunks>0").fetchone()[0],
            "summarized_chunks": db.execute("SELECT COUNT(*) FROM chunks WHERE summary IS NOT NULL").fetchone()[0],
            "topics_named": db.execute("SELECT COUNT(*) FROM topics WHERE name IS NOT NULL").fetchone()[0],
            "topics": db.execute("SELECT COUNT(*) FROM topics").fetchone()[0],
            "code_chunks": db.execute("SELECT COUNT(*) FROM code_chunks").fetchone()[0],
            "llm": config.LLM_MODEL, "llm_url": config.LLM_URL, "embed": config.EMBED_MODEL}


@app.get("/api/projects")
def projects():
    """Projects are canonical repos (clones and worktrees merged); non-repo folders stay as-is."""
    scope = repos.session_scope()
    by = {}
    with store.lock:
        rows = store.db.execute("SELECT id, n_chunks, updated FROM sessions WHERE n_chunks>0").fetchall()
    for sid, n, upd in rows:
        v = scope.get(sid)
        if not v:
            continue
        e = by.setdefault(v["project"], dict(name=v["project"], sessions=0, chunks=0, last="", repo=bool(v["root"]),
                                              root=v["root"], folders=set()))
        e["sessions"] += 1; e["chunks"] += n; e["last"] = max(e["last"], upd or "")
        e["folders"].add(v["folder"])
    return [dict(e, folders=sorted(e["folders"])) for e in sorted(by.values(), key=lambda e: -e["sessions"])]


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


class ChatReq(BaseModel):
    messages: list[dict]
    mode: str = "ask"  # ask | code
    session_id: str | None = None
    projects: list[str] = []
    sources: list[str] = []
    since: str | None = None
    until: str | None = None
    repo: str | None = None
    session_ids: list[str] | None = None


def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


_RELATIVE = [("today", 1), ("yesterday", 2), ("this week", 7), ("past week", 7), ("last week", 14), ("past few days", 5),
             ("this month", 31), ("past month", 31), ("last month", 62), ("recently", 14), ("lately", 14)]


def _time_scope(question: str):
    """'What did I do this week?' -> only sessions active in that window."""
    ql = question.lower()
    for phrase, days in _RELATIVE:
        if phrase in ql:
            return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(), phrase
    return None, None


def _build_context(req: ChatReq, question: str):
    sources = []
    if req.mode == "code":
        res = repos.experts(q=question, repo=req.repo)
        ctx = []
        for i, c in enumerate(res["chunks"][:6]):
            ch = repos.chunk(c["id"])
            ctx.append(f"[{i + 1}] {ch['repo']}/{ch['path']}:{ch['start']}-{ch['end']}\n{ch['text'][:1400]}")
            sources.append(dict(n=i + 1, code=c["id"], title=f"{ch['path']}:{ch['start']}", repo=ch["repo"], score=c["score"]))
        people = "\n".join(f"- {e['author']} ({round(e['share'] * 100)}% of matching code; files: {', '.join(e['files'][:3])})"
                           for e in res["experts"])
        return CODE_SYS + "\n\nCode excerpts:\n" + "\n\n".join(ctx) + "\n\nPeople who wrote the matching code (git blame):\n" + people, sources
    since, phrase = (req.since, None) if req.since or req.session_id else _time_scope(question)
    hits = store.search(question, 10, req.session_id, req.projects, req.sources, since, req.until, req.session_ids)
    if phrase and len(hits) < 3:  # too narrow -> fall back to everything
        hits, phrase = store.search(question, 10, None, req.projects, req.sources, None, req.until, req.session_ids), None
    scope = repos.session_scope()
    for h in hits:
        h["project"] = scope.get(h["session"], {}).get("project", h["project"])
    seen, summaries = set(), []
    for i, h in enumerate(hits):
        sources.append(dict(n=i + 1, session=h["session"], title=h["title"], project=h["project"],
                            source=h["source"], started=h["started"], score=h["score"], summary=h["summary"]))
        if h["session"] not in seen:
            seen.add(h["session"])
            with store.lock:
                r = store.db.execute("SELECT summary FROM sessions WHERE id=?", (h["session"],)).fetchone()
            if r and r[0]:
                summaries.append(f"- {h['title']} ({h['project']}, {(h['started'] or '')[:10]}): {r[0]}")
    topics = "; ".join(f"{t['name']} ({len(t['sessions'])})" for t in store.topics())
    excerpts = "\n\n".join(
        f"[{i + 1}] {h['source']} · {h['project']} · {(h['started'] or '')[:10]} · {h['title']}\n{h['text'][:1600]}"
        for i, h in enumerate(hits))
    when = f"Today is {datetime.now():%A %B %d, %Y}." + (f" Only sessions active {phrase} were retrieved." if phrase else "")
    system = (SYSTEM + f"\n\n{when}\n\nTopics across all sessions: {topics}\n\nSummaries of the matching sessions:\n"
              + "\n".join(summaries) + "\n\nExcerpts:\n" + excerpts)
    return system, sources


@app.post("/api/chat")
async def chat(req: ChatReq):
    loop = asyncio.get_running_loop()
    question = next((m["content"] for m in reversed(req.messages) if m["role"] == "user"), "")
    system, sources = await loop.run_in_executor(None, _build_context, req, question)
    msgs = [{"role": "system", "content": system}] + req.messages[-10:]

    async def gen():
        yield _sse({"sources": sources})
        in_think, buf = False, ""
        try:
            async with httpx.AsyncClient(timeout=None) as c:
                async with c.stream("POST", f"{config.LLM_URL}/chat/completions", json={
                        "model": config.LLM_MODEL, "messages": msgs, "stream": True, "temperature": 0.5,
                        "reasoning_effort": "none"}) as r:
                    if r.status_code != 200:
                        yield _sse({"error": (await r.aread()).decode()[:300]})
                        return
                    async for line in r.aiter_lines():
                        if not line.startswith("data: ") or line.endswith("[DONE]"):
                            continue
                        buf += json.loads(line[6:])["choices"][0]["delta"].get("content") or ""
                        while True:  # drop <think>...</think> if the model emits it inline
                            if in_think:
                                j = buf.find("</think>")
                                if j < 0:
                                    buf = buf[-8:]; break
                                buf, in_think = buf[j + 8:], False
                            else:
                                j = buf.find("<think>")
                                if j < 0:
                                    emit, buf = (buf[:-7], buf[-7:]) if len(buf) > 7 else ("", buf)
                                    if emit:
                                        yield _sse({"token": emit})
                                    break
                                if j:
                                    yield _sse({"token": buf[:j]})
                                buf, in_think = buf[j + 7:], True
                    if buf and not in_think:
                        yield _sse({"token": buf})
        except httpx.HTTPError as e:
            yield _sse({"error": f"LLM server unreachable at {config.LLM_URL}: {e!r}"})
        yield _sse({"done": True})

    return StreamingResponse(gen(), media_type="text/event-stream")


def main():
    from .cli import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
