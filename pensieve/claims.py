"""Which session holds a branch's work, when several sessions wrote to it.

A session claims the lines it wrote with its edit tools (Edit, Write, MultiEdit). A line counts when it is still on the
branch's current version of the file. A surviving line belongs to the latest session that wrote it. A session with more
than half of the surviving lines has the majority claim. A session without it loses the branch: the branch is not active
on its thread, and the thread's other sinks still decide its state."""
import json
import subprocess
from functools import lru_cache
from pathlib import Path

EDIT_TOOLS = {"Edit", "Write", "MultiEdit"}
MIN_LINE = 25  # shorter lines (imports, brackets, blank-ish lines) say too little to claim


def _lines_of(text: str) -> list[str]:
    return list(dict.fromkeys(x.strip() for x in text.splitlines() if len(x.strip()) >= MIN_LINE))


@lru_cache(maxsize=512)
def _written(path: str, mtime: float) -> tuple:
    """(file path, line) for each line that the transcript wrote with an edit tool."""
    out = []
    for line in Path(path).read_text(errors="ignore").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in EDIT_TOOLS):
                continue
            inp = block.get("input") or {}
            file_path = inp.get("file_path") or ""
            if not file_path:
                continue
            if "edits" in inp:
                texts = [e.get("new_string") or "" for e in inp["edits"] if isinstance(e, dict)]
            else:
                texts = [inp.get("new_string") or inp.get("content") or ""]
            for text in texts:
                out.extend((file_path, x) for x in _lines_of(text))
    return tuple(dict.fromkeys(out))


def written(path: str) -> tuple:
    """The (file, line) pairs a transcript wrote, cached by the transcript's modification time."""
    if not path:
        return ()
    try:
        return _written(path, Path(path).stat().st_mtime)
    except OSError:
        return ()


@lru_cache(maxsize=2048)
def _at_branch(root: str, branch: str, rel: str) -> str:
    r = subprocess.run(["git", "-C", root, "show", f"refs/heads/{branch}:{rel}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def shares(sessions: list[dict], root: str, branch: str, lines_of=None, text_of=None) -> dict:
    """Each session's claim on a branch: {session id: {"surviving", "written", "majority"}}.
    `sessions` are dicts with id, path and updated. `lines_of(session)` gives its (file, line) pairs and
    `text_of(file)` gives the branch's text of a file. By default they read the transcript and git."""
    lines_of = lines_of or (lambda s: written(s.get("path", "")))
    text_of = text_of or (lambda rel: _at_branch(root, branch, rel))
    owner, mine = {}, {}
    prefix = root.rstrip("/") + "/"
    for s in sorted(sessions, key=lambda x: x.get("updated") or 0):  # later sessions take a line over earlier ones
        pairs = [(p[len(prefix):], line) for p, line in lines_of(s) if p.startswith(prefix)]  # this checkout's files only
        mine[s["id"]] = len(pairs)
        cache = {}
        for rel, line in pairs:
            if rel not in cache:
                cache[rel] = text_of(rel)
            if line in cache[rel]:
                owner[(rel, line)] = s["id"]
    counts = {s["id"]: 0 for s in sessions}
    for sid in owner.values():
        counts[sid] += 1
    total = sum(counts.values())
    return {s["id"]: dict(surviving=counts[s["id"]], written=mine[s["id"]], share=counts[s["id"]] / total if total else 0,
                          majority=total > 0 and counts[s["id"]] * 2 > total)
            for s in sessions}


def apply(threads: list[dict]) -> None:
    """Settle the branches that several threads share. A thread without the majority claim loses the branch: the branch
    leaves its sinks, so the branch does not keep it open. Its other sinks still decide its state."""
    groups = {}
    for t in threads:
        for b in t.get("branches", []):
            groups.setdefault((b.get("repo", ""), b["branch"]), []).append(t)
    for (_, branch), ts in groups.items():
        ts = [t for t in ts if t.get("_sessions")]
        if len(ts) < 2:
            continue
        root = next((p["path"] for t in ts for p in t.get("places", [])
                     if p.get("branch") == branch and p.get("git") and p.get("path")), "")
        if not root:
            continue
        sessions = [dict(id=t["_sessions"][0]["id"], path=t["_sessions"][0].get("path", ""),
                         updated=t["_sessions"][0].get("updated") or 0) for t in ts]
        res = shares(sessions, root, branch)
        if not any(r["surviving"] for r in res.values()):  # nothing of theirs is on the branch: no evidence either way
            continue
        won = {t["_sessions"][0]["id"] for t in ts if res[t["_sessions"][0]["id"]]["majority"]}
        won_tickets = {k["key"] for t in ts if t["_sessions"][0]["id"] in won for k in t.get("tickets", [])}
        # the session with the largest share of the branch's lines names the threads that share it
        lead = max(ts, key=lambda t: res[t["_sessions"][0]["id"]]["share"])["_sessions"][0]
        for t in ts:
            t["_lead_title"] = lead.get("title", "")
            r = res[t["_sessions"][0]["id"]]
            t["claim"] = dict(branch=branch, lost=not r["majority"], **r)
            if not r["majority"]:  # the loser keeps only what it holds on its own: its other sinks decide its state
                t["branches"] = [b for b in t["branches"] if b["branch"] != branch]
                t["prs"] = [p for p in t.get("prs", []) if p.get("head") != branch]
                t["tickets"] = [k for k in t.get("tickets", []) if k["key"] not in won_tickets]
