"""Turn each agent's on-disk transcript format into a common Session."""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import SOURCES

_REMINDER = re.compile(r"<(system-reminder|task-notification|local-command-\w+|command-\w+)>.*?</\1>", re.S)


@dataclass
class Session:
    id: str
    source: str
    path: str
    project: str = ""
    title: str = ""
    started: str = ""
    updated: str = ""
    turns: list = field(default_factory=list)  # (role, text)
    touched: dict = field(default_factory=dict)  # abs path -> edited?


def _clean(text: str) -> str:
    return _REMINDER.sub("", text).strip()


def _lines(path: Path):
    with open(path, errors="replace") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _add(s: Session, role: str, text: str, ts: str):
    text = _clean(text)
    if not text:
        return
    s.turns.append((role, text))
    s.started = s.started or ts
    s.updated = ts or s.updated


_EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "write_file", "replace", "apply_patch"}
_PATH_KEYS = ("file_path", "absolute_path", "notebook_path", "path")


def _note_tool(s: Session, name: str, args):
    if not isinstance(args, dict):
        return
    for k in _PATH_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.startswith("/"):
            edited = name.lower() in _EDIT_TOOLS
            s.touched[v] = s.touched.get(v, False) or edited


def parse_claude(path: Path) -> Session:
    s = Session(id=f"claude:{path.stem}", source="claude", path=str(path))
    for d in _lines(path):
        t = d.get("type")
        if t == "ai-title":
            s.title = d.get("aiTitle") or d.get("title") or s.title
        if t not in ("user", "assistant") or d.get("isMeta") or d.get("isSidechain"):
            continue
        s.project = s.project or d.get("cwd", "")
        c = (d.get("message") or {}).get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    _note_tool(s, b.get("name", ""), b.get("input"))
            c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
        if isinstance(c, str):
            _add(s, t, c, d.get("timestamp", ""))
    return s


def parse_qwen(path: Path) -> Session:
    s = Session(id=f"qwen:{path.stem}", source="qwen", path=str(path))
    for d in _lines(path):
        t = d.get("type")
        if t not in ("user", "assistant") or d.get("subtype") in ("notification", "cron"):
            continue
        s.project = s.project or d.get("cwd", "")
        parts = (d.get("message") or {}).get("parts") or []
        for p_ in parts:
            fc = p_.get("functionCall") if isinstance(p_, dict) else None
            if fc:
                _note_tool(s, fc.get("name", ""), fc.get("args"))
        text = "\n".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
        _add(s, t, text, d.get("timestamp", ""))
    return s


def parse_codex(path: Path) -> Session:
    s = Session(id=f"codex:{path.stem}", source="codex", path=str(path))
    for d in _lines(path):
        p = d.get("payload") or {}
        if d.get("type") == "session_meta":
            s.project = p.get("cwd", s.project)
        if p.get("type") != "message" or p.get("role") not in ("user", "assistant"):
            continue
        text = "\n".join(b.get("text", "") for b in p.get("content", []) if isinstance(b, dict))
        _add(s, p["role"], text, d.get("timestamp", ""))
    return s


PARSERS = {"claude": parse_claude, "qwen": parse_qwen, "codex": parse_codex}


def discover():
    """Yield (source, path) for every transcript file currently on disk."""
    for name, root, pattern in SOURCES:
        if root.exists():
            for p in root.glob(pattern):
                if p.is_file() and "subagents" not in p.parts:
                    yield name, p


def parse(source: str, path: Path) -> Session:
    s = PARSERS[source](path)
    if not s.title:
        first = next((t for r, t in s.turns if r == "user"), "")
        s.title = " ".join(first.split())[:80] or path.stem
    return s
