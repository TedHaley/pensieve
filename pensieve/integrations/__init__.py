"""Optional add-ons that help agents get the most out of Pensieve: an instructions block for an agent's global
memory file (CLAUDE.md, AGENTS.md) and Claude Code skills. Nothing is installed unless the user turns it on.

The instructions block is kept between markers so it can be updated or removed without touching the rest of the file;
skills are copied into ~/.claude/skills/<name>/ with a marker file so only Pensieve's own copies are ever removed.
"""
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


def status() -> dict:
    return dict(
        instructions=[dict(id=t, label=label, path=str(f).replace(str(Path.home()), "~", 1),
                           present=f.parent.exists(), state=instructions_state(t))
                      for t, (label, f) in TARGETS.items()],
        skills=[dict(id=n, label=label, description=desc, recommended=rec, state=skill_state(n),
                     path=str(SKILLS_DIR / n).replace(str(Path.home()), "~", 1))
                for n, (label, desc, rec) in SKILLS.items()],
        claude_present=(Path.home() / ".claude").exists(),
        text=instructions())
