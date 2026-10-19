"""The tables of the move from v1 to v2 (pensieve/migrate.py): `migration_steps` (the result of each import, so that
an interrupted migration goes on where it stopped), `v1_doc_paths` (old file ids of connection items, for agents),
and the rows that each import adds. The imports add rows only when the same row is not there, so a step that runs
again adds nothing twice."""
from pensieve.models import Document, MigrationStep, StepStatus, V1DocPath, now
from pensieve.store import rows
from pensieve.store.db import Db


def steps(db: Db) -> dict[str, MigrationStep]:
    return {s.step: s for s in rows.select(db.conn, MigrationStep)}


def set_step(db: Db, step: str, status: StepStatus, count: int, error: str | None = None) -> MigrationStep:
    """Record a step's result. `tries` counts the runs that did not finish it."""
    def run(c):
        r = c.execute("SELECT tries FROM migration_steps WHERE step = ?", (step,)).fetchone()
        tries = (r[0] if r else 0) + (status != StepStatus.DONE)
        s = MigrationStep(step, status, count, error, tries, now())
        rows.upsert(c, s, ("step",))
        return s
    return db.write(run)


def add_new(db: Db, objs: list, same: tuple[str, ...]) -> int:
    """Insert the rows that have no row with the same values in the `same` columns. Returns the number added. One
    job: a failure adds nothing."""
    objs = list(objs)
    if not objs:
        return 0
    cls = type(objs[0])
    where = " AND ".join(f"{k} IS ?" for k in same)

    def run(c):
        n = 0
        for o in objs:
            if c.execute(f"SELECT 1 FROM {cls.__table__} WHERE {where} LIMIT 1",
                         [rows_value(o, k) for k in same]).fetchone():
                continue
            rows.insert(c, o)
            n += 1
        return n
    return db.write(run)


def put_all(db: Db, objs: list, key: tuple[str, ...]) -> int:
    """Upsert rows by their key. Returns the number of rows sent."""
    objs = list(objs)

    def run(c):
        for o in objs:
            rows.upsert(c, o, key)
        return len(objs)
    return db.write(run) if objs else 0


def rows_value(obj, name: str):
    v = getattr(obj, name)
    return int(v) if isinstance(v, bool) else getattr(v, "value", v)


def doc_for_v1_path(db: Db, path: str) -> int | None:
    """The DocId of a v1 connection item, by its `<sid>/<name>` path; None when v2 has not collected it (yet)."""
    p = rows.select_one(db.conn, V1DocPath, "path = ?", (path,))
    if not p:
        return None
    r = db.one(f"SELECT id FROM {Document.__table__} WHERE source_id = ? AND key = ?", (p.source_id, p.key))
    return r[0] if r else None


def meta(db: Db, key: str) -> str | None:
    """A value of index.db's `meta`."""
    r = db.one("SELECT value FROM meta WHERE key = ?", (key,))
    return r[0] if r else None


def metas(db: Db, prefix: str) -> dict[str, str]:
    return dict(db.query("SELECT key, value FROM meta WHERE key LIKE ? ESCAPE '\\'",
                         (prefix.replace("_", "\\_").replace("%", "\\%") + "%",)))


def set_meta(db: Db, key: str, value: str) -> None:
    db.write(lambda c: c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET "
                                 "value = excluded.value", (key, value)))


def table_exists(db: Db, table: str) -> bool:
    return db.one("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)) is not None
