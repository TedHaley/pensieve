"""User settings in ~/.pensieve/settings.json, editable at runtime from the visualizer, the Mac app, or MCP.

Command-line flags and env vars (--repos, --llm-url, ...) override the file for the current run only.
"""
import json
import os
import threading
from pathlib import Path

from . import config

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
    # Named slices of the index for agents: {"payments": {"sources": ["repos/<root>", "files/~/specs"], "description": ""}}
    "scopes": {},
    "default_scope": "",       # scope for the search panel and visualizer search ("" = everything)
    "hotkey": "ctrl+shift",    # read by the Mac app; tap Control+Shift. Or e.g. "cmd+shift+space"
    "editor": "default",       # how code results open: default | vscode | cursor | zed
    "appearance": "system",    # system | light | dark (the Mac app and the visualizer follow it)
    # What writes summaries, topic names and insights: auto (a local server if one is running; the UI asks) |
    # builtin (Qwen 3.5 9B via MLX) | server (llm_url/llm_model) | claude | codex | none
    "ai": "auto",
    "builtin_model": "mlx-community/Qwen3.5-9B-MLX-4bit",
    "agent_model": "haiku",    # model passed to `claude -p --model` when ai = claude
    "ai_on_battery": False,    # run background AI work (summaries, insights) on battery power
    "auto_update_check": True,  # the Mac app checks GitHub Releases daily and offers updates (never installs by itself)
    "llm_url": "http://localhost:1234/v1",
    "llm_model": "qwen/qwen3.5-9b",
}

DESCRIPTIONS = {
    "folders": "Folders whose documents are indexed for search (text, Markdown, code, PDF, Word).",
    "sweep_roots": "Where to look for git repos.",
    "sweep_depth": "How many folder levels below each sweep root to look for repos.",
    "max_repo_files": "Repos found by the sweep with more tracked files than this are skipped; add them to repos to index anyway.",
    "repos": "Extra git repos, or folders to search for repos. Always indexed, whatever their size.",
    "exclude": "Folder names never indexed or descended into.",
    "exclude_files": "File name patterns never indexed (secrets, keys, password exports). Case-insensitive globs.",
    "disabled": "Sources that are switched off (whole categories or single items). Easier to change from Sources.",
    "max_file_mb": "Skip documents larger than this.",
    "scopes": "Named slices of the index that agents work within (manage in Scopes).",
    "default_scope": "Scope used by the search panel and visualizer search; empty means everything.",
    "hotkey": "Shortcut for the search panel: 'ctrl+shift' (tap both) or a combo like 'cmd+shift+space'.",
    "editor": "Where code results open: default, vscode, cursor or zed.",
    "appearance": "System, light or dark. System follows macOS.",
    "ai": "What writes summaries and insights: builtin, server, claude, codex or none (auto = not chosen yet).",
    "builtin_model": "The MLX model the built-in engine runs.",
    "agent_model": "Model for the Claude Code engine (claude -p --model).",
    "ai_on_battery": "Keep writing summaries and insights in the background while on battery power.",
    "auto_update_check": "Check for new versions of Pensieve once a day. Updates are only installed when you choose.",
    "llm_url": "OpenAI-compatible endpoint for summaries and insights (LM Studio by default).",
    "llm_model": "Chat model used for summaries and insights.",
}

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
        if isinstance(d, bool):
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be true or false")
        elif isinstance(d, int):
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise ValueError(f"{k} must be a non-negative integer")
        elif isinstance(d, list):
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ValueError(f"{k} must be a list of strings")
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
        elif isinstance(d, dict):
            if not isinstance(v, dict) or not all(isinstance(x, bool) for x in v.values()):
                raise ValueError(f"{k} must map names to true/false")
            v = {**cur[k], **v}
        elif not isinstance(v, str):
            raise ValueError(f"{k} must be a string")
        if k == "appearance" and v not in ("system", "light", "dark"):
            raise ValueError("appearance must be system, light or dark")
        if k == "ai" and v not in ("auto", "builtin", "server", "claude", "codex", "none"):
            raise ValueError("ai must be auto, builtin, server, claude, codex or none")
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


def on_change(fn):
    _listeners.append(fn)


def enabled(source_id: str) -> bool:
    """A source is on unless it, or any category above it, is in `disabled`. 'files/~/Docs' -> checks 'files' too."""
    off = set(get("disabled"))
    cat = source_id.split("/", 1)[0]
    return source_id not in off and cat not in off


def set_enabled(source_id: str, on: bool) -> dict:
    off = [d for d in get("disabled") if d != source_id]
    if not on:
        off.append(source_id)
    return update({"disabled": off})


def paths(key) -> list[Path]:
    return [Path(p).expanduser() for p in get(key)]
