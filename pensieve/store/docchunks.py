"""index.db's document index: `doc_chunks`, its word index `fts_docs`, and `doc_index_state` (the hash of each
document when it was chunked). `fts_docs` is an external-content FTS5 table: each change of a chunk row also changes
it here, in the same job."""
from pensieve.models import DocChunk, now
from pensieve.store import rows
from pensieve.store.db import Db


def state(db: Db) -> dict[int, str]:
    """{doc id: hash} of the documents that have chunks."""
    return dict(db.query("SELECT doc_id, hash FROM doc_index_state"))


def chunks_of(db: Db, doc_id: int) -> list[tuple[int, int, str]]:
    """(chunk id, idx, text hash) of one document's chunks."""
    return db.query("SELECT id, idx, hash FROM doc_chunks WHERE doc_id = ? ORDER BY idx", (doc_id,))


def replace(db: Db, doc_id: int, doc_hash: str, drop: list[int], add: list[DocChunk]) -> list[int]:
    """Drop chunks by id and add new ones for one document, and record its hash. Returns the new chunk ids."""
    def run(c):
        _drop(c, drop)
        ids = []
        for ch in add:
            cid = rows.insert(c, ch)
            c.execute("INSERT INTO fts_docs (rowid, text) VALUES (?, ?)", (cid, ch.text))
            ids.append(cid)
        c.execute("INSERT OR REPLACE INTO doc_index_state VALUES (?, ?, ?)", (doc_id, doc_hash, now()))
        return ids
    return db.write(run)


def _drop(c, ids):
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for cid, text in c.execute(f"SELECT id, text FROM doc_chunks WHERE id IN ({rows.marks(len(part))})", part):
            c.execute("INSERT INTO fts_docs (fts_docs, rowid, text) VALUES ('delete', ?, ?)", (cid, text))
        c.execute(f"DELETE FROM doc_chunks WHERE id IN ({rows.marks(len(part))})", part)


def forget(db: Db, doc_ids) -> list[int]:
    """Drop every chunk of these documents and their state. Returns the dropped chunk ids."""
    doc_ids = list(doc_ids)

    def run(c):
        out = []
        for i in range(0, len(doc_ids), 500):
            part = doc_ids[i:i + 500]
            ids = [r[0] for r in c.execute(f"SELECT id FROM doc_chunks WHERE doc_id IN ({rows.marks(len(part))})",
                                           part)]
            _drop(c, ids)
            c.execute(f"DELETE FROM doc_index_state WHERE doc_id IN ({rows.marks(len(part))})", part)
            out += ids
        return out
    return db.write(run) if doc_ids else []


def vectors(db: Db):
    """(chunk id, vec) of every chunk, to fill the vector index again."""
    return db.conn.execute("SELECT id, vec FROM doc_chunks")


def ids_of_docs(db: Db, doc_ids) -> list[int]:
    doc_ids, out = list(doc_ids), []
    for i in range(0, len(doc_ids), 900):
        part = doc_ids[i:i + 900]
        out += [r[0] for r in db.query(f"SELECT id FROM doc_chunks WHERE doc_id IN ({rows.marks(len(part))})", part)]
    return out


def get(db: Db, ids) -> dict[int, DocChunk]:
    ids, out = list(ids), {}
    for i in range(0, len(ids), 900):
        part = ids[i:i + 900]
        out.update({ch.id: ch for ch in rows.select(db.conn, DocChunk, f"id IN ({rows.marks(len(part))})", part)})
    return out


def match(db: Db, fts_query: str, limit: int) -> list[tuple[int, float]]:
    """(chunk id, bm25) of the chunks that match a word query, best first (lower bm25 is better)."""
    return db.query("SELECT rowid, bm25(fts_docs) FROM fts_docs WHERE fts_docs MATCH ? ORDER BY 2 LIMIT ?",
                    (fts_query, limit))


def contains(db: Db, words: list[str], limit: int) -> list[int]:
    """Chunks whose text holds every one of `words` (short words that FTS can not match alone)."""
    where = " AND ".join("instr(lower(text), ?) > 0" for _ in words)
    return [r[0] for r in db.query(f"SELECT id FROM doc_chunks WHERE {where} LIMIT ?", [*map(str.lower, words), limit])]


def count(db: Db) -> int:
    return db.one("SELECT count(*) FROM doc_chunks")[0]
