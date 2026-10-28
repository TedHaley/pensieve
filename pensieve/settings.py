"""User settings in ~/.pensieve/settings.json, editable at runtime from the visualizer, the Mac app, or MCP.

Command-line flags and env vars (--repos, PENSIEVE_REPOS) override the file for the current run only.
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
    "max_repo_files": 5000,    # found repos bigger than this, with no agent use, wait for the user to choose them
    "repos": [],               # extra repo roots, or folders searched sweep_depth levels deep
    "exclude": ["node_modules", ".venv", "venv", "__pycache__", "Library", ".Trash", "dist", "build", "target",
                ".cache", "Applications", "Pictures", "Music", "Movies"],
    "exclude_files": ["*emergency kit*", "*recovery*code*", "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx", "*.keychain*",
                      ".env*", "*credential*", "*secret*", "*password*", "id_rsa*", "id_ed25519*", "*.ovpn"],
    # Sources switched off, by id (see sources.py): a whole category ("agents", "files", "repos") or one item
    # ("agents/codex", "files/~/Downloads", "repos/discover", "repos/<repo root>").
    "disabled": [],
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
    "default_scope": "",       # scope for search in the app and the Map ("" = everything)
    "editor": "default",       # how code results open: default | vscode | cursor | zed
    "appearance": "system",    # system | light | dark (the Mac app and the visualizer follow it)
    "insights_auto": True,     # rewrite insights when they're out of date (at most every 6 hours)
    "search_gate_holdout": 5,  # the search gate (gate.py) leaves 1 session in this many unblocked, as a control; 0 = none
    # What writes the notes, summaries and insights (engine.py): builtin | cli | server. "" = not chosen: load() picks one
    "mode": "session",  # session: threads only; developer: also indexing, Map, People and Insights
    "ticket_repos": {},  # ticket project prefix (ABC) -> the repo its work starts in (a to-do's worktree)
    "text_engine": "",
    "cli_command": "",         # cli: a command-line agent, run once per job (templates: engine.TEMPLATES)
    "llm_url": "http://localhost:1234/v1",  # server: an OpenAI-compatible URL that the user runs (LM Studio here)
    "llm_model": "",
    "llm_key": "",             # server: an API key for a hosted server; sent only to llm_url, never shown back
    "engine_rate": 20,         # cli and server: at most this many jobs a minute
    "auto_update_check": True,  # the Mac app checks GitHub Releases daily and offers updates (never installs by itself)
    "setup_done": False,       # the user has chosen what to index; until then nothing is indexed (see /api/setup)
    # Connections (connections.py): MCP servers whose data Pensieve collects with read-only tools. Each one is off
    # until it is in `connections_on`. `mcp_servers` are the servers that Pensieve connects to itself:
    # {"name", "url", "headers": {}} or {"name", "command", "args": [], "env": {}}. Secret values stay in this file.
    # A URL server with "oauth": true signs in through the browser; its tokens are in connections/.oauth/.
    "connections_on": [],
    "mcp_servers": [],
    "connections_every_hours": 6,  # how often to collect from each connection that is on; 0 = only on request
    "default_agent": "",           # the harness that runs the sessions Pensieve starts (agents.known()); "" = automatic
    "custom_agent": {"name": "", "start": "", "resume": ""},  # default_agent "custom": the user's own commands
    "connections_days": 30,        # how far back the first collection goes
    "projects_stale_days": 21,     # Projects tab: a project with no activity for this many days goes under "Show older"
}

DESCRIPTIONS = {
    "folders": "Folders whose documents are indexed for search (text, Markdown, code, PDF, Word).",
    "sweep_roots": "Where to look for git repos.",
    "sweep_depth": "How many folder levels below each sweep root to look for repos.",
    "max_repo_files": "Repos that discovery finds with more tracked files than this, and that no agent has used, are listed for you to choose instead of indexed on their own. Repos your agents work in and repos you add are indexed at any size.",
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
    "ticket_repos": "Which repo a ticket project's work starts in, by its key prefix (ABC): a to-do's worktree is made there. Set the first time you start one.",
    "skip_temp_sessions": "Don't index agent sessions that ran in a temp folder (scripts, tests, and Pensieve's own AI calls through Claude Code or Codex).",
    "scopes": "Named slices of the index that agents work within (manage in Scopes).",
    "default_scope": "Scope used by search in the app and on the Map; empty means everything.",
    "editor": "Where code results open: default, vscode, cursor or zed.",
    "mode": "Session: only your agent sessions, as threads. Developer: also your repos and files, indexed, and the Map, People and Insights.",
    "appearance": "System, light or dark. System follows macOS.",
    "insights_auto": "Rewrite insights when the sessions or topics behind them have changed a lot (at most every 6 hours).",
    "search_gate_holdout": "The search gate (Claude Code add-on) leaves one session in this many unblocked, so you can compare Pensieve with grep. 0 blocks every session.",
    "text_engine": "What writes summaries and insights in the background: builtin (Qwen 3.5 9B on this Mac, which Pensieve starts when there is work), cli (a command-line agent, once per job) or server (a model server that you run, local or hosted).",
    "cli_command": "The command-line agent that writes them when text_engine is cli. It gets the prompt on stdin, or in place of {prompt}; {out} is a file it can write its answer to.",
    "llm_url": "The OpenAI-compatible URL of a model server that you run, when text_engine is server: LM Studio (http://localhost:1234/v1), Ollama (http://localhost:11434/v1) or a hosted API.",
    "llm_model": "The model on that server.",
    "llm_key": "An API key for a hosted server. Pensieve sends it only to llm_url and never shows it again.",
    "engine_rate": "At most this many jobs a minute for the command-line agent or the model server.",
    "auto_update_check": "Check for new versions of Pensieve once a day. Updates are only installed when you choose.",
    "setup_done": "Whether the sources to index were chosen. Pensieve indexes nothing until they are.",
    "connections_on": "Connections switched on by hand (easier to change in Sources). A connection that Pensieve holds data from is on too, unless it is in disabled.",
    "mcp_servers": "MCP servers that Pensieve connects to itself, with a URL and headers, or a command and environment.",
    "connections_every_hours": "How often Pensieve collects from each connection that is on (0 = only when you ask).",
    "default_agent": "The harness: the coding agent that runs the sessions Pensieve starts in Terminal (new, forked and resumed): claude, codex, qwen, gemini, cursor, or custom (custom_agent). A session another agent wrote is taken over: the harness reads its transcript first. Empty: automatic, so a session resumes in the agent that wrote it, and a new one starts in the thread's own agent, else the first one installed.",
    "custom_agent": "Your own harness, for default_agent custom: a name, the command that starts a session with {prompt} where the first prompt goes, and optionally the command that resumes one of its own sessions with {sid}.",
    "connections_days": "How many days back the first collection from a connection goes.",
    "projects_stale_days": "In Projects, a project with no activity for this many days goes under Show older.",
}

_lock = threading.Lock()
_data: dict | None = None
_listeners = []


def _overrides():
    o = {}
    if os.environ.get("PENSIEVE_REPOS"):
        o["repos"] = [p for p in os.environ["PENSIEVE_REPOS"].split(os.pathsep) if p.strip()]
    return o


ENGINES = ("builtin", "cli", "server")


def _default_engine(s: dict) -> dict:
    """The text engine for someone who has not chosen one: Claude Code with Haiku when Claude Code is installed, else
    the built-in model when this Mac can run it, else a model server at llm_url (LM Studio's address by default).
    Setup asks, and Settings → Models changes it."""
    from pensieve import engine
    if config.find_exe("claude"):
        return dict(text_engine="cli", cli_command=s["cli_command"] or engine.TEMPLATES[0]["command"])
    return dict(text_engine="server" if engine.builtin_unsupported() else "builtin")


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
            if _data["text_engine"] not in ENGINES:  # not chosen, "agent" before 0.14.1, or "" (the agent) before 0.18.1
                _data.update(_default_engine(_data))
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
        if k == "mcp_servers":
            v = _mcp_servers(v, cur[k])
        elif isinstance(d, bool):
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
                if any(not isinstance(x, str) or x.split("/", 1)[0] not in ("agents", "files", "repos", "connections") for x in sc["sources"]):
                    raise ValueError(f"scope {n!r}: sources are ids like repos/<root>, files/<folder>, agents/claude, connections/<server>")
        elif k == "custom_agent":
            if not isinstance(v, dict) or set(v) - set(d) or not all(isinstance(x, str) for x in v.values()):
                raise ValueError("custom_agent is {name, start, resume}, each a string")
            v = {**d, **{x: y.strip() for x, y in v.items()}}
            if v["start"] and "{prompt}" not in v["start"]:
                raise ValueError("custom_agent.start must have {prompt} where the first prompt goes, e.g. my-agent -i {prompt}")
            if v["resume"] and "{sid}" not in v["resume"]:
                raise ValueError("custom_agent.resume must have {sid} where the session id goes, e.g. my-agent --resume {sid}")
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
        if k == "mode" and v not in ("session", "developer"):
            raise ValueError("mode must be session or developer")
        if k == "text_engine" and v not in ENGINES:
            raise ValueError("text_engine must be builtin, cli or server")
        if k == "llm_url" and v and not v.startswith(("http://", "https://")):
            raise ValueError("llm_url must start with http:// or https://, e.g. http://localhost:1234/v1")
        if k == "engine_rate" and not 1 <= v <= 600:
            raise ValueError("engine_rate must be between 1 and 600 jobs a minute")
        if k == "editor" and v not in ("default", "vscode", "cursor", "zed"):
            raise ValueError("editor must be default, vscode, cursor or zed")
        if k == "default_agent":
            from pensieve import agents
            if v not in ("", agents.CUSTOM, *agents.AGENTS):
                raise ValueError(f"default_agent must be one of: {', '.join([*agents.AGENTS, agents.CUSTOM])}, or empty")
        clean[k] = v
    global _data
    with _lock:
        _data = {**(_data or DEFAULTS), **clean}
        PATH.write_text(json.dumps(_data, indent=2))
        os.chmod(PATH, 0o600)  # it can hold MCP server credentials
    for fn in list(_listeners):
        fn(clean)
    return load()


def public(s: dict) -> dict:
    """Settings as the visualizer and agents see them: MCP server headers and environment are sent by name only,
    with empty values, and the model server's API key only as whether one is saved."""
    mcp = [{**c, **{f: {n: "" for n in c[f]} for f in ("headers", "env") if f in c}} for c in s.get("mcp_servers", [])]
    return {**s, "mcp_servers": mcp, "llm_key": "", "llm_key_set": bool(s.get("llm_key"))}


def _mcp_servers(v, cur: list) -> list:
    """Validate `mcp_servers`. A header or env value sent back empty keeps the saved value (clients see no secrets)."""
    if not isinstance(v, list) or not all(isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"].strip()
                                          for c in v):
        raise ValueError("mcp_servers must be a list of {name, url, headers} or {name, command, args, env}")
    names = [c["name"].strip() for c in v]
    if len(set(names)) != len(names):
        raise ValueError("each MCP server needs its own name")
    saved = {c["name"]: c for c in cur}
    out = []
    for c in v:
        name = c["name"].strip()
        url, command = c.get("url") or "", c.get("command") or ""
        if bool(url) == bool(command):
            raise ValueError(f"MCP server {name!r}: give a url or a command, not both")
        if url and not url.startswith(("http://", "https://")):
            raise ValueError(f"MCP server {name!r}: the url must start with http:// or https://")
        args = c.get("args") or []
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ValueError(f"MCP server {name!r}: args must be a list of strings")
        f = "headers" if url else "env"
        vals = c.get(f) or {}
        if not isinstance(vals, dict) or not all(isinstance(a, str) and isinstance(b, str) for a, b in vals.items()):
            raise ValueError(f"MCP server {name!r}: {f} must map names to text values")
        old = saved.get(name, {}).get(f, {})
        x = {"name": name, "url": url} if url else {"name": name, "command": command, "args": args}
        x[f] = {a: b or old.get(a, "") for a, b in vals.items()}
        if url and c.get("oauth"):  # signs in with OAuth (mcp_oauth); the tokens are kept outside this file
            x["oauth"] = True
            if isinstance(c.get("scope"), str) and c["scope"]:
                x["scope"] = c["scope"]
        out.append(x)
    return out


def on_change(fn):
    _listeners.append(fn)


_HAS_DATA: dict[str, tuple[float, bool]] = {}


def _has_data(sid: str) -> bool:
    """True when Pensieve already pulled data from this connection: items it collected, or results that an agent got
    from the server in a session. The agent-sessions source itself only reads what agents pulled, so it counts too."""
    import time
    now = time.monotonic()
    hit = _HAS_DATA.get(sid)
    if hit and now - hit[0] < 30:
        return hit[1]
    d = config.DATA_DIR / "connections"
    has = sid == "agent_sessions"
    if not has:
        from pensieve import store
        from pensieve.store import sources
        has = sources.has_data(store.store_db(), sid)
    if not has:
        try:
            has = sid in json.loads((d / "state.json").read_text()).get("session_servers", [])
        except (OSError, ValueError):
            pass
    _HAS_DATA[sid] = (now, has)
    return has


def enabled(source_id: str) -> bool:
    """A source is on unless it, or any category above it, is in `disabled`. 'files/~/Docs' -> checks 'files' too.
    A connection is on once it is in `connections_on`, or once Pensieve holds data from it. Switching it off puts it
    in `disabled`, which wins over both. The same choice is in `sources.switched` of store.db (storage v2); the
    lists here stay for the Mac app and older versions."""
    off = set(get("disabled"))
    if source_id in off or source_id.split("/", 1)[0] in off:
        return False
    cat, _, sid = source_id.partition("/")
    if cat == "connections" and sid:
        return source_id in get("connections_on") or _has_data(sid)
    return True


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
    if source_id.startswith("connections/"):  # off is explicit, so data from it does not switch it back on
        from pensieve import store
        from pensieve.models import Switched
        db = store.store_db()
        db.write(lambda c: c.execute("UPDATE sources SET switched = ? WHERE id = ?",
                                     (Switched.ON.value if on else Switched.OFF.value, source_id.split("/", 1)[1])))
        _HAS_DATA.pop(source_id.split("/", 1)[1], None)
        cur = [d for d in get("connections_on") if d != source_id]
        off = [d for d in get("disabled") if d != source_id]
        return update({"connections_on": cur + [source_id] if on else cur, "disabled": off if on else off + [source_id]})
    off = [d for d in get("disabled") if d != source_id]
    if not on:
        off.append(source_id)
    return update({"disabled": off})


def paths(key) -> list[Path]:
    return [Path(p).expanduser() for p in get(key)]
