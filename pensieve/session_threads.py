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

_writes: dict[str, tuple[float, list]] = {}  # transcript path -> (mtime, [(kind, key)])


def writes(path: str) -> list[tuple[str, str]]:
    """The (kind, key) sinks a Claude transcript writes to: branches, PRs and tickets (write edges only)."""
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
        out = [(e.kind, e.key) for e in extract.run_all(extract.Session(s.id, s.turns, records))
               if e.strength == extract.WRITE]
    except Exception:
        out = []
    _writes[path] = (mtime, out)
    return out


def _thread(t: dict, s: dict, ws: list) -> dict:
    """The thread of one writing session `s` inside task `t`: its sessions, PRs, branches, tickets and places only."""
    pr_keys = {k for kind, k in ws if kind == "pr"}
    branch_keys = {k for kind, k in ws if kind == "branch"}

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
    return {**t, "id": "th_" + hashlib.sha1(sid.encode()).hexdigest()[:8],
            "kind": "ticket" if tickets else "branch" if branches else "work",
            "title": s.get("title") or t.get("title", ""), "updated": s.get("updated") or t.get("updated", 0),
            "sessions": [x for x in t.get("sessions", []) if x["id"] == sid], "_sessions": [s],
            "prs": prs, "branch_prs": [], "branches": branches, "tickets": tickets,
            "keys": [k["key"] for k in tickets], "ticket_refs": [], "_shared": [], "shared_sessions": [],
            "places": [p for p in t.get("places", []) if p.get("branch") in names],
            "seen": [], "seen_more": 0, "summary": (s.get("summary") or "")[:400],
            "events": [e for e in t.get("events", []) if e.get("source") != "session" or e.get("ref") == sid]}


def split(tasks: list[dict]) -> list[dict]:
    """One thread per session of each task. A session with no write has no sinks: its thread is a thought."""
    out = []
    for t in tasks:
        for s in t.get("_sessions", []):
            out.append(_thread(t, s, writes(s.get("path", ""))))
    return out
