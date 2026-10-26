"""Operations shared by the REST API and the MCP server: resolve an item id, read it, open it on this Mac.

Item ids: 'file:/abs/path', 'code:<chunk id>', 'session:<session id>', 'folder:/abs/dir', 'person:<name>',
'doc:<document id>' (a collected ticket, PR, message or page; as returned by search). A 'file:' id of a connection
file from before storage v2 resolves to its document, for one release."""
import subprocess
import urllib.parse
import webbrowser
from pathlib import Path

from pensieve import settings

ctx = {}


def bind(**kw):
    ctx.update(kw)


def _doc(doc_id: int, id: str) -> dict:
    from pensieve import store
    from pensieve.store import documents
    d = documents.get(store.store_db(), doc_id)
    if d is None:
        raise KeyError(id)
    return dict(kind="doc", path=None, line=None, title=d.title, doc=d)


def resolve(id: str) -> dict:
    kind, _, ref = id.partition(":")
    if kind == "doc":
        if not ref.isdigit():
            raise KeyError(id)
        return _doc(int(ref), id)
    if kind == "file" and "/connections/" in ref:  # a v1 connection file: its document (for one release)
        from pensieve import docindex
        doc_id = docindex.resolve_path(ref)
        if doc_id is not None:
            return _doc(doc_id, id)
    if kind == "file":
        if not Path(ref).exists():
            raise KeyError(id)
        return dict(kind="file", path=ref, line=None, title=Path(ref).name)
    if kind == "code":
        c = ctx["repos"].chunk(int(ref))
        if not c:
            raise KeyError(id)
        return dict(kind="code", path=str(Path(c["checkout"]) / c["path"]), line=c["start"], chunk=c,
                    title=f"{c['repo']}/{c['path']}" + (f" @ {c['branch']}" if c["branch"] else ""))
    if kind == "session":
        s = ctx["store"].session(ref)
        if not s:
            raise KeyError(id)
        return dict(kind="session", path=None, line=None, title=s["title"], session=s)
    if kind == "folder":
        if not Path(ref).is_dir():
            raise KeyError(id)
        return dict(kind="folder", path=ref, line=None, title=Path(ref).name)
    if kind == "person" and ref:
        return dict(kind="person", path=None, line=None, title=ref)
    raise KeyError(id)


def _code_rows(where, args):
    repos = ctx["repos"]
    return repos.db.execute(f"SELECT repo, path, authors, last_ts FROM code_chunks WHERE wt='' AND {where}", args).fetchall()


def _folder(path, max_files=60):
    """A folder: its files, and who wrote its code (blamed lines) when it's in an indexed repo."""
    import json
    from collections import Counter
    rows, root = [], None
    for r in ctx["repos"].list():
        if path == r["root"] or path.startswith(r["root"] + "/"):
            root = r["root"]
    if root:
        rel = path[len(root) + 1:] if path != root else ""
        rows = _code_rows("repo=? AND path LIKE ?", (root, f"{rel}/%" if rel else "%"))
    owners, files = Counter(), Counter()
    for _, p, a, _ in rows:
        files[p] += 1
        for who, n in json.loads(a or "{}").items():
            owners[who.split(" <")[0]] += n
    names = sorted(files) if files else sorted(x.name + ("/" if x.is_dir() else "") for x in Path(path).iterdir()
                                                if not x.name.startswith("."))
    tot = sum(owners.values()) or 1
    return dict(id=f"folder:{path}", kind="folder", path=path, repo=Path(root).name if root else None,
                files=names[:max_files], n_files=len(names), truncated=len(names) > max_files,
                owners=[dict(name=w, share=round(n / tot, 3)) for w, n in owners.most_common(6)])


def _person(name):
    """Where a person wrote code (blamed lines per repo and folder) and when they were last active."""
    import json
    from collections import Counter, defaultdict
    nl = name.lower()
    per_repo, per_dir, last = Counter(), defaultdict(Counter), 0
    for repo, p, a, ts in _code_rows("lower(authors) LIKE ?", (f"%{nl}%",)):
        for who, n in json.loads(a or "{}").items():
            if who.split(" <")[0].lower() == nl:
                per_repo[repo] += n
                per_dir[repo]["/".join(p.split("/")[:2]) if "/" in p else "."] += n
                last = max(last, ts or 0)
    return dict(id=f"person:{name}", kind="person", name=name, last_change=last or None,
                repos=[dict(repo=Path(r).name, lines=n, top_folders=[d for d, _ in per_dir[r].most_common(4)])
                       for r, n in per_repo.most_common(6)])


def read(id: str, max_chars=20000) -> dict:
    """Text of an item: a file's extracted text, a code chunk with its owners and recent commits, or a transcript;
    a folder's files and owners; where a person wrote code."""
    r = resolve(id)
    if r["kind"] == "doc":
        from pensieve import store
        from pensieve.store import documents
        d = r["doc"]
        text = documents.markdown(store.store_db(), d.id) or ""
        return dict(id=f"doc:{d.id}", kind="doc", title=d.title, url=d.url, source=d.source_id, key=d.key,
                    doc_kind=d.kind.value, status=d.status, private=d.private, updated=d.time, text=text[:max_chars],
                    truncated=len(text) > max_chars)
    if r["kind"] == "folder":
        return _folder(r["path"])
    if r["kind"] == "person":
        return _person(r["title"])
    if r["kind"] == "file":
        f = ctx["files"].file(r["path"])
        if f and f["chunks"]:
            text = "\n".join(c["text"] for c in f["chunks"])
        else:
            from pensieve.files import extract
            text = extract(Path(r["path"]))
        return dict(id=id, kind="file", path=r["path"], text=text[:max_chars], truncated=len(text) > max_chars)
    if r["kind"] == "code":
        c = r["chunk"]
        owners = sorted(c["authors"].items(), key=lambda kv: -kv[1])[:5]
        return dict(id=id, kind="code", path=r["path"], repo=c["repo"], branch=c["branch"], lines=[c["start"], c["end"]], text=c["text"][:max_chars],
                    owners=[dict(author=a, lines=n) for a, n in owners], recent_commits=c["commits"][:5],
                    sessions=[dict(id=f"session:{s['id']}", title=s["title"]) for s in c["sessions"][:5]])
    s = r["session"]
    text, out = 0, []
    for t in s["turns"]:
        if text > max_chars:
            break
        out.append(f"{t['role']}: {t['text']}")
        text += len(out[-1])
    return dict(id=id, kind="session", title=s["title"], source=s["source"], project=s["project"], started=s["started"],
                summary=s["summary"], transcript="\n\n".join(out)[:max_chars], truncated=text > max_chars)


def _open_url(url: str) -> bool:
    return subprocess.run(["open", url], capture_output=True).returncode == 0


def visualize(id: str | None = None, view: str | None = None):
    """Show the visualizer: in the Mac app if it's installed, otherwise in the browser."""
    q = urllib.parse.urlencode({k: v for k, v in (("id", id), ("view", view)) if v})
    if _open_url("pensieve://visualize" + (f"?{q}" if q else "")):
        return dict(ok=True, where="app")
    frag = f"#open={urllib.parse.quote(id)}" if id else (f"#view={view}" if view else "")
    webbrowser.open(f"http://127.0.0.1:{ctx['port']}/{frag}")
    return dict(ok=True, where="browser")


def open_item(id: str, action="open"):
    if action not in ("open", "reveal", "visualize", "editor", "terminal"):
        raise ValueError("action must be open, reveal, visualize, editor or terminal")
    r = resolve(id)
    if r["kind"] == "doc":  # a collected item: its page in the service, else the app
        if action == "open" and r["doc"].url and _open_url(r["doc"].url):
            return dict(ok=True, url=r["doc"].url)
        return visualize(id=f"doc:{r['doc'].id}")
    if action in ("editor", "terminal"):  # a folder where work happens: open it to pick the work back up
        if r["kind"] != "folder":
            raise ValueError(f"{action} opens a folder")
        editor = settings.get("editor")
        if action == "editor" and editor in ("vscode", "cursor", "zed"):
            if _open_url(f"{editor}://file{urllib.parse.quote(r['path'])}"):
                return dict(ok=True, path=r["path"], editor=editor)
        subprocess.run(["open", "-a", "Terminal" if action == "terminal" else "Finder", r["path"]], check=False)
        return dict(ok=True, path=r["path"])
    if action == "visualize" or r["kind"] in ("session", "person"):
        return visualize(id=id)
    if r["kind"] == "folder" and action == "open":  # a folder opens in Finder
        subprocess.run(["open", r["path"]], check=False)
        return dict(ok=True, path=r["path"])
    if action == "reveal":
        subprocess.run(["open", "-R", r["path"]], check=False)
        return dict(ok=True, path=r["path"])
    editor = settings.get("editor")
    if r["line"] and editor in ("vscode", "cursor", "zed"):
        if _open_url(f"{editor}://file{urllib.parse.quote(r['path'])}:{r['line']}"):
            return dict(ok=True, path=r["path"], line=r["line"], editor=editor)
    subprocess.run(["open", r["path"]], check=False)
    return dict(ok=True, path=r["path"])
