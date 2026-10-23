"""Threads per session, built as the bench builds them (bench/graph_build.py, bench/graph_states.py).

Each session is one thread. The thread holds only what that session wrote: the sinks its own transcript changes
(pensieve/extract, write edges). A session that writes nothing holds no sinks, so it is a thought. Several threads can
hold the same sink.

A task (the app's grouping of sessions that share a branch, PR or ticket) is split into one thread per session, so no
thread takes state from a sink another session wrote."""
import hashlib
import json
import os
from pathlib import Path

from pensieve import sink_status
from pensieve.chains import _mentions
from pensieve.extract import git_tools, mentions  # registers the extractors that writes() runs

EXTRACTORS = (git_tools, mentions)

_writes: dict[str, tuple[float, list]] = {}  # transcript path -> (mtime, [(kind, key, evidence)])


def _extract(path: str) -> list[tuple[str, str, str]]:
    """The (kind, key, evidence) write edges of a Claude transcript, cached by the file's mtime."""
    if not path:
        return []
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    hit = _writes.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    from pensieve import extract, parsers
    out = []
    try:
        s = parsers.parse("claude", Path(path))
        records = []
        for line in Path(path).read_text(errors="ignore").splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        out = [(e.kind, e.key, e.evidence) for e in extract.run_all(extract.Session(s.id, s.turns, records))
               if e.strength == extract.WRITE]
    except Exception:
        out = []
    _writes[path] = (mtime, out)
    return out


def writes(path: str) -> list[tuple[str, str]]:
    """The (kind, key) sinks a Claude transcript writes to: branches, PRs and tickets (write edges only)."""
    return [(kind, key) for kind, key, _ in _extract(path)]


def evidence(path: str) -> dict[tuple[str, str], list[str]]:
    """What a transcript did to each sink it wrote to: {(kind, key): ["git commit", "git push x", ...]}, in order."""
    out: dict[tuple[str, str], list[str]] = {}
    for kind, key, ev in _extract(path):
        out.setdefault((kind, key), []).append(ev)
    return out


def uncommitted(t: dict, s: dict) -> list[tuple[str, str]]:
    """The branch a session wrote to without a commit: lines it wrote with an edit tool are in the uncommitted diff
    of a checkout of the task on that branch. One (branch, key) per such place; nothing when another branch is open
    there. A session whose edits were committed since (by it or by another) has no line left in the diff."""
    from pensieve import claims, work
    pairs = claims.written(s.get("path", ""))
    if not pairs:
        return []
    out = []
    for p in t.get("places", []):
        root, branch = p.get("path", ""), p.get("branch", "")
        if not (p.get("git") and root and branch and p.get("changed")) or p.get("head") or branch in work.DEFAULT_BRANCHES:
            continue
        last_commit = work._git(root, "log", "-1", "--format=%ct", ttl=30)
        if last_commit.isdigit() and (s.get("updated") or 0) < int(last_commit):
            continue  # the session ended before the last commit: what it wrote is committed, or was dropped
        prefix = root.rstrip("/") + "/"
        mine = [(f[len(prefix):], line) for f, line in pairs if f.startswith(prefix)]
        added: dict[str, set] = {}
        for rel in {f for f, _ in mine}:
            diff = work._git(root, "diff", "HEAD", "--", rel, ttl=30)
            added[rel] = {ln[1:].strip() for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")}
        if any(line in added.get(rel, ()) for rel, line in mine):
            slug = sink_status.github_repo(root)
            out.append(("branch", f"{slug}@{branch}" if slug else branch))
    return out


def _thread(t: dict, s: dict, ws: list, facts: dict | None = None) -> dict:
    """The thread of one writing session `s` inside task `t`: its sessions, PRs, branches, tickets and places only.
    Its text fields (where it stopped, its ending, its files) are the session's own, not the task's. `facts` are the
    session's text facts (work._facts), when the caller has them."""
    pr_keys = {k for kind, k in ws if kind == "pr"}
    branch_keys = {k for kind, k in ws if kind == "branch"}
    facts = facts or {}

    def bkey(b: dict) -> str:
        slug = sink_status.github_repo(b.get("repo", ""))
        return f"{slug}@{b['branch']}" if slug else b["branch"]

    prs = [p for p in t.get("prs", []) if sink_status.pr_key(p.get("url", "")) in pr_keys]
    have = {sink_status.pr_key(p.get("url", "")) for p in prs}
    for k in sorted(pr_keys - have):  # a PR this session wrote that the task does not list: its state from gh
        info = sink_status.pr_info(k)
        org, _, num = k.partition("#")
        prs.append(dict(url=f"https://github.com/{org}/pull/{num}", state=info.get("state", "").lower(),
                        head=info.get("head", ""), title="", own=True))
    branches = [b for b in t.get("branches", []) if bkey(b) in branch_keys or b["branch"] in branch_keys]
    have_b = {bkey(b) for b in branches}
    for k in sorted(branch_keys - have_b):  # a branch this session wrote that the task does not list
        repo, sep, name = k.partition("@")
        branches.append(dict(repo=repo if sep else "", branch=name if sep else k))
    text = " ".join([f"{p.get('title', '')} {p.get('head', '')}" for p in prs] + [b["branch"] for b in branches])
    tickets = [k for k in t.get("tickets", []) if k.get("key") and _mentions(text, k["key"])]
    names = {b["branch"] for b in branches}
    sid = s["id"]
    files = sorted(str(p) for p in (s.get("edited") or ()))
    return {**t, "id": thread_id(sid),
            "kind": "ticket" if tickets else "branch" if branches else "work",
            "title": s.get("title") or t.get("title", ""), "updated": s.get("updated") or t.get("updated", 0),
            "sessions": [x for x in t.get("sessions", []) if x["id"] == sid], "_sessions": [s],
            "_siblings": [dict(id=x["id"], title=x.get("title", ""), updated=x.get("updated") or 0)
                          for x in t.get("sessions", []) if x["id"] != sid],
            "_writes": list(ws),
            "prs": prs, "branch_prs": [], "branches": branches, "tickets": tickets,
            "keys": [k["key"] for k in tickets], "ticket_refs": [], "_shared": [], "shared_sessions": [],
            "places": [p for p in t.get("places", []) if p.get("branch") in names],
            "seen": [], "seen_more": 0, "summary": (s.get("summary") or "")[:400],
            "last_words": facts.get("last", ""), "ending": facts.get("ending", ""),
            "last_summary": (s.get("summary") or "")[:800], "earlier": [],
            "files": files[:15] if files else t.get("files", []),
            "commits": t.get("commits", []) if branches else [],
            "events": [e for e in t.get("events", []) if e.get("source") != "session" or e.get("ref") == sid]}


def thread_id(sid: str) -> str:
    return "th_" + hashlib.sha1(sid.encode()).hexdigest()[:8]


def split(tasks: list[dict], facts: dict | None = None) -> list[dict]:
    """One thread per session of each task. A session with no write has no sinks: its thread is a thought. A session
    that edited files still uncommitted on the task's branch wrote to that branch. `facts`: {session id: text facts}."""
    out = []
    for t in tasks:
        for s in t.get("_sessions", []):
            ws = writes(s.get("path", ""))
            ws += [w for w in uncommitted(t, s) if w not in ws]
            out.append(_thread(t, s, ws, (facts or {}).get(s["id"])))
    return out
