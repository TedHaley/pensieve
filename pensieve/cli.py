"""`pensieve` command.

    pensieve [serve]   run the indexer + visualizer + MCP server (http://127.0.0.1:8765, MCP at /mcp)
    pensieve mcp       MCP over stdio for agents that don't speak HTTP; starts the server if it isn't running

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
    ap.add_argument("command", nargs="?", choices=["serve", "mcp"], default="serve")
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
        return stdio_bridge(a.port, a.scope)

    import uvicorn
    from . import config, settings
    url = f"http://localhost:{a.port}"
    print(f"Pensieve → {url}  (MCP: {url}/mcp)\n  data: {config.DATA_DIR}\n  llm:  {settings.get('llm_model')} @ {settings.get('llm_url')}", flush=True)
    if not a.no_open:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("pensieve.server:app", host="127.0.0.1", port=a.port, log_level="warning")


def _up(base):
    import httpx
    try:
        return httpx.get(f"{base}/api/status", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def stdio_bridge(port, scope="auto"):
    """Relay newline-delimited JSON-RPC between stdin/stdout and the server's streamable HTTP endpoint."""
    import json
    import httpx
    from . import config
    base = f"http://127.0.0.1:{port}"
    if not _up(base):
        log = open(config.DATA_DIR / "server.log", "a")
        subprocess.Popen([sys.executable, "-m", "pensieve.cli", "serve", "--no-open", "--port", str(port)],
                         stdout=log, stderr=log, start_new_session=True)
        for _ in range(120):
            if _up(base):
                break
            time.sleep(0.5)
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    from urllib.parse import urlencode
    url = f"{base}/mcp?" + urlencode({"scope": scope, "cwd": os.getcwd()})
    with httpx.Client(timeout=None) as c:
        for line in sys.stdin:
            if not line.strip():
                continue
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
