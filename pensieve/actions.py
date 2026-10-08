"""Operations shared by the REST API and the MCP server: resolve an item id, read it, open it on this Mac.

Item ids: 'file:/abs/path', 'code:<chunk id>', 'session:<session id>' (as returned by search)."""
import subprocess
import urllib.parse
import webbrowser
from pathlib import Path

from . import settings

ctx = {}


def bind(**kw):
    ctx.update(kw)


def resolve(id: str) -> dict:
    kind, _, ref = id.partition(":")
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
    raise KeyError(id)


def read(id: str, max_chars=20000) -> dict:
    """Text of an item: a file's extracted text, a code chunk with its owners and recent commits, or a transcript."""
    r = resolve(id)
    if r["kind"] == "file":
        f = ctx["files"].file(r["path"])
        if f and f["chunks"]:
            text = "\n".join(c["text"] for c in f["chunks"])
        else:
            from .files import extract
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


def show_search(query: str = ""):
    ok = _open_url("pensieve://search?" + urllib.parse.urlencode({"q": query}))
    return dict(ok=ok, note=None if ok else "The Pensieve Mac app isn't installed or running.")


def open_item(id: str, action="open"):
    if action not in ("open", "reveal", "visualize"):
        raise ValueError("action must be open, reveal or visualize")
    r = resolve(id)
    if action == "visualize" or r["kind"] == "session":
        return visualize(id=id)
    if action == "reveal":
        subprocess.run(["open", "-R", r["path"]], check=False)
        return dict(ok=True, path=r["path"])
    from . import apps
    nid = apps.note_id(r["path"])
    if nid:  # an exported Apple Note: show the real note
        subprocess.run(["osascript", "-e", f'tell application "Notes" to show note id "{nid}"', "-e",
                        'tell application "Notes" to activate'], check=False)
        return dict(ok=True, app="Notes")
    editor = settings.get("editor")
    if r["line"] and editor in ("vscode", "cursor", "zed"):
        if _open_url(f"{editor}://file{urllib.parse.quote(r['path'])}:{r['line']}"):
            return dict(ok=True, path=r["path"], line=r["line"], editor=editor)
    subprocess.run(["open", r["path"]], check=False)
    return dict(ok=True, path=r["path"])
