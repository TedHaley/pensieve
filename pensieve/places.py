"""Where the user's agents work: the git repos and folders that their sessions ran in, and the files those sessions
read and edited (session_files). Ranked by use, these show what is most worth indexing: setup and Settings → Sources
suggest the ones that aren't indexed, and the repo sweep indexes the busiest first (ROADMAP B11)."""
import time
from pathlib import Path

from pensieve.projects import project_root

HOME = Path.home()
SKIP = ("/tmp/", "/private/", "/var/", "/System/", "/Library/", "/Applications/", "/opt/", "/usr/")
TTL = 300  # seconds a computed list is reused


def _folder(d: Path) -> Path | None:
    """A non-repo place: the folder two levels under home (~/Desktop/notes), or None for what isn't worth suggesting
    (home itself, ~/Library, dot folders such as ~/.claude, whose sessions and memory Pensieve reads already)."""
    try:
        rel = d.relative_to(HOME)
    except ValueError:
        return None
    if not rel.parts or rel.parts[0].startswith(".") or rel.parts[0] == "Library":
        return None
    return HOME / Path(*rel.parts[:2])


ROWS = ("SELECT s.id, s.project, s.updated, f.path, f.edited FROM sessions s "
        "LEFT JOIN session_files f ON f.session_id = s.id WHERE s.n_chunks > 0")


def compute(rows, state) -> list[dict]:
    """Every place the agents worked: {root, name, kind (repo | folder), sessions, files, edited, last, state}, busiest
    first. `rows` are ROWS's; `state(root, kind)` says whether a place is indexed, the way repos.check does (a dict
    with `state`, and for a clone the `root` of the repo it is indexed as)."""
    where, out = {}, {}

    def place(d: str):
        if d not in where:
            if not d or d.startswith(SKIP) or not d.startswith("/"):
                where[d] = None
            else:
                root = project_root(d)
                where[d] = (root, "repo") if root else ((str(f), "folder") if (f := _folder(Path(d))) else None)
        return where[d]

    for sid, cwd, updated, path, edited in rows:
        for d, file in ((cwd, None), (str(Path(path).parent) if path else None, path)):
            p = place(d) if d else None
            if not p:
                continue
            o = out.setdefault(p, dict(root=p[0], name=Path(p[0]).name, kind=p[1], sessions=set(), files=set(),
                                       edited=set(), last=""))
            o["sessions"].add(sid)
            o["last"] = max(o["last"], updated or "")
            if file:
                o["files"].add(file)
                if edited:
                    o["edited"].add(file)
    res = []
    for o in out.values():
        if not Path(o["root"]).is_dir():  # a deleted project
            continue
        st = state(o["root"], o["kind"])
        res.append(dict(o, sessions=len(o["sessions"]), files=len(o["files"]), edited=len(o["edited"]),
                        state=st["state"], indexed_as=st.get("root") if st.get("root") != o["root"] else None))
    res.sort(key=lambda o: (o["sessions"], o["files"], o["last"]), reverse=True)
    return res


def transcript_rows() -> list[tuple]:
    """ROWS's shape from the transcript files alone, before anything is indexed (setup): one row per session, with
    its working folder and time, and no files. Claude Code and Qwen Code name each project folder after the working
    folder; a Codex transcript names it in its first line."""
    import json
    from datetime import datetime, timezone
    from pensieve import config
    from pensieve.parsers import _decode
    folders, rows = {}, []
    for _, name, root, pattern in config.session_sources():
        for f in root.glob(pattern):
            if "subagents" in f.parts:
                continue
            if name == "codex":
                try:
                    with open(f, errors="replace") as fh:
                        cwd = ((json.loads(fh.readline() or "{}").get("payload") or {}).get("cwd")) or ""
                except (OSError, ValueError):
                    cwd = ""
            else:
                proj = f.relative_to(root).parts[0]
                if proj not in folders:  # decoding tries many folders on disk: once per project, and never for a
                    # folder under SKIP (a temp folder), which compute() leaves out anyway
                    skip = proj.startswith(tuple(s.rstrip("/").replace("/", "-") + "-" for s in SKIP))
                    folders[proj] = "" if skip else _decode(proj)
                cwd = folders[proj]
            when = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat()
            rows.append((str(f), cwd, when, None, None))
    return rows


_cache = (0.0, None)


def places(fetch, state, fresh=False) -> list[dict]:
    """compute on fetch()'s rows, reused for TTL seconds: it runs git once per repo."""
    global _cache
    if fresh or _cache[1] is None or time.time() - _cache[0] > TTL:
        _cache = (time.time(), compute(fetch(), state))
    return _cache[1]


def busiest_first(roots, ranked) -> list:
    """`roots` with the ones used most in `ranked` (places' list) first; the rest keep their order."""
    rank = {o["root"]: i for i, o in enumerate(ranked)}
    return sorted(roots, key=lambda r: rank.get(str(r), len(rank)))
