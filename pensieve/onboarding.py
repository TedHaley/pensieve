"""First run (and after a reset): what Pensieve is, who it is for, which sources to scan, what to index and which
model embeds them for search. The app's setup sheet and the MCP setup tools both run these steps, so an agent that installs
Pensieve can ask the user the same questions in its conversation.

Nothing reads the user's folders before `scan`: the categories it is given are the only ones it looks in, so macOS
asks for folder access when the user has chosen to look, not when the app starts."""
import time
from pathlib import Path

from pensieve import actions, apps, config, content, settings, sources

ABOUT = [
    "Pensieve follows your work across AI agent sessions, ties up the loose ends (the threads you left open), and helps you organize and track all of your ideas.",
    "Session mode, the default, reads your agent sessions and builds threads from them. Nothing else is indexed.",
    "Developer mode also indexes your git repos and files, and unlocks the Map, People and Insights.",
    "Everything runs on this Mac. Nothing is uploaded.",
]

# The mode decides which categories are scanned. Session mode reads only agent sessions. Developer mode also reads
# git repos and files into an index, so that sessions get richer context.
PROFILES = [
    dict(id="session", label="Session mode", categories=["agents"],
         description="Reads only your historic agent sessions (Claude Code, Codex, Qwen Code) and builds threads from "
                     "them. Nothing else is read or indexed."),
    dict(id="developer", label="Developer mode", categories=["agents", "repos", "files"],
         description="Also reads your git repos and your files into an index, so sessions get richer context. Unlocks "
                     "the Map, People and Insights."),
]

ENERGY_NOTE = ("Indexing uses a lot of power: it runs the embedding model on your Mac's GPU until it finishes, so the Mac "
               "gets warm and the battery drains faster. Plug it in if you can. When the first index is done, energy use "
               "drops back to normal: later changes take seconds to index. Search works while it indexes.")
REPO_FILES_PER_MINUTE = 500  # a rough guide before the scan; `start` estimates from file sizes and this Mac's speed
SESSION_CHUNKS = 35  # chunks in an average indexed agent session
KEY_NAMES = {"ctrl": "Control", "control": "Control", "shift": "Shift", "cmd": "Command", "command": "Command",
             "alt": "Option", "opt": "Option", "option": "Option", "space": "Space"}


def next_steps() -> str:
    keys = "+".join(KEY_NAMES.get(k, k.upper()) for k in settings.get("hotkey").lower().split("+"))
    return (f"{'Tap' if keys in ('Control+Shift', 'Shift+Control') else 'Press'} {keys} anywhere to search with "
            f"Pensieve. To explore what it indexed, choose Open Map in the Pensieve menu bar icon, or go to "
            f"http://localhost:{config.PORT}.")
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
            "energy": ENERGY_NOTE, "files_per_minute": REPO_FILES_PER_MINUTE,
            "embed_model": config.EMBED_MODEL,
            "embed_models": [dict(id=k, label=m["label"], about=m["about"]) for k, m in config.EMBED_MODELS.items()],
            "content": content_types(), "text_engine": settings.get("text_engine"), "engines": engines()}


def engines() -> list[dict]:
    """The text engines that setup offers, with why one can't run on this Mac."""
    from pensieve import engine
    claude = None if config.find_exe("claude") else "Claude Code is not installed."
    return [dict(id="cli", label="Claude Code (Haiku)", command=engine.TEMPLATES[0]["command"], unavailable=claude,
                 about="Your Claude Code writes them with its smallest model, one job at a time. It uses your Claude plan."),
            dict(id="builtin", label="Built-in Qwen 3.5 9B", unavailable=engine.builtin_unsupported(),
                 about="Runs on this Mac, and nothing leaves it. Pensieve starts it when there is work and stops it "
                       "after 10 minutes without work. It downloads about 5 GB once, or uses LM Studio's copy."),
            dict(id="server", label="Model server", unavailable=None,
                 about="A server that you run, such as LM Studio or Ollama, or a hosted API. Set it in Settings → Models.")]


CONTENT_NOTES = {  # what switching a kind on costs; speeds measured on an M4 Max
    "images": "Downloads 335 MB. Reads about 8 images a second.",
    "video": "Downloads 335 MB (the same download as images). Reads an hour of video in about 6 minutes.",
    "audio": "Downloads 611 MB. Reads an hour of sound in about 30 seconds.",
}


def content_types() -> list[dict]:
    """What Pensieve reads in the chosen sources. Text is always read; images, video and audio are choices (start's
    `content`), off by default: then those files are found by name."""
    out = []
    for k in content.status():
        o = dict(id=k["id"], label=k["label"], on=k["on"], description=k["description"],
                 available=k["id"] == "text" or k.get("needs") != "decoder")
        if k["id"] != "text":
            o.update(needs_model=k["needs_model"], note=CONTENT_NOTES[k["id"]])
        if k.get("needs") == "decoder":
            o["note"] = "Needs the Pensieve app (its video and sound decoder), or PyAV: pip install av."
        out.append(o)
    return out


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
            done = _indexed_sessions(root)
            items["agents"].append(dict(id=sid, label=sources.AGENTS.get(name, name), detail=_tilde(root), size=n,
                                        unit="sessions", available=n > 0, checked=n > 0 and on(sid), indexed=min(done, n),
                                        seconds=_seconds(max(0, n - done) * SESSION_CHUNKS)))
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
    from pensieve import places
    repos = actions.ctx["repos"]
    repos._extra = None
    found = repos._extra_roots()
    big = {sk["root"]: sk["files"] for sk in getattr(repos, "skipped", [])}
    # the repos the user's agents work in, from their transcripts: offered first, and checked even when big
    used = {o["root"]: o["sessions"] for o in places.compute(places.transcript_rows(), lambda r, k: {"state": "unknown"})
            if o["kind"] == "repo"}
    found = list(dict.fromkeys([*sorted(used, key=used.get, reverse=True), *found]))
    out = [dict(id="repos/discover", label="Find new repos automatically",
                detail=f"Repos your agents work in, and repos under {', '.join(s['sweep_roots'])} and your folders. "
                       f"Found repos with more than {s['max_repo_files']:,} files and no agent use are listed for you "
                       f"to choose, not indexed on their own", size=None, unit="", available=True,
                checked=on("repos/discover"), rule=True)]
    clones = {}  # one row per remote: Pensieve indexes a single clone of each (Repos.discover)
    for r in sorted(set(found) | set(big), key=lambda r: (len(Path(r).name), Path(r).name.lower())):
        clones.setdefault(norm_remote(git(r, "config", "--get", "remote.origin.url")) or r, []).append(r)
    agent_n = lambda c: max(used.get(x, 0) for x in c)
    for r, *others in sorted(clones.values(), key=lambda c: (-agent_n(c), Path(c[0]).name.lower())):
        est = repos.estimate_chunks(r)
        also = f" · also {len(others)} other clone{'s' if len(others) > 1 else ''}" if others else ""
        n = agent_n([r, *others])
        work = f" · your agents worked here in {n} session{'s' if n > 1 else ''}" if n else ""
        out.append(dict(id=f"repos/{r}", label=Path(r).name, detail=_tilde(Path(r)) + also + work,
                        size=big.get(r) or est["files"], unit="files", available=True, agents=n,
                        checked=(r not in big or n > 0) and on(f"repos/{r}"), big=r in big,
                        clones=[f"repos/{o}" for o in others], indexed=est["indexed"], seconds=_seconds(est["chunks"])))
    return out


def _indexed_sessions(root) -> int:
    store = actions.ctx["store"]
    return store.db.execute("SELECT COUNT(*) FROM sessions WHERE n_chunks>0 AND path LIKE ?",
                            (str(root) + "/%",)).fetchone()[0]


def _seconds(chunks) -> int:
    """How long embedding this many chunks takes on this Mac."""
    return round(chunks / actions.ctx["repos"].embed_rate())


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


def start(chosen=None, recommended=False, embed_model=None, kinds=None, text_engine=None) -> dict:
    """Index exactly the chosen source ids (or, with `recommended`, what the scan pre-checked): every other source is
    switched off, then indexing starts. Categories that were never scanned are switched off whole. `embed_model` picks
    the search model (guide()["embed_models"]).
    On an empty index the search model changes at once; otherwise the switch runs in the background (reembed).
    `kinds`: which of images, video and audio to read by content (content.KINDS); the others are found by name. None
    leaves them as they are. They need the EmbeddingGemma 2 search model: with kinds and no `embed_model`, that
    model is chosen. Their model parts download in the background. `text_engine`: what writes notes (engines())."""
    if chosen is None and not recommended:
        raise ValueError("Send the source ids to index, or recommended: true")
    if text_engine:
        from pensieve import engine
        settings.update({"text_engine": text_engine, **({"cli_command": settings.get("cli_command") or
                                                          engine.TEMPLATES[0]["command"]} if text_engine == "cli" else {})})
    unknown = set(kinds or ()) - set(content.KINDS)
    if unknown:
        raise ValueError(f"unknown content {', '.join(sorted(unknown))}; one of: {', '.join(content.KINDS)}")
    if kinds and (embed_model or config.EMBED_MODEL) != config.GEMMA:
        if embed_model:
            raise ValueError(f"{', '.join(kinds)} need the {config.GEMMA} search model, not {embed_model}")
        embed_model = config.GEMMA
    if kinds is not None:
        settings.update({"file_types": {k: {"index": "content" if k in kinds else "name", "map": k in kinds}
                                        for k in content.KINDS}})
    if embed_model:
        if embed_model not in config.EMBED_MODELS:
            raise ValueError(f"unknown search model {embed_model!r}; one of: {', '.join(config.EMBED_MODELS)}")
        from pensieve import reembed
        settings.update({"embed_model": embed_model})
        c = actions.ctx
        reembed.switch_empty(c["store"], [c["store"], c["repos"], c["files"]])
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
    # added to the repos setting: big repos, which discovery skips, and repos found only in agent transcripts
    big = [i["id"][6:] for i in items if (i.get("big") or i.get("agents")) and i["id"] in chosen]
    changes = {"disabled": off, "repos": list(dict.fromkeys(settings.get("repos") + big)), "setup_done": True}
    settings.update(changes)
    _cache.clear()
    _scanned.clear()
    picked = [i for i in items if i["id"] in chosen and not i.get("rule")]
    files = sum(i["size"] or 0 for i in picked if i["unit"] == "files")
    secs = sum(i.get("seconds") or 0 for i in picked)
    return {"done": True, "indexing": [i["label"] for i in picked], "repo_files": files,
            "indexed_files": sum(i.get("indexed") or 0 for i in picked if i["unit"] == "files"),
            "minutes": -(-secs // 60), "documents": any(i["id"].startswith(("files/", "apps/")) for i in picked),
            "energy": ENERGY_NOTE, "next": next_steps()}
