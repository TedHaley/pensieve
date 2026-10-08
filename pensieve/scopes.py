"""Scopes: named slices of the index that an agent (or the search panel) works within.

A scope lists source ids, the same ids as Sources: a category ("agents", "files", "repos", "apps") or an item
("repos/<repo root>", "files/~/Documents/specs", "agents/claude", "apps/obsidian"). Everything stays in one index;
a scope just filters every query. Agent sessions come along with the repos they ran in.

'auto' picks a scope from the agent's working directory: the first named scope that includes that repo, else just
that repo. Outside any repo, auto means everything."""
import contextvars
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import apps, settings

ALL = None  # no scope: everything
current = contextvars.ContextVar("pensieve_scope", default=None)  # set per MCP request


@dataclass
class Scope:
    name: str
    sources: list
    description: str = ""
    repos: set | None = field(default_factory=set)     # None = every repo
    folders: list | None = field(default_factory=list)  # None = every indexed file
    agents: set | None = field(default_factory=set)     # None = every agent's sessions; sessions in `repos` always count

    def label(self):
        return self.name


def _build(name, sources, description=""):
    s = Scope(name=name, sources=list(sources), description=description)
    for sid in sources:
        cat, _, item = sid.partition("/")
        if cat == "repos":
            if not item:
                s.repos = None
            elif s.repos is not None:
                s.repos.add(item.rstrip("/"))
        elif cat == "files":
            if not item:
                s.folders = None
            elif s.folders is not None:
                s.folders.append(str(Path(item).expanduser().resolve()))
        elif cat == "apps":
            if s.folders is not None:
                ids = [k for k in apps.APPS] if not item else [item]
                for k in ids:
                    a = apps.APPS.get(k)
                    if a and a["available"]():
                        s.folders += [str(p.resolve()) for p in a["roots"]()]
        elif cat == "agents":
            if not item:
                s.agents = None
            elif s.agents is not None:
                s.agents.add(item)
        else:
            raise ValueError(f"unknown source {sid!r} in scope {name!r}")
    return s


def named() -> dict:
    return settings.get("scopes")


def resolve(name: str | None, cwd: str | None = None, repos=None) -> Scope | None:
    """Scope object for a name ('' / 'all' -> None, 'auto' -> from cwd). Raises KeyError for an unknown name."""
    if not name or name == "all":
        return ALL
    if name == "auto":
        if not cwd or repos is None:
            return ALL
        root, _ = repos.locate(cwd)
        if root is None:
            from .projects import project_root
            pr = project_root(cwd)
            root = repos.locate(pr)[0] if pr else None
        if root is None:
            return ALL
        for n, sc in named().items():
            if f"repos/{root}" in sc.get("sources", []):
                s = _build(n, sc["sources"], sc.get("description", ""))
                s.name = f"{n} (auto)"
                return s
        return _build(f"{Path(root).name} (auto)", [f"repos/{root}"], f"The repo at {root}")
    if name.startswith("repos/") or name.startswith("files/"):  # ad-hoc single-source scope, e.g. ?scope=repos/<root>
        return _build(name, [name])
    sc = named().get(name)
    if sc is None:
        raise KeyError(name)
    return _build(name, sc["sources"], sc.get("description", ""))


def from_request() -> Scope | None:
    return current.get()


# ---- membership ---------------------------------------------------------------
_cache = {}


def _under(path: str, folders) -> bool:
    return any(path == f or path.startswith(f + "/") for f in folders)


def session_ids(scope: Scope, store, repos) -> set:
    with store.lock:
        rows = store.db.execute("SELECT id, source FROM sessions WHERE n_chunks>0").fetchall()
    if scope.agents is None:
        return {sid for sid, _ in rows}
    roots = scope.repos
    sc = repos.session_scope() if roots is None or roots else {}
    out = set()
    for sid, src in rows:
        if src in scope.agents:
            out.add(sid)
        elif roots is None and sc.get(sid, {}).get("root"):
            out.add(sid)
        elif roots and sc.get(sid, {}).get("root") in roots:
            out.add(sid)
    return out


def allowlist(scope: Scope, kind: str, store, repos, index) -> np.ndarray | None:
    """Chunk ids of `kind` ('file' | 'code' | 'session') inside the scope, for the vector index's allowlist.
    None = no restriction. Cached until the table changes."""
    if scope is None:
        return None
    table = {"file": "file_chunks", "code": "code_chunks", "session": "chunks"}[kind]
    with store.lock:
        fp = store.db.execute(f"SELECT COUNT(*), MAX(id) FROM {table}").fetchone()
    key = (scope.name, tuple(scope.sources), kind, fp)
    hit = _cache.get(key)
    if hit is not None and time.time() - hit[0] < 120:
        return hit[1]
    with store.lock:
        if kind == "code":
            if scope.repos is None:
                ids = None
            elif not scope.repos:
                ids = []
            else:
                ph = ",".join("?" * len(scope.repos))
                ids = [r[0] for r in store.db.execute(f"SELECT id FROM code_chunks WHERE repo IN ({ph})", list(scope.repos))]
        elif kind == "file":
            if scope.folders is None:
                ids = None
            elif not scope.folders:
                ids = []
            else:
                ids = [cid for cid, p in store.db.execute("SELECT id, path FROM file_chunks") if _under(p, scope.folders)]
        else:
            ids = None
    if kind == "session":
        if scope.agents is None:
            ids = None
        else:
            sids = session_ids(scope, store, repos)
            with store.lock:
                ids = [cid for cid, s in store.db.execute("SELECT id, session_id FROM chunks") if s in sids]
    if ids is not None:
        with store.lock:
            ids = np.array([i for i in ids if index.contains(i)], dtype=np.uint64)
    if len(_cache) > 64:
        _cache.clear()
    _cache[key] = (time.time(), ids)
    return ids


def contains(scope: Scope, item_id: str, store, repos) -> bool:
    """Is a search-result id ('file:..', 'code:..', 'session:..') inside the scope?"""
    if scope is None:
        return True
    kind, _, ref = item_id.partition(":")
    if kind == "file":
        return scope.folders is None or _under(str(Path(ref).resolve()), scope.folders) or _under(ref, scope.folders)
    if kind == "code":
        if scope.repos is None:
            return True
        with store.lock:
            r = store.db.execute("SELECT repo FROM code_chunks WHERE id=?", (int(ref),)).fetchone()
        return bool(r) and r[0] in scope.repos
    if kind == "session":
        return ref in session_ids(scope, store, repos)
    return False


def repo_allowed(scope: Scope, root: str) -> bool:
    return scope is None or scope.repos is None or root in scope.repos


def describe(scope: Scope | None) -> dict:
    if scope is None:
        return {"name": "all", "description": "Everything Pensieve indexes", "sources": []}
    return {"name": scope.name, "description": scope.description, "sources": scope.sources}
