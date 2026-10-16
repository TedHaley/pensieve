"""MCP server: lets any agent search, read, and configure Pensieve. Served at http://127.0.0.1:<port>/mcp
(streamable HTTP); `pensieve mcp` bridges stdio to it for agents that only speak stdio."""
import contextvars
import functools
import json
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from pensieve import actions, onboarding, scopes, settings, sources, usage

# Tool descriptions are sent to the agent on every turn, so connections get the tools for finding code and people by
# default; ?tools=all (or `pensieve mcp --all-tools`) adds the ones that manage Pensieve itself.
AGENT_TOOLS = {"search", "read", "similar", "overview", "who_knows", "team", "unexplored", "knowledge_gaps", "list_repos",
               "current_scope", "open", "index_path", "report_feedback", "stale"}
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
    string also works. "quotes" = exact, -word excludes, ext:py, in:<path>, by:<person>, person:<name>,
    kind:code|doc|session|folder|repo|person. One line per hit, best first: path:line  enclosing definition ▸ the
    line that matched  [id for read/similar]. If those lines show what you need, use the hit: no grep to confirm."""
    sc = _scope()
    r = _c()["searcher"].find(f"{query} kind:{kind}" if kind else query, limit, scope=sc)
    cwd = (agent_cwd.get() or "").rstrip("/")
    s = _c()["searcher"]
    with s.lock:
        wts = [p for (p,) in s.db.execute("SELECT path FROM worktrees")]

    def elsewhere(x):  # a linked worktree's copy (another branch) that the agent isn't working in: ranked last
        if " @ " not in (x.get("subtitle") or ""):
            return False
        wt = next((w for w in wts if (x.get("path") or "").startswith(w + "/")), None)
        # by its worktree, not by cwd: agent worktrees can sit inside the repo (.claude/worktrees/…)
        return not (cwd and wt and (cwd == wt or cwd.startswith(wt + "/")))
    res = [x for x in r["results"] if not elsewhere(x)] + [x for x in r["results"] if elsewhere(x)]
    usage.note(n_results=len(res), results=[[x.get("id"), x.get("path")] for x in res], laps=r.get("timing"))
    res = _without_live_sessions(res, s, cwd)
    _mark_near_copies(res, s)
    _to_own_checkout(res, s, cwd)
    found = _lines(res)
    if getattr(sc, "unindexed", False):  # only past sessions in this repo can match; its code isn't indexed
        return (f"{sc.description}. `pensieve repo index` indexes it." + (f"\n{found}" if found else ""))
    return found or "No matches."


LIVE_SECONDS = 300  # a session in the agent's folder written to this recently is the agent's own conversation


def _without_live_sessions(results, searcher, cwd):
    """Leave out sessions still being written in the agent's folder: the calling agent's own conversation matches
    its own question best, but holds nothing it doesn't already know. MCP doesn't say which session calls, so a
    second live session in the same folder is left out too."""
    sids = [x["id"][8:] for x in results if str(x.get("id", "")).startswith("session:")]
    if not cwd or not sids:
        return results
    with searcher.lock:
        rows = searcher.db.execute(f"SELECT id, project, path FROM sessions WHERE id IN ({','.join('?' * len(sids))})",
                                   sids).fetchall()
    now, live = time.time(), set()
    for sid, project, path in rows:
        p = (project or "").rstrip("/")
        if p and (cwd == p or cwd.startswith(p + "/") or p.startswith(cwd + "/")):
            try:
                if now - Path(path).stat().st_mtime < LIVE_SECONDS:
                    live.add(sid)
            except OSError:
                pass
    return [x for x in results if not (str(x.get("id", "")).startswith("session:") and x["id"][8:] in live)]


def _to_own_checkout(results, searcher, cwd):
    """An agent working in a linked worktree sees a main-checkout hit at the worktree's own copy of the file, when
    that copy is the same: the index holds a worktree's files only where they differ from the main checkout."""
    if not cwd:
        return
    with searcher.lock:
        row = next(((w, repo) for w, repo in searcher.db.execute("SELECT path, repo FROM worktrees")
                    if cwd == w or cwd.startswith(w + "/")), None)
        if not row:
            return
        wt, root = row
        changed = {p for (p,) in searcher.db.execute("SELECT path FROM checkout_files WHERE repo=? AND wt=?", (root, wt))}
    for x in results:
        path = x.get("path") or ""
        if str(x.get("id", "")).startswith("code:") and path.startswith(root + "/"):
            rel = path[len(root) + 1:]
            if rel in changed:
                x["own_differs"] = True  # the main checkout's version: the agent's copy is another
            elif (Path(wt) / rel).is_file():
                x["path"] = str(Path(wt) / rel)


NEAR_COPY = 0.9  # matched code this alike in two files: the query can't tell them apart, so the agent reads both


def _mark_near_copies(results, searcher):
    """Note on a code hit whose matched code is a near-copy of a higher hit's in another file (a vendored or
    duplicated file, the same pipeline for two domains), so the agent compares them instead of taking the first."""
    import difflib
    code = [x for x in results if str(x.get("id", "")).startswith("code:") and x.get("path")]
    if len(code) < 2:
        return
    ids = [int(x["id"][5:]) for x in code]
    with searcher.lock:
        text = dict(searcher.db.execute(f"SELECT id, text FROM code_chunks WHERE id IN ({','.join('?' * len(ids))})", ids))
    for j, b in enumerate(code):
        tb = text.get(ids[j]) or ""
        for i in range(j):
            a, ta = code[i], text.get(ids[i]) or ""
            if a["path"] != b["path"] and ta and tb and difflib.SequenceMatcher(None, ta, tb).ratio() >= NEAR_COPY:
                b["near_copy"] = a["path"]
                break


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
            if x.get("own_differs"):
                where += " (main checkout; differs in your worktree)"
            if x.get("near_copy"):
                other = x["near_copy"]
                other = other[len(cwd.rstrip("/")) + 1:] if cwd and other.startswith(cwd.rstrip("/") + "/") else other
                where += f" (near-copy of {other}: read both to tell them apart)"
            if x.get("focus"):  # code: the line that matched, inside its definition
                cut = lambda s, n: s[:n] + ("…" if len(s) > n else "")
                snip = (f"{cut(x['definition'], 90)}  ▸ " if x.get("definition") else "") + cut(x["focus"], 140)
        out.append(f"{where}  {snip}  [{x.get('id')}]".replace("    ", "  "))
        if x.get("excerpt"):  # the top hits: a few numbered lines, as Read would show them
            out += [f"    {n}│ {t.rstrip()}" for n, t in x["excerpt"]]
    return "\n".join(out)


@tool()
def read(id: str, max_chars: int = 12000) -> dict:
    """Full text of a search result id: code with its owners and recent commits, a document, or a session. Several
    ids, separated by spaces, are read in one call ("code:12 code:40 session:…"), sharing max_chars."""
    ids = [i for i in id.replace(",", " ").split() if i]
    if len(ids) <= 1:
        _check(id.strip())
        return actions.read(id.strip(), max_chars)
    each = max(2000, max_chars // len(ids))
    out = []
    for i in ids[:8]:
        try:
            _check(i)
            out.append(actions.read(i, each))
        except KeyError:
            out.append({"id": i, "error": "not found"})
        except ValueError as e:  # outside the connection's scope
            out.append({"id": i, "error": str(e)})
    return {"items": out, **({"skipped": ids[8:]} if len(ids) > 8 else {})}


def prompt_context(prompt: str, cwd: str) -> str:
    """For Claude Code's UserPromptSubmit hook: Pensieve's best code matches for a prompt sent in an indexed repo, as
    the search tool shows them, or "" (a slash command, a few words, or a folder Pensieve doesn't index)."""
    import re
    text = (prompt or "").strip()
    if not cwd or text.startswith("/") or len(text.split()) < 3:
        return ""
    sc = scopes.resolve("auto", cwd, _c()["repos"])
    if sc is None or getattr(sc, "unindexed", False) or not sc.repos:
        return ""
    q = re.sub(r"\s+", " ", text.replace('"', " "))[:400]  # a prompt's quotes aren't search syntax
    t, tokens = time.perf_counter(), (scopes.current.set(sc), agent_cwd.set(cwd))
    try:
        res = [x for x in _c()["searcher"].find(q, 5, kinds=["code"], scope=sc)["results"] if x.get("kind") == "code"]
        found = _lines(res)[:2500]
    finally:
        scopes.current.reset(tokens[0])
        agent_cwd.reset(tokens[1])
    usage.record(ts=time.time(), source="hook", tool="prompt_search", client="claude-code", cwd=cwd, scope=sc.name,
                 args={"query": q[:200]}, ms=round((time.perf_counter() - t) * 1000, 2), ok=True, n_results=len(res),
                 results=[[x.get("id"), x.get("path")] for x in res], out_chars=len(found))
    if not found:
        return ""
    return (f"Pensieve searched this prompt in {sc.name} before you started (path:line, enclosing definition ▸ "
            f"matched line). If a hit answers it, use it; search or read only for what is missing.\n{found}")


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


SMALL_REPO = 150  # files: up to this many, the overview lists every file under its topic


def _dependencies(manifests: dict) -> list[str]:
    """Package names from a repo's manifests (pyproject.toml, package.json, go.mod, Cargo.toml, Gemfile): enough to
    say what it is built with."""
    import re
    out = []
    t = manifests.get("pyproject.toml", "")
    m = re.search(r"(?ms)^dependencies\s*=\s*\[(.*?)^\]", t)
    if m:  # one requirement per line: the package name opens its string ("mlx-lm>=0.2; sys_platform == 'darwin'")
        out += re.findall(r"(?m)^\s*['\"]([A-Za-z0-9][A-Za-z0-9_.\-]*)", m.group(1))
    t = manifests.get("package.json", "")
    m = re.search(r'(?s)"dependencies"\s*:\s*\{(.*?)\}', t)
    if m:
        out += re.findall(r'"([^"]+)"\s*:', m.group(1))
    out += re.findall(r"(?m)^\s*(?:require\s+)?([\w.\-]+/[\w.\-/]+)\s+v[\d.]", manifests.get("go.mod", ""))
    m = re.search(r"(?ms)^\[dependencies\](.*?)(?:^\[|\Z)", manifests.get("Cargo.toml", ""))
    if m:
        out += re.findall(r"(?m)^([\w\-]+)\s*=", m.group(1))
    out += re.findall(r"(?m)^\s*gem\s+['\"]([^'\"]+)", manifests.get("Gemfile", ""))
    return list(dict.fromkeys(x for x in out if x))


@tool()
def overview(repo: str | None = None) -> str:
    """Where things are in a repo, in one call: size, languages, top folders, the README's start, dependencies, the
    largest files, then topics (files grouped by meaning, from Pensieve's index) with their files and authors. It does
    not say what the code does or how the parts work: read a topic's main files before you describe it. repo: the
    one you are in by default. Files indexed in the last minute can be missing until the map catches up."""
    from collections import Counter
    from pathlib import Path
    sc = _scope()
    if repo:
        _check_repo(repo)
        root = _c()["repos"]._root(repo)
    elif sc is not None and sc.repos and len(sc.repos) == 1:
        root = next(iter(sc.repos))
    else:
        names = [r["name"] for r in _c()["repos"].list() if scopes.repo_allowed(sc, r["root"])]
        raise ValueError(f"name a repo: {', '.join(names[:20]) or 'none indexed'}")
    dm = _c()["datamap"]
    pts = {p["id"]: p for p in dm.points()["points"] if (p.get("repo_root") or "") == root}
    code = [p for p in pts.values() if p["type"] == "code"]
    rel = lambda p: p["path"][len(root) + 1:] if p["path"].startswith(root + "/") else p["path"]
    langs = Counter(p["ext"] or "(none)" for p in code).most_common(6)
    tops = Counter(rel(p).split("/")[0] + ("/" if "/" in rel(p) else "") for p in code).most_common(8)
    readmes = sorted((rel(p) for p in code if p["title"].lower().startswith("readme")), key=lambda r: (r.count("/"), r))[:5]
    authors = Counter(p["author"] for p in code if p.get("author")).most_common(5)
    out = [f"{Path(root).name}: {len(code):,} files, {sum(p['type'] == 'session' for p in pts.values())} agent sessions",
           "languages: " + ", ".join(f"{e} {n}" for e, n in langs),
           "top folders: " + ", ".join(f"{d} {n}" for d, n in tops),
           "most lines by: " + ", ".join(f"{a} ({n} files)" for a, n in authors)]
    if readmes:
        out.append("READMEs: " + ", ".join(readmes))
    big = sorted(code, key=lambda p: -(p.get("chunks") or 0))[:6]  # topics count files: a big one weighs as much as a small one
    out.append("largest files: " + ", ".join(f"{rel(p)} ({p['chunks']} chunks)" for p in big))
    s = _c()["searcher"]
    first = {}
    with s.lock:  # what it is and what it's built with, from the index (the README's start, the manifests' dependencies)
        for path, text in s.db.execute(
                "SELECT path, text FROM code_chunks WHERE repo=? AND wt='' AND path IN (?,?,?,?,?,?) ORDER BY path, start",
                (root, "README.md", "pyproject.toml", "package.json", "go.mod", "Cargo.toml", "Gemfile")):
            first[path] = first.get(path, "") + text + "\n"
    if first.get("README.md"):
        out.append("README starts: " + " ".join(first["README.md"].split())[:600])
    deps = _dependencies(first)
    if deps:
        out.append("dependencies: " + ", ".join(deps[:25]))
    def holds(d):  # subfolders of d with their file counts, biggest first
        return Counter(rel(p)[len(d):].split("/")[0] + "/" for p in code if rel(p).startswith(d) and "/" in rel(p)[len(d):])
    for d, _ in tops[:4]:  # the biggest folders, two levels down
        if d.endswith("/") and (subs := holds(d)):
            out.append(f"{d} holds: " + ", ".join(f"{s} {n}" for s, n in subs.most_common(10)))
            for s, _ in subs.most_common(4):
                if inner := holds(d + s):
                    out.append(f"  {d}{s} holds: " + ", ".join(f"{x} {n}" for x, n in inner.most_common(10)))
    # more topics than the map shows (it has 8 colors): a small repo still splits into parts worth naming
    res = dm.scoped_topics([root], k=max(4, min(10, round(len(pts) ** 0.5 / 2))))
    members = Counter(res["of"].values())
    out.append(f"\n{len(res['topics'])} topics, biggest first. A topic groups files that are about the same thing; "
               f"its name comes from keywords. Read its main files to learn what it does.")
    for t in sorted(res["topics"], key=lambda t: -members[t["id"]]):
        ids = [i for i, c in res["of"].items() if c == t["id"] and i in pts]
        if len(code) <= SMALL_REPO:  # every file, so no part can be left out unseen
            reps = {i: k for k, i in enumerate(t["reps"])}
            files = [rel(pts[i]) for i in sorted((i for i in ids if pts[i]["type"] == "code"), key=lambda i: reps.get(i, 1e9))]
        else:
            files = [rel(pts[i]) for i in t["reps"] if i in pts and pts[i]["type"] == "code"][:8]
        sess = [pts[i]["title"][:60] for i in t["reps"] if i in pts and pts[i]["type"] == "session"][:2]
        who = Counter(pts[i]["author"] for i in ids if pts[i]["type"] == "code" and pts[i].get("author")).most_common(3)
        out.append(f"- {t['name']} ({len(ids)} items)" + (f": {t['description']}" if t.get("description") else ""))
        out.append(f"  keywords: {t['keywords']}")
        if files:
            out.append(("  files, main first: " if len(code) <= SMALL_REPO else "  main files: ") + ", ".join(files))
        if sess:
            out.append("  sessions: " + "; ".join(sess))
        if who:
            out.append("  authors: " + ", ".join(f"{a} ({n})" for a, n in who))
    return "\n".join(out)


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


@tool()
def stale(days: int = 14) -> dict:
    """What looks abandoned on this Mac, to help the user keep a lean system. Each item has a `kind`:
    "worktree": a git worktree with no commit and no agent session in `days` days. verdict "remove": clean, its
    commits merged or pushed; `command` removes it (`deletes` lists the throwaway folders, such as .venv, that go
    with it). "review": it has work that exists nowhere else; `keep_work` pushes it.
    "branches": local branches merged into the default branch, idle `days` days, not checked out. "remove":
    `command` is `git branch -d`, which refuses anything unmerged. "review": git branch -d refuses them for now.
    "repo": an indexed repo no agent session used in 3 months, with no git activity either. verdict "unindex":
    `command` removes it from Pensieve's index only; the repo stays on disk.
    Show the user the list, then run a command only after they say yes to that item. Pensieve drops removed
    worktrees from its index on its next sweep."""
    from pensieve import stale as st
    items = st.from_store(_c()["store"], max(1, days))
    home = str(Path.home())
    short = lambda s: s.replace(home, "~") if isinstance(s, str) else s
    return {"days": days, "chunks": sum(w["chunks"] for w in items),
            "items": [{k: short(v) for k, v in w.items() if v not in (None, False, [], "")} for w in items]}


@tool()
def report_feedback(kind: str, summary: str, details: str = "") -> dict:
    """Report a problem with Pensieve, or an improvement, when you meet one: a tool error, a search that missed
    code you then found another way, a wrong or stale result, something slow, or a tool that would have saved you
    calls. kind: "bug" or "idea". summary: one line. details: what you did (the query or tool call), what you got,
    what you expected. Keep secrets and private code out. The report stays on this Mac until the user sends it."""
    from pensieve import feedback
    x = feedback.add(kind, summary, details, source=agent_client.get() or "agent", context={"cwd": agent_cwd.get()})
    return {"saved": x["id"], "note": "Saved on this Mac. The user can review and send it in Settings → Feedback."}


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
    """What Pensieve indexes, by category (agents, files, repos, apps, connections), with on/off state and item counts.
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
    """Each file type in Files & folders (text, pdf, office, data, code, images, video, audio, other): whether its
    content is read (index: content | name | off), whether it shows on the map, how many files it has, and a histogram
    of their text length. Images, video and audio set to content download their model part first. Change one with update_settings({"file_types": {"pdf": {"index": "name", "map": false}}}); the chunk
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
    5. Ask which of images, video and audio to read by what they show or say (`content`: each entry's note says
       what it costs). All three are off by default: then those files are found by name.
    6. Ask which insights engine to use (`ai`; "none" is fine, it can change later in Settings).
    7. Tell them indexing takes a while and uses a lot of energy, so plug in a laptop, then call setup_start."""
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
def setup_start(sources: list[str] | None = None, recommended: bool = False, ai: str | None = None,
                content: list[str] | None = None) -> dict:
    """Finish setup: index exactly these source ids from setup_scan (or, with recommended=true, the pre-checked
    ones); everything else is switched off. `ai` sets the insights engine (an id from setup_guide's `ai`, or "none").
    `content`: which of "images", "video" and "audio" to read by what they show or say (this selects the
    EmbeddingGemma 2 search model, and their model parts download in the background); the others are found by name.
    Indexing starts at once; search works meanwhile. Tell the user `minutes` (the time left, counting only what isn't
    indexed yet), the `energy` note when minutes > 1, and `next` (how to search and see the map)."""
    res = onboarding.start(sources, recommended, ai, kinds=content)
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
