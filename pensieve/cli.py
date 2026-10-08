"""`pensieve` command: parse options into env vars before anything reads config, then serve."""
import argparse
import os
import threading
import webbrowser


def main():
    ap = argparse.ArgumentParser(prog="pensieve", description="A local knowledge map of your AI coding sessions.")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PENSIEVE_PORT", 8765)))
    ap.add_argument("--data", help="where the index lives (default ~/.pensieve)")
    ap.add_argument("--repos", nargs="+", metavar="PATH",
                    help="extra git repos to index, or folders to search for repos (3 levels deep). "
                         "By default only repos your agent sessions ran in are indexed.")
    ap.add_argument("--llm-url", help="OpenAI-compatible endpoint (default http://localhost:1234/v1, LM Studio)")
    ap.add_argument("--llm-model", help="chat model name (default qwen/qwen3.5-9b)")
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    a = ap.parse_args()
    for flag, env in [(a.data, "PENSIEVE_DATA"), (a.llm_url, "PENSIEVE_LLM_URL"), (a.llm_model, "PENSIEVE_LLM_MODEL")]:
        if flag:
            os.environ[env] = flag
    if a.repos:
        os.environ["PENSIEVE_REPOS"] = os.pathsep.join(os.path.abspath(os.path.expanduser(p)) for p in a.repos)

    import uvicorn
    from . import config
    url = f"http://localhost:{a.port}"
    print(f"Pensieve → {url}\n  data: {config.DATA_DIR}\n  llm:  {config.LLM_MODEL} @ {config.LLM_URL}", flush=True)
    if not a.no_open:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("pensieve.server:app", host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
