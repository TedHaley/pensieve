"""The resources that a context card points to, and how far each one reaches.

A card has a core (problem, goal, state, next) and resources: the evidence behind it. A resource's reach says who
can open it, and so what a shared card does with it:

  local       only on this machine: a transcript, a memory file, uncommitted or unpushed changes, a file outside
              git, a folder. A shared card summarizes it and drops the path.
  git         on the repo's remote: a branch, a pushed commit, a file at a pushed commit, a PR. A shared card
              links it (a permalink at the commit) for anyone who can read the repo.
  mcp         in a service that agents reach through an MCP server: a ticket, a message, a page. A shared card
              links it, names the service, and adds a summary in case the reader lacks access.
  restricted  online, but likely private to its owner: a Claude artifact. A shared card links it and summarizes it.
  public      anyone can open it. A shared card links it.
  private     a direct message, a group message or a private channel. Context for this Mac's cards and agents; a
              shared card leaves it out whole: no link, no title, no summary.

`collect` lists a task's resources, most useful first. `read_full` reads a local or restricted resource whole, at
share time, for its summary; `summarize` writes that summary for someone who continues the work."""
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pensieve import work

LOCAL, GIT, MCP, RESTRICTED, PUBLIC, PRIVATE = "local", "git", "mcp", "restricted", "public", "private"

KINDS = {  # kind -> (what it is, its reach)
    "session": ("an agent session transcript", LOCAL),
    "memory": ("an agent memory file", LOCAL),
    "change": ("uncommitted or unpushed changes in a checkout", LOCAL),
    "file": ("a file outside any git remote", LOCAL),
    "folder": ("a checkout or worktree folder", LOCAL),
    "branch": ("a branch on the remote", GIT),
    "code": ("a file at a pushed commit", GIT),
    "pr": ("a pull request", GIT),
    "ticket": ("a ticket", MCP),
    "message": ("a message or thread", MCP),
    "page": ("a page or document in a service", MCP),
    "artifact": ("a Claude artifact", RESTRICTED),
    "link": ("a public web page", PUBLIC),
}
SUMMARIZE = {LOCAL, RESTRICTED, MCP}  # in a shared card, what has a summary (local: instead of its ref)
MAX = {"session": 3, "memory": 3, "change": 2, "file": 4, "folder": 2, "branch": 2, "code": 8, "pr": 5, "ticket": 3,
       "message": 4, "page": 4, "artifact": 4, "link": 4}


@dataclass
class Resource:
    kind: str
    title: str
    ref: dict = field(default_factory=dict)  # path, url, repo/commit/path or source: how to open it
    summary: str = ""
    relevance: float = 0.5
    reach: str = ""

    def __post_init__(self):
        self.reach = self.reach or KINDS[self.kind][1]

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in ("", {}, None)}


def _remote_branch(root: str, branch: str) -> str:
    """The commit of the branch on origin, or "" when origin doesn't have it."""
    return work._git(root, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}") if branch else ""


def collect(t: dict) -> list[Resource]:
    """The task's resources, most useful first within each kind."""
    out = []
    places = [p for p in t.get("places", [])]
    roots = {p["path"].rstrip("/") + "/": p for p in places}
    for p in places:
        out.append(Resource("folder", Path(p["path"]).name, {"path": p["path"]}, relevance=0.3))
        if not p.get("git"):
            continue
        root, branch = p["path"], p.get("branch") or ""
        git = re.sub(r"//[^@/]+@", "//", work._git(root, "remote", "get-url", "origin", ttl=3600))
        web, remote = work.web_url(git), _remote_branch(root, branch)
        if remote and branch not in work.DEFAULT_BRANCHES:
            out.append(Resource("branch", f"{p.get('repo') or Path(root).name}@{branch}",
                                {"git": git, "web": work._branch_url(web, branch) if web else "", "branch": branch,
                                 "commit": remote[:12]}, relevance=0.8))
        if p.get("changed") or p.get("ahead"):
            what = ", ".join(x for x in (p.get("changed") and f"{p['changed']} uncommitted files",
                                         p.get("ahead") and f"{p['ahead']} commits not pushed") if x)
            out.append(Resource("change", f"{what} on {branch or 'the checkout'}",
                                {"path": root, "branch": branch, "base": f"origin/{branch}" if remote else ""}, relevance=0.9))
    for pr in t["prs"] + t.get("branch_prs", []):
        out.append(Resource("pr", pr.get("title") or pr["url"], {"url": pr["url"], "state": pr.get("state") or ""},
                            relevance=0.9 if pr.get("state") in ("open", "draft") else 0.6))
    for k in t["tickets"]:
        out.append(Resource("ticket", f"{k['key']} {k.get('title') or ''}".strip(),
                            {"url": k.get("url") or "", "key": k["key"], "status": k.get("status") or "",
                             "source": work._server_label(k["server"]) if k.get("server") else ""}, relevance=0.8))
    for i in t.get("items", []):
        if i.get("url") or i.get("private"):
            src = work._server_label(i["server"]) if i.get("server") else ""
            kind = "message" if i.get("private") or re.search(r"slack|teams|chat|mail", src, re.I) else "page"
            out.append(Resource(kind, i["title"], {"url": i.get("url") or "", "source": src, "path": i.get("path") or ""},
                                relevance=0.6, reach=PRIVATE if i.get("private") else ""))
    seen = set()
    for n, s in enumerate(t["sessions"][:MAX["session"]]):
        out.append(Resource("session", s["title"], {"session": s["id"]}, relevance=0.9 - 0.1 * n))
        for a in s.get("artifacts", []):
            if a.get("url") and a["url"] not in seen:
                seen.add(a["url"])
                out.append(Resource("artifact", f"Artifact from “{s['title']}”", {"url": a["url"], "session": s["id"]},
                                    relevance=0.5))
            elif a.get("path") and a["path"] not in seen and "/.claude/worktrees/" not in a["path"]:
                seen.add(a["path"])  # a file in another worktree belongs to another branch's work
                out.append(_file(a["path"], roots))
    order = {k: i for i, k in enumerate(KINDS)}
    out.sort(key=lambda r: (order[r.kind], -r.relevance))
    kept, count = [], {}
    for r in out:
        count[r.kind] = count.get(r.kind, 0) + 1
        if count[r.kind] <= MAX[r.kind]:
            kept.append(r)
    return kept


def _file(path: str, roots: dict) -> Resource:
    """A file that a session made: in a checkout and the same on the remote branch (git, a permalink), changed
    locally (local), an agent memory file (local), or outside any repo (local)."""
    if "/.claude/" in path and "/memory/" in path:
        return Resource("memory", Path(path).name, {"path": path}, relevance=0.4)
    root = next((r for r in roots if path.startswith(r)), None)
    if root:
        p, rel = roots[root], path[len(root):]
        branch = p.get("branch") or ""
        remote = _remote_branch(p["path"], branch)
        tracked = work._git(p["path"], "ls-files", "--", rel)
        same = remote and tracked and not work._git(p["path"], "diff", "--name-only", remote, "--", rel)
        if same:
            git = re.sub(r"//[^@/]+@", "//", work._git(p["path"], "remote", "get-url", "origin", ttl=3600))
            web = work.web_url(git)
            return Resource("code", rel, {"repo": web, "commit": remote[:12], "path": rel,
                                          "url": f"{web}/blob/{remote[:12]}/{rel}" if "github.com" in web else ""}, relevance=0.6)
        return Resource("file", rel, {"path": path, "root": p["path"], "repo_path": rel,
                                      "base": f"origin/{branch}" if remote else "HEAD"}, relevance=0.7)
    return Resource("file", Path(path).name, {"path": path}, relevance=0.5)


MAX_READ = 24000  # characters of a document that a summary reads


def read_full(r: Resource, store) -> str:
    """A local or restricted resource, read whole at share time (cut to the last MAX_READ characters): the
    transcript of a session, the text of a file, the diff of a change, the session text around an artifact's link."""
    ref = r.ref
    try:
        if r.kind == "session":
            s = store.session(ref["session"]) or {}
            text = "\n\n".join(f"{t['role']}: {t['text']}" for t in s.get("turns", []))
        elif r.kind == "file" and ref.get("root"):  # a file in a checkout that differs from the remote: its diff
            text = work._git(ref["root"], "diff", ref["base"], "--", ref["repo_path"], ttl=0) or \
                Path(ref["path"]).read_text(errors="replace")
        elif r.kind in ("memory", "file"):
            p = Path(ref["path"])
            text = p.read_text(errors="replace") if p.is_file() and p.stat().st_size < 2_000_000 else ""
        elif r.kind == "change":
            diff = work._git(ref["path"], "diff", "HEAD", "--stat", ttl=0) + "\n" + work._git(ref["path"], "diff", "HEAD", ttl=0)
            log = work._git(ref["path"], "log", "-p", "--format=commit %h %s", f"{ref['base']}..HEAD", ttl=0) if ref.get("base") else ""
            text = (log + "\n" + diff).strip()
        elif r.kind == "artifact":
            s = store.session(ref["session"]) or {}
            whole = "\n".join(t["text"] for t in s.get("turns", []))
            at = whole.find(ref["url"])
            text = whole[max(0, at - 6000): at + 2000] if at >= 0 else ""
        elif r.kind in ("message", "page") and ref.get("path"):
            text = Path(ref["path"]).read_text(errors="replace")
        else:
            text = ""
    except (OSError, KeyError, ValueError):
        text = ""
    return text[-MAX_READ:]


SUMMARY_PROMPT = """Someone is about to continue a piece of work that a teammate started. Write what this {what}
tells them, for that work, in at most {words} words.

The work: {title}
Its goal: {goal}

The {what} ("{name}"):
<document>
{text}
</document>

State only facts from the document: decisions, causes found, results, what was tried, what is still open. Name PRs,
tickets, tables and functions. {extra}Do not name local folders or file paths, do not quote secrets or keys, and do
not say how the document is stored. Leave out the owner's housekeeping on their own machine (files to delete, local
setup, notes to self). If the document says nothing useful for the work, answer exactly: nothing useful."""
EXTRA = {"session": "End with the follow-ups and questions that the session ended with, and what it chose to leave "
                    "out of scope. ",
         "change": "Say what the changes do, and whether they are tested. "}
WORDS = {"session": 110, "change": 70}


def summarize(r: Resource, text: str, card: dict) -> str:
    """A summary of a resource for someone who continues the work, written by the insights engine. "" when the
    engine is off or the document holds nothing useful. The caller scrubs it like the rest of a shared card."""
    from pensieve import llm
    if not text.strip():
        return ""
    core = card.get("core", {})
    try:
        out = llm.complete(SUMMARY_PROMPT.format(what=KINDS[r.kind][0], words=WORDS.get(r.kind, 60), title=card.get("title", ""),
                                                 goal=core.get("goal") or core.get("problem") or "", name=r.title,
                                                 extra=EXTRA.get(r.kind, ""), text=text), max_tokens=300, kind="card summary")
    except Exception:  # the engine is offline, or waits for your agent: no summary
        return ""
    out = (out if isinstance(out, str) else json.dumps(out)).strip()
    return "" if out.lower().startswith("nothing useful") else out
