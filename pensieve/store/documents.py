"""The `documents` table and its people, changes and sessions: the items that Pensieve collected from its sources.

`upsert` merges a new copy of an item into its row in one write job. A copy with the same hash as the row changes
nothing. Reads by time use the `documents_time` index (`coalesce(updated_at, seen_at)`)."""
import hashlib
import json
from dataclasses import dataclass

from pensieve.models import (Document, DocumentChange, DocumentPerson, DocumentSession, DocKind, PersonRole,
                             now)
from pensieve.store import rows
from pensieve.store.db import Db

TIME = "coalesce(updated_at, seen_at)"


@dataclass(frozen=True)
class Write:
    """A new copy of a document, as the caller made it from the old row (see `upsert`)."""
    kind: DocKind
    title: str
    url: str | None
    status: str | None
    branch: str | None
    created_at: int | None
    updated_at: int | None
    private: bool
    text: str
    raw: dict
    people: tuple = ()          # (IdentityKey, PersonRole, name)
    changes: tuple = ()         # (field, old, new)


@dataclass(frozen=True)
class Result:
    id: int
    changed: bool   # the row's content changed (or the row is new)
    seen: bool      # a session saw it for the first time
    doc: Document | None = None


@dataclass(frozen=True)
class Seen:
    session_id: str
    at: int


def raw_hash(raw: dict) -> str:
    return hashlib.sha1(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()


def upsert(db: Db, source_id: str, key: str, make, seen: Seen | None = None, wait: bool = True):
    """Merge a copy of one document. `make(old)` gets the stored Document (or None) and returns a `Write`, or None
    to keep the row. It runs on the writer thread, inside the transaction, so two copies of one item do not race.
    `seen`: the agent session that saw the item. Returns a Result (or a Future of it without `wait`)."""
    def run(c):
        old = rows.select_one(c, Document, "source_id = ? AND key = ?", (source_id, key))
        w = make(old)
        if w is None and old is None:
            return None
        doc_id, changed = old.id if old else None, False
        if w is not None:
            raw = json.dumps(w.raw, sort_keys=True, default=str)
            h = hashlib.sha1(raw.encode()).hexdigest()
            if old is None or old.hash != h or old.title != w.title or old.private != w.private:
                seen_at = max(old.seen_at or 0, seen.at) if old and seen else (seen.at if seen else
                                                                                (old.seen_at if old else None))
                if seen_at is None and w.updated_at is None:
                    seen_at = now()  # no time of its own: when it was collected
                doc = Document(doc_id, source_id, key, w.kind, w.title, w.url, w.status, w.branch, w.created_at,
                               w.updated_at, seen_at, w.private, w.text, raw, h)
                if old is None:
                    doc_id = rows.insert(c, doc)
                else:
                    rows.upsert(c, doc, ("source_id", "key"))
                c.execute("DELETE FROM document_people WHERE doc_id = ?", (doc_id,))
                rows.insert_many(c, (DocumentPerson(doc_id, k, PersonRole(r), n) for k, r, n in
                                     dict.fromkeys(w.people)), or_="IGNORE")
                at = now()
                rows.insert_many(c, (DocumentChange(doc_id, at, f, o, n) for f, o, n in w.changes), or_="REPLACE")
                changed = True
        first = False
        if seen is not None:
            first = c.execute("SELECT 1 FROM document_sessions WHERE doc_id = ? AND session_id = ?",
                              (doc_id, seen.session_id)).fetchone() is None
            c.execute("INSERT INTO document_sessions VALUES (?, ?, ?) ON CONFLICT (doc_id, session_id) DO UPDATE "
                      "SET seen_at = max(seen_at, excluded.seen_at)", (doc_id, seen.session_id, seen.at))
            if not changed:
                c.execute("UPDATE documents SET seen_at = max(coalesce(seen_at, 0), ?) WHERE id = ?", (seen.at, doc_id))
        return Result(doc_id, changed, first, rows.select_one(c, Document, "id = ?", (doc_id,)) if changed or first
                      else None)
    return db.write(run, wait=wait)


def get(db: Db, id: int) -> Document | None:
    return rows.select_one(db.conn, Document, "id = ?", (id,))


def get_many(db: Db, ids) -> list[Document]:
    ids = list(ids)
    return rows.select(db.conn, Document, f"id IN ({rows.marks(len(ids))})", ids) if ids else []


def by_key(db: Db, source_id: str, key: str) -> Document | None:
    return rows.select_one(db.conn, Document, "source_id = ? AND key = ?", (source_id, key))


def window(db: Db, since: int, until: int | None = None, sources=None, private: bool | None = None) -> list[Document]:
    """The documents whose time is in (since, until], newest first, from `sources` (ids) when given."""
    where, args = f"{TIME} > ?", [since]
    if until is not None:
        where += f" AND {TIME} <= ?"
        args.append(until)
    if sources is not None:
        sources = list(sources)
        where += f" AND source_id IN ({rows.marks(len(sources))})"
        args += sources
    if private is not None:
        where += " AND private = ?"
        args.append(int(private))
    return rows.select(db.conn, Document, where, args, order=f"{TIME} DESC")


def of_kinds(db: Db, kinds, sources, since: int | None = None) -> list[Document]:
    """The documents of these kinds from `sources`, newest first (after `since` when given)."""
    kinds, sources = list(kinds), list(sources)
    where = f"kind IN ({rows.marks(len(kinds))}) AND source_id IN ({rows.marks(len(sources))})"
    args = [*kinds, *sources]
    if since is not None:
        where += f" AND {TIME} > ?"
        args.append(since)
    return rows.select(db.conn, Document, where, args, order=f"{TIME} DESC")


def ticket_prefixes(db: Db, sources) -> set[str]:
    """The project prefixes of the ticket keys of `sources` (ABC of ABC-123)."""
    sources = list(sources)
    return {r[0] for r in db.query(f"SELECT DISTINCT substr(key, 1, instr(key, '-') - 1) FROM documents WHERE "
                                   f"kind = 'ticket' AND source_id IN ({rows.marks(len(sources))})", sources)}


def by_keys(db: Db, keys, sources, kind: str = "ticket") -> list[Document]:
    """The documents of `sources` with these keys (ticket keys by default)."""
    keys, sources = list(keys), list(sources)
    out = []
    for i in range(0, len(keys), 500):
        part = keys[i:i + 500]
        out += rows.select(db.conn, Document, f"kind = ? AND key IN ({rows.marks(len(part))}) AND source_id IN "
                                              f"({rows.marks(len(sources))})", [kind, *part, *sources])
    return out


def by_branches(db: Db, branches, sources) -> list[Document]:
    """The pull requests of `sources` from these head branches."""
    branches, sources = list(branches), list(sources)
    out = []
    for i in range(0, len(branches), 500):
        part = branches[i:i + 500]
        out += rows.select(db.conn, Document, f"branch IN ({rows.marks(len(part))}) AND source_id IN "
                                              f"({rows.marks(len(sources))})", [*part, *sources])
    return out


def ids_since(db: Db, since: int) -> list[tuple[int, str]]:
    """(id, hash) of the documents whose time is after `since`: what the indexer compares with its own state."""
    return db.query(f"SELECT id, hash FROM documents WHERE {TIME} > ?", (since,))


def all_hashes(db: Db, sources=None) -> list[tuple[int, str, str]]:
    """(id, hash, source id) of every document (of `sources`)."""
    if sources is None:
        return db.query("SELECT id, hash, source_id FROM documents")
    sources = list(sources)
    return db.query(f"SELECT id, hash, source_id FROM documents WHERE source_id IN ({rows.marks(len(sources))})",
                    sources)


def count(db: Db, source_id: str | None = None) -> int:
    if source_id is None:
        return db.one("SELECT count(*) FROM documents")[0]
    return db.one("SELECT count(*) FROM documents WHERE source_id = ?", (source_id,))[0]


def counts(db: Db) -> dict[str, int]:
    return dict(db.query("SELECT source_id, count(*) FROM documents GROUP BY source_id"))


def people(db: Db, doc_id: int) -> list[DocumentPerson]:
    return rows.select(db.conn, DocumentPerson, "doc_id = ?", (doc_id,))


def people_of(db: Db, ids) -> dict[int, list[DocumentPerson]]:
    ids, out = list(ids), {}
    for i in range(0, len(ids), 900):
        part = ids[i:i + 900]
        for p in rows.select(db.conn, DocumentPerson, f"doc_id IN ({rows.marks(len(part))})", part):
            out.setdefault(p.doc_id, []).append(p)
    return out


def changes(db: Db, doc_id: int) -> list[DocumentChange]:
    return rows.select(db.conn, DocumentChange, "doc_id = ?", (doc_id,), order="at, field")


def changes_of(db: Db, ids) -> dict[int, list[DocumentChange]]:
    ids, out = list(ids), {}
    for i in range(0, len(ids), 900):
        part = ids[i:i + 900]
        for ch in rows.select(db.conn, DocumentChange, f"doc_id IN ({rows.marks(len(part))})", part, order="at"):
            out.setdefault(ch.doc_id, []).append(ch)
    return out


def sessions(db: Db, doc_id: int) -> list[DocumentSession]:
    return rows.select(db.conn, DocumentSession, "doc_id = ?", (doc_id,), order="seen_at")


def of_session(db: Db, session_id: str) -> list[int]:
    return [r[0] for r in db.query("SELECT doc_id FROM document_sessions WHERE session_id = ?", (session_id,))]


def delete(db: Db, ids) -> None:
    ids = list(ids)
    if ids:
        db.write(lambda c: c.execute(f"DELETE FROM documents WHERE id IN ({rows.marks(len(ids))})", ids))


def markdown(db: Db, id: int) -> str | None:
    """The document as Markdown, for agents and cards: front matter, its fields, its people and its changes. It is
    made from the row each time; it is not stored."""
    from pensieve import connections
    d = get(db, id)
    if d is None:
        return None
    src = db.one("SELECT name FROM sources WHERE id = ?", (d.source_id,))
    return connections.document_markdown(d, src[0] if src else d.source_id, people(db, id), changes(db, id),
                                         sessions(db, id))
