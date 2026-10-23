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
OWN_MARK = "[pensieve text job]"  # the first line of every prompt Pensieve's text engine sends (engine.MARK)
VERSION = 2  # of the cache files: a new version reads every transcript again


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


def facts(path: str, source: str = "claude") -> Facts | None:
    """One transcript's facts, read up to date. None when the file is gone."""
    with _lock:
        f = _facts.get(path) or _load(path)
        try:
            st = os.stat(path)
        except OSError:
            _facts.pop(path, None)
            return None
        if f and (f.mtime, f.size) == (st.st_mtime, st.st_size):
            _facts[path] = f
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
    seen). Stops after `limit_seconds` and goes on at the next call."""
    end = time.monotonic() + limit_seconds if limit_seconds else None
    changed, seen = [], 0
    for source, p in parsers.discover():
        if p.suffix != ".jsonl":
            continue
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
            if have is None:
                cached = _load(path)
                if cached and (cached.mtime, cached.size) == (st.st_mtime, st.st_size):
                    _facts[path] = cached
                    continue
        if facts(path, source) is not None:
            changed.append(path)
    return changed, seen


def all_facts() -> dict[str, Facts]:
    with _lock:
        return dict(_facts)


def forget(path: str):
    with _lock:
        _facts.pop(path, None)
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
                elif blk.get("type") == "tool_result" and blk.get("tool_use_id") in pending:
                    cmd, bcwd, recorded = pending.pop(blk["tool_use_id"])
                    rc = blk.get("content")
                    out = " ".join(x.get("text", "") for x in rc if isinstance(x, dict)) if isinstance(rc, list) else str(rc or "")
                    _git_write(f, cmd, bcwd, recorded, out)
            c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
        if isinstance(c, str):
            before = len(s.turns)
            parsers._add(s, t, c, when)
            if len(s.turns) > before:
                if f.n_turns == 0 and t == "user" and c.lstrip().startswith(OWN_MARK):
                    st["own"] = True  # a session Pensieve's own text engine ran: not the user's work
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
    links counted, Artifact links, the last words, the ending, and the last turns."""
    turns = [tuple(t) for t in f.tail]
    texts = [f"{'User' if r == 'user' else 'Assistant'}: {t}" for r, t in turns]
    return dict(keys=Counter({tuple(k.split("-", 1)): n for k, n in f.keys.items()}),
                ticket_urls=[tuple(x) for x in f.ticket_urls], prs=Counter(f.prs), links=list(f.links),
                last=_tail(texts[-1]) if texts else "", ending=_tail("\n".join(texts[-2:]), 2400) if texts else "",
                turns=turns)


def _tail(text: str, n: int = 600) -> str:
    if len(text) <= n:
        return text
    cut = text[-n:]
    return cut.split("\n", 1)[1] if "\n" in cut[:n // 2] else cut
