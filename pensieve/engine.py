"""The background writer of Pensieve's text jobs (agent_jobs: thread notes, summaries, topic names, insights). The
setting `text_engine` picks it:

    builtin  Qwen 3.5 9B on this Mac (mlx_lm server on BUILTIN_PORT): Pensieve starts it on the first job and stops it
             after IDLE_SECONDS without one
    cli      a command-line agent (cli_command, e.g. `claude -p …`), run once per job in an empty folder
    server   a model server that the user runs, local or hosted: an OpenAI-compatible URL (llm_url, llm_model,
             llm_key), such as LM Studio or Ollama on this Mac

There is never more than one built-in server: a server that already answers on BUILTIN_PORT is used, whoever
started it (a backend from before a restart), and only the holder of a lock file can start one. Only one job runs
at a time across every Pensieve process (a lock file), with a time limit and at most `engine_rate` jobs a minute.
An agent can still answer jobs with pending_work and submit_work (with tools=all): the first answer wins.

Every prompt to a command opens with MARK, and the command runs in a TEMP_PREFIX folder: the indexer never takes
such a session for the user's work, so notes are never written about notes."""
import fcntl
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from pensieve import agent_jobs, config, settings

CONN = "pensieve-engine"  # the engine's name in the queue, as an agent session's
TIMEOUT = 180  # seconds for one job
TEMP_PREFIX = "pensieve-ai-"  # the empty folder a command runs in; the indexer never indexes a session there
MARK = "[pensieve text job]"  # the first line of every prompt the engine sends; the indexer skips such sessions
_NO_TOOLS = "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit"
# Commands that answer one prompt without tools, MCP servers, hooks or a saved transcript. The prompt goes on stdin
# unless the command has {prompt}; {out} is a file that the command writes its answer to.
TEMPLATES = [
    dict(id="claude", label="Claude Code (Haiku)",
         command=f'claude -p --model haiku --strict-mcp-config --setting-sources "" --no-session-persistence '
                 f'--tools "" --disallowedTools {_NO_TOOLS} --output-format text --max-turns 1'),
    dict(id="codex", label="Codex", command="codex exec --skip-git-repo-check --sandbox read-only --output-last-message {out} -"),
    dict(id="gemini", label="Gemini CLI", command="gemini --output-format text --prompt {prompt}"),
    dict(id="qwen", label="Qwen Code", command="qwen --approval-mode plan --output-format text {prompt}"),
    dict(id="ollama", label="Ollama", command="ollama run qwen3"),
]
BUILTIN_MODEL = "mlx-community/Qwen3.5-9B-MLX-4bit"
BUILTIN_PORT = 8766
BUILTIN_URL = f"http://127.0.0.1:{BUILTIN_PORT}/v1"
IDLE_SECONDS = 10 * 60  # the built-in server holds about 6 GB of memory: stop it after this long without a job
MIN_RAM_GB = 16
state = dict(at=0.0, error=None, answered=0, busy=False)  # for the Settings page
builtin = dict(status="off", detail="", last_used=time.time(), download=None)  # a server found at start gets the full idle time
_lock = threading.Lock()


def _json_from(t: str):
    return json.loads(t[t.find("{"):t.rfind("}") + 1])


def _strip_think(t: str) -> str:
    return (t.split("</think>", 1)[1] if "</think>" in t else t).strip()


def _schema_hint(schema) -> str:
    return ("\n\nReply with only a JSON object (no prose, no code fences) that matches this JSON schema:\n"
            + json.dumps(schema)) if schema else ""


def run_command(command: str, text: str, timeout: int = TIMEOUT, folder: str = "engine") -> str:
    """Run a command-line agent once on `text` in an empty folder, at background priority, and return its answer.
    A command that runs too long is killed with everything it started. The folder is a fixed one, emptied each time:
    an agent CLI keeps a record for each working folder it sees (Claude Code's projects), so a new folder per job
    would leave thousands. Callers that can run at the same time use different `folder` names."""
    argv = shlex.split(command) or [""]
    exe = config.find_exe(argv[0])
    if not exe:
        raise RuntimeError(f"can't find {argv[0] or 'a command'}")
    tmp = os.path.join(tempfile.gettempdir(), TEMP_PREFIX + folder)
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    out_file = os.path.join(tmp, "answer.txt")
    args = [exe] + [text if a == "{prompt}" else out_file if a == "{out}" else a for a in argv[1:]]
    if os.path.exists("/usr/sbin/taskpolicy"):  # background QoS: it yields to whatever the user is doing
        args = ["/usr/sbin/taskpolicy", "-b"] + args
    p = subprocess.Popen(args, cwd=tmp, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, start_new_session=True)
    try:
        out, err = p.communicate(None if "{prompt}" in argv else text, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        raise RuntimeError(f"{argv[0]} took more than {timeout} s") from None
    if p.returncode:
        raise RuntimeError(f"{argv[0]} failed: {(err or out).strip()[:300]}")
    if "{out}" in argv and os.path.exists(out_file):
        return open(out_file).read().strip()
    return out.strip()


def server_models(url: str, key: str = "", timeout: float = 4) -> list[str]:
    """The model ids that an OpenAI-compatible server lists. Raises RuntimeError when it can't be reached."""
    import httpx
    try:
        r = httpx.get(f"{url.rstrip('/')}/models", timeout=timeout,
                      headers={"Authorization": f"Bearer {key}"} if key else {})
    except httpx.HTTPError as e:
        raise RuntimeError(f"can't reach {url} ({type(e).__name__})") from e
    if r.status_code in (401, 403):
        raise RuntimeError("the server needs a valid API key")
    if r.status_code != 200:
        raise RuntimeError(f"{url}/models answered {r.status_code}")
    return sorted(m["id"] for m in r.json().get("data", []) if isinstance(m, dict) and m.get("id"))


def _first_model(url: str, key: str) -> str:
    """The server's first chat model, for a server with no model chosen (embedding models are left out)."""
    chat = [m for m in server_models(url, key) if "embed" not in m.lower()]
    if not chat:
        raise RuntimeError(f"{url} lists no chat model: load one, or pick the model in Settings → Models")
    return chat[0]


def ask_server(url: str, model: str, key: str, system: str, prompt: str, schema, max_tokens: int) -> str:
    import httpx
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    # Qwen 3 thinks first unless told not to; LM Studio reads reasoning_effort, other servers the template switch
    body = {"model": model, "messages": msgs, "max_tokens": max_tokens, "temperature": 0.2,
            "reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False}}
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "strict": True, "schema": schema}}
    try:
        r = httpx.post(f"{url.rstrip('/')}/chat/completions", json=body, timeout=TIMEOUT,
                       headers={"Authorization": f"Bearer {key}"} if key else {})
    except httpx.HTTPError as e:
        raise RuntimeError(f"can't reach {url} ({type(e).__name__})") from e
    if r.status_code != 200:
        raise RuntimeError(f"{url} answered {r.status_code}: {r.text[:200]}")
    m = r.json()["choices"][0]["message"]
    # a reasoning model on some servers answers only in its reasoning field
    return _strip_think(m.get("content") or m.get("reasoning_content") or m.get("reasoning") or "")


def builtin_unsupported() -> str | None:
    """Why this Mac can't run the built-in model, or None."""
    import platform
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return "The built-in model needs an Apple Silicon Mac."
    try:
        gb = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout) / 2**30
    except (ValueError, OSError):
        gb = 0
    if gb < MIN_RAM_GB:
        return f"The built-in model needs {MIN_RAM_GB} GB of memory."
    return None


def _local_copy() -> str | None:
    """The same MLX model that LM Studio downloaded already, so it isn't downloaded twice."""
    import glob
    for d in glob.glob(os.path.expanduser(f"~/.lmstudio/models/*/{BUILTIN_MODEL.split('/')[-1]}")):
        if os.path.exists(os.path.join(d, "config.json")):
            return d
    return None


def _have_weights() -> bool:
    if _local_copy():
        return True
    from huggingface_hub import try_to_load_from_cache
    return all(isinstance(try_to_load_from_cache(BUILTIN_MODEL, f), str)
               for f in ("config.json", "model.safetensors.index.json"))


def start_download():
    """Download the built-in model on a background thread. Safe to call again while it runs."""
    t = builtin["download"]
    if (t and t.is_alive()) or _have_weights():
        return

    def run():
        from huggingface_hub import snapshot_download
        os.environ.pop("HF_HUB_OFFLINE", None)
        builtin.update(status="downloading", detail="Downloading Qwen 3.5 9B (about 5 GB) in the background")
        try:
            snapshot_download(BUILTIN_MODEL)
            builtin.update(status="off", detail="")
        except Exception as e:
            builtin.update(status="error", detail=f"The download failed: {e!r}"[:300])
    builtin["download"] = threading.Thread(target=run, daemon=True, name="builtin-download")
    builtin["download"].start()


def builtin_pids() -> list[int]:
    """The built-in servers that run now, oldest first, whichever Pensieve process started them."""
    out = subprocess.run(["pgrep", "-f", f"mlx_lm server .*--port {BUILTIN_PORT}"], capture_output=True, text=True).stdout
    return sorted(int(p) for p in out.split())


def _builtin_up() -> bool:
    try:
        server_models(BUILTIN_URL, timeout=2)
        return True
    except RuntimeError:
        return False


def ensure_builtin() -> str:
    """The URL of the one built-in server. A server that answers is used as it is. Otherwise, under the lock file,
    a server that still loads is waited for, extra servers are stopped, and a server is started only when none runs."""
    builtin["last_used"] = time.time()
    if _builtin_up():
        builtin.update(status="ready", detail="")
        return BUILTIN_URL
    why = builtin_unsupported()
    if why:
        raise RuntimeError(why)
    if not _have_weights():  # never wait for a 5 GB download: get it in the background and try again later
        start_download()
        raise RuntimeError(builtin["detail"] or "Downloading the built-in model")
    with open(config.DATA_DIR / "builtin.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        pids = builtin_pids()
        for p in pids[1:]:
            os.kill(p, signal.SIGTERM)
        if not pids:
            builtin.update(status="starting", detail="Loading Qwen 3.5 9B")
            argv = [sys.executable, "-m", "mlx_lm", "server", "--model", _local_copy() or BUILTIN_MODEL,
                    "--host", "127.0.0.1", "--port", str(BUILTIN_PORT)]
            if os.path.exists("/usr/sbin/taskpolicy"):
                argv = ["/usr/sbin/taskpolicy", "-b"] + argv
            with open(config.DATA_DIR / "builtin-llm.log", "a") as log:
                subprocess.Popen(argv, stdout=log, stderr=log, start_new_session=True)
        for _ in range(600):  # loading at background priority can take a few minutes on a busy Mac
            if _builtin_up():
                builtin.update(status="ready", detail="")
                return BUILTIN_URL
            if not builtin_pids():
                builtin.update(status="error", detail="The built-in model stopped: see ~/.pensieve/builtin-llm.log")
                raise RuntimeError(builtin["detail"])
            time.sleep(1)
    raise RuntimeError("The built-in model is still loading")


def stop_builtin():
    for p in builtin_pids():
        try:
            os.kill(p, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if builtin["status"] != "downloading":
        builtin.update(status="off", detail="")


def sweep():
    """Stop the built-in server when it is not the engine, or after IDLE_SECONDS without a job."""
    if (settings.get("text_engine") != "builtin" or time.time() - builtin["last_used"] > IDLE_SECONDS) \
            and not _lock.locked() and builtin_pids():
        stop_builtin()


def answer(job: dict, s: dict | None = None, folder: str = "engine") -> str:
    """One job's answer from the engine in the settings `s` (default: the saved ones)."""
    s = s or settings.load()
    if s["text_engine"] == "cli":
        text = MARK + "\n" + (job["system"] + "\n\n" if job["system"] else "") + job["prompt"] + _schema_hint(job["schema"])
        out = _strip_think(run_command(s["cli_command"], text, folder=folder))
    elif s["text_engine"] == "server":
        out = ask_server(s["llm_url"], s["llm_model"] or _first_model(s["llm_url"], s["llm_key"]), s["llm_key"],
                         job["system"], job["prompt"], job["schema"], max(job["max_tokens"], 400))
    elif s["text_engine"] == "builtin":
        url = ensure_builtin()
        out = ask_server(url, _first_model(url, ""), "", job["system"], job["prompt"], job["schema"],
                         max(job["max_tokens"], 400))
    else:
        raise RuntimeError("no text engine: pick one in Settings → Models")
    if job["schema"]:
        out = json.dumps(_json_from(out))  # a parse error here is the engine's error, not the queue's
    return out


def test(s: dict) -> dict:
    """Ask the engine in `s` for one word: {ok, reply, ms, error}."""
    t = time.time()
    try:
        reply = answer(dict(system="", prompt="Reply with the single word: ready", schema=None, max_tokens=20), s,
                       folder="test")  # the Test button runs while the background worker may run
        return dict(ok=bool(reply), reply=reply[:200], ms=round((time.time() - t) * 1000), error=None)
    except Exception as e:
        return dict(ok=False, reply="", ms=round((time.time() - t) * 1000), error=str(e)[:300])


def run_once() -> int:
    """Answer the newest waiting job, if the engine is on and no other Pensieve process runs one now. Returns how
    many it answered (0 or 1)."""
    if not settings.get("text_engine"):  # no background engine: the connected agent answers the queue over MCP
        return 0
    if not _lock.acquire(blocking=False):
        return 0
    try:
        with open(config.DATA_DIR / "engine.lock", "w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return 0  # another Pensieve process is answering one
            jobs = agent_jobs.pending(1, "", CONN)
            if not jobs:
                return 0
            state["busy"] = True
            try:
                agent_jobs.submit(jobs[0]["id"], answer(jobs[0]), CONN, settings.get("text_engine"))
            except Exception as e:
                state.update(error=f"{jobs[0]['kind']}: {e}"[:300])
                raise
            state.update(at=time.time(), error=None, answered=state["answered"] + 1)
            return 1
    finally:
        state["busy"] = False
        _lock.release()


def status() -> dict:
    s = settings.load()
    return dict(engine=s["text_engine"], waiting=sum(agent_jobs.counts().values()), busy=state["busy"],
                last_answer=state["at"] or None, answered=state["answered"], error=state["error"],
                builtin=dict(status=builtin["status"], detail=builtin["detail"], unsupported=builtin_unsupported(),
                             running=bool(builtin_pids()), local_copy=bool(_local_copy())),
                templates=[dict(t, installed=bool(config.find_exe(shlex.split(t["command"])[0]))) for t in TEMPLATES])
