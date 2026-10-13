"""Turn each agent's on-disk transcript format into a common Session."""
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


@dataclass(frozen=True)
class Transcript:
    """One discoverable session. `path` is also its stable identity in Pensieve.

    File-based agents use the transcript path directly. OpenCode stores many sessions in one SQLite database, so its
    identity is `<database>#<session id>` while mtime/size come from that session's rows.
    """
    source: str
    path: Path
    mtime: float
    size: int


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
_PATH_KEYS = ("file_path", "absolute_path", "notebook_path", "filePath", "absolutePath", "notebookPath", "path")


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


def _opencode_ref(path: Path) -> tuple[Path, str]:
    database, sep, session_id = str(path).rpartition("#")
    if not sep or not session_id:
        raise ValueError(f"invalid OpenCode session reference: {path}")
    return Path(database), session_id


def _opencode_db(path: Path):
    """Open OpenCode's live WAL database without ever creating or writing it."""
    uri_path = str(path.resolve()).replace("%", "%25").replace("?", "%3F").replace("#", "%23")
    db = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True, timeout=2)
    db.execute("PRAGMA query_only=ON")
    return db


def _iso(ms) -> str:
    if not isinstance(ms, (int, float)):
        return ""
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def _json(text) -> dict:
    try:
        value = json.loads(text or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def parse_opencode(path: Path) -> Session:
    database, session_id = _opencode_ref(path)
    s = Session(id=f"opencode:{session_id}", source="opencode", path=str(path))
    with _opencode_db(database) as db:
        row = db.execute(
            "SELECT directory, title, time_created, time_updated FROM session WHERE id=?", (session_id,)
        ).fetchone()
        if not row:
            return s
        s.project, s.title, created, updated = row
        session_started, session_updated = _iso(created), _iso(updated)
        s.started, s.updated = session_started, session_updated
        rows = db.execute(
            "SELECT m.id, m.time_created, m.data, p.data FROM message m "
            "LEFT JOIN part p ON p.message_id=m.id WHERE m.session_id=? "
            "ORDER BY m.time_created, m.id, p.id", (session_id,)
        )
        message_id, role, timestamp, texts = None, "", "", []

        def add_message():
            if role in ("user", "assistant"):
                _add(s, role, "\n".join(texts), timestamp)

        for mid, message_created, message_data, part_data in rows:
            if mid != message_id:
                add_message()
                message_id = mid
                message = _json(message_data)
                role = message.get("role", "")
                timestamp = _iso((message.get("time") or {}).get("created") or message_created)
                texts = []
                if not s.project and role == "assistant":
                    s.project = (message.get("path") or {}).get("cwd", "")
            part = _json(part_data)
            if part.get("type") == "text" and not part.get("ignored") and not part.get("synthetic"):
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
            elif part.get("type") == "tool":
                _note_tool(s, part.get("tool", ""), (part.get("state") or {}).get("input"))
            elif part.get("type") == "patch":
                for edited in part.get("files") or []:
                    if isinstance(edited, str) and edited.startswith("/"):
                        s.touched[edited] = True
        add_message()
        s.started = session_started or s.started
        s.updated = session_updated or s.updated
    return s


PARSERS = {"claude": parse_claude, "qwen": parse_qwen, "codex": parse_codex, "opencode": parse_opencode}


def _discover_opencode(database: Path):
    if not database.is_file():
        return
    try:
        with _opencode_db(database) as db:
            rows = db.execute(
                "SELECT s.id, s.time_updated, "
                "COALESCE((SELECT SUM(length(data)) FROM message WHERE session_id=s.id), 0) + "
                "COALESCE((SELECT SUM(length(data)) FROM part WHERE session_id=s.id), 0) "
                "FROM session s WHERE s.parent_id IS NULL ORDER BY s.id"
            ).fetchall()
    except sqlite3.DatabaseError:
        return
    for session_id, updated, size in rows:
        yield Transcript("opencode", Path(f"{database}#{session_id}"), (updated or 0) / 1000, size or 0)


def discover_source(source: str, root: Path, pattern: str):
    """Yield every session in one configured source, including virtual OpenCode database records."""
    if source == "opencode":
        yield from _discover_opencode(root / pattern)
        return
    for path in root.glob(pattern):
        if path.is_file() and "subagents" not in path.parts:
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            yield Transcript(source, path, st.st_mtime, st.st_size)


def discover():
    """Yield every configured session currently on disk."""
    for sid, name, root, pattern in session_sources():
        if settings.enabled(sid) and settings.enabled(f"agents/{name}") and root.exists():
            yield from discover_source(name, root, pattern)


def parse(source: str, path: Path) -> Session:
    s = PARSERS[source](path)
    if not s.title:
        first = next((t for r, t in s.turns if r == "user"), "")
        s.title = " ".join(first.split())[:80] or path.stem
    return s
