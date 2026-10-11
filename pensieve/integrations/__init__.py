"""Optional add-ons that help agents get the most out of Pensieve: Pensieve as an MCP server for Claude Code, an
instructions block for an agent's global memory file (CLAUDE.md, AGENTS.md), Claude Code skills, and a Claude Code
hook. Nothing is installed unless the user turns it on.

The instructions block is kept between markers so it can be updated or removed without touching the rest of the file;
skills are copied into ~/.claude/skills/<name>/ with a marker file so only Pensieve's own copies are ever removed.
The hooks are the entries in ~/.claude/settings.json whose commands post to Pensieve (HOOK_SPEC).
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
START, END = "<!-- pensieve:start -->", "<!-- pensieve:end -->"
MARK = ".pensieve"  # inside an installed skill folder: Pensieve put it there

TARGETS = {  # id -> (label, memory file the instructions go in)
    "claude": ("Claude Code", Path.home() / ".claude" / "CLAUDE.md"),
    "codex": ("Codex", Path.home() / ".codex" / "AGENTS.md"),
    "gemini": ("Gemini CLI", Path.home() / ".gemini" / "GEMINI.md"),
    "qwen": ("Qwen Code", Path.home() / ".qwen" / "QWEN.md"),
}
SKILLS_DIR = Path.home() / ".claude" / "skills"
SKILLS = {  # id -> (label, what it does, recommended)
    "pensieve-search": ("Search before grep",
                        "When and how to search: query styles, reading the top hit, similar code, past sessions.", True),
    "pensieve-teammates": ("Connect with teammates",
                           "Suggests who knows an area (git blame) and drafts the question; never messages anyone.",
                           False),
    "pensieve-plan": ("Plan with prior art and experts",
                      "Plans for new code list existing code to reuse, earlier attempts, people to loop in and "
                      "knowledge risks.", False),
}


CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
HOOKS = {  # id -> (label, what it does, recommended)
    "claude-hook": ("Index edits right away",
                    "After each Edit or Write, Claude Code tells Pensieve the file changed, so its next search finds "
                    "the new code instead of the old.", True),
    "claude-repo-check": ("Ask before indexing a new repo",
                          "When a session starts in a repo Pensieve doesn't index, Claude asks you once whether to "
                          "index it. If you say no, it uses grep there and doesn't ask again.", True),
}
HOOK_SPEC = {  # id -> (Claude Code hook event, matcher or None, the URL path the hook posts its JSON to)
    "claude-hook": ("PostToolUse", "Edit|MultiEdit|Write|NotebookEdit", "/api/touched"),
    "claude-repo-check": ("SessionStart", None, "/api/repo-check"),
}


CLAUDE_JSON = Path.home() / ".claude.json"  # Claude Code keeps user-scope MCP servers here, under "mcpServers"
CODEX_TOML = Path.home() / ".codex" / "config.toml"
APP_SUPPORT = Path.home() / "Library" / "Application Support"
# Pensieve as an MCP server in each agent. id -> (agent, config file, key of the server table or None, the folder
# that shows the agent is installed). Claude Code's file is changed through its CLI; Codex's is TOML.
MCP = {
    "claude-mcp": ("Claude Code", CLAUDE_JSON, None, Path.home() / ".claude"),
    "codex-mcp": ("Codex", CODEX_TOML, None, Path.home() / ".codex"),
    "cursor-mcp": ("Cursor", Path.home() / ".cursor" / "mcp.json", "mcpServers", Path.home() / ".cursor"),
    "gemini-mcp": ("Gemini CLI", Path.home() / ".gemini" / "settings.json", "mcpServers", Path.home() / ".gemini"),
    "qwen-mcp": ("Qwen Code", Path.home() / ".qwen" / "settings.json", "mcpServers", Path.home() / ".qwen"),
    "windsurf-mcp": ("Windsurf", Path.home() / ".codeium" / "windsurf" / "mcp_config.json", "mcpServers",
                     Path.home() / ".codeium" / "windsurf"),
    "claude-desktop-mcp": ("Claude Desktop", APP_SUPPORT / "Claude" / "claude_desktop_config.json", "mcpServers",
                           APP_SUPPORT / "Claude"),
    "vscode-mcp": ("VS Code", APP_SUPPORT / "Code" / "User" / "mcp.json", "servers", APP_SUPPORT / "Code" / "User"),
}
TOML_START, TOML_END = "# pensieve:start", "# pensieve:end"


def _claude_bin():
    from pensieve.llm import _find
    return shutil.which("claude") or _find("claude")


def _pensieve_bin() -> str:
    """This install's `pensieve` command, by full path: an agent may start it without ~/.local/bin on PATH."""
    here = Path(sys.executable).with_name("pensieve")
    return str(here) if here.exists() else shutil.which("pensieve") or "pensieve"


def mcp_present(item: str) -> bool:
    if item == "claude-mcp":
        return bool(_claude_bin())
    return MCP[item][3].exists()


def _read_json(f: Path) -> dict:
    if not f.exists() or not f.read_text().strip():
        return {}
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"{f} is not valid JSON ({e}); leaving it alone") from e


def _write(f: Path, text: str):
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".pensieve-tmp")
    tmp.write_text(text)
    os.replace(tmp, f)


def mcp_state(item: str) -> str:
    """'on', 'off', or 'theirs' (Codex has a pensieve server that Pensieve didn't add)."""
    f, key = MCP[item][1], MCP[item][2]
    try:
        if item == "codex-mcp":
            import tomllib
            text = f.read_text() if f.exists() else ""
            if "pensieve" not in (tomllib.loads(text).get("mcp_servers") or {}):
                return "off"
            return "on" if TOML_START in text else "theirs"
        servers = _read_json(f).get(key or "mcpServers") or {}
    except (OSError, ValueError):
        return "off"
    return "on" if "pensieve" in servers else "off"


def set_mcp(item: str, on: bool) -> str:
    agent, f, key, _ = MCP[item]
    if not mcp_present(item):
        raise ValueError(f"{agent} isn't installed")
    state = mcp_state(item)
    if state == "theirs":
        raise ValueError(f"{f} already has a pensieve server that Pensieve didn't add; leaving it alone")
    if on == (state == "on"):
        return state
    if item == "claude-mcp":  # through the claude CLI, which owns ~/.claude.json and writes it while it runs
        argv = [_claude_bin(), "mcp", "add", "--scope", "user", "pensieve", "--", _pensieve_bin(), "mcp"] if on \
            else [_claude_bin(), "mcp", "remove", "--scope", "user", "pensieve"]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        if r.returncode:
            raise ValueError((r.stderr or r.stdout).strip() or f"`claude mcp` failed ({r.returncode})")
    elif item == "codex-mcp":  # a marked block, so removing it leaves the rest of config.toml as it was
        text = f.read_text() if f.exists() else ""
        rest = re.sub(rf"\n*{re.escape(TOML_START)}.*?{re.escape(TOML_END)}\n?", "\n", text, flags=re.S).strip("\n")
        block = f'{TOML_START}\n[mcp_servers.pensieve]\ncommand = {json.dumps(_pensieve_bin())}\nargs = ["mcp"]\n{TOML_END}\n'
        _write(f, ((rest + "\n\n" if rest else "") + block) if on else (rest + "\n" if rest else ""))
    else:
        s = _read_json(f)
        servers = s.setdefault(key, {})
        if on:
            servers["pensieve"] = {**({"type": "stdio"} if key == "servers" else {}), "command": _pensieve_bin(), "args": ["mcp"]}
        else:
            servers.pop("pensieve", None)
            if not servers:
                s.pop(key)
        _write(f, json.dumps(s, indent=2) + "\n")
    return mcp_state(item)


def _how_long(files: int) -> str:
    """Indexing time varies about 6x with how many pieces each file splits into, so only a range."""
    return "a few minutes" if files < 1000 else "10 to 30 minutes" if files < 5000 else "an hour or more"


def repo_note(c: dict) -> str | None:
    """What the SessionStart hook tells the agent about the repo it starts in (see repos.check), or None."""
    name = c.get("name")
    if c["state"] == "unknown":
        return (f"Pensieve (the semantic search MCP server) does not index the repo {name} ({c['files']:,} tracked file{'' if c['files'] == 1 else 's'}). "
                f"Before your first code search in this session, ask the user once: \"Index {name} with Pensieve? It "
                f"takes {_how_long(c['files'])} in the background.\" If yes, run `pensieve repo index` in this repo, "
                f"then use grep until indexing finishes. If no, run `pensieve repo skip` in this repo, then use grep and "
                f"file reads for its code. Pensieve keeps the answer, so it doesn't ask again.")
    if c["state"] == "declined":
        return (f"The user chose not to index the repo {name} with Pensieve. For its code, use grep and file reads, "
                f"not Pensieve search. Pensieve still covers the user's other repos, documents and agent sessions.")
    if c["state"] == "pending":
        return f"Pensieve is indexing the repo {name}. Until it finishes, its search can miss this repo's code; use grep then."
    return None


def hook_command(hook: str, port: int) -> str:
    """curl posts the hook's JSON to the backend. If the backend isn't running, the hook does nothing."""
    event, _, url = HOOK_SPEC[hook]
    if event == "SessionStart":  # its answer is the hook's output: a note for the session
        return (f"curl -s -m 2 -X POST -H 'Content-Type: application/json' --data-binary @- "
                f"http://127.0.0.1:{port}{url} 2>/dev/null || true")
    return (f"curl -s -m 1 -X POST -H 'Content-Type: application/json' --data-binary @- "
            f"http://127.0.0.1:{port}{url} >/dev/null 2>&1 || true")


def _hook_entry(hook: str, port: int) -> dict:
    _, matcher, _ = HOOK_SPEC[hook]
    return {**({"matcher": matcher} if matcher else {}), "hooks": [{"type": "command", "command": hook_command(hook, port)}]}


def _claude_settings() -> dict:
    if not CLAUDE_SETTINGS.exists():
        return {}
    try:
        return json.loads(CLAUDE_SETTINGS.read_text() or "{}")
    except json.JSONDecodeError as e:
        raise ValueError(f"{CLAUDE_SETTINGS} is not valid JSON ({e}); leaving it alone") from e


def _ours(entry, url: str) -> bool:
    return isinstance(entry, dict) and any(url in str(h.get("command", "")) for h in entry.get("hooks", [])
                                           if isinstance(h, dict))


def hook_state(hook: str, port: int) -> str:
    """'off', 'on', or 'stale' (installed with another port or tool list)."""
    event, _, url = HOOK_SPEC[hook]
    try:
        entries = _claude_settings().get("hooks", {}).get(event, [])
    except ValueError:
        return "off"
    mine = [e for e in entries if _ours(e, url)]
    if not mine:
        return "off"
    return "on" if mine == [_hook_entry(hook, port)] else "stale"


def set_hook(hook: str, on: bool, port: int) -> str:
    event, _, url = HOOK_SPEC[hook]
    s = _claude_settings()
    hooks = s.setdefault("hooks", {})
    entries = [e for e in hooks.get(event, []) if not _ours(e, url)]
    if on:
        entries.append(_hook_entry(hook, port))
    if entries:
        hooks[event] = entries
    else:
        hooks.pop(event, None)
    if not hooks:
        s.pop("hooks")
    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = CLAUDE_SETTINGS.with_suffix(".json.pensieve-tmp")
    tmp.write_text(json.dumps(s, indent=2) + "\n")
    os.replace(tmp, CLAUDE_SETTINGS)
    return hook_state(hook, port)


def instructions() -> str:
    return (HERE / "instructions.md").read_text()


def _block() -> str:
    return f"{START}\n{instructions().strip()}\n{END}\n"


def _strip(text: str) -> str:
    return re.sub(rf"\n*{re.escape(START)}.*?{re.escape(END)}\n?", "\n", text, flags=re.S).strip("\n")


def instructions_state(target: str) -> str:
    """'off', 'on', or 'stale' (an older Pensieve block is installed)."""
    f = TARGETS[target][1]
    text = f.read_text() if f.exists() else ""
    m = re.search(rf"{re.escape(START)}.*?{re.escape(END)}\n?", text, re.S)
    if not m:
        return "off"
    return "on" if m.group(0).strip() == _block().strip() else "stale"


def set_instructions(target: str, on: bool) -> str:
    f = TARGETS[target][1]
    text = f.read_text() if f.exists() else ""
    rest = _strip(text)
    if on:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text((rest + "\n\n" if rest else "") + _block())
    elif text and rest != text.strip("\n"):
        f.write_text(rest + "\n" if rest else "")
    return instructions_state(target)


def skill_state(name: str) -> str:
    """'off', 'on', 'stale' (an older Pensieve copy) or 'theirs' (a folder by that name that Pensieve didn't make)."""
    d = SKILLS_DIR / name
    if not d.exists():
        return "off"
    if not (d / MARK).exists():
        return "theirs"
    src = HERE / "skills" / name / "SKILL.md"
    return "on" if (d / "SKILL.md").exists() and (d / "SKILL.md").read_text() == src.read_text() else "stale"


def set_skill(name: str, on: bool) -> str:
    if name not in SKILLS:
        raise ValueError(f"unknown skill {name!r}")
    d, state = SKILLS_DIR / name, skill_state(name)
    if state == "theirs":
        raise ValueError(f"{d} exists and wasn't installed by Pensieve; leaving it alone")
    if on:
        if SKILLS_DIR.is_symlink() and not SKILLS_DIR.exists():
            raise ValueError(f"{SKILLS_DIR} links to {os.readlink(SKILLS_DIR)}, which doesn't exist; "
                             "fix or remove that link, then try again")
        if d.exists():
            shutil.rmtree(d)
        shutil.copytree(HERE / "skills" / name, d)
        (d / MARK).write_text("Installed by Pensieve; turning the skill off in Pensieve removes this folder.\n")
    elif state != "off":
        shutil.rmtree(d)
    return skill_state(name)


def status(port: int) -> dict:
    return dict(
        mcp=[dict(id=n, label=agent, path=str(f).replace(str(Path.home()), "~", 1), state=mcp_state(n),
                  available=mcp_present(n)) for n, (agent, f, _, _) in MCP.items()],
        hooks=[dict(id=n, label=label, description=desc, recommended=rec, state=hook_state(n, port),
                    path=str(CLAUDE_SETTINGS).replace(str(Path.home()), "~", 1))
               for n, (label, desc, rec) in HOOKS.items()],
        instructions=[dict(id=t, label=label, path=str(f).replace(str(Path.home()), "~", 1),
                           present=f.parent.exists(), state=instructions_state(t))
                      for t, (label, f) in TARGETS.items()],
        skills=[dict(id=n, label=label, description=desc, recommended=rec, state=skill_state(n),
                     path=str(SKILLS_DIR / n).replace(str(Path.home()), "~", 1))
                for n, (label, desc, rec) in SKILLS.items()],
        claude_present=(Path.home() / ".claude").exists(),
        text=instructions())
