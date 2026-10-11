"""Optional add-ons that help agents get the most out of Pensieve: an instructions block for an agent's global
memory file (CLAUDE.md, AGENTS.md), Claude Code skills, and a Claude Code hook. Nothing is installed unless the user
turns it on.

The instructions block is kept between markers so it can be updated or removed without touching the rest of the file;
skills are copied into ~/.claude/skills/<name>/ with a marker file so only Pensieve's own copies are ever removed.
The hook is the PostToolUse entry in ~/.claude/settings.json whose command posts to /api/touched.
"""
import json
import os
import re
import shutil
from pathlib import Path

HERE = Path(__file__).parent
START, END = "<!-- pensieve:start -->", "<!-- pensieve:end -->"
MARK = ".pensieve"  # inside an installed skill folder: Pensieve put it there

TARGETS = {  # id -> (label, memory file the instructions go in)
    "claude": ("Claude Code", Path.home() / ".claude" / "CLAUDE.md"),
    "codex": ("Codex", Path.home() / ".codex" / "AGENTS.md"),
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
}
HOOK_TOOLS = "Edit|MultiEdit|Write|NotebookEdit"
HOOK_URL = "/api/touched"


def hook_command(port: int) -> str:
    return (f"curl -s -m 1 -X POST -H 'Content-Type: application/json' --data-binary @- "
            f"http://127.0.0.1:{port}{HOOK_URL} >/dev/null 2>&1 || true")


def _claude_settings() -> dict:
    if not CLAUDE_SETTINGS.exists():
        return {}
    try:
        return json.loads(CLAUDE_SETTINGS.read_text() or "{}")
    except json.JSONDecodeError as e:
        raise ValueError(f"{CLAUDE_SETTINGS} is not valid JSON ({e}); leaving it alone") from e


def _ours(entry) -> bool:
    return any(HOOK_URL in str(h.get("command", "")) for h in entry.get("hooks", []) if isinstance(h, dict))


def hook_state(port: int) -> str:
    """'off', 'on', or 'stale' (installed with another port or tool list)."""
    try:
        entries = _claude_settings().get("hooks", {}).get("PostToolUse", [])
    except ValueError:
        return "off"
    mine = [e for e in entries if isinstance(e, dict) and _ours(e)]
    if not mine:
        return "off"
    want = {"matcher": HOOK_TOOLS, "hooks": [{"type": "command", "command": hook_command(port)}]}
    return "on" if mine == [want] else "stale"


def set_hook(on: bool, port: int) -> str:
    s = _claude_settings()
    hooks = s.setdefault("hooks", {})
    entries = [e for e in hooks.get("PostToolUse", []) if not (isinstance(e, dict) and _ours(e))]
    if on:
        entries.append({"matcher": HOOK_TOOLS, "hooks": [{"type": "command", "command": hook_command(port)}]})
    if entries:
        hooks["PostToolUse"] = entries
    else:
        hooks.pop("PostToolUse", None)
    if not hooks:
        s.pop("hooks")
    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = CLAUDE_SETTINGS.with_suffix(".json.pensieve-tmp")
    tmp.write_text(json.dumps(s, indent=2) + "\n")
    os.replace(tmp, CLAUDE_SETTINGS)
    return hook_state(port)


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
        if d.exists():
            shutil.rmtree(d)
        shutil.copytree(HERE / "skills" / name, d)
        (d / MARK).write_text("Installed by Pensieve; turning the skill off in Pensieve removes this folder.\n")
    elif state != "off":
        shutil.rmtree(d)
    return skill_state(name)


def status(port: int) -> dict:
    return dict(
        hooks=[dict(id=n, label=label, description=desc, recommended=rec, state=hook_state(port),
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
