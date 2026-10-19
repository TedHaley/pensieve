"""The Projects tab: projects as groups of tickets with their branches and pull requests (project_model.py).

The build runs in a background thread, at most every REBUILD_SECONDS unless asked, and keeps its result in
projects/ticket_projects.json. A warm build takes well under a second: connection items are read again only when a
collected file changes, gh results are cached for project_model.PR_TTL, and git and the context cards have their own
caches. The global store lock is held only for a few short reads."""
import re
import threading
import time
from collections import Counter

from pensieve import project_model, settings

DIR = project_model.DIR
OUT = DIR / "ticket_projects.json"
VERSION = 2
REBUILD_SECONDS = 120

_lock = threading.RLock()
_state = {"data": None, "building": False, "error": None, "started": 0.0}
listeners = []             # called (from a worker thread) when a new build is ready


def stale_days() -> int:
    try:
        return int(settings.get("projects_stale_days") or project_model.STALE_DAYS)
    except (KeyError, TypeError, ValueError):
        return project_model.STALE_DAYS


def _to_json(b: project_model.Build) -> dict:
    return dict(v=VERSION, built=b.built, took=b.took, me=b.me, counts=b.counts, stale_days=b.stale_days,
                projects=[project_model.to_dict(p) for p in b.projects])


def data() -> dict | None:
    with _lock:
        if _state["data"] is None:
            d = project_model._read(OUT, None)
            _state["data"] = d if d and d.get("v") == VERSION else None
        return _state["data"]


def rebuild(store, force=False) -> bool:
    """Start a background build, unless one is running or the last one is new enough. A forced build asked for
    while one runs follows it."""
    with _lock:
        _state["store"] = store
        d = data()
        fresh = d and time.time() - d.get("built", 0) < REBUILD_SECONDS and d.get("stale_days") == stale_days()
        if _state["building"]:
            _state["again"] = _state.get("again") or force
            return False
        if fresh and not force:
            return False
        _state["building"] = True
        _state["started"] = time.time()

    def run():
        while True:
            try:
                d = _to_json(project_model.build(store, stale_days=stale_days()))
                with _lock:
                    _state["data"] = d
                project_model._write(OUT, d)
                _state["error"] = None
            except Exception as e:
                import traceback
                traceback.print_exc()
                _state["error"] = str(e)[:300]
            with _lock:
                if not _state.pop("again", False):
                    _state["building"] = False
                    break
        for fn in listeners:
            fn()
    threading.Thread(target=run, daemon=True, name="projects").start()
    return True


def _prs_fetched():
    if _state.get("store") is not None:
        rebuild(_state["store"], force=True)


project_model.on_prs.append(_prs_fetched)


def _text(e: dict) -> str:
    return " ".join([e["name"], e["ref"], " ".join(m["name"] for m in e["members"]),
                     " ".join(f"{t['key']} {t['title']}" for t in e["tickets"]),
                     " ".join(f"{p['title']} {p['repo']} {p['head']}" for p in e["prs"])]).lower()


def matching(ps: list[dict], q: str) -> list[dict]:
    """Projects whose name, tickets, pull requests or people have all the words of `q`."""
    words = [w for w in re.findall(r"[\w.-]+", q.lower()) if w]
    return [e for e in ps if all(w in _text(e) for w in words)]


def summary(q: str = "") -> dict:
    """GET /api/projects: the user's projects, then the others; each with its tickets, pull requests and people."""
    d = data()
    if not d:
        return dict(projects=[], building=_state["building"], error=_state["error"], built=None)
    ps = d["projects"]
    if q.strip():
        ps = matching(ps, q)
    return dict(built=d["built"], took=d.get("took"), counts=d.get("counts"), stale_days=d.get("stale_days"),
                building=_state["building"], error=_state["error"], me=d.get("me"), projects=ps)


def project(pid: str) -> dict | None:
    """GET /api/projects/<id>: one project, with the ties that put each ticket's branches and pull requests in it."""
    d = data()
    return next((x for x in (d or {}).get("projects", []) if x["id"] == pid), None)


def find(name_or_id: str) -> dict | None:
    """A project by its id, a ticket key in it, or words of its name."""
    d = data() or {}
    ps = d.get("projects", [])
    s = name_or_id.strip()
    hit = next((p for p in ps if p["id"] == s or p["ref"] == s), None) or \
        next((p for p in ps if any(t["key"] == s.upper() for t in p["tickets"])), None)
    if hit:
        return hit
    hits = matching(ps, s)
    return hits[0] if hits else None


def best_for(ids: list[str]) -> list[dict]:
    """The projects whose context cards hold most of these Map items' sessions: the Map's "Open as project"."""
    d = data()
    sids = {i.split(":", 1)[1] for i in ids if i.startswith("session:")}
    if not d or not sids:
        return []
    n = Counter()
    for p in d["projects"]:
        k = len(sids & set(p.get("sessions") or []))
        if k:
            n[p["id"]] = k
    by = {p["id"]: p for p in d["projects"]}
    return [dict(id=k, name=by[k]["name"], n=c, share=round(c / len(sids), 3)) for k, c in n.most_common(3)]
