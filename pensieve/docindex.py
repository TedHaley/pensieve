"""The search index of collected documents (storage v2): chunks of each document's text in index.db (`doc_chunks`,
`fts_docs`) and their vectors in vectors/docs.tv.

`sync` compares each document's hash in store.db with the hash it had when it was chunked (`doc_index_state`), and
chunks again only what changed. A chunk whose text did not change keeps its row and its vector. The documents of a
source that is switched off, or that another source covers, leave the index. Search hits are `doc:<id>`."""
import hashlib
import threading
import time

import numpy as np

from pensieve import config, connections, settings
from pensieve import store as v2
from pensieve.models import DocChunk
from pensieve.store import docchunks, documents, sources as store_sources


def _path():
    return v2.vectors_dir() / "docs.tv"


class DocIndex:
    def __init__(self):
        from turbovec import IdMapIndex
        self.idx_db, self.store_db = v2.index_db(), v2.store_db()
        self.lock = threading.RLock()  # the vector index: turbovec is not safe for a write during a search
        _path().parent.mkdir(parents=True, exist_ok=True)
        self.index = IdMapIndex.load(str(_path())) if _path().exists() else IdMapIndex(dim=config.EMBED_DIM, bit_width=4)
        self.progress = None
        missing, ids = [], []
        for cid, blob in docchunks.vectors(self.idx_db):
            if not self.index.contains(cid):
                ids.append(cid)
                missing.append(np.frombuffer(blob, np.float16).astype(np.float32))
        if ids:
            self.index.add_with_ids(np.stack(missing), np.array(ids, dtype=np.uint64))
            self.index.sync(str(_path()))

    def sync(self, log=lambda m: print(m, flush=True), budget: float | None = None) -> int:
        """Chunk and embed the documents that changed; drop the ones that left. Returns the documents touched."""
        end = time.monotonic() + budget if budget else None
        srcs = connections.indexed()
        want = {i: h for i, h, _ in documents.all_hashes(self.store_db, srcs)}
        have = docchunks.state(self.idx_db)
        gone = [i for i in have if i not in want]
        todo = [i for i, h in want.items() if have.get(i) != h]
        touched = 0
        if gone:
            self._remove(docchunks.forget(self.idx_db, gone))
            touched += len(gone)
        size, cap = settings.get("chunk_chars"), settings.get("max_chunks")
        from pensieve.files import chunk_text
        for n in range(0, len(todo), 32):
            if end and time.monotonic() > end:
                break
            self.progress = dict(name="documents", root="", done=n, total=len(todo))
            for d in documents.get_many(self.store_db, todo[n:n + 32]):
                self._index_doc(d, chunk_text(d.text, size, cap) if d.text.strip() else [])
                touched += 1
        self.progress = None
        if touched:
            with self.lock:
                self.index.sync(str(_path()))
            log(f"indexed {touched} documents")
        return touched

    def _index_doc(self, d, chunks):
        texts = [t for _, _, t in chunks]
        hashes = [hashlib.sha1(t.encode()).hexdigest() for t in texts]
        old = {(idx, h): cid for cid, idx, h in docchunks.chunks_of(self.idx_db, d.id)}
        keep = set(enumerate(hashes))
        drop = [cid for k, cid in old.items() if k not in keep]
        new = [i for i, h in enumerate(hashes) if (i, h) not in old]
        from pensieve.indexer import embed
        vecs = embed([texts[i] for i in new], titles=[d.title] * len(new)) if new else np.zeros((0, config.EMBED_DIM))
        add = [DocChunk(None, d.id, i, texts[i], v.astype(np.float16).tobytes(), hashes[i]) for i, v in zip(new, vecs)]
        ids = docchunks.replace(self.idx_db, d.id, d.hash, drop, add)
        self._remove(drop)
        if ids:
            with self.lock:
                self.index.add_with_ids(np.asarray(vecs, dtype=np.float32), np.array(ids, dtype=np.uint64))

    def _remove(self, ids):
        with self.lock:
            for cid in ids:
                try:
                    self.index.remove(cid)
                except Exception:
                    pass

    def allow(self, sources) -> np.ndarray:
        """The chunk ids of the documents of `sources`, for a search within a scope."""
        ids = [i for i, _, _ in documents.all_hashes(self.store_db, sources)]
        return np.array(docchunks.ids_of_docs(self.idx_db, ids), dtype=np.uint64)

    def search(self, qv: np.ndarray, k: int, allow: np.ndarray | None = None) -> list[tuple[int, float]]:
        """(chunk id, cosine score) of the nearest chunks."""
        with self.lock:
            if allow is not None:
                if not len(allow):
                    return []
                sc, ids = self.index.search(qv[None, :], k=min(k, len(allow)), allowlist=allow)
            else:
                if not len(self.index):
                    return []
                sc, ids = self.index.search(qv[None, :], k=k)
        return [(int(i), float(s)) for s, i in zip(sc[0], ids[0])]

    def rows(self, ids) -> dict[int, dict]:
        """For each chunk id: the chunk and its document, as search shows a hit."""
        chunks = docchunks.get(self.idx_db, ids)
        docs = {d.id: d for d in documents.get_many(self.store_db, {c.doc_id for c in chunks.values()})}
        names = {s.id: s.name for s in store_sources.all(self.store_db)} if docs else {}
        out = {}
        for cid, c in chunks.items():
            d = docs.get(c.doc_id)
            if d is None:
                continue
            when = d.time
            out[cid] = dict(id=f"doc:{d.id}", text=c.text, vec=c.vec, title=d.title, ext="", path=None, line=None,
                            mtime=float(when) if when else None, key=f"doc:{d.id}", doc=d.id, url=d.url,
                            private=d.private, source=d.source_id,
                            subtitle=" · ".join(x for x in (names.get(d.source_id, d.source_id), d.kind.value,
                                                            d.status or "") if x))
        return out


def resolve_path(path: str) -> int | None:
    """The document of a v1 connection file (~/.pensieve/connections/<source>/<name>.md), for the ids that agents
    kept from before storage v2."""
    from pathlib import Path
    p = Path(path)
    if p.suffix != ".md" or p.parent.parent != config.DATA_DIR / "connections":
        return None
    sid, stem = p.parent.name, p.stem
    db = v2.store_db()
    hit = documents.by_key(db, sid, stem)
    if hit:
        return hit.id
    for doc_id, key in db.query("SELECT id, key FROM documents WHERE source_id = ?", (sid,)):
        if any(connections._fname(kn, key) == stem for kn in ("key", "id", "number", "ts", "url", "gid", "permalink",
                                                              "webUrl")):
            return doc_id
    return None


_current: DocIndex | None = None
_get_lock = threading.Lock()


def get() -> DocIndex:
    """This process's document index (the server's, or the MCP command's), made at the first use."""
    global _current
    if _current is None:
        with _get_lock:
            if _current is None:
                _current = DocIndex()
    return _current


def similar(doc_id: int, k: int = 8) -> list[dict]:
    """The documents nearest in meaning to one document (the mean of its chunk vectors): {id, title, url, score}."""
    from pensieve.store import rows
    di = get()
    vecs = [np.frombuffer(c.vec, np.float16).astype(np.float32) for c in
            rows.select(di.idx_db.conn, DocChunk, "doc_id = ?", (doc_id,))]
    if not vecs:
        return []
    q = np.mean(vecs, axis=0)
    q /= np.linalg.norm(q) + 1e-9
    best = {}
    hits = di.search(q.astype(np.float32), k * 4)
    got = di.rows([cid for cid, _ in hits])
    for cid, s in hits:
        r = got.get(cid)
        if r and r["doc"] != doc_id and r["id"] not in best:
            best[r["id"]] = dict(id=r["id"], title=r["title"], subtitle=r["subtitle"], url=r["url"], score=round(s, 4))
    return list(best.values())[:k]
