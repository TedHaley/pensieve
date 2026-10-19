"""The move from v1 to storage v2 (docs/design/storage.md, "Migration" and "Cutover").

v2 starts clean in DATA_DIR/v2 and indexes again. On the first start it imports from v1 only what cannot be made again
or costs time or money to make again. Each import is one small step that is allowed to fail; `migration_steps` in
store.db keeps each step's result, so a migration that stops goes on at the next start with the steps not done.
When the first full v2 index is done (sessions, files, repos and documents each report one full pass), `delete_v1`
removes the v1 data, once.

PENSIEVE_MIGRATE_DRY_RUN=1: report what would be imported and deleted, and write nothing (not even DATA_DIR/v2)."""
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from pensieve import config, models
from pensieve.models import StepStatus

MAX_TRIES = 3        # a step that fails this many times is given up, and no longer keeps its v1 files
V1_NOTE_VERSION = 10  # the note format of v1's last release (work.NOTE_VERSION); older notes are written again anyway
PARTS = ("sessions", "files", "repos", "documents")  # each reports its first full v2 pass with mark_indexed
DELETED_MARK = "v1-deleted.json"  # in DATA_DIR/v2: delete_v1 ran, and runs no more

# What delete_v1 removes from the v1 data folder: exactly these names (and the *.tv files at its top level).
DELETE = ("pensieve.db", "pensieve.db-wal", "pensieve.db-shm", "index.tv", "code.tv", "files.tv", "connections",
          "people", "projects", "threads", "git", "work_notes.json", "work_done.json", "usage.db", "usage.db-wal",
          "usage.db-shm", "agent_jobs.db", "agent_jobs.db-wal", "agent_jobs.db-shm", "index.db")
KEEP_IN_CONNECTIONS = {".oauth"}  # sign-in tokens of the user's own MCP connections (mcp_oauth.py)
# The v1 files that a step reads: kept until the step is done or given up.
PROTECTS = {"person_overrides": ("people",), "org_evidence": ("people",), "card_states": ("pensieve.db",),
            "embed_cache": ("pensieve.db",), "card_notes": ("work_notes.json",), "card_done": ("work_done.json",),
            "usage": ("usage.db",), "doc_paths": ("connections",)}
# Paths that v1 code in this process still uses: delete_v1 keeps them, so it never removes a live index.
LIVE = (("pensieve.config", "DB_PATH"), ("pensieve.config", "INDEX_PATH"), ("pensieve.repos", "CODE_INDEX_PATH"),
        ("pensieve.repos", "SHADOW_DIR"), ("pensieve.files", "FILES_INDEX_PATH"), ("pensieve.connections", "DIR"),
        ("pensieve.people", "DIR"), ("pensieve.team_projects", "DIR"), ("pensieve.usage", "PATH"),
        ("pensieve.agent_jobs", "DB"), ("pensieve.work", "_notes_path"), ("pensieve.work", "_done_path"),
        ("pensieve.history", "_dir"))

_lock = threading.Lock()
_deleting = threading.Lock()
_marked: set[str] = set()
progress: dict = {"phase": "unknown"}  # for /api/status; see view()
listeners: list = []  # fn() after each change of progress


def log(msg: str):
    print(f"migrate: {msg}", flush=True)


def dry_run() -> bool:
    return os.environ.get("PENSIEVE_MIGRATE_DRY_RUN") == "1"


def v1_dir() -> Path:
    return config.DATA_DIR


def _set(**kw):
    progress.update(kw)
    for fn in list(listeners):
        try:
            fn()
        except Exception:
            traceback.print_exc()


def v1_detected(v1: Path | None = None) -> bool:
    """v1 data is here and delete_v1 has not finished: pensieve.db exists, and DATA_DIR/v2 has no deleted mark."""
    v1 = v1 or v1_dir()
    return (v1 / "pensieve.db").exists() and not (v1 / "v2" / DELETED_MARK).exists()


def _final(s) -> bool:
    return s is not None and (s.status == StepStatus.DONE or (s.status == StepStatus.FAILED and s.tries >= MAX_TRIES))


def pending_steps(store) -> list[str]:
    """The import steps that are not done (nor given up)."""
    from pensieve.store import migration
    have = migration.steps(store)
    return [s.name for s in STEPS if not _final(have.get(s.name))]


def _mtime(p: Path) -> int:
    try:
        return int(p.stat().st_mtime)
    except OSError:
        return models.now()


def _json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except FileNotFoundError:
        return default


def _str(v) -> str | None:
    if v is None or isinstance(v, str):
        return v
    return json.dumps(v)


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def read_person_overrides(v1: Path) -> list:
    """people/overrides.json: {merge: [[a, b]], split: [key], me: [key]}, the user's choices on the People tab."""
    p = v1 / "people" / "overrides.json"
    o, at, out = _json(p, {}), _mtime(p), []
    for pair in o.get("merge") or []:
        if isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, str) and x for x in pair):
            out.append(models.PersonOverride(None, models.OverrideAction.MERGE, pair[0], pair[1], at))
    for k in o.get("split") or []:
        if isinstance(k, str) and k:
            out.append(models.PersonOverride(None, models.OverrideAction.SPLIT, k, None, at))
    for k in o.get("me") or []:
        if isinstance(k, str) and k:
            out.append(models.PersonOverride(None, models.OverrideAction.ME, k, None, at))
    return out


V1_STATES = {"discarded": "forgotten"}  # task_states.RENAMED


def read_card_states(v1: Path) -> list:
    """pensieve.db task_states, the rows that the user set (an agent's or the automatic state is made again)."""
    db = _ro(v1 / "pensieve.db")
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'task_states'").fetchone():
            return []
        rows = db.execute("SELECT task_id, state, reason, at FROM task_states WHERE by = 'user' ORDER BY at").fetchall()
    finally:
        db.close()
    states = {s.value for s in models.StateSet}
    out = []
    for task, state, reason, at in rows:
        state = V1_STATES.get(state, state)
        if task and state in states:
            out.append(models.CardStateSet(None, task, models.StateSet(state), models.SetBy.USER, reason, int(at or 0)))
    return out


def read_feedback(v1: Path) -> list:
    """feedback.jsonl: bug reports and ideas. An agent's name and version go into the text."""
    p, out = v1 / "feedback.jsonl", []
    if not p.exists():
        return out
    for line in p.read_text(errors="replace").splitlines():
        try:
            x = json.loads(line)
        except ValueError:
            continue
        kind, status = x.get("kind"), x.get("status") or "new"
        if kind not in ("bug", "idea") or status not in ("new", "sent", "dismissed") or not x.get("summary"):
            continue
        src = x.get("source") or "user"
        text = x["summary"] + (f"\n\n{x['details']}" if x.get("details") else "")
        if src != "user":
            text += f"\n\n(from {src}" + (f", Pensieve {x['version']}" if x.get("version") else "") + ")"
        out.append(models.Feedback(None, int(x.get("ts") or 0), models.FeedbackBy.USER if src == "user" else
                                   models.FeedbackBy.AGENT, models.FeedbackKind(kind), text,
                                   models.FeedbackStatus(status)))
    return out


NOTE_FIELDS = ("title", "problem", "goal", "state", "next", "question")


def read_card_notes(v1: Path) -> list:
    """work_notes.json: {card id: note}, the AI notes of the cards. Only notes of v1's last format: an older note
    is written again in any case."""
    out = []
    for card, n in (_json(v1 / "work_notes.json", {}) or {}).items():
        if not isinstance(n, dict) or n.get("v") != V1_NOTE_VERSION or not n.get("sig"):
            continue
        extra = {k: v for k, v in n.items() if k not in NOTE_FIELDS + ("v", "sig", "at")}
        out.append(models.CardNote(card, V1_NOTE_VERSION, *(_str(n.get(k)) for k in NOTE_FIELDS), n["sig"], "v1",
                                   int(n.get("at") or 0), json.dumps(extra)))
    return out


def read_card_done(v1: Path) -> list:
    """work_done.json: {card id: {key, done: {item: why}}}, the note items that a later commit did."""
    p = v1 / "work_done.json"
    at, out = _mtime(p), []
    for card, d in (_json(p, {}) or {}).items():
        for item, why in ((d or {}).get("done") or {}).items():
            out.append(models.CardDone(card, item, at, _str(why) or ""))
    return out


def _gh(login: str) -> str:
    login = (login or "").strip().lower()
    return login if ":" in login else f"gh:{login}"


def read_org_evidence(v1: Path) -> list:
    """people/org_evidence.json: pull requests with their reviewers, Slack titles and first commits, kept across
    builds. Manager-title and team counts are not kept: v2 derives them from the titles and the graph."""
    ev = _json(v1 / "people" / "org_evidence.json", {}) or {}
    if ev.get("v") != 1:
        return []
    prs, reviews, titles, first = [], [], [], []
    for url, p in (ev.get("prs") or {}).items():
        at = int(p.get("ts") or 0) or None
        prs.append(models.OrgPullRequest(url, p.get("repo") or "", _gh(p["author"]) if p.get("author") else None,
                                         None, at))
        for who, state in [(x, models.ReviewState.APPROVED) for x in p.get("approvers") or []] + \
                          [(x, models.ReviewState.COMMENTED) for x in p.get("reviewers") or []]:
            reviews.append(models.OrgReview(url, _gh(who), state, at))
    for key, seen in (ev.get("titles") or {}).items():
        for title, s in (seen or {}).items():
            titles.append(models.OrgTitle(key, title, int(s.get("first") or 0), int(s.get("last") or 0),
                                          int(s.get("n") or 0)))
    for key, at in (ev.get("first") or {}).items():
        if at:
            first.append(models.OrgFirstCommit(key, int(at)))
    return prs + reviews + titles + first


def _usage_model():
    """The model of usage_calls (phase 4), or None while this build has none."""
    return getattr(models, "UsageCall", None)


def read_usage(v1: Path) -> list[dict]:
    """usage.db calls: the log of MCP tool calls, as dicts of v1 columns."""
    p = v1 / "usage.db"
    if not p.exists() or not p.stat().st_size:
        return []
    db = _ro(p)
    db.row_factory = sqlite3.Row
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'calls'").fetchone():
            return []
        return [dict(r) for r in db.execute("SELECT * FROM calls ORDER BY id")]
    finally:
        db.close()


def _usage_rows(calls: list[dict], cls) -> list:
    """v1 calls as rows of `cls`: the fields of the same name; `at` from v1's `ts`; ids given again."""
    from dataclasses import fields
    from typing import get_type_hints
    hints, out = get_type_hints(cls), []
    for c in calls:
        kw = {}
        for f in fields(cls):
            v = None if f.name == "id" else c.get(f.name, c.get("ts") if f.name == "at" else None)
            t = [a for a in (getattr(hints[f.name], "__args__", None) or (hints[f.name],)) if a is not type(None)][0]
            if v is not None and t is int:
                v = int(v)
            elif v is not None and t is bool:
                v = bool(v)
            kw[f.name] = v
        out.append(cls(**kw))
    return out


def read_doc_paths(v1: Path) -> list:
    """connections/<sid>/<name>.md: the key in each item's front matter, so that an old file id finds its document."""
    root, out = v1 / "connections", []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        for md in sorted(d.glob("*.md")):
            key = _front_key(md)
            if key:
                out.append(models.V1DocPath(f"{d.name}/{md.stem}", d.name, key))
    return out


def _front_key(md: Path) -> str | None:
    try:
        with md.open(errors="replace") as f:
            if f.readline().strip() != "---":
                return None
            for _ in range(40):
                line = f.readline()
                if not line or line.strip() == "---":
                    return None
                if line.startswith("key: "):
                    v = line[5:].strip()
                    try:
                        v = json.loads(v)
                    except ValueError:
                        pass
                    return str(v) if v not in (None, "") else None
    except OSError:
        return None
    return None


def _embedcache():
    """embedcache.fill_from_v1 (phase 6), or None while this build has none."""
    try:
        from pensieve import embedcache
    except ImportError:
        return None
    return getattr(embedcache, "fill_from_v1", None)


def count_v1_vectors(v1: Path) -> dict[str, int]:
    """The v1 chunks with a vector: what fill_from_v1 can give embed_cache (before it removes repeated texts)."""
    db, out = _ro(v1 / "pensieve.db"), {}
    try:
        for t in ("chunks", "code_chunks", "file_chunks"):
            try:
                out[t] = db.execute(f"SELECT COUNT(*) FROM {t} WHERE vec IS NOT NULL").fetchone()[0]
            except sqlite3.Error:
                out[t] = 0
    finally:
        db.close()
    return out


@dataclass
class Step:
    name: str
    about: str
    read: object                          # fn(v1) -> rows
    same: tuple[str, ...] = ()            # columns that make a row the same as one already imported
    write: object = None                  # fn(store, rows) -> int, for a step that does not use add_new
    available: object = field(default=lambda: True)  # False: its v2 part is not in this build (the step waits)


def _write_org(store, objs) -> int:
    from pensieve.store import migration
    keys = {models.OrgPullRequest: ("url",), models.OrgReview: ("url", "reviewer_key", "state"),
            models.OrgTitle: ("key", "title"), models.OrgFirstCommit: ("key",)}
    return sum(migration.add_new(store, [o for o in objs if type(o) is cls], same) for cls, same in keys.items())


def _write_usage(store, calls) -> int:
    from pensieve.store import migration
    cls = _usage_model()
    same = tuple(k for k in ("at", "tool", "conn", "args") if k in models.columns(cls))
    return migration.add_new(store, _usage_rows(calls, cls), same)


def _write_embed(store, _) -> int:
    return _embedcache()(v1_dir() / "pensieve.db")


def _write_doc_paths(store, objs) -> int:
    from pensieve.store import migration
    return migration.put_all(store, objs, ("path",))


STEPS = [
    Step("person_overrides", "person merges, splits and 'This is me'", read_person_overrides, ("action", "a", "b")),
    Step("card_states", "card states set by the user", read_card_states, ("card_id", "state", "at")),
    Step("feedback", "bug reports and ideas", read_feedback, ("at", "text")),
    Step("card_notes", "AI notes of the cards", read_card_notes, ("card_id",)),
    Step("card_done", "note items that a commit did", read_card_done, ("card_id", "item")),
    Step("org_evidence", "org chart evidence", read_org_evidence, write=_write_org),
    Step("usage", "MCP tool call history", read_usage, write=_write_usage, available=lambda: _usage_model() is not None),
    Step("doc_paths", "old file ids of connection items", read_doc_paths, write=_write_doc_paths),
    Step("embed_cache", "vectors of v1 chunks, by the hash of their text", lambda v1: [], write=_write_embed,
         available=lambda: _embedcache() is not None),
]


def import_v1(v1: Path, store, dry: bool | None = None) -> dict[str, dict]:
    """Run the import steps that are not done. Each step reads v1, writes its rows in one job, and records its
    result; a step that fails is logged and the next one runs. With `dry`, nothing is written: the result says what
    each step would import. Returns {step: {status, count, error}}."""
    from pensieve.store import migration
    dry = dry_run() if dry is None else dry
    have = {} if dry else migration.steps(store)
    todo = [s for s in STEPS if not _final(have.get(s.name))]
    out = {s: dict(status=st.status.value, count=st.count, error=st.error) for s, st in have.items()}
    _set(phase="dry_run" if dry else "importing", done=len(STEPS) - len(todo), total=len(STEPS))
    for i, s in enumerate(todo):
        _set(step=s.name, about=s.about)
        t = time.time()
        try:
            objs = s.read(v1)
            if dry:
                r = dict(status="would import" if s.available() else "waits for its v2 part", count=len(objs))
                if s.name == "embed_cache":
                    r["by_table"] = count_v1_vectors(v1)
                    r["count"] = sum(r["by_table"].values())
            elif not s.available():
                st = migration.set_step(store, s.name, StepStatus.SKIPPED, 0, "not in this build yet")
                r = dict(status=st.status.value, count=0, error=st.error, read=len(objs))
            else:
                n = s.write(store, objs) if s.write else migration.add_new(store, objs, s.same)
                migration.set_step(store, s.name, StepStatus.DONE, n)
                r = dict(status="done", count=n, read=len(objs))
        except Exception as e:
            traceback.print_exc()
            r = dict(status="failed", count=0, error=repr(e)[:300])
            if not dry:
                try:
                    migration.set_step(store, s.name, StepStatus.FAILED, 0, r["error"])
                except Exception:
                    traceback.print_exc()
        out[s.name] = r
        log(f"{s.name}: {r['status']}, {r.get('count')} rows" + (f" of {r['read']} read" if r.get("read") else "")
            + (f" ({r['error']})" if r.get("error") else "") + f" in {time.time() - t:.1f} s")
        _set(done=len(STEPS) - len(todo) + i + 1, steps=out)
    return out


def mark_indexed(part: str):
    """A part (sessions, files, repos, documents) finished a full pass. The first one is kept in index.db's meta;
    when all parts have one, delete_v1 runs."""
    if part in _marked or dry_run():
        return
    try:
        from pensieve import store as st
        from pensieve.store import migration
        idx = st.index_db()
        if migration.meta(idx, f"first_index:{part}") is None:
            migration.set_meta(idx, f"first_index:{part}", str(models.now()))
            log(f"first full index of {part} done")
        _marked.add(part)
    except Exception:
        traceback.print_exc()
        return
    _set(first_index=first_index())
    if first_index_done():
        maybe_delete()


def first_index() -> dict[str, int | None]:
    from pensieve import store as st
    from pensieve.store import migration
    have = migration.metas(st.index_db(), "first_index:")
    return {p: int(have[f"first_index:{p}"]) if f"first_index:{p}" in have else None for p in PARTS}


def first_index_done() -> bool:
    return all(first_index().values())


def _live_paths() -> set[Path]:
    """The paths that v1 code loaded in this process still reads and writes."""
    out = set()
    for mod, attr in LIVE:
        m = sys.modules.get(mod)
        v = getattr(m, attr, None) if m else None
        try:
            v = v() if callable(v) else v
            if v:
                out.add(Path(v).resolve())
        except Exception:
            continue
    return out


def plan_delete(v1: Path, pending=()) -> dict:
    """What delete_v1 would remove and keep, with the reason for each kept path. `pending`: the import steps not
    done, whose v1 files stay."""
    v1 = v1.resolve()
    live = _live_paths()
    protect = {}
    for step in pending:
        for n in PROTECTS.get(step, ()):
            protect.setdefault(n, step)
    names = list(DELETE) + sorted(p.name for p in v1.glob("*.tv") if p.name not in DELETE)
    remove, keep = [], []
    for n in names:
        p = v1 / n
        if not (p.exists() or p.is_symlink()):
            continue
        if n == "index.db" and p.is_file() and p.stat().st_size:
            keep.append(dict(path=n, why="not empty: not the v1 placeholder"))
            continue
        base = n.split("-")[0] if n.endswith(("-wal", "-shm")) else n
        if base in protect:
            keep.append(dict(path=n, why=f"its import ({protect[base]}) is not done"))
            continue
        rp = (v1 / base).resolve()
        if not p.is_symlink() and any(x == rp or rp in x.parents for x in live):
            keep.append(dict(path=n, why="v1 code in this process still uses it"))
            continue
        remove.append(dict(path=n, bytes=_size(p)))
    return dict(remove=remove, keep=keep)


def _size(p: Path) -> int:
    if p.is_symlink() or p.is_file():
        return p.lstat().st_size
    n = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                n += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return n


def _remove(p: Path):
    if p.is_symlink() or p.is_file():
        p.unlink()
    else:
        shutil.rmtree(p)


def delete_v1(v1: Path, store=None, dry: bool | None = None, force: bool = False, pending=None) -> dict:
    """Remove the v1 data: only the names in DELETE (and *.tv) directly in `v1`, never settings.json, logs, v2/ or
    anything outside it. Only after the first full v2 index (unless `force`, for tests). A path that a step still
    needs, or that v1 code here still uses, is kept and the next call tries again. When nothing is kept, the mark
    DATA_DIR/v2/v1-deleted.json says so, and later calls do nothing."""
    dry = dry_run() if dry is None else dry
    mark = v1 / "v2" / DELETED_MARK
    if mark.exists():
        return dict(status="already done", removed=[], keep=[])
    if not dry and not force and not first_index_done():
        return dict(status="waiting for the first full index", removed=[], keep=[])
    if pending is None:
        pending = pending_steps(store) if store is not None else ()
    plan = plan_delete(v1, pending)
    if dry:
        return dict(status="dry run", removed=plan["remove"], keep=plan["keep"])
    with _deleting:
        _set(phase="cleanup")
        removed = []
        for item in plan["remove"]:
            p = v1 / item["path"]
            try:
                if p.name == "connections" and not p.is_symlink():
                    for c in p.iterdir():
                        if c.name not in KEEP_IN_CONNECTIONS:
                            _remove(c)
                    if not any(p.iterdir()):
                        p.rmdir()
                else:
                    _remove(p)
                removed.append(item)
                log(f"removed {item['path']} ({item['bytes'] / 1e6:.1f} MB)")
            except OSError as e:
                plan["keep"].append(dict(path=item["path"], why=repr(e)[:200]))
                log(f"could not remove {item['path']}: {e!r}")
        for k in plan["keep"]:
            log(f"kept {k['path']}: {k['why']}")
        status = "done" if not plan["keep"] else "partly done"
        if store is not None:
            from pensieve.store import migration
            migration.set_step(store, "delete_v1", StepStatus.DONE if status == "done" else StepStatus.FAILED,
                               len(removed), None if status == "done" else f"kept {len(plan['keep'])} paths")
        if status == "done":
            mark.parent.mkdir(parents=True, exist_ok=True)
            mark.write_text(json.dumps(dict(at=models.now(), removed=removed), indent=1))
        _set(phase="done" if status == "done" else "indexing", deleted=status == "done", kept=plan["keep"])
        return dict(status=status, removed=removed, keep=plan["keep"])


def maybe_delete():
    """delete_v1 in the background, when v1 is here, every import step is done and the first full index is done."""
    if dry_run() or not v1_detected() or _deleting.locked():
        return

    def run():
        try:
            from pensieve import store as st
            s = st.store_db()
            if pending_steps(s):
                return
            r = delete_v1(v1_dir(), s)
            log(f"delete_v1: {r['status']}, removed {len(r['removed'])}, kept {len(r['keep'])}")
        except Exception:
            traceback.print_exc()
    threading.Thread(target=run, daemon=True, name="migrate-delete").start()


def run():
    """Detect v1, import it, and delete it when the first index is done (or report what would happen, in a dry run).
    Runs on its own thread at the server's start; safe to run again after a stop."""
    with _lock:
        v1 = v1_dir()
        try:
            if not v1_detected(v1):
                _set(phase="done" if (v1 / "v2" / DELETED_MARK).exists() else "none")
                return
            if dry_run():
                r = import_v1(v1, None, dry=True)
                d = delete_v1(v1, None, dry=True, pending={k for k, x in r.items() if x["status"] != "would import"})
                _set(phase="dry_run", step=None, steps=r, delete=d)
                log("dry run: " + json.dumps(dict(imports=r, delete=d)))
                return
            from pensieve import store as st
            s = st.store_db()
            import_v1(v1, s)
            _set(phase="indexing", step=None, first_index=first_index(), pending=pending_steps(s))
            if first_index_done():
                maybe_delete()
        except Exception as e:
            traceback.print_exc()
            _set(phase="error", error=repr(e)[:300])


def start():
    threading.Thread(target=run, daemon=True, name="migrate").start()


def view() -> dict:
    """The migration's progress, for /api/status: phase (none, importing, indexing, cleanup, done, dry_run, error),
    the step running, steps done of total, each step's result, and each part's first full index."""
    return dict(progress)


def job() -> dict | None:
    """A job for /api/syncing while the import runs, in the form of the other jobs."""
    if progress.get("phase") != "importing":
        return None
    return dict(kind="migrate", name=progress.get("about") or "", done=progress.get("done", 0),
                total=progress.get("total", len(STEPS)))


def compat_id(id: str) -> str:
    """`doc:<id>` for an old file id of a connection item (file:~/.pensieve/connections/<sid>/<name>.md) whose file
    is gone; any other id as it is. For agents, during v2's first release."""
    if not id.startswith("file:") or not id.endswith(".md"):
        return id
    try:
        p = Path(id[5:]).expanduser()
        if p.exists() or p.parent.parent != config.DATA_DIR / "connections":
            return id
        from pensieve import store as st
        from pensieve.store import migration
        doc = migration.doc_for_v1_path(st.store_db(), f"{p.parent.name}/{p.stem}")
        return f"doc:{doc}" if doc is not None else id
    except Exception:
        return id


def main():
    """python -m pensieve.migrate [--dry-run]: run the import once, outside the server."""
    if "--dry-run" in sys.argv:
        os.environ["PENSIEVE_MIGRATE_DRY_RUN"] = "1"
    run()
    print(json.dumps(view(), indent=1, default=str))


if __name__ == "__main__":
    main()
