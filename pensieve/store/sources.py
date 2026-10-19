"""The `sources` table: the places that Pensieve collects documents from, and whether each one is switched on."""
from pensieve.models import Source, SourceKind, SourceOrigin, SourceStatus, Switched, now
from pensieve.store import rows
from pensieve.store.db import Db


def ensure(db: Db, id: str, kind: SourceKind, name: str, origin: SourceOrigin, connector: str | None = None,
           status: SourceStatus = SourceStatus.UNKNOWN) -> None:
    """Add a source when it is new; a source that exists keeps its row (its status, switch and run times)."""
    db.write(lambda c: rows.insert(c, Source(id, kind, connector, name, origin, status), or_="IGNORE"))


def put(db: Db, s: Source) -> None:
    """Add or update a source. The user's switch (`switched`) is kept: only `set_switched` changes it."""
    db.write(lambda c: rows.upsert(c, s, ("id",), keep=("switched",)))


def get(db: Db, id: str) -> Source | None:
    return rows.select_one(db.conn, Source, "id = ?", (id,))


def all(db: Db) -> list[Source]:
    return rows.select(db.conn, Source, order="id")


def set_switched(db: Db, id: str, switched: Switched | None) -> None:
    db.write(lambda c: c.execute("UPDATE sources SET switched = ? WHERE id = ?",
                                 (switched.value if switched else None, id)))


def set_run(db: Db, id: str, ok: bool, error: str | None = None, at: int | None = None) -> None:
    """Record a collection: its time, or its failure and error."""
    at = at or now()
    if ok:
        db.write(lambda c: c.execute("UPDATE sources SET collected_at = ?, error = NULL WHERE id = ?", (at, id)))
    else:
        db.write(lambda c: c.execute("UPDATE sources SET failed_at = ?, error = ? WHERE id = ?", (at, error, id)))


def has_data(db: Db, id: str) -> bool:
    return db.one("SELECT 1 FROM documents WHERE source_id = ? LIMIT 1", (id,)) is not None


def switched_on(db: Db, id: str) -> bool:
    """The `switched` rule: on when the user switched it on, or when it holds data and the user did not switch it
    off."""
    r = db.one("SELECT switched FROM sources WHERE id = ?", (id,))
    if r and r[0]:
        return r[0] == Switched.ON
    return has_data(db, id)


def delete(db: Db, id: str) -> None:
    """A source and its documents (their people, changes and sessions go with them)."""
    def run(c):
        c.execute("DELETE FROM documents WHERE source_id = ?", (id,))
        c.execute("DELETE FROM sources WHERE id = ?", (id,))
    db.write(run)
