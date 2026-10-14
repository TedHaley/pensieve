"""User settings in ~/.pensieve/settings.json, editable at runtime from the visualizer, the Mac app, or MCP.

Command-line flags and env vars (--repos, --llm-url, ...) override the file for the current run only.
"""
import json
import os
import threading
from pathlib import Path

from pensieve import config

PATH = config.DATA_DIR / "settings.json"

DEFAULTS = {
    "folders": ["~/Documents", "~/Desktop", "~/Downloads"],  # indexed as files (repos inside them get the git treatment)
    "sweep_roots": ["~"],
    "sweep_depth": 4,
    "max_repo_files": 5000,    # swept repos bigger than this are skipped (list them in `repos` to include anyway)
    "repos": [],               # extra repo roots, or folders searched sweep_depth levels deep
    "exclude": ["node_modules", ".venv", "venv", "__pycache__", "Library", ".Trash", "dist", "build", "target",
                ".cache", "Applications", "Pictures", "Music", "Movies"],
    "exclude_files": ["*emergency kit*", "*recovery*code*", "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx", "*.keychain*",
                      ".env*", "*credential*", "*secret*", "*password*", "id_rsa*", "id_ed25519*", "*.ovpn"],
    # Sources switched off, by id (see sources.py): a whole category ("agents", "files", "repos", "apps") or one item
    # ("agents/codex", "files/~/Downloads", "repos/sweep", "repos/<repo root>", "apps/apple_notes").
    "disabled": ["apps/apple_notes"],
    "max_file_mb": 20,
    # Per file type in Files & folders (see files.TYPES): index = content | name | off, map = shown on the map.
    # "other" can't be set to content. The images setting covers images in git repos too.
    "file_types": {"text": {"index": "content", "map": True}, "pdf": {"index": "content", "map": True},
                   "office": {"index": "content", "map": True}, "data": {"index": "content", "map": True},
                   "code": {"index": "content", "map": True}, "images": {"index": "name", "map": False},
                   "video": {"index": "name", "map": False}, "audio": {"index": "name", "map": False},
                   "other": {"index": "name", "map": False}},
    "off_map": [],             # sources indexed for search but left off the map, by id like `disabled`
    "chunk_chars": 1500,       # characters per document chunk (one search hit, one embedding)
    "max_chunks": 2000,        # chunks read per document; 0 = no limit. Longer documents are cut there.
    "skip_temp_sessions": True,  # agent sessions that ran in a temp folder (e.g. Pensieve's own AI calls) aren't indexed
    "session_folders": [],     # more agent transcript folders, as "agent:path", e.g. "claude:~/work/.claude/projects"
    # Named slices of the index for agents: {"payments": {"sources": ["repos/<root>", "files/~/specs"], "description": ""}}
    "scopes": {},
    "default_scope": "",       # scope for the search panel and visualizer search ("" = everything)
    "hotkey": "ctrl+shift",    # read by the Mac app; tap Control+Shift. Or e.g. "cmd+shift+space"
    "editor": "default",       # how code results open: default | vscode | cursor | zed
    "appearance": "system",    # system | light | dark (the Mac app and the visualizer follow it)
    "embed_model": "",         # the embedding model to index with (config.EMBED_MODELS); "" = the index's own
    # What writes summaries, topic names and insights: auto (a local server if one is running; the UI asks) |
    # builtin (Qwen 3.5 9B via MLX) | server (llm_url/llm_model/llm_key: local or hosted) | cli (cli_command) | none
    "ai": "auto",
    "builtin_model": "mlx-community/Qwen3.5-9B-MLX-4bit",
    "cli_command": "",         # an AI command-line tool, run once per job (templates: CLI_TEMPLATES)
    "cli_privacy": "cloud",    # local | cloud: whether cli_command sends text off this Mac
    "agent_model": "haiku",    # model of the engine `ai: claude` from before cli; it moves into cli_command
    "ai_on_battery": False,    # run background AI work (summaries, insights) on battery power
    "insights_auto": True,     # rewrite insights when they're out of date (at most every 6 hours)
    "auto_update_check": True,  # the Mac app checks GitHub Releases daily and offers updates (never installs by itself)
    "setup_done": False,       # the user has chosen what to index; until then nothing is indexed (see /api/setup)
    "llm_url": "http://localhost:1234/v1",
    "llm_model": "qwen/qwen3.5-9b",
    "llm_key": "",             # API key for a hosted server; sent only to llm_url, never shown back in full
    # Saved model servers to pick from: [{"name", "url", "model", "key"}]. Using one copies it into llm_url/llm_model/llm_key.
    "llm_connections": [],
}

DESCRIPTIONS = {
    "folders": "Folders whose documents are indexed for search (text, Markdown, code, PDF, Word).",
    "sweep_roots": "Where to look for git repos.",
    "sweep_depth": "How many folder levels below each sweep root to look for repos.",
    "max_repo_files": "Repos found by discovery with more tracked files than this are skipped; switch one on, or add it in Sources, to index it anyway.",
    "repos": "Repos, or folders of repos, added in Sources. Always indexed, whatever their size.",
    "exclude": "Folder names never indexed or descended into.",
    "exclude_files": "File name patterns never indexed (secrets, keys, password exports). Case-insensitive globs.",
    "disabled": "Sources that are switched off (whole categories or single items). Easier to change from Sources.",
    "max_file_mb": "Skip documents larger than this.",
    "file_types": "For each file type: read its content, index only its name, or skip it; and whether it shows on the map.",
    "off_map": "Sources that are searchable but do not show on the map.",
    "chunk_chars": "Characters in each piece of a document. Larger pieces keep more context together in one result.",
    "max_chunks": "Pieces read from each document (0 = all). Text after this limit is not searchable.",
    "session_folders": "More folders of agent sessions, as agent:path (claude, codex or qwen). Easier to add from Sources.",
    "skip_temp_sessions": "Don't index agent sessions that ran in a temp folder (scripts, tests, and Pensieve's own AI calls through Claude Code or Codex).",
    "scopes": "Named slices of the index that agents work within (manage in Scopes).",
    "default_scope": "Scope used by the search panel and Map search; empty means everything.",
    "hotkey": "Shortcut for the search panel. ⌘Space turns off the Spotlight shortcut while Pensieve runs. "
              "Choose another shortcut to give ⌘Space back to Spotlight.",
    "editor": "Where code results open: default, vscode, cursor or zed.",
    "appearance": "System, light or dark. System follows macOS.",
    "embed_model": "The model that turns text into vectors for search. Switching embeds the text already indexed "
                   "again with the new model, in the background; search keeps working meanwhile.",
    "ai": "What writes summaries and insights: builtin, server, claude, codex or none (auto = not chosen yet).",
    "builtin_model": "The MLX model the built-in engine runs.",
    "agent_model": "Model of the old Claude Code engine; cli_command holds it now.",
    "cli_command": "The AI command-line tool that writes summaries and insights when ai is cli. It gets the prompt on "
                   "stdin, or in place of {prompt}; {out} is a file it can write its answer to. It must answer without "
                   "using tools.",
    "cli_privacy": "Whether cli_command sends text off this Mac: local or cloud.",
    "ai_on_battery": "Keep writing summaries and insights in the background while on battery power.",
    "insights_auto": "Rewrite insights when the sessions or topics behind them have changed a lot (at most every 6 hours).",
    "auto_update_check": "Check for new versions of Pensieve once a day. Updates are only installed when you choose.",
    "setup_done": "Whether the sources to index were chosen. Pensieve indexes nothing until they are.",
    "llm_url": "OpenAI-compatible endpoint, local (LM Studio, Ollama) or hosted, for summaries and insights.",
    "llm_model": "Chat model used for summaries and insights.",
    "llm_key": "API key for a hosted server (sent as a Bearer token to llm_url only). Empty for local servers.",
    "llm_connections": "Saved model servers, each with a name, URL, model and optional API key (manage in Settings → Models).",
}

_NO_TOOLS = "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit"
# Commands that answer one prompt without using tools. The prompt goes on stdin unless the command has {prompt}; {out}
# is a file the command writes its answer to.
CLI_TEMPLATES = [
    dict(id="claude", label="Claude Code", privacy="cloud",
         command=f"claude -p --model haiku --output-format text --max-turns 1 --disallowedTools {_NO_TOOLS}"),
    dict(id="codex", label="Codex", privacy="cloud",
         command="codex exec --skip-git-repo-check --sandbox read-only --output-last-message {out} -"),
    dict(id="qwen", label="Qwen Code", privacy="cloud", command="qwen --approval-mode plan --output-format text {prompt}"),
    dict(id="gemini", label="Gemini CLI", privacy="cloud", command="gemini --output-format text --prompt {prompt}"),
    dict(id="ollama", label="Ollama", privacy="local", command="ollama run llama3.2"),
]


def cli_template(id, model=None):
    t = dict(next(x for x in CLI_TEMPLATES if x["id"] == id))
    if model and id == "claude":
        t["command"] = t["command"].replace("--model haiku", f"--model {model}")
    return t


_lock = threading.Lock()
_data: dict | None = None
_listeners = []


def _overrides():
    o = {}
    if os.environ.get("PENSIEVE_REPOS"):
        o["repos"] = [p for p in os.environ["PENSIEVE_REPOS"].split(os.pathsep) if p.strip()]
    if os.environ.get("PENSIEVE_LLM_URL"):
        o["llm_url"] = os.environ["PENSIEVE_LLM_URL"]
    if os.environ.get("PENSIEVE_LLM_MODEL"):
        o["llm_model"] = os.environ["PENSIEVE_LLM_MODEL"]
    return o


def load() -> dict:
    global _data
    with _lock:
        if _data is None:
            saved = {}
            if PATH.exists():
                try:
                    saved = json.loads(PATH.read_text())
                except (OSError, json.JSONDecodeError):
                    pass
            _data = {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}
            if saved and "setup_done" not in saved:  # set up by a version from before the setup step
                _data["setup_done"] = True
            if _data["ai"] in ("claude", "codex"):  # the engines before cli: the same CLI through its template
                t = cli_template(_data["ai"], _data["agent_model"])
                _data.update(ai="cli", cli_command=t["command"], cli_privacy=t["privacy"])
            off = _data["disabled"]  # the two repo-discovery rules became one
            if "repos/sessions" in off or "repos/sweep" in off:
                _data["disabled"] = [d for d in off if d not in ("repos/sessions", "repos/sweep")] + \
                    (["repos/discover"] if "repos/sessions" in off and "repos/sweep" in off else [])
        return {**_data, **_overrides()}


def get(key):
    return load()[key]


def update(changes: dict) -> dict:
    """Validate and persist a partial update; returns the new settings. Raises ValueError on bad input."""
    cur = load()
    clean = {}
    for k, v in changes.items():
        if k not in DEFAULTS:
            raise ValueError(f"unknown setting {k!r}; valid: {', '.join(DEFAULTS)}")
        d = DEFAULTS[k]
        if k == "llm_connections":
            if not isinstance(v, list) or not all(isinstance(c, dict) and c.get("name") and all(
                    isinstance(c.get(f, ""), str) for f in ("name", "url", "model", "key")) for c in v):
                raise ValueError("llm_connections must be a list of {name, url, model, key}")
            if any(not c.get("url", "").startswith(("http://", "https://")) for c in v):
                raise ValueError("each connection URL must start with http:// or https://")
            keys = {c["name"]: c.get("key", "") for c in cur[k]}  # clients only ever see key_set
            v = [{"name": c["name"], "url": c["url"], "model": c.get("model", ""),
                  "key": c["key"] if "key" in c else keys.get(c["name"], "")} for c in v]
        elif isinstance(d, bool):
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be true or false")
        elif isinstance(d, int):
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise ValueError(f"{k} must be a non-negative integer")
        elif isinstance(d, list):
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ValueError(f"{k} must be a list of strings")
        elif k == "embed_model":
            from pensieve import config
            if v and v not in config.EMBED_MODELS:
                raise ValueError(f"embed_model must be one of: {', '.join(config.EMBED_MODELS)}")
        elif k == "scopes":
            if not isinstance(v, dict):
                raise ValueError("scopes must map names to {sources: [...], description: ''}")
            for n, sc in v.items():
                if not n or n in ("all", "auto") or "/" in n:
                    raise ValueError(f"invalid scope name {n!r} (reserved or contains '/')")
                if not isinstance(sc, dict) or not isinstance(sc.get("sources"), list) or not sc["sources"]:
                    raise ValueError(f"scope {n!r} needs a non-empty sources list")
                if any(not isinstance(x, str) or x.split("/", 1)[0] not in ("agents", "files", "repos", "apps") for x in sc["sources"]):
                    raise ValueError(f"scope {n!r}: sources are ids like repos/<root>, files/<folder>, agents/claude, apps/obsidian")
        elif k == "file_types":
            if not isinstance(v, dict):
                raise ValueError("file_types maps a type to {index: content|name|off, map: true|false}")
            merged = {t: dict(x) for t, x in cur[k].items()}
            for t, x in v.items():
                if t not in d or not isinstance(x, dict):
                    raise ValueError(f"unknown file type {t!r}; valid: {', '.join(d)}")
                if x.get("index", "content") not in ("content", "name", "off") or not isinstance(x.get("map", True), bool):
                    raise ValueError(f"file_types.{t}: index is content, name or off; map is true or false")
                merged[t] = {**merged.get(t, d[t]), **x}
                if t == "other" and merged[t]["index"] == "content":
                    raise ValueError(f"{t} files have no text to read: index them by name or switch them off")
            v = merged
        elif isinstance(d, dict):
            if not isinstance(v, dict) or not all(isinstance(x, bool) for x in v.values()):
                raise ValueError(f"{k} must map names to true/false")
            v = {**cur[k], **v}
        elif not isinstance(v, str):
            raise ValueError(f"{k} must be a string")
        if k == "chunk_chars" and not 200 <= v <= 20000:
            raise ValueError("chunk_chars must be between 200 and 20000")
        if k == "appearance" and v not in ("system", "light", "dark"):
            raise ValueError("appearance must be system, light or dark")
        if k == "ai" and v in ("claude", "codex"):  # the engines before cli: the same CLI through its template
            t = cli_template(v, cur["agent_model"])
            clean.update(cli_command=t["command"], cli_privacy=t["privacy"])
            v = "cli"
        if k == "ai" and v not in ("auto", "builtin", "server", "cli", "none"):
            raise ValueError("ai must be auto, builtin, server, cli or none")
        if k == "cli_privacy" and v not in ("local", "cloud"):
            raise ValueError("cli_privacy must be local or cloud")
        if k == "llm_url" and not v.startswith(("http://", "https://")):
            raise ValueError("llm_url must start with http:// or https://, e.g. http://localhost:1234/v1")
        if k == "editor" and v not in ("default", "vscode", "cursor", "zed"):
            raise ValueError("editor must be default, vscode, cursor or zed")
        clean[k] = v
    global _data
    with _lock:
        _data = {**(_data or DEFAULTS), **clean}
        PATH.write_text(json.dumps(_data, indent=2))
    for fn in list(_listeners):
        fn(clean)
    return load()


def public(s: dict) -> dict:
    """Settings as the visualizer and agents see them: the API key is never sent back, only whether one is saved."""
    conns = [{**{k: v for k, v in c.items() if k != "key"}, "key_set": bool(c.get("key"))} for c in s.get("llm_connections", [])]
    return {**s, "llm_key": "", "llm_key_set": bool(s.get("llm_key")), "llm_connections": conns}


def on_change(fn):
    _listeners.append(fn)


def enabled(source_id: str) -> bool:
    """A source is on unless it, or any category above it, is in `disabled`. 'files/~/Docs' -> checks 'files' too."""
    off = set(get("disabled"))
    cat = source_id.split("/", 1)[0]
    return source_id not in off and cat not in off


def on_map(source_id: str) -> bool:
    """Shown on the map unless it, or its category, is in `off_map` (switched-off sources aren't indexed at all)."""
    off = set(get("off_map"))
    return source_id not in off and source_id.split("/", 1)[0] not in off


def set_on_map(source_id: str, on: bool) -> dict:
    off = [d for d in get("off_map") if d != source_id]
    if not on:
        off.append(source_id)
    return update({"off_map": off})


def file_type(t: str) -> dict:
    mine = get("file_types")
    if t in ("images", "video", "audio") and t not in mine and "media" in mine:  # before 0.7.1: one "media" type
        return {**DEFAULTS["file_types"][t], **mine["media"]}
    return {**DEFAULTS["file_types"].get(t, {"index": "name", "map": False}), **mine.get(t, {})}


def set_enabled(source_id: str, on: bool) -> dict:
    off = [d for d in get("disabled") if d != source_id]
    if not on:
        off.append(source_id)
    return update({"disabled": off})


def paths(key) -> list[Path]:
    return [Path(p).expanduser() for p in get(key)]
