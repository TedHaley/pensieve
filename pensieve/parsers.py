"""Turn each agent's on-disk transcript format into a common Session."""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from pensieve import settings
from pensieve.config import session_sources

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


MEMORY_GLOB = "*/memory/*.md"  # Claude Code's memory files, in the project folder next to its transcripts
_projects = {}  # project folder -> the working folder its transcripts ran in


def _project_of(folder: Path) -> str:
    """The working folder of a Claude Code project folder, from the cwd in its transcripts. The folder's name encodes
    the path with every "/" (and ".") turned into "-", so it can't be decoded reliably."""
    if folder not in _projects:
        cwd = ""
        for f in sorted(folder.glob("*.jsonl"), key=lambda p: -p.stat().st_mtime)[:3]:
            for d in _lines(f):
                cwd = d.get("cwd") or ""
                if cwd:
                    break
            if cwd:
                break
        _projects[folder] = cwd or _decode(folder.name)
    return _projects[folder]


def _decode(name: str) -> str:
    """A project folder name back to a path that exists, matching each "-" as "/", "-" or "." (longest folder first);
    "" when no folder on disk fits."""
    parts, cur = name.lstrip("-").split("-"), Path("/")
    i = 0
    while i < len(parts):
        for j in range(len(parts), i, -1):
            hit = next((cur / n for n in {"-".join(parts[i:j]), ".".join(parts[i:j])} if (cur / n).is_dir()), None)
            if hit is None and parts[i] == "" and j > i + 1:  # "--x": a hidden folder (".x")
                hit = next((cur / ("." + n) for n in ("-".join(parts[i + 1:j]),) if (cur / ("." + n)).is_dir()), None)
            if hit:
                cur, i = hit, j
                break
        else:
            return ""
    return str(cur)


def parse_memory(path: Path) -> Session:
    """One Claude Code memory file (a fact, decision or preference that outlives its session) as a one-turn session."""
    from datetime import datetime, timezone
    text = path.read_text(errors="replace")
    meta, body = {}, text
    if text.startswith("---\n"):
        head, sep, rest = text[4:].partition("\n---\n")
        if sep:
            body = rest
            for ln in head.splitlines():
                k, _, v = ln.strip().partition(":")
                if k in ("name", "description", "modified") and v.strip():
                    meta[k] = v.strip().strip("\"'")
    ts = meta.get("modified") or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    folder = path.parent.parent
    s = Session(id=f"claude:memory/{folder.name}/{path.stem}", source="claude", path=str(path), project=_project_of(folder),
                title=f"Memory: {meta.get('description') or meta.get('name') or path.stem}"[:120], started=ts, updated=ts)
    s.turns.append(("memory", f"{meta.get('name') or path.stem}: {meta.get('description', '')}\n\n{body.strip()}"))
    return s


PARSERS = {"claude": parse_claude, "qwen": parse_qwen, "codex": parse_codex}


def discover():
    """Yield (source, path) for every transcript file currently on disk, and Claude Code's memory files (but not
    MEMORY.md, its index of them)."""
    for sid, name, root, pattern in session_sources():
        if settings.enabled(sid) and settings.enabled(f"agents/{name}") and root.exists():
            for p in root.glob(pattern):
                if p.is_file() and "subagents" not in p.parts:
                    yield name, p
            if name == "claude":
                for p in root.glob(MEMORY_GLOB):
                    if p.is_file() and p.name != "MEMORY.md":
                        yield name, p


def parse(source: str, path: Path) -> Session:
    s = parse_memory(path) if path.suffix == ".md" else PARSERS[source](path)
    if not s.title:
        first = next((t for r, t in s.turns if r == "user"), "")
        s.title = " ".join(first.split())[:80] or path.stem
    return s
