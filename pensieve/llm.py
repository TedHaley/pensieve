"""Where summaries, topic names and insights get written. One setting, `ai`, picks the engine:

    builtin  Qwen 3.5 9B run by Pensieve itself with MLX (Apple Silicon; ~5 GB download on first use)
    server   any OpenAI-compatible server (LM Studio by default) at llm_url / llm_model
    claude   the Claude Code CLI (`claude -p`): uses your Claude account; text leaves this Mac
    codex    the Codex CLI (`codex exec`): uses your OpenAI account; text leaves this Mac
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

from . import config, settings


class Offline(Exception):
    """The chosen engine isn't available right now (not running, not chosen, still downloading)."""


BUILTIN_PORT = 8766
_builtin = {"proc": None, "status": "off", "detail": "", "lock": threading.Lock()}


def _apple_silicon():
    return sys.platform == "darwin" and platform.machine() == "arm64"


def _server_up(url, timeout=1.5):
    try:
        return httpx.get(f"{url}/models", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


def options():
    """The engines a user can pick, with whether each is usable on this Mac."""
    s = settings.load()
    have_mlx = _apple_silicon()
    if have_mlx:
        try:
            import mlx_lm  # noqa: F401
        except ImportError:
            have_mlx = False
    return [
        dict(id="builtin", label="Built-in: Qwen 3.5 9B", privacy="local", available=have_mlx,
             description="Pensieve runs the model itself. Nothing leaves this Mac.",
             note="Downloads about 5 GB the first time; needs Apple Silicon and 16 GB of memory." if have_mlx
             else "Needs an Apple Silicon Mac."),
        dict(id="server", label="LM Studio or another local server", privacy="local", available=_server_up(s["llm_url"]),
             description=f"An OpenAI-compatible server at {s['llm_url']} running {s['llm_model']}.",
             note="Running now." if _server_up(s["llm_url"]) else "Not running: start LM Studio's server (port 1234)."),
        dict(id="claude", label="Claude Code", privacy="cloud", available=bool(shutil.which("claude") or _find("claude")),
             description="Uses your Claude Code login (claude -p). Session and code text is sent to Anthropic.",
             note="Installed." if (shutil.which("claude") or _find("claude")) else "Claude Code isn't installed."),
        dict(id="codex", label="Codex", privacy="cloud", available=bool(shutil.which("codex") or _find("codex")),
             description="Uses your Codex login (codex exec). Session and code text is sent to OpenAI.",
             note="Installed." if (shutil.which("codex") or _find("codex")) else "Codex isn't installed."),
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
        st, detail = ("ready", f"{settings.get('llm_model')} at {settings.get('llm_url')}") if ok else ("offline", f"No server at {settings.get('llm_url')}")
    elif p == "builtin":
        st, detail = _builtin["status"], _builtin["detail"] or "Starts on first use."
        if st == "off":
            st = "starting" if _builtin["proc"] else "ready"
    else:
        exe = shutil.which(p) or _find(p)
        st, detail = ("ready", exe) if exe else ("error", f"{p} isn't installed")
    return dict(setting=a, provider=p, configured=a != "auto", status=st, detail=detail, options=options())


# ---- builtin: an mlx_lm server we start and stop ------------------------------
def _local_copy(model: str):
    """An MLX copy LM Studio already downloaded (same repo name), so we don't fetch 5 GB again."""
    import glob
    for d in glob.glob(os.path.expanduser(f"~/.lmstudio/models/*/{model.split('/')[-1]}")):
        if os.path.exists(os.path.join(d, "config.json")):
            return d
    return None

def _ensure_builtin():
    url = f"http://127.0.0.1:{BUILTIN_PORT}/v1"
    if _server_up(url):
        _builtin.update(status="ready", detail=settings.get("builtin_model"))
        return url
    with _builtin["lock"]:
        p = _builtin["proc"]
        if p is None or p.poll() is not None:
            model = settings.get("builtin_model")
            from huggingface_hub import try_to_load_from_cache
            local = _local_copy(model)
            cached = bool(local) or isinstance(try_to_load_from_cache(model, "config.json"), str)
            _builtin.update(status="starting" if cached else "downloading",
                            detail=f"Loading {model}…" if cached else f"Downloading {model} (about 5 GB)…")
            log = open(config.DATA_DIR / "builtin-llm.log", "a")
            env = {**os.environ}
            env.pop("HF_HUB_OFFLINE", None)  # the chat model may not be downloaded yet
            if not cached:  # fetch first, so the server starts with the weights on disk
                from huggingface_hub import snapshot_download
                try:
                    snapshot_download(model)
                except Exception as e:
                    _builtin.update(status="error", detail=f"Download failed: {e!r}"[:300])
                    raise Offline(_builtin["detail"])
                _builtin.update(status="starting", detail=f"Loading {model}…")
            _builtin["proc"] = subprocess.Popen(
                [sys.executable, "-m", "mlx_lm", "server", "--model", local or model, "--host", "127.0.0.1", "--port", str(BUILTIN_PORT)],
                stdout=log, stderr=log, env=env, start_new_session=True)
    for _ in range(240):
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
    _builtin.update(proc=None, status="off", detail="")


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


def _openai(url, model, msgs, max_tokens, temperature, schema, structured=True):
    body = {"model": model, "messages": msgs, "max_tokens": max_tokens, "temperature": temperature,
            "reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False}}
    if schema and structured:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "strict": True, "schema": schema}}
    try:
        r = httpx.post(f"{url}/chat/completions", timeout=600, json=body)
    except httpx.ConnectError as e:
        raise Offline(f"No model server at {url}") from e
    r.raise_for_status()
    return _strip_think(r.json()["choices"][0]["message"].get("content") or "")


def _cli(argv_fn, prompt, system, timeout=600):
    """Run an agent CLI once, non-interactively, with no tools, and return its final text."""
    full = (system + "\n\n" if system else "") + prompt
    with tempfile.TemporaryDirectory() as tmp:  # an empty working dir: the agent has nothing to read or edit
        argv, out_file = argv_fn(full, tmp)
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=tmp, stdin=subprocess.DEVNULL)
        if r.returncode != 0:
            raise RuntimeError(f"{argv[0]} failed: {(r.stderr or r.stdout).strip()[:300]}")
        if out_file and os.path.exists(out_file):
            return open(out_file).read().strip()
        return r.stdout.strip()


def _claude_argv(text, tmp):
    exe = shutil.which("claude") or _find("claude")
    if not exe:
        raise Offline("Claude Code isn't installed")
    return [exe, "-p", text, "--model", settings.get("agent_model") or "haiku", "--output-format", "text",
            "--disallowedTools", "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit", "--max-turns", "1"], None


def _codex_argv(text, tmp):
    exe = shutil.which("codex") or _find("codex")
    if not exe:
        raise Offline("Codex isn't installed")
    out = os.path.join(tmp, "last.txt")
    return [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--output-last-message", out, text], out


def complete(prompt: str, system: str = "", max_tokens: int = 200, temperature: float = 0.2, schema: dict | None = None):
    """One-shot completion with the chosen engine. With `schema`, returns the parsed JSON object.
    Raises Offline when no engine is available, so callers can skip AI work quietly."""
    p = provider()
    if p == "none":
        raise Offline("AI is off")
    if p in ("claude", "codex"):
        text = _cli(_claude_argv if p == "claude" else _codex_argv, prompt + (_schema_hint(schema) if schema else ""), system)
        return _json_from(text) if schema else _strip_think(text)
    msgs = ([{"role": "system", "content": system}] if system else [])
    if p == "builtin":
        url, model, structured = _ensure_builtin(), settings.get("builtin_model"), False
    else:
        url, model, structured = settings.get("llm_url"), settings.get("llm_model"), True
    content = prompt + (_schema_hint(schema) if schema and not structured else "")
    t = _openai(url, model, msgs + [{"role": "user", "content": content}], max_tokens, temperature, schema, structured)
    return _json_from(t) if schema else t
