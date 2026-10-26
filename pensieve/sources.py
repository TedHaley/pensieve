"""Everything Pensieve can index, grouped into categories that can be switched on and off as a whole or item by
item. Switching a source off removes what it had indexed on the next sweep."""
from pathlib import Path

from pensieve import config, connections, connectors, settings

AGENTS = {"claude": "Claude Code", "codex": "Codex", "qwen": "Qwen Code"}
CATEGORIES = [
    ("agents", "AI agent sessions", "Your conversations with coding agents, linked to the code they touched."),
    ("files", "Files & folders", "Documents in these folders: text, Markdown, code, PDF, Word, PowerPoint. Git repos inside them are indexed as repos."),
    ("repos", "Git repositories", "Code with git blame and history, including uncommitted changes and linked worktrees."),
    ("connections", "Connections", "Tickets, messages and pages from your MCP servers (Jira, Slack, Linear and others), "
     "collected with read-only tools and kept over time, with each change. A connection is on once Pensieve holds data from it, unless you switch it off."),
]


def _ago(t) -> str:
    import time
    if not t:
        return ""
    s = time.time() - t
    return "just now" if s < 90 else f"{int(s // 60)} min ago" if s < 5400 else \
        f"{int(s // 3600)} h ago" if s < 172800 else f"{int(s // 86400)} days ago"


def connection_items() -> list[dict]:
    on = settings.enabled
    out = [dict(id=f"connections/{connections.SESSIONS}", label="From your agent sessions",
                detail="What your agents read with MCP tools in past sessions: issues, threads, pages. Nothing new is fetched.",
                available=True, enabled=on(f"connections/{connections.SESSIONS}"),
                count=connections._count(connections.SESSIONS), unit="items")]
    rank = {"connected": 0, "pending": 1, "failed": 2, "unknown": 3}
    for r in sorted(connections.servers(), key=lambda r: (rank.get(r["status"], 4), r["name"].lower())):
        reads, total = sum(t["read"] for t in r["tools"]), len(r["tools"])
        via = _via(r)
        if r["status"] == "connected":
            parts = [f"{reads} of {total} tools read-only", via]
            if r.get("error"):
                parts.insert(0, f"last collection failed: {r['error']}")
            elif r.get("collected"):
                parts.append(f"collected {_ago(r['collected'])}")
                if r.get("tokens"):
                    parts.append(f"{r['tokens'] / 1000:,.0f}k tokens a run" +
                                 (f" (${r['cost_usd']:.2f} at API prices)" if r.get("cost_usd") else ""))
            detail = " · ".join(parts)
        elif r["status"] == "needs-auth" and r["origin"] == "own":
            detail = "Needs sign-in: use Sign in under Cloud connectors"
        elif r["status"] == "needs-auth" and r["origin"] == "cli":
            detail = "Needs sign-in: run gh auth login in a terminal"
        elif r["status"] == "needs-auth":
            detail = "Needs sign-in: sign in to it in Claude Code (/mcp) or on claude.ai"
        elif r["status"] == "pending":
            detail = f"Was still connecting at the last check · {via}"
        else:
            detail = f"{r['status']}: {r.get('error') or 'could not connect'} · {via}"
        label = r["name"].removeprefix("claude.ai ").split(":")[-1]  # plugin:<plugin>:<server> -> <server>
        out.append(dict(id=f"connections/{r['id']}", label=label, detail=detail,
                        error=bool(r.get("error")), available=r["status"] in ("connected", "pending") or r["items"] > 0,
                        na=detail, enabled=on(f"connections/{r['id']}"), count=r["items"], unit="items",
                        collect=r["status"] == "connected" and reads > 0,
                        remove=dict(kind="connections", path=r["name"]) if r["origin"] == "own" else None))
    return out


def _via(r: dict) -> str:
    if r["origin"] == "api":
        return f"with your Slack app{' as ' + r['account'] if r.get('account') else ''}"
    return {"cli": "through the gh command line", "own": "added here"}.get(
        r["origin"]) or f"from the {' and '.join(r.get('agents') or [])} config"


_RANK = {"connected": 0, "pending": 1, "needs-auth": 2, "failed": 3}


def connector_rows() -> list[dict]:
    """The cloud connectors that Pensieve knows (connectors.PROFILES), each with its state on this Mac: the servers
    found for it (through Claude Code, another agent's config, gh, or added here), whether one is on, and the ways to
    add it as one of Pensieve's own servers."""
    from pensieve import mcp_oauth
    found = connections.servers()
    skip = connections.covered(found)
    rows = []
    for pid in connectors.ORDER:
        p = connectors.PROFILES[pid]
        mine = sorted((r for r in found if connectors.match(r["name"], r["id"]) == pid),
                      key=lambda r: (not settings.enabled(f"connections/{r['id']}"), r["id"] in skip,
                                     _RANK.get(r["status"], 4)))
        best = mine[0] if mine else None
        on = bool(best) and settings.enabled(f"connections/{best['id']}")
        if not best:
            state, detail = "none", "Not connected"
        elif best["status"] == "connected":
            state = "on" if on else "found"
            detail = f"{'Collecting' if on else 'Found'} {_via(best)}" + ("" if on else ": switch it on below")
        elif best["status"] == "needs-auth" or best.get("oauth"):
            state, detail = "needs-auth", f"Needs sign-in ({_via(best)})"
        else:
            state, detail = best["status"], f"{best['status'].capitalize()} ({_via(best)})"
        if best and len(mine) > 1:
            detail += f" · also {', '.join(_via(r) + (' (not collected: one copy is enough)' if r['id'] in skip else '') for r in mine[1:])}"
        signin = next((r["id"] for r in mine if r.get("oauth") and r["status"] != "connected"), None)
        folders = [str(f) for f in connectors.drive_folders()] if pid == "drive" else []
        if folders and not best:
            have = {str(Path(f).expanduser()) for f in settings.get("folders")}
            state = "on" if any(f in have for f in folders) else "folder"
            detail = "Your Drive for desktop folder is in Files & folders" if state == "on" else \
                "Drive for desktop is on this Mac: add its folder"
        rows.append(dict(id=pid, label=p["label"], state=state, detail=detail, sid=best["id"] if best else None,
                         token=any(r["id"] == connections.SLACK for r in mine),
                         items=sum(r["items"] for r in mine), signin=signin, folders=folders,
                         signed_in=bool(signin) and mcp_oauth.signed_in(signin),
                         add=[{k: v for k, v in a.items() if k not in ("command", "args")} for a in p["add"]]))
    return rows


def connections_note() -> str:
    st = connections._load()
    when = f" Checked {_ago(st['discovered'])}." if st.get("discovered") else " Checking soon."
    if st.get("discover_error"):
        when += f" The last check failed: {st['discover_error']}"
    agents = "Claude Code, Codex, Cursor, Gemini CLI and your other agents"
    return (f"Every MCP server that {agents} can use, found by itself, and the servers you add here. Pensieve calls "
            "only read-only tools." + when)


def _home(p) -> str:
    return str(p).replace(str(Path.home()), "~", 1)


def catalog(store, repos, files) -> list[dict]:
    on = settings.enabled
    db = store.db
    per_root = dict(db.execute("SELECT root, COUNT(*) FROM files GROUP BY root").fetchall())
    per_repo = dict(db.execute("SELECT repo, COUNT(DISTINCT path) FROM code_chunks GROUP BY repo").fetchall())
    repo_rows = db.execute("SELECT root, name FROM repos ORDER BY name").fetchall()
    wt_rows = db.execute("SELECT path, repo, branch FROM worktrees ORDER BY path").fetchall()
    per_wt = dict(db.execute("SELECT wt, COUNT(*) FROM checkout_files WHERE wt!='' GROUP BY wt").fetchall())

    agents = []
    for sid, k, root, _ in config.session_sources():
        extra = "@" in sid
        n = store.db.execute("SELECT COUNT(*) FROM sessions WHERE n_chunks>0 AND source=? AND path LIKE ?",
                             (k, str(root) + "/%")).fetchone()[0]
        where = root.parent.name if root.name in ("projects", "sessions") else root.name
        agents.append(dict(id=sid, label=AGENTS.get(k, k) + (f" · {where}" if extra else ""), detail=_home(root),
                           available=root.is_dir(), enabled=on(sid), count=n, unit="sessions",
                           remove=dict(kind="agents", path=sid.split("@", 1)[1]) if extra else None))

    folders = []
    for f in settings.get("folders"):
        p = Path(f).expanduser()
        folders.append(dict(id=f"files/{f}", label=p.name or f, detail=_home(p), available=p.is_dir(), enabled=on(f"files/{f}"),
                            count=per_root.get(str(p), 0), unit="files", remove=dict(kind="files", path=f)))

    s = settings.load()
    skipped = getattr(repos, "skipped", [])
    repo_items = [
        dict(id="repos/discover", label="Discover git repositories",
             detail=f"Repos your agents worked in, and repos under {', '.join(s['sweep_roots'])} and your folders "
                    f"({s['sweep_depth']} levels deep). Found repos with more than {s['max_repo_files']:,} files and "
                    f"no agent use are listed below for you to switch on, not indexed on their own",
             available=True, enabled=on("repos/discover"), kind="rule"),
    ]
    added = {a: str(Path(a).expanduser()) for a in s["repos"]}  # added in Sources: indexed whatever discovery finds

    def added_by(root):
        return next((a for a, p in added.items() if root == p or root.startswith(p + "/")), None)

    for root, name in repo_rows:
        a = added_by(root)
        note = " · not syncing" if repos.switched_off(root) else ""
        repo_items.append(dict(id=f"repos/{root}", label=name, detail=_home(root) + note,
                               available=Path(root).is_dir(), enabled=on(f"repos/{root}"), count=per_repo.get(root, 0), unit="files",
                               parent=None if a else "repos/discover",
                               remove=dict(kind="repos", path=a if a and added[a] == root else root)))
        for wt, repo, branch in (w for w in wt_rows if w[1] == root):  # synced with their repo: no switch of their own
            repo_items.append(dict(id=f"worktree/{wt}", kind="worktree", label=Path(wt).name,
                                   detail=f"{branch or 'detached'} · {_home(wt)}", available=Path(wt).is_dir(),
                                   enabled=on(f"repos/{root}"), count=per_wt.get(wt, 0), unit="changed files",
                                   parent=f"repos/{root}"))
    indexed = {r for r, _ in repo_rows}
    for root, _ in repos.found:  # found by the sweep, waiting for their turn
        if root not in indexed and not added_by(root):
            repo_items.append(dict(id=f"repos/{root}", label=Path(root).name, detail=f"{_home(root)} · indexing soon",
                                   available=True, enabled=on(f"repos/{root}"), count=0, unit="files", parent="repos/discover"))
    for a, p in added.items():  # a repo not indexed yet (switched off, or waiting for the sweep), or a folder of repos
        if not any(r == p for r, _ in repo_rows):
            is_repo, enabled = (Path(p) / ".git").exists(), on(f"repos/{p}")
            note = ("switched off" if not enabled else "indexing soon") if is_repo else "repos inside are always indexed"
            repo_items.append(dict(id=f"repos/{p}", label=Path(p).name, detail=f"{_home(p)} · {note}",
                                   available=Path(p).is_dir(), enabled=enabled, count=sum(n for r, n in per_repo.items()
                                   if r.startswith(p + "/")), unit="files", remove=dict(kind="repos", path=a)))
    for sk in skipped:
        if sk["root"] in added.values():  # listed above, as added
            continue
        repo_items.append(dict(id=f"repos/{sk['root']}", label=Path(sk["root"]).name, detail=f"{_home(sk['root'])} · not indexed yet: "
                               f"{sk['files']:,} files and no agent use (switch on to index it)", available=True, enabled=False, count=0, unit="files",
                               parent="repos/discover"))
    for d in settings.get("disabled"):  # repos switched off no longer appear in the repos table; keep them listed
        if d.startswith("repos/") and d not in ("repos/sessions", "repos/sweep", "repos/discover") and not any(i["id"] == d for i in repo_items):
            r = d[6:]
            repo_items.append(dict(id=d, label=Path(r).name, detail=_home(r), available=Path(r).is_dir(), enabled=False, count=0,
                                   unit="files", parent="repos/discover"))

    items = {"agents": agents, "files": folders, "repos": repo_items, "connections": connection_items()}
    for it in (i for group in items.values() for i in group if i.get("kind") not in ("rule", "worktree")):
        it["map"] = settings.on_map(it["id"])
    return [dict(id=c, label=label, description=desc, enabled=on(c), map=settings.on_map(c), items=items[c],
                 count=sum(i.get("count", 0) for i in items[c] if i.get("kind") not in ("rule", "worktree")),
                 addable=True,
                 **({"note": connections_note(), "connectors": connector_rows()} if c == "connections" else {}))
            for c, label, desc in CATEGORIES]


def set_source(id: str, on: bool, repos=None, map_only=False):
    """Switch a category or item on/off (or, with map_only, only on/off the map). Switching on a repo the sweep
    skipped as too big adds it to `repos`."""
    valid = {c for c, _, _ in CATEGORIES}
    if id.split("/", 1)[0] not in valid:
        raise ValueError(f"unknown source {id!r}: ids start with {', '.join(sorted(valid))}")
    if map_only:
        return settings.set_on_map(id, on)
    if on and id.startswith("repos/") and repos is not None:
        root = id[6:]
        if any(sk["root"] == root for sk in getattr(repos, "skipped", [])) and root not in settings.get("repos"):
            settings.update({"repos": settings.get("repos") + [root]})
    return settings.set_enabled(id, on)
