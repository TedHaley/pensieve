"""First run (and after a reset): what Pensieve is, who it is for, which sources to scan, what to index and which AI
engine writes insights. The app's setup sheet and the MCP setup tools both run these steps, so an agent that installs
Pensieve can ask the user the same questions in its conversation.

Nothing reads the user's folders before `scan`: the categories it is given are the only ones it looks in, so macOS
asks for folder access when the user has chosen to look, not when the app starts."""
import time
from pathlib import Path

from pensieve import actions, apps, config, llm, settings, sources

ABOUT = [
    "Pensieve searches everything on this Mac by meaning: documents, git repos and your AI agent sessions.",
    "Agents get the right context in one search through its MCP server, instead of a disk-wide grep.",
    "A map shows your work by topic, and git history shows who knows each part of the code.",
    "Everything is indexed and searched on this Mac. Nothing is uploaded.",
]

# Who the user is decides which categories are worth scanning; the scan then pre-checks what it finds in them.
PROFILES = [
    dict(id="developer", label="Software development", categories=["repos", "agents"],
         description="Code in git repos, with its history and who wrote what."),
    dict(id="agents", label="AI coding agents", categories=["agents", "repos"],
         description="Your Claude Code, Codex or Qwen Code conversations, linked to the code they changed."),
    dict(id="office", label="Documents and office work", categories=["files"],
         description="Word, PDF, PowerPoint, spreadsheets and text in Documents, Desktop and Downloads."),
    dict(id="notes", label="Research and notes", categories=["files", "apps"],
         description="Notes apps such as Obsidian and Apple Notes, and folders of Markdown."),
]

ENERGY_NOTE = ("Indexing runs the embedding model on your Mac until it finishes, so the Mac gets warm and uses more battery. "
               "Plug it in if you can. Search works while it indexes, and later changes are quick to index.")
REPO_FILES_PER_MINUTE = 500  # first index on Apple Silicon, limited by the embedding model
_cache = {}  # categories -> (time, candidates): repo discovery walks the disk
_scanned = set()  # categories the user has let Pensieve look in; `start` never reads any other


def _tilde(p) -> str:
    return str(p).replace(str(Path.home()), "~", 1)


def reads(category: str) -> str:
    """Where scanning a category looks, in words: the folders macOS may ask the user to allow."""
    s = settings.load()
    if category == "agents":
        return "Agent session folders: " + ", ".join(sorted({_tilde(root) for _, _, root, _ in config.session_sources()}))
    if category == "files":
        return "Your folders: " + ", ".join(s["folders"])
    if category == "repos":
        return (f"Folders under {', '.join(s['sweep_roots'])}, {s['sweep_depth']} levels deep, to find git repos. "
                "This includes Desktop and Documents.")
    return "The list of Obsidian vaults. Apple Notes is read only if you choose it, and macOS asks first."


def guide() -> dict:
    """Everything the first step needs, without reading any folder."""
    cats = {c: (label, desc) for c, label, desc in sources.CATEGORIES}
    return {"done": settings.get("setup_done"), "about": ABOUT, "profiles": PROFILES,
            "categories": [dict(id=c, label=label, description=desc, reads=reads(c)) for c, (label, desc) in cats.items()],
            "ai": [{k: o[k] for k in ("id", "label", "privacy", "available", "description", "note") if k in o}
                   for o in llm.options()],
            "energy": ENERGY_NOTE, "files_per_minute": REPO_FILES_PER_MINUTE}


def categories_for(profiles) -> list[str]:
    want = {c for p in PROFILES if p["id"] in set(profiles) for c in p["categories"]}
    return [c for c, _, _ in sources.CATEGORIES if c in want]


def scan(categories=None, fresh=False) -> list[dict]:
    """What can be indexed in these categories (all by default), with sizes. Only these categories' folders are read."""
    cats = tuple(c for c, _, _ in sources.CATEGORIES if categories is None or c in set(categories))
    _scanned.update(cats)
    hit = _cache.get(cats)
    if not fresh and hit and time.time() - hit[0] < 600:
        return hit[1]
    s = settings.load()
    on = settings.enabled
    items = {}
    if "agents" in cats:
        temp = ("-private-", "-tmp-", "-var-folders-") if s["skip_temp_sessions"] else ()
        items["agents"] = []
        for sid, name, root, pattern in config.session_sources():
            # a project folder is named after the session's working folder; parsing thousands of transcripts to read
            # their cwd would make this list slow, and sessions in temp folders are skipped at indexing anyway
            n = sum(1 for p in root.glob(pattern) if "subagents" not in p.parts
                    and not p.relative_to(root).parts[0].startswith(temp)) if root.is_dir() else 0
            items["agents"].append(dict(id=sid, label=sources.AGENTS.get(name, name), detail=_tilde(root), size=n,
                                        unit="sessions", available=n > 0, checked=n > 0 and on(sid)))
    if "files" in cats:
        items["files"] = [dict(id=f"files/{f}", label=Path(f).name or f, detail=_tilde(Path(f).expanduser()), size=None,
                               unit="", available=Path(f).expanduser().is_dir(),
                               checked=Path(f).expanduser().is_dir() and on(f"files/{f}")) for f in s["folders"]]
    if "repos" in cats:
        items["repos"] = _repos(s, on)
    if "apps" in cats:
        items["apps"] = [dict(id=f"apps/{k}", label=a["label"], detail=a["detail"], size=None, unit="",
                              available=a["available"](), checked=a["available"]() and on(f"apps/{k}"))
                         for k, a in apps.APPS.items()]
    data = [dict(id=c, label=label, description=desc, reads=reads(c), items=items[c])
            for c, label, desc in sources.CATEGORIES if c in cats]
    _cache[cats] = (time.time(), data)
    return data


def _repos(s, on):
    from pensieve.repos import git, norm_remote
    repos = actions.ctx["repos"]
    repos._extra = None
    found = repos._extra_roots()
    big = {sk["root"]: sk["files"] for sk in getattr(repos, "skipped", [])}
    out = [dict(id="repos/discover", label="Find new repos automatically",
                detail=f"Repos under {', '.join(s['sweep_roots'])} and your folders, and repos your agents work in, "
                       f"up to {s['max_repo_files']:,} files each", size=None, unit="", available=True,
                checked=on("repos/discover"), rule=True)]
    clones = {}  # one row per remote: Pensieve indexes a single clone of each (Repos.discover)
    for r in sorted(set(found) | set(big), key=lambda r: (len(Path(r).name), Path(r).name.lower())):
        clones.setdefault(norm_remote(git(r, "config", "--get", "remote.origin.url")) or r, []).append(r)
    for r, *others in sorted(clones.values(), key=lambda c: Path(c[0]).name.lower()):
        n = big.get(r) or git(r, "ls-files").count("\n")
        also = f" · also {len(others)} other clone{'s' if len(others) > 1 else ''}" if others else ""
        out.append(dict(id=f"repos/{r}", label=Path(r).name, detail=_tilde(Path(r)) + also, size=n, unit="files",
                        available=True, checked=r not in big and on(f"repos/{r}"), big=r in big,
                        clones=[f"repos/{o}" for o in others]))
    return out


def add(kind: str, path: str) -> str:
    """Add a folder the scan didn't find: 'files' (documents), 'repos' (a repo, or a folder of repos, whatever their
    size) or 'agents' (a folder of agent transcripts). Returns the source id to pass to `start`. ValueError if unusable."""
    p = Path(path).expanduser()
    if not p.is_dir():
        raise ValueError(f"not a folder: {path}")
    if kind == "files":
        cur = settings.get("folders")
        if path not in cur:
            settings.update({"folders": cur + [path]})
        sid = f"files/{path}"
    elif kind == "repos":
        cur = settings.get("repos")
        if not any(Path(r).expanduser() == p for r in cur):
            settings.update({"repos": cur + [_tilde(p)]})
        sid = f"repos/{p}"
    elif kind == "agents":
        found = config.detect_sessions(p)
        if not found:
            raise ValueError(f"No Claude Code, Codex or Qwen Code sessions found in {path}")
        name, root = found
        if (name, str(root)) not in {(n, str(r)) for _, n, r, _ in config.session_sources()}:
            settings.update({"session_folders": settings.get("session_folders") + [f"{name}:{_tilde(root)}"]})
        sid = next(s for s, n, r, _ in config.session_sources() if (n, str(r)) == (name, str(root)))
    else:
        raise ValueError(f"unknown kind {kind!r}: use files, repos or agents")
    _cache.clear()
    _scanned.add(kind)
    return sid


def index(path: str, cwd=None) -> dict:
    """Index a folder from now on, whatever it holds: a git repo, a folder of repos, a folder of agent sessions, or
    documents. Turns on that one source; when its whole category was off, the category's other sources stay off."""
    from pensieve.repos import find_repos
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd or Path.cwd()) / p
    p = p.resolve()
    if not p.is_dir():
        raise ValueError(f"not a folder: {path}")
    if (p / ".git").exists() or find_repos(p, 2, set(settings.get("exclude"))):
        kind, what = "repos", "git repo" if (p / ".git").exists() else "folder of git repos"
    elif config.detect_sessions(p):
        kind, what = "agents", "agent sessions"
    else:
        kind, what = "files", "documents"
    sid = add(kind, _tilde(p) if kind == "files" else str(p))
    off = [d for d in settings.get("disabled") if d != sid]
    if kind in off:  # the category was off: only this source comes on
        off.remove(kind)
        others = {"files": [f"files/{f}" for f in settings.get("folders")],
                  "agents": [s for s, _, _, _ in config.session_sources()], "repos": ["repos/discover"]}[kind]
        off += [o for o in others if o != sid and o not in off]
    settings.update({"disabled": off})
    _scanned.clear()
    return {"id": sid, "path": str(p), "indexed_as": what,
            "note": "Indexing starts within seconds; search covers it as it goes." if settings.get("setup_done")
            else "Pensieve isn't set up yet, so nothing is indexed until setup is done (setup_guide)."}


def start(chosen=None, recommended=False, ai=None) -> dict:
    """Index exactly the chosen source ids (or, with `recommended`, what the scan pre-checked): every other source is
    switched off, then indexing starts. Categories that were never scanned are switched off whole. `ai` picks the
    insights engine (an id from guide()["ai"], or "none")."""
    if chosen is None and not recommended:
        raise ValueError("Send the source ids to index, or recommended: true")
    if ai is not None and ai not in {o["id"] for o in llm.options()} | {"none", "auto"}:
        raise ValueError(f"unknown AI engine {ai!r}")
    if _scanned:
        cats = scan(sorted(_scanned))
    elif chosen is None or chosen:  # `pensieve setup <ids>` chooses without a scan first
        cats = scan()
    else:  # nothing chosen: no folder needs reading
        cats = []
    items = [i for c in cats for i in c["items"]]
    known = {i["id"] for i in items}
    chosen = {i["id"] for i in items if i["checked"]} if chosen is None else set(chosen)
    unknown = chosen - known
    if unknown:
        raise ValueError(f"unknown sources: {', '.join(sorted(unknown))}; scan lists them")
    all_cats = [c for c, _, _ in sources.CATEGORIES]
    off = [d for d in settings.get("disabled") if d not in known and d not in all_cats]
    off += [i for i in known if i not in chosen]
    off += [c for i in items if i["id"] not in chosen for c in i.get("clones", [])]  # else another clone stands in
    off = [d for d in off if d not in {c for i in items if i["id"] in chosen for c in i.get("clones", [])}]
    off += [c for c in all_cats if not any(i in chosen and i.startswith(c + "/") for i in known)]  # incl. never scanned
    big = [i["id"][6:] for i in items if i.get("big") and i["id"] in chosen]
    changes = {"disabled": off, "repos": list(dict.fromkeys(settings.get("repos") + big)), "setup_done": True}
    if ai is not None:
        changes["ai"] = ai
    settings.update(changes)
    _cache.clear()
    _scanned.clear()
    picked = [i for i in items if i["id"] in chosen and not i.get("rule")]
    files = sum(i["size"] or 0 for i in picked if i["unit"] == "files")
    return {"done": True, "indexing": [i["label"] for i in picked], "repo_files": files,
            "minutes": round(files / REPO_FILES_PER_MINUTE), "energy": ENERGY_NOTE}
