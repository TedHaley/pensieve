"""The transcripts read once for Threads: each one's facts, kept in memory and in a small file cache, so a growing
transcript parses only its new lines and a restart parses nothing twice.

A transcript's facts are what a thread needs and nothing else: its title and times, the folder and branch of each
turn, what it wrote to (branches, pull requests, from its git and gh calls), the lines it wrote with an edit tool
(claims.py), the files it edited and read, the ticket keys and links in its text, and its last turns. Threads read
no index and no model: the transcript folders, git and gh are enough.

`sync()` walks the agents' transcript folders (parsers.discover) and reads what changed. `facts(path)` gives one
transcript's facts. The cache lives in DATA_DIR/threads/facts/, one JSON file per transcript."""
import hashlib
import json
import os
import re
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pensieve import config, parsers
from pensieve.extract import git_tools as gt

TAIL_TURNS = 8        # the last turns kept: the user's last words and the agent's last reply come from them
MIN_LINE = 25         # a shorter written line (an import, a bracket) says too little for a claim (claims.py)
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d{1,6})\b")
TICKET_URL = re.compile(r"(https://[\w-]+\.atlassian\.net/browse|https://linear\.app/[\w-]+/issue)/([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
PR_URL = re.compile(r"https://(?:github\.com/[\w.-]+/[\w.-]+/pull/\d+|[\w.-]*gitlab[\w.-]*/[\w./-]+?/-/merge_requests/\d+"
                    r"|bitbucket\.org/[\w.-]+/[\w.-]+/pull-requests/\d+|dev\.azure\.com/[\w./%-]+?/pullrequest/\d+)")
ARTIFACT_URL = re.compile(r"https://claude\.ai/(?:code/)?artifact/[\w-]+")
# Web documents a session made, opened or named: (service, the document's URL)
WEB_DOCS = [
    ("Notion", re.compile(r"https://(?:www\.)?notion\.so/[\w./%?=&#-]+|https://[\w-]+\.notion\.site/[\w./%?=&#-]+")),
    ("Slack", re.compile(r"https://[\w-]+\.slack\.com/archives/[A-Z0-9]+(?:/p\d+)?(?:\?thread_ts=[\d.]+)?")),
    ("Google Docs", re.compile(r"https://docs\.google\.com/(?:document|spreadsheets|presentation|forms)/d/[\w-]+")),
    ("Google Drive", re.compile(r"https://drive\.google\.com/(?:file/d|drive/folders)/[\w-]+")),
    ("Confluence", re.compile(r"https://[\w-]+\.atlassian\.net/wiki/[\w./%+-]+")),
    ("Figma", re.compile(r"https://(?:www\.)?figma\.com/(?:file|design|board|proto)/[\w-]+(?:/[\w%-]+)?")),
]
READ_ONE = re.compile(r"get|fetch|read|view|open|download", re.I)  # a tool that reads one item, not a search or a list
SKIP_TOOL = re.compile(r"search|list|query|find|users?_?profile|reactions|emojis|members|authenticate", re.I)
DOC_EXT = {".md", ".txt", ".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".csv", ".rtf", ".pages", ".key", ".numbers",
           ".html", ".eml"}  # a local file read as context, not code
WRITE_TOOL = re.compile(r"create|send|post|update|append|add|duplicate|reply|schedule|edit|move|draft|publish|write",
                        re.I)
DOC_RANK = {"made": 0, "read": 1, "named": 2}  # a document the session made comes first
OWN_MARK = "[pensieve text job]"  # the first line of every prompt Pensieve's text engine sends (engine.MARK)
# The first words of the sessions that Pensieve starts to close work (cards.RESOLVE_PROMPTS): a close-out, not a thread
CLOSE_MARKS = ("[pensieve ship]", "[pensieve discard]")
VERSION = 7  # of the cache files: a new version reads every transcript again


@dataclass
class Facts:
    id: str
    source: str
    path: str
    project: str = ""
    title: str = ""
    started: str = ""
    updated: str = ""
    offset: int = 0                    # bytes read, whole lines only
    size: int = 0
    mtime: float = 0.0
    branches: dict = field(default_factory=dict)   # "cwd\tbranch" -> time of its last turn
    writes: list = field(default_factory=list)     # [kind, key, evidence] write edges, in order
    written: list = field(default_factory=list)    # [file, line] lines written with an edit tool
    edited: list = field(default_factory=list)     # files edited
    read: list = field(default_factory=list)       # files only read
    keys: dict = field(default_factory=dict)       # "PREFIX-123" -> times named
    ticket_urls: list = field(default_factory=list)
    prs: dict = field(default_factory=dict)        # PR url -> times named
    links: list = field(default_factory=list)      # published Artifact links
    # Artifacts the Artifact tool published: url -> [title, description, first publish, last publish, created here]
    artifacts: dict = field(default_factory=dict)
    docs: list = field(default_factory=list)       # [url, service, made | read | named, title] web documents
    tail: list = field(default_factory=list)       # the last TAIL_TURNS turns, [role, text]
    n_turns: int = 0
    state: dict = field(default_factory=dict)      # the reader's own state between two reads
    v: int = VERSION

    def updated_ts(self) -> float:
        return _ts(self.updated)


def _ts(s: str) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else 0.0
    except ValueError:
        return 0.0


def cache_dir() -> Path:
    return config.DATA_DIR / "threads" / "facts"


def _cache_path(path: str) -> Path:
    return cache_dir() / (hashlib.sha1(path.encode()).hexdigest()[:20] + ".json")


_facts: dict[str, Facts] = {}
_lock = threading.RLock()
# path -> [mtime, size, id, source, updated, n_turns, own]: enough to skip an unchanged transcript and to pick the
# sessions of a window without opening each facts file (index.json next to them)
_index: dict[str, list] = {}
_index_loaded = False
_index_dirty = False


def _load(path: str) -> Facts | None:
    try:
        d = json.loads(_cache_path(path).read_text())
    except (OSError, ValueError):
        return None
    if d.get("v") != VERSION or d.get("path") != path:
        return None
    return Facts(**{k: v for k, v in d.items() if k in Facts.__dataclass_fields__})


def _save(f: Facts):
    p = _cache_path(f.path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(f), ensure_ascii=False))
    os.replace(tmp, p)
    _note(f)


def _note(f: Facts):
    """The index row of a transcript's facts."""
    global _index_dirty
    row = [f.mtime, f.size, f.id, f.source, f.updated, f.n_turns, bool(f.state.get("own"))]
    if _index.get(f.path) != row:
        _index[f.path] = row
        _index_dirty = True


def _index_path() -> Path:
    return cache_dir() / "index.json"


def _load_index():
    global _index_loaded
    if _index_loaded:
        return
    try:
        d = json.loads(_index_path().read_text())
        if d.get("v") == VERSION:
            _index.update(d.get("files", {}))
    except (OSError, ValueError):
        pass
    _index_loaded = True


def _save_index():
    global _index_dirty
    if not _index_dirty:
        return
    p = _index_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"v": VERSION, "files": _index}))
    os.replace(tmp, p)
    _index_dirty = False


def facts(path: str, source: str = "claude") -> Facts | None:
    """One transcript's facts, read up to date. None when the file is gone."""
    with _lock:
        _load_index()
        try:
            st = os.stat(path)
        except OSError:
            _facts.pop(path, None)
            return None
        f = _facts.get(path)
        if f is None and _index.get(path, [None, None])[:2] == [st.st_mtime, st.st_size]:
            f = _load(path)  # unchanged since its facts were written: the file is enough
        elif f is None:
            f = _load(path)
        if f and (f.mtime, f.size) == (st.st_mtime, st.st_size):
            _facts[path] = f
            _note(f)
            return f
        if f is None or st.st_size < f.offset:  # a new transcript, or one written again from the start
            f = Facts(id=f"{source}:{Path(path).stem}", source=source, path=path)
        _read(f, Path(path))
        f.mtime, f.size = st.st_mtime, st.st_size
        _facts[path] = f
        _save(f)
        return f


def sync(limit_seconds: float | None = None) -> tuple[list[str], int]:
    """Read every transcript that changed since the last call. Returns (the paths that changed, the transcripts
    seen). An unchanged transcript costs one stat: its facts stay on disk until a window needs them. Stops after
    `limit_seconds` and goes on at the next call."""
    end = time.monotonic() + limit_seconds if limit_seconds else None
    changed, seen = [], 0
    with _lock:
        _load_index()
    for source, p in discover():
        seen += 1
        if end and time.monotonic() > end:
            break
        path = str(p)
        try:
            st = p.stat()
        except OSError:
            continue
        with _lock:
            have = _facts.get(path)
            if have and (have.mtime, have.size) == (st.st_mtime, st.st_size):
                continue
            if have is None and _index.get(path, [None, None])[:2] == [st.st_mtime, st.st_size]:
                continue
        if facts(path, source) is not None:
            changed.append(path)
    with _lock:
        _save_index()
    return changed, seen


def discover():
    """(agent, path) of every transcript of the agents switched on in Sources: the transcript files only, not the
    memory files or subagents' transcripts, with one directory listing per agent folder."""
    from pensieve import settings
    for sid, name, root, pattern in config.session_sources():
        if not (settings.enabled(sid) and settings.enabled(f"agents/{name}") and root.exists()):
            continue
        for p in root.glob(pattern):
            if p.suffix == ".jsonl" and "subagents" not in p.parts and p.is_file():
                yield name, p


def sessions_since(since: float) -> list[Facts]:
    """The facts of every transcript with a turn after `since` (a time), loaded from disk as needed: the sessions
    of a window. Pensieve's own engine sessions and empty transcripts are left out."""
    with _lock:
        _load_index()
        want = [p for p, row in _index.items() if row[5] and not row[6] and _ts(row[4] or "") > since]
    out = []
    for p in want:
        f = _facts.get(p) or facts(p, _index[p][3])
        if f is not None:
            out.append(f)
    return out


def find(sid: str) -> Facts | None:
    """The facts of one session by its id (source:uuid, as the cards name sessions), loaded from disk as needed."""
    with _lock:
        _load_index()
        path, source = next(((p, row[3]) for p, row in _index.items() if row[2] == sid), (None, ""))
    return (_facts.get(path) or facts(path, source)) if path else None


def all_facts() -> dict[str, Facts]:
    with _lock:
        return dict(_facts)


def forget(path: str):
    global _index_dirty
    with _lock:
        _facts.pop(path, None)
        if _index.pop(path, None) is not None:
            _index_dirty = True
            _save_index()
    try:
        _cache_path(path).unlink()
    except OSError:
        pass


def _read(f: Facts, path: Path):
    """Read the lines after f.offset into f. A Claude transcript gives everything; another agent's gives the turns,
    the files and the text facts (parsers), not the git writes."""
    if f.source != "claude":
        return _read_other(f, path)
    s = parsers.Session(id=f.id, source="claude", path=str(path), project=f.project, title=f.title,
                        started=f.started, updated=f.updated, offset=f.offset)
    s.turns = [tuple(t) for t in f.tail]
    st = f.state
    pending = st.setdefault("bash", {})        # tool_use id -> [command, cwd, recorded branch]
    for d in parsers._lines(path, s):
        t = d.get("type")
        if t == "ai-title":
            s.title = d.get("aiTitle") or d.get("title") or s.title
        if t not in ("user", "assistant") or d.get("isMeta") or d.get("isSidechain"):
            continue
        if d.get("entrypoint") and "entry" not in st:
            st["entry"] = d["entrypoint"]  # "cli" for an interactive session, "sdk-cli" for `claude -p` and the SDK
        cwd, when = d.get("cwd") or "", d.get("timestamp") or ""
        s.project = s.project or cwd
        b = d.get("gitBranch") or ""
        if b and b != "HEAD":
            k = f"{cwd}\t{b}"
            f.branches[k] = max(f.branches.get(k, 0.0), _ts(when))
        c = (d.get("message") or {}).get("content")
        if isinstance(c, list):
            for blk in c:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "tool_use":
                    name, args = blk.get("name", ""), blk.get("input") or {}
                    parsers._note_tool(s, name, args)
                    if name == "Bash":
                        pending[blk.get("id", "")] = [args.get("command", ""), cwd, b]
                    elif name in EDIT_TOOLS:
                        _written(f, args)
                    elif name == "Artifact" and args.get("action", "publish") == "publish" and not args.get("asset"):
                        st.setdefault("artifact_pub", {})[blk.get("id", "")] = args.get("description") or ""
                    elif name.startswith("mcp__"):  # a page or message tool: the documents it makes or reads
                        short = name.rsplit("__", 1)[-1]
                        if WRITE_TOOL.search(short):
                            _web_docs(f, json.dumps(args), "made")
                            st.setdefault("mcp_made", {})[blk.get("id", "")] = name
                        elif READ_ONE.search(short) and not SKIP_TOOL.search(short):
                            st.setdefault("mcp_read", {})[blk.get("id", "")] = [name, args]
                elif blk.get("type") == "tool_result" and blk.get("tool_use_id") in st.get("artifact_pub", {}):
                    desc = st["artifact_pub"].pop(blk["tool_use_id"])
                    if not blk.get("is_error"):
                        _published(f, d.get("toolUseResult"), desc, when)
                elif blk.get("type") == "tool_result" and blk.get("tool_use_id") in st.get("mcp_made", {}):
                    st["mcp_made"].pop(blk["tool_use_id"])
                    _web_docs(f, _result_text(blk), "made")
                elif blk.get("type") == "tool_result" and blk.get("tool_use_id") in st.get("mcp_read", {}):
                    name, args = st["mcp_read"].pop(blk["tool_use_id"])
                    if not blk.get("is_error"):
                        _context_doc(f, name, args, _result_text(blk))
                elif blk.get("type") == "tool_result" and blk.get("tool_use_id") in pending:
                    cmd, bcwd, recorded = pending.pop(blk["tool_use_id"])
                    rc = blk.get("content")
                    out = " ".join(x.get("text", "") for x in rc if isinstance(x, dict)) if isinstance(rc, list) else str(rc or "")
                    _git_write(f, gt.shell_code(cmd), bcwd, recorded, out)
            c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
        if isinstance(c, str):
            before = len(s.turns)
            parsers._add(s, t, c, when)
            if len(s.turns) > before:
                if f.n_turns == 0 and t == "user" and c.lstrip().startswith((OWN_MARK, *CLOSE_MARKS)):
                    st["own"] = True  # a text job, or a ship or discard session: not a thread of its own
                _text_facts(f, s.turns[-1][1])
                f.n_turns += 1
    if len(pending) > 200:  # calls whose result never came
        for k in list(pending)[:-50]:
            pending.pop(k)
    _take(f, s)


def _read_other(f: Facts, path: Path):
    s = parsers.Session(id=f.id, source=f.source, path=str(path), project=f.project, title=f.title,
                        started=f.started, updated=f.updated, offset=f.offset)
    s.turns = [tuple(t) for t in f.tail]
    before = len(s.turns)
    s = parsers.PARSERS[f.source](path, s)
    for _, text in s.turns[before:]:
        _text_facts(f, text)
        f.n_turns += 1
    _take(f, s)


def _take(f: Facts, s: parsers.Session):
    """Copy the parser's state into the facts: title, times, folder, files, the tail of the turns, the offset."""
    f.project, f.started, f.updated, f.offset = s.project, s.started, s.updated, s.offset
    if not s.title or f.state.get("auto_title") == s.title:
        first = next((t for r, t in s.turns if r == "user"), "") or (f.tail[0][1] if f.tail else "")
        s.title = " ".join(first.split())[:80] or Path(f.path).stem
        f.state["auto_title"] = s.title
    f.title = s.title
    edited, read = set(f.edited), set(f.read)
    for p, e in s.touched.items():
        (edited if e else read).add(p)
    f.edited, f.read = sorted(edited), sorted(read - edited)
    f.tail = [list(t) for t in s.turns[-TAIL_TURNS:]]


def _text_facts(f: Facts, text: str):
    for a, b in KEY.findall(text):
        k = f"{a}-{b}"
        f.keys[k] = f.keys.get(k, 0) + 1
    for base, key in TICKET_URL.findall(text):
        f.ticket_urls.append([base, key])
    for u in PR_URL.findall(text):
        f.prs[u] = f.prs.get(u, 0) + 1
    for u in ARTIFACT_URL.findall(text):
        if u not in f.links:
            f.links.append(u)
    _web_docs(f, text, "named")


def _published(f: Facts, res, desc: str, when: str):
    """Keep what one Artifact publish returned: its title, the description sent with it, the first and the last
    publish in this session, and whether this session created it. Claude Code keeps the result on the line
    (toolUseResult): url, title, and updated, which is false for a new Artifact."""
    if not isinstance(res, dict) or not ARTIFACT_URL.fullmatch(res.get("url") or ""):
        return
    u, ts = res["url"], _ts(when)
    a = f.artifacts.get(u) or ["", "", ts, ts, False]
    a[0] = res.get("title") or a[0]
    a[1] = desc or a[1]
    a[2], a[3] = min(a[2], ts), max(a[3], ts)
    a[4] = a[4] or res.get("updated") is False
    f.artifacts[u] = a
    if u not in f.links:
        f.links.append(u)


def _web_docs(f: Facts, text: str, how: str, first: bool = False):
    """Keep the web documents in `text`, each once, with the strongest way the session met it. `first`: only the
    first one (the item a read tool returned, not the links inside it)."""
    if "https://" not in text:
        return
    found = sorted(((m.start(), service, m.group(0)) for service, rx in WEB_DOCS for m in rx.finditer(text)))
    for _, service, u in found[:1] if first else found:
        _keep_doc(f, u.rstrip(".,);]'\""), service, how)


def _result_text(blk: dict) -> str:
    rc = blk.get("content")
    return " ".join(x.get("text", "") for x in rc if isinstance(x, dict)) if isinstance(rc, list) else str(rc or "")


def _keep_doc(f: Facts, url: str, service: str, how: str, title: str = ""):
    d = next((d for d in f.docs if d[0] == url), None)
    if d is None:
        f.docs.append([url, service, how, title])
        return
    if len(d) < 4:
        d.append("")
    if DOC_RANK[how] < DOC_RANK[d[2]]:
        d[2] = how
    d[3] = d[3] or title


SLACK_HOST = "https://slack.com"  # the workspace's own host takes its place where Pensieve knows it (work._artifacts)


def _context_doc(f: Facts, tool: str, args: dict, text: str):
    """The item a read tool returned, as a document the session used as context: a Slack thread or channel, a Gmail
    message or thread, else the first web document in the call or its result, with a title when the result has one."""
    short = tool.rsplit("__", 1)[-1].lower()
    try:  # a JSON result: its text values, with their line breaks
        d = json.loads(text)
        text = "\n".join(str(v) for v in d.values()) if isinstance(d, dict) else text
    except ValueError:
        pass
    if "slack" in tool.lower() and args.get("channel_id"):
        ch, ts = str(args["channel_id"]), str(args.get("message_ts") or args.get("thread_ts") or "")
        url = f"{SLACK_HOST}/archives/{ch}" + (f"/p{ts.replace('.', '')}" if ts else "")
        m = re.search(r"Message TS: [\d.]+\n(?:\s*\n)?(.{3,})", text)
        _keep_doc(f, url, "Slack", "read", m.group(1).strip()[:90] if m else "")
        return
    if "gmail" in tool.lower() and ("thread" in short or "message" in short):
        mid = next((str(args[k]) for k in ("thread_id", "threadId", "message_id", "messageId", "id") if args.get(k)), "")
        if mid:
            m = re.search(r"(?i)\"?subject\"?\s*[:=]\s*\"?([^\"\n\\]{3,120})", text)
            _keep_doc(f, f"https://mail.google.com/mail/u/0/#all/{mid}", "Gmail", "read", m.group(1).strip() if m else "")
        return
    before = len(f.docs)
    _web_docs(f, json.dumps(args), "read", first=True)
    if len(f.docs) == before:
        _web_docs(f, text, "read", first=True)
    if len(f.docs) > before:
        m = re.search(r"\"title\"\s*:\s*\"([^\"]{3,120})\"|^#\s+(.{3,120})$", text, re.M)
        if m and not f.docs[-1][3]:
            f.docs[-1][3] = (m.group(1) or m.group(2)).strip()


def _written(f: Facts, args: dict):
    """The lines an edit tool wrote: (file, line) for each line long enough to be its own (claims)."""
    file_path = args.get("file_path") or ""
    if not file_path:
        return
    texts = [e.get("new_string") or "" for e in args["edits"] if isinstance(e, dict)] if "edits" in args \
        else [args.get("new_string") or args.get("content") or ""]
    seen = {(a, b) for a, b in f.written} if len(f.written) < 20000 else set()
    for text in texts:
        for line in text.splitlines():
            line = line.strip()
            if len(line) >= MIN_LINE and (file_path, line) not in seen:
                seen.add((file_path, line))
                f.written.append([file_path, line])


def _git_write(f: Facts, cmd: str, cwd: str, recorded: str, out: str):
    """The branch and pull request writes of one Bash call, as extract/git_tools reads them, kept incrementally:
    the checkout state of the session lives in f.state."""
    st = f.state
    folder = cwd or st.get("folder", "")
    cd = gt.CD.search(cmd)
    if cd and not folder:
        folder = cd.group(1)
    st["folder"] = folder
    repo = gt.origin_repo(folder) if folder else ""
    branch = st.get("branch", "")
    for m in gt.CHECKOUT_B.finditer(cmd):
        branch = m.group(1)
        if branch not in gt.GENERIC:
            _edge(f, "branch", gt._branch_key(repo, branch), f"created {branch}")
    for m in gt.CHECKOUT.finditer(cmd):
        if not gt.CHECKOUT_B.search(cmd):
            branch = m.group(1)
    st["branch"] = branch
    on = branch or recorded
    low = out.lower()[:200]
    for m in gt.PUSH.finditer(cmd):
        name = m.group(1)
        if name not in gt.GENERIC:
            _edge(f, "branch", gt._branch_key(repo, name), f"git push {name}")
    if gt.PUSH_BARE.search(cmd) and on and on not in gt.GENERIC and not gt.PUSH.search(cmd) and "error" not in low:
        _edge(f, "branch", gt._branch_key(repo, on), f"git push {on}")
    if gt.COMMIT.search(cmd) and on and on not in gt.GENERIC and "error" not in low:
        _edge(f, "branch", gt._branch_key(repo, on), "git commit")
    if gt.PR_CREATE.search(cmd):
        for org_repo, n in gt.PR_URL.findall(out)[:1]:
            _edge(f, "pr", f"{org_repo}#{n}", "gh pr create")
    for m in gt.PR_MERGE.finditer(cmd):
        flag = gt.REPO_FLAG.search(cmd)
        target = flag.group(1) if flag else repo
        if target:
            _edge(f, "pr", f"{target}#{m.group(1)}", "gh pr merge")


def _edge(f: Facts, kind: str, key: str, evidence: str):
    f.writes.append([kind, key, evidence])


def writes(path: str) -> list[tuple[str, str]]:
    """The (kind, key) sinks a transcript wrote to, in order, each once."""
    f = facts(path) if path else None
    return list(dict.fromkeys((k, key) for k, key, _ in f.writes)) if f else []


def evidence(path: str) -> dict[tuple[str, str], list[str]]:
    """What a transcript did to each sink it wrote to: {(kind, key): ["git commit", "git push x", ...]}, in order."""
    f = facts(path) if path else None
    out: dict[tuple[str, str], list[str]] = {}
    for k, key, ev in (f.writes if f else []):
        out.setdefault((k, key), []).append(ev)
    return out


def headless(path: str) -> bool:
    """Whether a transcript is of a session that nobody typed into: `claude -p`, or an app on the Agent SDK."""
    f = facts(path) if path else None
    return bool(f) and f.state.get("entry") == "sdk-cli"


def written(path: str) -> tuple:
    """The (file, line) pairs a transcript wrote with an edit tool."""
    f = facts(path) if path else None
    return tuple((a, b) for a, b in f.written) if f else ()


def branches(path: str) -> dict[tuple[str, str], float]:
    """Every (cwd, branch) a transcript records, with the time of its last turn there."""
    f = facts(path) if path else None
    return {tuple(k.split("\t", 1)): v for k, v in f.branches.items()} if f else {}


def text_facts(f: Facts) -> dict:
    """The text facts in the shape work.group reads: keys as counted (prefix, number) pairs, ticket links, PR
    links counted, Artifact links and what their publishes returned, web documents (made first), the last words, the
    ending, and the last turns."""
    turns = [tuple(t) for t in f.tail]
    texts = [f"{'User' if r == 'user' else 'Assistant'}: {t}" for r, t in turns]
    return dict(keys=Counter({tuple(k.split("-", 1)): n for k, n in f.keys.items()}),
                ticket_urls=[tuple(x) for x in f.ticket_urls], prs=Counter(f.prs), links=list(f.links),
                artifacts={u: list(a) for u, a in f.artifacts.items()},
                docs=sorted(((*d[:3], d[3] if len(d) > 3 else "") for d in f.docs), key=lambda d: DOC_RANK[d[2]]),
                read_docs=[p for p in f.read if Path(p).suffix.lower() in DOC_EXT],
                last=_tail(texts[-1]) if texts else "", ending=_tail("\n".join(texts[-2:]), 2400) if texts else "",
                turns=turns)


def _tail(text: str, n: int = 600) -> str:
    if len(text) <= n:
        return text
    cut = text[-n:]
    return cut.split("\n", 1)[1] if "\n" in cut[:n // 2] else cut
