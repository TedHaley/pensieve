"""Everything Pensieve can index, grouped into categories that can be switched on and off as a whole or item by
item. Switching a source off removes what it had indexed on the next sweep."""
from pathlib import Path

from . import apps, config, settings

AGENTS = {"claude": "Claude Code", "codex": "Codex", "qwen": "Qwen Code"}
CATEGORIES = [
    ("agents", "AI agent sessions", "Your conversations with coding agents, linked to the code they touched."),
    ("files", "Files & folders", "Documents in these folders: text, Markdown, code, PDF, Word, PowerPoint. Git repos inside them are indexed as repos."),
    ("repos", "Git repositories", "Code with git blame and history, including uncommitted changes and linked worktrees."),
    ("apps", "Apps", "Content that lives inside apps on this Mac."),
]


def _home(p) -> str:
    return str(p).replace(str(Path.home()), "~", 1)


def catalog(store, repos, files) -> list[dict]:
    on = settings.enabled
    with store.lock:
        db = store.db
        per_agent = dict(db.execute("SELECT source, COUNT(*) FROM sessions WHERE n_chunks>0 GROUP BY source").fetchall())
        per_root = dict(db.execute("SELECT root, COUNT(*) FROM files GROUP BY root").fetchall())
        per_repo = dict(db.execute("SELECT repo, COUNT(DISTINCT path) FROM code_chunks GROUP BY repo").fetchall())
        wts = dict(db.execute("SELECT repo, COUNT(*) FROM worktrees GROUP BY repo").fetchall())
        repo_rows = db.execute("SELECT root, name FROM repos ORDER BY name").fetchall()

    agents = [dict(id=f"agents/{k}", label=v, detail=_home(root), available=root.is_dir(), enabled=on(f"agents/{k}"),
                   count=per_agent.get(k, 0), unit="sessions")
              for k, root, _ in config.SOURCES for v in [AGENTS.get(k, k)]]

    folders = []
    for f in settings.get("folders"):
        p = Path(f).expanduser()
        folders.append(dict(id=f"files/{f}", label=p.name or f, detail=_home(p), available=p.is_dir(), enabled=on(f"files/{f}"),
                            count=per_root.get(str(p), 0), unit="files", removable=True))

    s = settings.load()
    skipped = getattr(repos, "skipped", [])
    repo_items = [
        dict(id="repos/discover", label="Discover git repositories",
             detail=f"Repos your agents worked in, and repos under {', '.join(s['sweep_roots'])} and your folders "
                    f"({s['sweep_depth']} levels deep, up to {s['max_repo_files']:,} files each)",
             available=True, enabled=on("repos/discover"), kind="rule"),
    ]
    for root, name in repo_rows:
        repo_items.append(dict(id=f"repos/{root}", label=name, detail=_home(root) + (f" · {wts[root]} worktrees" if wts.get(root) else ""),
                               available=Path(root).is_dir(), enabled=on(f"repos/{root}"), count=per_repo.get(root, 0), unit="files",
                               parent="repos/discover"))
    for sk in skipped:
        repo_items.append(dict(id=f"repos/{sk['root']}", label=Path(sk["root"]).name, detail=f"{_home(sk['root'])} · skipped: "
                               f"{sk['files']:,} files (switch on to index anyway)", available=True, enabled=False, count=0, unit="files",
                               parent="repos/discover"))
    for d in settings.get("disabled"):  # repos switched off no longer appear in the repos table; keep them listed
        if d.startswith("repos/") and d not in ("repos/sessions", "repos/sweep", "repos/discover") and not any(i["id"] == d for i in repo_items):
            r = d[6:]
            repo_items.append(dict(id=d, label=Path(r).name, detail=_home(r), available=Path(r).is_dir(), enabled=False, count=0,
                                   unit="files", parent="repos/discover"))

    app_items = []
    for k, a in apps.APPS.items():
        st = apps.state(k)
        roots = [str(p) for p in a["roots"]()] if a["available"]() else []
        app_items.append(dict(id=f"apps/{k}", label=a["label"], detail=st.get("error") or a["detail"], error=bool(st.get("error")),
                              available=a["available"](), enabled=on(f"apps/{k}"),
                              count=sum(n for r, n in per_root.items() if any(r == x or r.startswith(x + "/") for x in roots)), unit="items"))

    items = {"agents": agents, "files": folders, "repos": repo_items, "apps": app_items}
    return [dict(id=c, label=label, description=desc, enabled=on(c), items=items[c],
                 count=sum(i.get("count", 0) for i in items[c]), addable=c == "files")
            for c, label, desc in CATEGORIES]


def set_source(id: str, on: bool, repos=None):
    """Switch a category or item on/off. Switching on a repo the sweep skipped as too big adds it to `repos`."""
    valid = {c for c, _, _ in CATEGORIES}
    if id.split("/", 1)[0] not in valid:
        raise ValueError(f"unknown source {id!r}: ids start with {', '.join(sorted(valid))}")
    if on and id.startswith("repos/") and repos is not None:
        root = id[6:]
        if any(sk["root"] == root for sk in getattr(repos, "skipped", [])) and root not in settings.get("repos"):
            settings.update({"repos": settings.get("repos") + [root]})
    return settings.set_enabled(id, on)
