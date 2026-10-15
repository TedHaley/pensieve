"""`pensieve` command.

    pensieve [serve]   run the indexer + visualizer + MCP server (http://127.0.0.1:8765, MCP at /mcp)
    pensieve mcp       MCP over stdio for agents that don't speak HTTP; starts the server if it isn't running
    pensieve integrate [all | recommended | claude-mcp claude codex pensieve-search claude-hook ...] [--remove]
                       agent add-ons: Pensieve as Claude Code's MCP server, an instructions block for CLAUDE.md /
                       AGENTS.md, skills (shared by the agents), and Claude Code hooks (report edits; ask before indexing a repo)
    pensieve repo [status | index | skip] [path]
                       whether Pensieve indexes the git repo at path (default: here); index it, or never ask again
    pensieve setup [recommended | <source id> ...] [images] [video] [audio]
                       choose what to index on first run or after a reset (no items: list the sources)
    pensieve search <query> [--kind code|doc|session|...] [--limit 8] [--scope auto]
                       search as an agent does over MCP, scoped to the repo you run it in; prints the same lines

Options are turned into env vars before anything reads config."""
import argparse
import os
import subprocess
import sys
import threading
import time
import webbrowser


def main():
    ap = argparse.ArgumentParser(prog="pensieve", description="Semantic search and a 3D map over your files, git repos and AI agent sessions.")
    ap.add_argument("command", nargs="?", choices=["serve", "mcp", "integrate", "repo", "setup", "search"],
                    default="serve")
    ap.add_argument("--kind", help="search only: code, doc, session, folder, repo or person")
    ap.add_argument("--limit", type=int, default=8, help="search only: how many results")
    ap.add_argument("items", nargs="*", help="integrate: what to install (no items: show what's available); "
                                             "repo: status | index | skip | remove, then a folder (default: this one); "
                                             "setup: the source ids to index, or recommended (no items: list them)")
    ap.add_argument("--remove", action="store_true", help="integrate only: remove the named add-ons")
    ap.add_argument("--all-tools", action="store_true",
                    help="mcp only: also expose the tools that change Pensieve's settings, folders and scopes")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PENSIEVE_PORT", 8765)))
    ap.add_argument("--data", help="where the index lives (default ~/.pensieve)")
    ap.add_argument("--repos", nargs="+", metavar="PATH",
                    help="extra git repos to index, or folders to search for repos (adds to settings for this run)")
    ap.add_argument("--llm-url", help="OpenAI-compatible endpoint (default http://localhost:1234/v1, LM Studio)")
    ap.add_argument("--llm-model", help="chat model name (default qwen/qwen3.5-9b)")
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    ap.add_argument("--scope", default="auto",
                    help="mcp only: the slice of the index this agent works within: a scope name, 'all', or 'auto' "
                         "(default: the repo the agent was started in, or a named scope that includes it)")
    a = ap.parse_args()
    os.environ["PENSIEVE_PORT"] = str(a.port)
    for flag, env in [(a.data, "PENSIEVE_DATA"), (a.llm_url, "PENSIEVE_LLM_URL"), (a.llm_model, "PENSIEVE_LLM_MODEL")]:
        if flag:
            os.environ[env] = flag
    if a.repos:
        os.environ["PENSIEVE_REPOS"] = os.pathsep.join(os.path.abspath(os.path.expanduser(p)) for p in a.repos)
    if a.command == "mcp":
        return stdio_bridge(a.port, a.scope, a.all_tools)
    if a.command == "integrate":
        return integrate(a.items, a.remove)
    if a.command == "repo":
        return repo(a.port, *a.items[:2])
    if a.command == "setup":
        return setup(a.port, a.items)
    if a.command == "search":
        return search(a.port, " ".join(a.items), a.kind, a.limit, a.scope)

    from pensieve import reset
    done = reset.apply_pending()  # before anything opens the database
    from pensieve import reembed
    reembed.finish()  # a switch to another embedding model whose vectors are ready
    import uvicorn
    from pensieve import config, settings
    if done:
        print(f"Pensieve was reset ({'index and settings' if done == 'all' else 'index'}); indexing from scratch.", flush=True)
    url = f"http://localhost:{a.port}"
    print(f"Pensieve → {url}  (MCP: {url}/mcp)\n  data: {config.DATA_DIR}\n  llm:  {settings.get('llm_model')} @ {settings.get('llm_url')}", flush=True)
    if not a.no_open and not os.environ.get("PENSIEVE_RESTARTED"):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    # open streams (the app's live-update connection) would otherwise hold a graceful shutdown open indefinitely
    uvicorn.run("pensieve.server:app", host="127.0.0.1", port=a.port, log_level="warning", timeout_graceful_shutdown=2)
    # Python would otherwise wait at exit for indexing/layout threads, which can run for minutes and keep writing
    # after the server is gone; SQLite rolls back whatever they had in flight.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def integrate(items, remove=False):
    from pensieve import config, integrations as ig
    st = ig.status(config.PORT)
    if not items:
        print("Pensieve as an MCP server, per agent:")
        for k in st["mcp"]:
            print(f"  {k['id']:<20} {k['state'] if k['available'] else '-':<6} {k['label']} ({k['path']})"
                  f"{'' if k['available'] else ': not installed'}")
        print("Instructions block (tells the agent to search with Pensieve before grepping):")
        for t in st["instructions"]:
            print(f"  {t['id']:<20} {t['state']:<6} {t['path']}")
        print(f"Skills (in {ig._tilde(ig.SKILLS_DIR)}, with a link for Claude Code and Qwen Code):")
        for k in st["skills"]:
            print(f"  {k['id']:<20} {k['state']:<6} {k['label']}{' (recommended)' if k['recommended'] else ''}: {k['description']}")
        print(f"Claude Code hooks (in {st['hooks'][0]['path']}):")
        for k in st["hooks"]:
            print(f"  {k['id']:<20} {k['state']:<6} {k['label']}{' (recommended)' if k['recommended'] else ''}: {k['description']}")
        print("\npensieve integrate recommended        # every agent on this Mac: the MCP server, the instructions block")
        print("                                      # and pensieve-search; for Claude Code also both hooks")
        print("pensieve integrate <id>... [--remove]")
        return
    targets = {t["id"] for t in st["instructions"]}
    found = [k["id"] for k in st["mcp"] if k["available"]]
    claude = "claude-mcp" in found
    if items == ["all"]:
        items = [*found, *(t["id"] for t in st["instructions"] if t["present"]), *(ig.SKILLS if found else ()),
                 *(ig.HOOKS if claude else ())]
    elif items == ["recommended"]:
        if not found:
            print("No agent found on this Mac. Add Pensieve to your agent as the MCP server "
                  f"`{ig._pensieve_bin()} mcp` (stdio), or http://127.0.0.1:{config.PORT}/mcp over HTTP.")
            return
        items = [*found, *(t["id"] for t in st["instructions"] if t["present"]),
                 *(n for n, v in ig.SKILLS.items() if v[2]), *(n for n, v in ig.HOOKS.items() if v[2] and claude)]
    for it in items:
        try:
            if it in targets:
                state = ig.set_instructions(it, not remove)
            elif it in ig.MCP:
                state = ig.set_mcp(it, not remove)
            elif it in ig.HOOKS:
                state = ig.set_hook(it, not remove, config.PORT)
            else:
                state = ig.set_skill(it, not remove)
            print(f"{it}: {state}")
        except (ValueError, KeyError, OSError) as e:
            print(f"{it}: {e}", file=sys.stderr)
    if not remove and any(it in ig.MCP for it in items):
        print("Agents load MCP servers when they start: restart any that are open to give them Pensieve.")


def repo(port, action="status", path="."):
    """Whether Pensieve indexes the git repo at path; `index` adds it, `skip` switches it off in Sources. Either
    answer is kept, so the SessionStart hook doesn't ask about this repo again. `remove` also drops it from the
    index (the ✕ in Sources); the repo stays on disk."""
    import httpx
    if action not in ("status", "index", "skip", "remove"):
        sys.exit(f"pensieve repo: unknown action {action!r} (status, index, skip or remove)")
    base, cwd = f"http://127.0.0.1:{port}", os.path.abspath(os.path.expanduser(path))
    try:
        if action == "remove":
            c = httpx.get(f"{base}/api/repo-check", params={"cwd": cwd}, timeout=30).json()
            if c.get("state") == "none":
                sys.exit(f"{cwd} is not in a git repo.")
            r = httpx.delete(f"{base}/api/sources/repos", params={"path": c["root"]}, timeout=30)
            if r.status_code < 400:
                print(f"Pensieve removes {c.get('name')} from its index and stops indexing it. The repo stays on disk; "
                      f"`pensieve repo index` adds it back.")
                return
        elif action == "status":
            r = httpx.get(f"{base}/api/repo-check", params={"cwd": cwd}, timeout=30)
        else:
            r = httpx.post(f"{base}/api/repo-choice", json={"cwd": cwd, "index": action == "index"}, timeout=30)
    except httpx.HTTPError:
        sys.exit("Pensieve isn't running. Open the Pensieve app (or run `pensieve`), then try again.")
    if r.status_code >= 400:
        sys.exit(r.json().get("detail", r.text) if "json" in r.headers.get("content-type", "") else r.text)
    c = r.json()
    print({"none": f"{cwd} is not in a git repo.",
           "indexed": f"Pensieve indexes {c.get('name')}.",
           "pending": f"Pensieve is indexing {c.get('name')}; search covers it when that finishes.",
           "declined": f"Pensieve doesn't index {c.get('name')} (switched off in Settings > Sources).",
           "unknown": f"Pensieve doesn't index {c.get('name')} yet ({c.get('files', 0):,} tracked files). "
                      f"`pensieve repo index` adds it; `pensieve repo skip` stops the question."}[c["state"]])


def setup(port, items):
    """Choose what Pensieve indexes (first run, or after a reset). No items: list the sources, for the user to pick
    from; `recommended`: the pre-checked ones; otherwise the ids to index. Nothing is indexed until this is done."""
    import httpx
    base = f"http://127.0.0.1:{port}"
    _start_server(base, port)
    try:
        if not items:
            d = httpx.get(f"{base}/api/setup", timeout=300).json()
            print("Pensieve setup: choose what to index." + ("" if not d["done"] else " (Already set up; this changes it.)"))
            for c in d["candidates"]:
                print(f"\n{c['label']}: {c['description']}")
                for i in c["items"]:
                    size = f"{i['size']:,} {i['unit']}" if i.get("size") is not None else ""
                    mark = "x" if i["checked"] else (" " if i["available"] else "-")
                    note = " (big: slow to index)" if i.get("big") else "" if i["available"] else " (not found on this Mac)"
                    if i.get("indexed"):
                        note += f", {i['indexed']:,} already indexed"
                    if i.get("seconds") is not None and i["available"]:
                        note += f", {_duration(i['seconds'])} to index" if i["seconds"] >= 30 else ", up to date"
                    print(f"  [{mark}] {i['id']:<48} {i['label']}  {size}{note}")
            print(f"\n[x] = recommended. {d['energy']}")
            print("\nAlso read by content (off: found by name). Add the word to switch it on:")
            for c in d["content"]:
                if c["id"] != "text":
                    print(f"  {c['id']:<8} {'on ' if c['on'] else 'off'}  {c['description']} {c.get('note', '')}")
            print("\nNext: `pensieve setup recommended`, or `pensieve setup <id> <id> ...` with exactly the ids to index.")
            return
        kinds = [i for i in items if i in ("images", "video", "audio")]  # content.KINDS
        items = [i for i in items if i not in kinds]
        body = {"recommended": True} if items == ["recommended"] else {"sources": items}
        body["content"] = kinds or None
        r = httpx.post(f"{base}/api/setup", json=body, timeout=300)
    except httpx.HTTPError as e:
        sys.exit(f"Couldn't reach Pensieve ({e}). Open the Pensieve app, then try again.")
    if r.status_code >= 400:
        sys.exit(r.json().get("detail", r.text))
    d = r.json()
    print("Pensieve is indexing: " + (", ".join(d["indexing"]) or "nothing"))
    if d["repo_files"]:
        print(f"Code: {d['repo_files']:,} files, {d.get('indexed_files', 0):,} of them already indexed.")
    left = "about " + _duration(d["minutes"] * 60) if d["minutes"] > 1 else "about a minute" if d["minutes"] else "nothing"
    print(f"Time left: {left}" + (", plus your documents (not estimated)." if d.get("documents") else "."))
    if d["minutes"] > 1:
        print(d["energy"])
    print(d.get("next", ""))
    print("Change what Pensieve indexes any time in Settings > Sources.")


def _duration(secs) -> str:
    m = round(secs / 60)
    return "under a minute" if m < 1 else f"{m} minute{'s' if m > 1 else ''}" if m < 90 else f"{m / 60:.1f} hours"


def _start_server(base, port):
    """Start the backend in the background if it isn't running."""
    if _up(base):
        return
    from pensieve import config
    log = open(config.DATA_DIR / "server.log", "a")
    subprocess.Popen([sys.executable, "-m", "pensieve.cli", "serve", "--no-open", "--port", str(port)],
                     stdout=log, stderr=log, start_new_session=True)
    for _ in range(120):
        if _up(base):
            return
        time.sleep(0.5)


def _up(base):
    import httpx
    try:
        return httpx.get(f"{base}/api/status", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def search(port, query, kind=None, limit=8, scope="auto"):
    """One search through the server's MCP tool, scoped like an agent started here, printed as the tool returns it."""
    import json
    import httpx
    from urllib.parse import urlencode
    if not query.strip():
        sys.exit('usage: pensieve search "what the code does, plus names you would guess" [--kind code]')
    base = f"http://127.0.0.1:{port}"
    _start_server(base, port)
    args = {"query": query, "limit": limit, **({"kind": kind} if kind else {})}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search", "arguments": args}}
    url = f"{base}/mcp?" + urlencode({"scope": scope, "cwd": os.getcwd(), "client": "pensieve-cli"})
    try:
        r = httpx.post(url, json=body, timeout=300,
                       headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"})
    except httpx.HTTPError as e:
        sys.exit(f"Couldn't reach Pensieve ({e}). Open the Pensieve app, then try again.")
    if r.status_code >= 400:
        sys.exit(r.text)
    d = r.json()
    if "error" in d:
        sys.exit(d["error"].get("message", json.dumps(d["error"])))
    res = d.get("result") or {}
    text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
    print(text)
    if res.get("isError"):
        sys.exit(1)


def stdio_bridge(port, scope="auto", all_tools=False):
    """Relay newline-delimited JSON-RPC between stdin/stdout and the server's streamable HTTP endpoint."""
    import json
    import httpx
    base = f"http://127.0.0.1:{port}"
    _start_server(base, port)
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    import uuid
    from urllib.parse import urlencode
    params = {"scope": scope, "cwd": os.getcwd(), **({"tools": "all"} if all_tools else {}), "conn": uuid.uuid4().hex[:12]}
    url = f"{base}/mcp?" + urlencode(params)
    with httpx.Client(timeout=None) as c:
        for line in sys.stdin:
            if not line.strip():
                continue
            if "client" not in params and '"initialize"' in line:  # the usage log names the agent that called
                try:
                    params["client"] = json.loads(line)["params"]["clientInfo"]["name"]
                    url = f"{base}/mcp?" + urlencode(params)
                except (ValueError, KeyError, TypeError):
                    pass
            try:
                r = c.post(url, content=line, headers=headers)
                if r.status_code == 202 or not r.content:
                    continue
                if r.status_code >= 400:  # e.g. unknown scope
                    msg = json.loads(line)
                    if "id" in msg:
                        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {
                            "code": -32000, "message": r.json().get("error", r.text) if "json" in r.headers.get("content-type", "") else r.text}}) + "\n")
                        sys.stdout.flush()
                    continue
                body = r.text
                if r.headers.get("content-type", "").startswith("text/event-stream"):
                    body = "\n".join(l[6:] for l in body.splitlines() if l.startswith("data: "))
                sys.stdout.write(body.strip() + "\n")
            except httpx.HTTPError as e:
                msg = json.loads(line)
                if "id" in msg:
                    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                                 "error": {"code": -32000, "message": f"Pensieve server unreachable: {e!r}"}}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
