"""SQLite metadata + turbovec ANN index + embedding + 3D projection."""
import hashlib
import json
import re
import sqlite3
import threading
from collections import defaultdict
from pathlib import Path

import numpy as np

from . import config, parsers
from .layout import cluster, fit3d, place_new, unit
from .projects import project_name

LAYOUT_VERSION = "umap-v1"

_embedder = None
_embed_lock = threading.Lock()


def embedder():
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer(config.EMBED_MODEL, device=config.device())
        m.max_seq_length = config.EMBED_MAX_TOKENS
        _embedder = m
    return _embedder


def embed(texts, query=False) -> np.ndarray:
    with _embed_lock:
        v = embedder().encode(texts, batch_size=16, normalize_embeddings=True,
                              prompt_name="query" if query else None, show_progress_bar=False)
    return np.asarray(v, dtype=np.float32)


def chunk_session(turns):
    """Group into user->assistant exchanges, splitting long ones on paragraph boundaries."""
    exchanges, cur = [], []
    for role, text in turns:
        if role == "user" and cur:
            exchanges.append(cur)
            cur = []
        cur.append(f"{'User' if role == 'user' else 'Assistant'}: {text}")
    if cur:
        exchanges.append(cur)
    out = []
    for ex in exchanges:
        buf = ""
        for para in "\n\n".join(ex).split("\n\n"):
            while len(para) > config.CHUNK_CHARS:  # hard-split giant paragraphs
                head, para = para[:config.CHUNK_CHARS], para[config.CHUNK_CHARS:]
                if buf:
                    out.append(buf); buf = ""
                out.append(head)
            if buf and len(buf) + len(para) > config.CHUNK_CHARS:
                out.append(buf); buf = ""
            buf = f"{buf}\n\n{para}" if buf else para
        if buf:
            out.append(buf)
    return out


def reconcile(index, db, table):
    """Re-add vectors present in SQLite but missing from the ANN index (e.g. after an unclean shutdown)."""
    ids, vecs = [], []
    for cid, blob in db.execute(f"SELECT id, vec FROM {table}"):
        if not index.contains(cid):
            ids.append(cid)
            vecs.append(np.frombuffer(blob, np.float16).astype(np.float32))
    if ids:
        index.add_with_ids(np.stack(vecs), np.array(ids, dtype=np.uint64))
    return len(ids)


class Store:
    def __init__(self):
        from turbovec import IdMapIndex
        self.lock = threading.RLock()
        self.db = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(
          id TEXT PRIMARY KEY, source TEXT, project TEXT, title TEXT, path TEXT,
          mtime REAL, size INTEGER, started TEXT, updated TEXT, n_chunks INTEGER,
          x REAL, y REAL, z REAL, cluster INTEGER);
        CREATE TABLE IF NOT EXISTS chunks(
          id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, idx INTEGER,
          hash TEXT, text TEXT, vec BLOB, x REAL, y REAL, z REAL);
        CREATE INDEX IF NOT EXISTS chunks_sess ON chunks(session_id, idx);
        CREATE TABLE IF NOT EXISTS topics(id INTEGER PRIMARY KEY, name TEXT, description TEXT, keywords TEXT, members TEXT);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS session_files(session_id TEXT, path TEXT, edited INTEGER, PRIMARY KEY(session_id, path));
        """)
        for tbl, col in [("sessions", "project_name TEXT"), ("sessions", "summary TEXT"),
                         ("sessions", "tags TEXT"), ("chunks", "summary TEXT")]:
            try:
                self.db.execute(f"ALTER TABLE {tbl} ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        for sid, proj in self.db.execute("SELECT id, project FROM sessions WHERE project_name IS NULL").fetchall():
            self.db.execute("UPDATE sessions SET project_name=? WHERE id=?", (project_name(proj or ""), sid))
        self.db.commit()
        if config.INDEX_PATH.exists():
            self.index = IdMapIndex.load(str(config.INDEX_PATH))
        else:
            self.index = IdMapIndex(dim=config.EMBED_DIM, bit_width=4)
        if reconcile(self.index, self.db, "chunks"):
            self.index.sync(str(config.INDEX_PATH))

    # ---- ingestion -------------------------------------------------------
    def sync_files(self, log=lambda m: print(m, flush=True), on_progress=None) -> int:
        """Index new/changed transcript files. Returns number of sessions touched."""
        import time
        with self.lock:
            known = {r[0]: (r[1], r[2]) for r in self.db.execute("SELECT path, mtime, size FROM sessions")}
        touched = 0
        for source, path in parsers.discover():
            st = path.stat()
            if known.get(str(path)) == (st.st_mtime, st.st_size):
                continue
            if time.time() - st.st_mtime < config.SETTLE_SECONDS:
                continue
            try:
                self._index_session(source, path, st)
                touched += 1
                log(f"indexed {source}:{path.stem[:8]}")
                if on_progress and touched % 8 == 0:
                    self.reproject()
                    on_progress(touched)
            except Exception as e:  # one bad file shouldn't stop the sweep
                log(f"skip {path}: {e!r}")
        if touched:
            self.reproject()
            with self.lock:
                self.index.sync(str(config.INDEX_PATH))
        return touched

    def save_session_files(self, sid, touched: dict):
        with self.lock:
            self.db.execute("DELETE FROM session_files WHERE session_id=?", (sid,))
            rows = [(sid, p, int(e)) for p, e in touched.items()] or [(sid, "", 0)]
            self.db.executemany("INSERT OR REPLACE INTO session_files VALUES(?,?,?)", rows)
            self.db.commit()

    def _index_session(self, source, path: Path, st):
        s = parsers.parse(source, path)
        self.save_session_files(s.id, s.touched)
        chunks = chunk_session(s.turns)
        hashes = [hashlib.sha1(c.encode()).hexdigest() for c in chunks]
        with self.lock:
            old = {(r[1], r[2]): r[0] for r in self.db.execute(
                "SELECT id, idx, hash FROM chunks WHERE session_id=?", (s.id,))}
            keep = {(i, h) for i, h in enumerate(hashes)}
            stale = [cid for k, cid in old.items() if k not in keep]
            new = [i for i, h in enumerate(hashes) if (i, h) not in old]
        vecs = embed([chunks[i] for i in new]) if new else np.zeros((0, config.EMBED_DIM), np.float32)
        with self.lock:
            for cid in stale:
                self.index.remove(cid)
                self.db.execute("DELETE FROM chunks WHERE id=?", (cid,))
            ids = []
            for i, v in zip(new, vecs):
                cur = self.db.execute(
                    "INSERT INTO chunks(session_id, idx, hash, text, vec) VALUES(?,?,?,?,?)",
                    (s.id, i, hashes[i], chunks[i], v.astype(np.float16).tobytes()))
                ids.append(cur.lastrowid)
            if ids:
                self.index.add_with_ids(vecs, np.array(ids, dtype=np.uint64))
            self.db.execute(
                "INSERT INTO sessions(id,source,project,project_name,title,path,mtime,size,started,updated,n_chunks) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET source=excluded.source, "
                "project=excluded.project, project_name=excluded.project_name, title=excluded.title, path=excluded.path, mtime=excluded.mtime, "
                "size=excluded.size, started=excluded.started, updated=excluded.updated, n_chunks=excluded.n_chunks",
                (s.id, source, s.project, project_name(s.project), s.title, str(path), st.st_mtime, st.st_size,
                 s.started, s.updated, len(chunks)))
            if new or stale:
                self.db.execute("UPDATE sessions SET summary=NULL, tags=NULL WHERE id=?", (s.id,))
            self.db.commit()

    # ---- projection + topics -------------------------------------------
    def meta(self, key, value=None):
        with self.lock:
            if value is None:
                r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                return r[0] if r else None
            self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))

    def needs_layout(self):
        with self.lock:
            return bool(self.db.execute("SELECT 1 FROM chunks WHERE x IS NULL LIMIT 1").fetchone())

    def reproject(self, force=False):
        """UMAP refit when the corpus grew meaningfully (or on first run); otherwise place new points by neighbours."""
        with self.lock:
            rows = self.db.execute("SELECT id, session_id, vec, x, y, z FROM chunks").fetchall()
            fit_n = int(self.meta("session_fit_n") or 0)
            version_ok = self.meta("layout") == LAYOUT_VERSION
        if len(rows) < 4:
            return False
        cid = np.array([r[0] for r in rows])
        sid = np.array([r[1] for r in rows])
        X = unit(np.stack([np.frombuffer(r[2], np.float16) for r in rows]).astype(np.float32))
        have = np.array([r[3] is not None for r in rows])
        n = len(rows)
        full = force or not version_ok or have.sum() < 0.5 * n or n - fit_n > max(60, 0.15 * fit_n)
        sessions = sorted(set(sid))
        C = unit(np.stack([X[sid == s].mean(axis=0) for s in sessions]))
        if full:
            P = fit3d(X, n_neighbors=15, min_dist=0.1)
            SP = fit3d(C, n_neighbors=10, min_dist=0.35)
            lab = cluster(C)
            with self.lock:
                self.db.executemany("UPDATE chunks SET x=?, y=?, z=? WHERE id=?",
                                    [(*map(float, p), int(i)) for p, i in zip(P, cid)])
                self.db.executemany("UPDATE sessions SET x=?, y=?, z=?, cluster=? WHERE id=?",
                                    [(*map(float, p), int(c), s) for p, c, s in zip(SP, lab, sessions)])
                self._save_topics(dict(zip(sessions, map(int, lab))))
                self.meta("session_fit_n", n)
                self.meta("layout", LAYOUT_VERSION)
                self.db.commit()
            return True
        if not have.all():
            P_old = np.array([[r[3], r[4], r[5]] for r in rows if r[3] is not None], dtype=np.float32)
            P_new = place_new(X[have], P_old, X[~have])
            with self.lock:
                self.db.executemany("UPDATE chunks SET x=?, y=?, z=? WHERE id=?",
                                    [(*map(float, p), int(i)) for p, i in zip(P_new, cid[~have])])
        with self.lock:
            placed = {r[0]: (r[1], r[2], r[3], r[4]) for r in self.db.execute(
                "SELECT id, x, y, z, cluster FROM sessions WHERE x IS NOT NULL")}
            missing = [i for i, s in enumerate(sessions) if s not in placed]
            if missing and placed:
                known = [i for i, s in enumerate(sessions) if s in placed]
                Pk = np.array([placed[sessions[i]][:3] for i in known], dtype=np.float32)
                Pn = place_new(C[known], Pk, C[missing], k=3, jitter=0.05)
                nearest = (C[missing] @ C[known].T).argmax(1)
                for j, i in enumerate(missing):
                    cl = placed[sessions[known[nearest[j]]]][3]
                    self.db.execute("UPDATE sessions SET x=?, y=?, z=?, cluster=? WHERE id=?",
                                    (*map(float, Pn[j]), cl, sessions[i]))
            self.db.commit()
        return bool(missing) or not have.all()

    def _keywords(self, by: dict):
        """topic id -> top TF-IDF terms over member titles, tags and summaries."""
        from sklearn.feature_extraction.text import TfidfVectorizer
        info = {r[0]: r[1:] for r in self.db.execute("SELECT id, title, tags, summary FROM sessions")}
        ids = sorted(by)
        docs = [" ".join(f"{info[s][0]} {info[s][1] or ''} {info[s][2] or ''}" for s in by[c] if s in info) for c in ids]
        try:
            vec = TfidfVectorizer(stop_words="english", max_features=4000, token_pattern=r"[A-Za-z][A-Za-z_\-]{2,}")
            M = vec.fit_transform(docs)
            terms = np.array(vec.get_feature_names_out())
            return {c: ", ".join(terms[np.argsort(-M[i].toarray()[0])[:5]]) for i, c in enumerate(ids)}
        except ValueError:
            return {c: "" for c in ids}

    def scoped_topics(self, sids):
        """Topics re-clustered within a subset of sessions (e.g. one repo). LLM names are cached by membership,
        so the same subset keeps its names; unnamed topics come back with keyword names and named=False."""
        ids, C = self.centroids()
        want = set(sids)
        idx = [i for i, s in enumerate(ids) if s in want]
        if not idx:
            return []
        sub = [ids[i] for i in idx]
        lab = cluster(C[idx], kmin=2, kmax=6) if len(sub) >= 6 else np.zeros(len(sub), int)
        by = defaultdict(list)
        for s, c in zip(sub, lab):
            by[int(c)].append(s)
        with self.lock:
            kw = self._keywords(by)
            out = []
            for c in sorted(by):
                mem = sorted(by[c])
                key = "tname:" + hashlib.sha1(",".join(mem).encode()).hexdigest()[:16]
                named = json.loads(self.meta(key) or "null")
                out.append(dict(id=c, key=key, named=bool(named), keywords=kw[c], sessions=mem,
                                name=named["name"] if named else (kw[c].split(",")[0].strip().title() or f"Topic {c + 1}"),
                                description=named["description"] if named else ""))
        return out

    def _save_topics(self, assign: dict):
        """Persist topic membership; topics that mostly match a previous one keep its LLM name."""
        old = [(json.loads(m or "[]"), name, desc) for m, name, desc in
               self.db.execute("SELECT members, name, description FROM topics")]
        by = {}
        for s, c in assign.items():
            by.setdefault(c, []).append(s)
        ids = sorted(by)
        kw = self._keywords(by)
        self.db.execute("DELETE FROM topics")
        for c in ids:
            mem = set(by[c])
            best = max(old, key=lambda o: len(mem & set(o[0])) / len(mem | set(o[0])), default=None)
            name = desc = None
            if best and len(mem & set(best[0])) / len(mem | set(best[0])) >= 0.5:
                name, desc = best[1], best[2]
            self.db.execute("INSERT INTO topics VALUES(?,?,?,?,?)", (c, name, desc, kw[c], json.dumps(sorted(mem))))

    # ---- queries -----------------------------------------------------------
    def topics(self):
        with self.lock:
            rows = self.db.execute("SELECT id, name, description, keywords, members FROM topics ORDER BY id").fetchall()
        return [dict(id=r[0], name=r[1] or (r[3] or "").split(",")[0].strip().title() or f"Topic {r[0] + 1}",
                     named=bool(r[1]), description=r[2] or "", keywords=r[3] or "", sessions=json.loads(r[4] or "[]"))
                for r in rows]

    def points(self, level="session"):
        with self.lock:
            if level == "chunk":
                rows = self.db.execute(
                    "SELECT c.id, c.session_id, s.title, s.source, s.cluster, c.x, c.y, c.z, s.project_name, c.summary, "
                    "s.started, c.idx FROM chunks c JOIN sessions s ON s.id=c.session_id WHERE c.x IS NOT NULL").fetchall()
                return {"level": level, "points": [
                    dict(id=r[0], session=r[1], title=r[2], source=r[3], cluster=r[4], p=r[5:8], project=r[8],
                         summary=r[9], started=r[10], idx=r[11]) for r in rows]}
            rows = self.db.execute(
                "SELECT id, title, source, project_name, cluster, started, updated, n_chunks, x, y, z, summary, tags "
                "FROM sessions WHERE x IS NOT NULL AND n_chunks>0").fetchall()
            return {"level": level, "points": [
                dict(id=r[0], session=r[0], title=r[1], source=r[2], project=r[3], cluster=r[4], started=r[5],
                     updated=r[6], n=r[7], p=r[8:11], summary=r[11], tags=r[12]) for r in rows]}

    def _allow(self, session_id=None, projects=None, sources=None, since=None, until=None, session_ids=None):
        if not (session_id or projects or sources or since or until or session_ids is not None):
            return None
        sql, args = "SELECT c.id FROM chunks c JOIN sessions s ON s.id=c.session_id WHERE 1=1", []
        if session_id:
            sql += " AND c.session_id=?"; args.append(session_id)
        if session_ids is not None:
            sql += f" AND c.session_id IN ({','.join('?' * len(session_ids)) or 'NULL'})"; args += list(session_ids)
        if projects:
            sql += f" AND s.project_name IN ({','.join('?' * len(projects))})"; args += list(projects)
        if sources:
            sql += f" AND s.source IN ({','.join('?' * len(sources))})"; args += list(sources)
        if since:
            sql += " AND s.updated >= ?"; args.append(since)
        if until:
            sql += " AND s.started <= ?"; args.append(until)
        return np.array([r[0] for r in self.db.execute(sql, args) if self.index.contains(r[0])], dtype=np.uint64)

    def search(self, query, k=8, session_id=None, projects=None, sources=None, since=None, until=None, session_ids=None):
        q = embed([query], query=True)
        with self.lock:
            allow = self._allow(session_id, projects, sources, since, until, session_ids)
            if allow is not None and not len(allow):
                return []
            if not self.db.execute("SELECT 1 FROM chunks LIMIT 1").fetchone():
                return []
            scores, ids = self.index.search(q, k=k, allowlist=allow) if allow is not None else self.index.search(q, k=k)
            out = []
            for sc, cid in zip(scores[0], ids[0]):
                r = self.db.execute(
                    "SELECT c.id, c.session_id, c.idx, c.text, s.title, s.source, s.project_name, c.summary, s.started "
                    "FROM chunks c JOIN sessions s ON s.id=c.session_id WHERE c.id=?", (int(cid),)).fetchone()
                if r:
                    out.append(dict(chunk=r[0], session=r[1], idx=r[2], text=r[3], title=r[4], source=r[5],
                                    project=r[6], summary=r[7], started=r[8], score=float(sc)))
            return out

    def centroids(self):
        with self.lock:
            rows = self.db.execute("SELECT session_id, vec FROM chunks").fetchall()
        by = {}
        for s, v in rows:
            by.setdefault(s, []).append(np.frombuffer(v, np.float16).astype(np.float32))
        ids = list(by)
        return ids, unit(np.stack([np.mean(by[s], axis=0) for s in ids])) if ids else np.zeros((0, config.EMBED_DIM))

    def similar(self, sid, k=6):
        ids, C = self.centroids()
        if sid not in ids:
            return []
        sims = C @ C[ids.index(sid)]
        out = []
        for j in np.argsort(-sims)[1:k + 1]:
            with self.lock:
                r = self.db.execute("SELECT title, project_name, source, started, summary FROM sessions WHERE id=?",
                                    (ids[j],)).fetchone()
            if r:
                out.append(dict(id=ids[j], title=r[0], project=r[1], source=r[2], started=r[3], summary=r[4], score=float(sims[j])))
        return out

    def session(self, sid):
        with self.lock:
            r = self.db.execute("SELECT id,source,project_name,title,path,started,updated,summary,tags,cluster,project "
                                "FROM sessions WHERE id=?", (sid,)).fetchone()
        if not r:
            return None
        s = parsers.parse(r[1], Path(r[4]))
        return dict(id=r[0], source=r[1], project=r[2], title=r[3], started=r[5], updated=r[6], summary=r[7], tags=r[8],
                    cluster=r[9], cwd=r[10], turns=[dict(role=a, text=b) for a, b in s.turns])
