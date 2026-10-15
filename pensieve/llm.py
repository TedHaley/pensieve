"""Where summaries, topic names and insights get written. One setting, `ai`, picks the engine:

    builtin  Qwen 3.5 9B run by Pensieve itself with MLX (Apple Silicon; ~5 GB download on first use)
    server   any OpenAI-compatible server, local (LM Studio, Ollama…) or hosted, at llm_url / llm_model (+ llm_key)
    cli      an AI command-line tool (cli_command, e.g. `claude -p …`, `codex exec …`, `ollama run …`), run once per
             job; text leaves this Mac unless the tool runs locally (cli_privacy)
    none     no AI: maps, search, activity, keyword topics and team commits still work
    auto     not chosen yet: use `server` if one is running, otherwise nothing (and the UI asks)
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import httpx

from pensieve import config, settings


OWN_PREFIX = "pensieve-ai-"


class Offline(Exception):
    """The chosen engine isn't available right now (not running, not chosen, still downloading)."""


BUILTIN_PORT = 8766
IDLE_SECONDS = 10 * 60   # unload the built-in model after this long unused (it holds ~6 GB of memory)
MIN_RAM_GB = 16
_builtin = {"proc": None, "status": "off", "detail": "", "lock": threading.Lock(), "last_used": 0.0,
            "download": None, "progress": None}


def ram_gb():
    try:
        return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout) / 2**30
    except (ValueError, OSError):
        return 0


def on_battery() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        return "Battery Power" in subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def paused() -> str | None:
    """Why background AI work should wait right now, or None."""
    if not settings.get("ai_on_battery") and on_battery():
        return "Paused on battery power"
    return None


def _apple_silicon():
    return sys.platform == "darwin" and platform.machine() == "arm64"


def _auth(key=None):
    key = settings.get("llm_key") if key is None else key
    return {"Authorization": f"Bearer {key}"} if key else {}


def _server_up(url, timeout=1.5, key=None):
    try:
        return httpx.get(f"{url.rstrip('/')}/models", timeout=timeout, headers=_auth(key)).status_code == 200
    except httpx.HTTPError:
        return False


def is_local(url) -> bool:
    from urllib.parse import urlparse
    return (urlparse(url).hostname or "") in ("localhost", "127.0.0.1", "::1") or (urlparse(url).hostname or "").endswith(".local")


def server_models(url, key=None, timeout=4):
    """Check an OpenAI-compatible server and list its models: {ok, models, error}."""
    try:
        r = httpx.get(f"{url.rstrip('/')}/models", timeout=timeout, headers=_auth(key))
    except httpx.HTTPError as e:
        return dict(ok=False, models=[], error=f"Can't reach {url} ({type(e).__name__})")
    if r.status_code in (401, 403):
        return dict(ok=False, models=[], error="The server needs a valid API key")
    if r.status_code != 200:
        return dict(ok=False, models=[], error=f"{url}/models answered {r.status_code}")
    try:
        ids = [m["id"] for m in r.json().get("data", []) if isinstance(m, dict) and m.get("id")]
    except (ValueError, AttributeError):
        return dict(ok=False, models=[], error="That isn't an OpenAI-compatible server (no model list)")
    return dict(ok=True, models=sorted(ids), error=None)


def options():
    """The engines a user can pick, with whether each is usable on this Mac."""
    s = settings.load()
    have_mlx = _apple_silicon() and ram_gb() >= MIN_RAM_GB
    if have_mlx:
        try:
            import mlx_lm  # noqa: F401
        except ImportError:
            have_mlx = False
    local = _local_copy(settings.get("builtin_model"))
    return [
        dict(id="builtin", label="Built-in: Qwen 3.5 9B", privacy="local", available=have_mlx,
             description="Pensieve runs the model itself, in the background at low priority. Nothing leaves this Mac.",
             note=("Uses the copy LM Studio already downloaded." if local else
                   "Downloads about 6 GB in the background; you can keep working.") if have_mlx
             else f"Needs an Apple Silicon Mac with {MIN_RAM_GB} GB of memory."),
        dict(id="server", label="Model server", privacy="local" if is_local(s["llm_url"]) else "cloud", available=True,
             url=s["llm_url"], model=s["llm_model"], key_set=bool(s["llm_key"]), up=(up := _server_up(s["llm_url"])),
             connections=[dict(name=c["name"], url=c["url"], model=c["model"], key_set=bool(c["key"]), local=is_local(c["url"]))
                          for c in s["llm_connections"]],
             description="Any model with an OpenAI-compatible API at an address: on this Mac (LM Studio, Ollama), on "
                         "another computer, or a cloud provider. "
                         + ("Nothing leaves this Mac." if is_local(s["llm_url"]) else "Session and code text is sent to that server."),
             note=f"Connected to {s['llm_url']}." if up else f"Can't reach {s['llm_url']} right now."),
        dict(id="cli", label="Command line", privacy=s["cli_privacy"], available=True, command=s["cli_command"],
             templates=[dict(t, installed=bool(_exe(t["command"]))) for t in settings.CLI_TEMPLATES],
             description="An AI command-line tool you already use, such as Claude Code, Codex, Qwen Code or Ollama. "
                         "Pensieve runs the command once per job, in an empty folder, and reads its answer.",
             note=(f"Runs {_exe(s['cli_command'])}." if _exe(s["cli_command"]) else
                   f"Can't find {_argv(s['cli_command'])[0]}.") if s["cli_command"].strip() else "Pick a template or type a command."),
        dict(id="none", label="Off", privacy="local", available=True,
             description="No AI. Maps, search, activity, keyword topics and team commits still work.", note=""),
    ]


def _find(name):
    for d in (os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.npm-global/bin")):
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    return None


def provider() -> str:
    """The engine actually in use ('auto' resolved)."""
    a = settings.get("ai")
    if a == "auto":
        return "server" if _server_up(settings.get("llm_url")) else "none"
    return a


def status() -> dict:
    a = settings.get("ai")
    p = provider()
    if p == "none":
        st, detail = "off", ("Not set up yet." if a == "auto" else "AI is off.")
    elif p == "server":
        ok = _server_up(settings.get("llm_url"))
        st, detail = ("ready", f"{settings.get('llm_model')} at {settings.get('llm_url')}") if ok else ("offline", f"Can't reach {settings.get('llm_url')}")
    elif p == "builtin":
        st, detail = _builtin["status"], _builtin["detail"]
        if st == "off":
            st, detail = ("ready", "Loads when there's work, and unloads after 10 minutes idle.") if _have_weights() \
                else ("downloading", "Waiting to download…")
    else:
        cmd = settings.get("cli_command")
        st, detail = ("ready", cmd) if _exe(cmd) else ("error", f"Can't find {_argv(cmd)[0]}" if cmd.strip() else "No command set")
    why = paused() if p != "none" else None
    return dict(setting=a, provider=p, configured=a != "auto", status=st, detail=detail, options=options(),
                enabled=p != "none", paused=why, progress=_builtin["progress"] if p == "builtin" else None,
                on_battery_setting=settings.get("ai_on_battery"))


# ---- builtin: an mlx_lm server we start and stop ------------------------------
def _local_copy(model: str):
    """An MLX copy LM Studio already downloaded (same repo name), so we don't fetch 5 GB again."""
    import glob
    for d in glob.glob(os.path.expanduser(f"~/.lmstudio/models/*/{model.split('/')[-1]}")):
        if os.path.exists(os.path.join(d, "config.json")):
            return d
    return None

def _have_weights():
    model = settings.get("builtin_model")
    if _local_copy(model):
        return True
    from huggingface_hub import try_to_load_from_cache
    return isinstance(try_to_load_from_cache(model, "config.json"), str) and \
        isinstance(try_to_load_from_cache(model, "model.safetensors.index.json"), str)


def start_download():
    """Fetch the built-in model's weights on a background thread, reporting progress. Safe to call repeatedly."""
    t = _builtin["download"]
    if (t and t.is_alive()) or _have_weights():
        return
    model = settings.get("builtin_model")

    def run():
        from huggingface_hub import HfApi, snapshot_download
        from huggingface_hub.constants import HF_HUB_CACHE
        os.environ.pop("HF_HUB_OFFLINE", None)
        try:
            info = HfApi().model_info(model, files_metadata=True)
            total = sum(x.size or 0 for x in info.siblings) or 1
            folder = os.path.join(HF_HUB_CACHE, "models--" + model.replace("/", "--"))
            done = threading.Event()

            def watch():  # progress = bytes on disk under the model's cache folder (blobs + in-flight .incomplete)
                while not done.wait(2):
                    n = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(folder) for f in fs
                            if not os.path.islink(os.path.join(d, f)))
                    _builtin.update(progress=min(0.99, n / total),
                                    detail=f"Downloading Qwen 3.5 9B in the background: {n / 1e9:.1f} of {total / 1e9:.1f} GB")
            threading.Thread(target=watch, daemon=True).start()
            _builtin.update(status="downloading", progress=0.0, detail="Starting the download…")
            snapshot_download(model)
            done.set()
            _builtin.update(status="off", progress=1.0, detail="")
        except Exception as e:
            _builtin.update(status="error", detail=f"Download failed: {e!r}"[:300])
    _builtin["download"] = threading.Thread(target=run, daemon=True, name="builtin-model-download")
    _builtin["download"].start()


def sweep():
    """Unload the built-in model when it hasn't been used for IDLE_SECONDS (frees ~6 GB)."""
    p = _builtin["proc"]
    if p and p.poll() is None and time.time() - _builtin["last_used"] > IDLE_SECONDS:
        stop_builtin()


def _ensure_builtin():
    url = f"http://127.0.0.1:{BUILTIN_PORT}/v1"
    _builtin["last_used"] = time.time()
    if _server_up(url):
        _builtin.update(status="ready", detail=settings.get("builtin_model"))
        return url
    if not _have_weights():  # never block on a 6 GB download: fetch it in the background and try again later
        start_download()
        raise Offline(_builtin["detail"] or "Downloading the built-in model")
    with _builtin["lock"]:
        p = _builtin["proc"]
        if p is None or p.poll() is not None:
            model = settings.get("builtin_model")
            from huggingface_hub import try_to_load_from_cache
            local = _local_copy(model)
            _builtin.update(status="starting", detail="Loading Qwen 3.5 9B…")
            log = open(config.DATA_DIR / "builtin-llm.log", "a")
            argv = [sys.executable, "-m", "mlx_lm", "server", "--model", local or model, "--host", "127.0.0.1",
                    "--port", str(BUILTIN_PORT)]
            if os.path.exists("/usr/sbin/taskpolicy"):  # background QoS: yields to whatever you're doing
                argv = ["/usr/sbin/taskpolicy", "-b"] + argv
            _builtin["proc"] = subprocess.Popen(argv, stdout=log, stderr=log, start_new_session=True)
    for _ in range(600):  # loading at background priority can take a few minutes on a busy Mac
        if _server_up(url):
            _builtin.update(status="ready", detail=settings.get("builtin_model"))
            return url
        if _builtin["proc"].poll() is not None:
            _builtin.update(status="error", detail="The built-in model stopped; see ~/.pensieve/builtin-llm.log")
            raise Offline(_builtin["detail"])
        time.sleep(1)
    raise Offline("The built-in model is still loading")


def stop_builtin():
    p = _builtin["proc"]
    if p and p.poll() is None:
        p.terminate()
    _builtin.update(proc=None, status="off" if _builtin["status"] != "downloading" else "downloading",
                    detail="" if _builtin["status"] != "downloading" else _builtin["detail"])


# ---- completion ---------------------------------------------------------------
def _json_from(t: str):
    return json.loads(t[t.find("{"):t.rfind("}") + 1])


def _schema_hint(schema):
    return ("\n\nReply with only a JSON object (no prose, no code fences) that matches this JSON schema:\n"
            + json.dumps(schema))


def _strip_think(t):
    if "</think>" in t:
        t = t.split("</think>", 1)[1]
    return t.strip()


def _openai(url, model, msgs, max_tokens, temperature, schema, structured=True, headers=None):
    body = {"model": model, "messages": msgs, "max_tokens": max_tokens, "temperature": temperature,
            "reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False}}
    if schema and structured:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "strict": True, "schema": schema}}
    try:
        r = httpx.post(f"{url.rstrip('/')}/chat/completions", timeout=600, json=body, headers=headers or {})
    except httpx.ConnectError as e:
        raise Offline(f"No model server at {url}") from e
    r.raise_for_status()
    return _strip_think(r.json()["choices"][0]["message"].get("content") or "")


def _argv(command):
    import shlex
    try:
        return shlex.split(command) or [""]
    except ValueError:
        return [command]


def _exe(command):
    """The command's program on this Mac (the app's PATH misses ~/.local/bin and the like), or None."""
    name = _argv(command)[0]
    return (shutil.which(name) or _find(name)) if name else None


def run_command(command, prompt, system="", timeout=600):
    """Run an AI command-line tool once and return its answer: the prompt goes on stdin, or in place of {prompt};
    {out} is a file it can write the answer to instead of stdout."""
    full = (system + "\n\n" if system else "") + prompt
    exe = _exe(command)
    if not exe:
        raise Offline(f"Can't find {_argv(command)[0]}" if command.strip() else "No command set")
    # an empty working dir: the agent has nothing to read or edit; the prefix lets the indexer skip the session the
    # CLI records for this call (it isn't the user's work)
    with tempfile.TemporaryDirectory(prefix=OWN_PREFIX) as tmp:
        out_file = os.path.join(tmp, "answer.txt")
        argv = [exe] + [full if a == "{prompt}" else out_file if a == "{out}" else a for a in _argv(command)[1:]]
        stdin = None if "{prompt}" in _argv(command) else full
        r = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, cwd=tmp,
                           stdin=subprocess.DEVNULL if stdin is None else None)
        if r.returncode != 0:
            raise RuntimeError(f"{_argv(command)[0]} failed: {(r.stderr or r.stdout).strip()[:300]}")
        if "{out}" in _argv(command) and os.path.exists(out_file):
            return open(out_file).read().strip()
        return r.stdout.strip()


def test_command(command):
    """Send a one-word prompt through `command`: {ok, reply, ms, error}."""
    t = time.time()
    try:
        reply = run_command(command, "Reply with the single word: ready", timeout=120)
        return dict(ok=bool(reply), reply=reply[:300], ms=round((time.time() - t) * 1000), error=None if reply else "No answer")
    except (Offline, RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        return dict(ok=False, reply="", ms=round((time.time() - t) * 1000), error=str(e)[:300] or type(e).__name__)


_running = {"n": 0, "lock": threading.Lock()}  # completions in progress, for the search timing log


def busy() -> bool:
    """A completion is running now (it can slow a search down: same CPU, GPU and memory)."""
    return _running["n"] > 0


def complete(prompt: str, system: str = "", max_tokens: int = 200, temperature: float = 0.2, schema: dict | None = None):
    """One-shot completion with the chosen engine. With `schema`, returns the parsed JSON object.
    Raises Offline when no engine is available, so callers can skip AI work quietly."""
    with _running["lock"]:
        _running["n"] += 1
    try:
        return _complete(prompt, system, max_tokens, temperature, schema)
    finally:
        with _running["lock"]:
            _running["n"] -= 1


def _complete(prompt, system, max_tokens, temperature, schema):
    p = provider()
    if p == "none":
        raise Offline("AI is off")
    if p == "cli":
        text = run_command(settings.get("cli_command"), prompt + (_schema_hint(schema) if schema else ""), system)
        return _json_from(text) if schema else _strip_think(text)
    msgs = ([{"role": "system", "content": system}] if system else [])
    if p == "builtin":
        url, model, structured = _ensure_builtin(), settings.get("builtin_model"), False
    else:
        url, model, structured = settings.get("llm_url"), settings.get("llm_model"), True
    content = prompt + (_schema_hint(schema) if schema and not structured else "")
    t = _openai(url, model, msgs + [{"role": "user", "content": content}], max_tokens, temperature, schema, structured,
                headers=_auth() if p == "server" else None)
    return _json_from(t) if schema else t
