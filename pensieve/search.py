"""One search box over files, code and agent sessions, Google-style:

    pricing model                 -> by meaning (semantic)
    "rate limit"                  -> must contain those exact words (case-insensitive substring)
    "rate limit" retries          -> contains the phrase, ranked by meaning of 'retries'
    -draft  -"old notes"          -> exclude
    kind:code  ext:pdf  in:anastomo   -> filters (kind is file, code or session; in matches the path or project)
"""
import re
import time
from pathlib import Path

import numpy as np

from .indexer import embed

CORPORA = {  # kind -> (chunk table, fts table)
    "file": ("file_chunks", "fts_files"),
    "code": ("code_chunks", "fts_code"),
    "session": ("chunks", "fts_sessions"),
}
FTS_VERSION = "trigram-v1"
_TOKEN = re.compile(r'(-?)"([^"]*)"?|(\S+)')
_FILTER = re.compile(r"^(kind|ext|in):(.+)$", re.I)
_KINDS = {"file": "file", "files": "file", "doc": "file", "docs": "file", "code": "code", "repo": "code",
          "session": "session", "sessions": "session", "chat": "session", "agent": "session"}


def parse(q: str) -> dict:
    exact, exclude, words, filters = [], [], [], {}
    for neg, phrase, word in _TOKEN.findall(q):
        if phrase or (neg and phrase == "" and word == ""):
            if phrase.strip():
                (exclude if neg else exact).append(phrase.strip())
            continue
        m = _FILTER.match(word)
        if m:
            key, val = m.group(1).lower(), m.group(2).lower().lstrip(".")
            if key == "kind":
                val = _KINDS.get(val, val)
            filters[key] = val
        elif word.startswith("-") and len(word) > 1:
            exclude.append(word[1:])
        elif word:
            words.append(word)
    return dict(semantic=" ".join(words), exact=exact, exclude=exclude, filters=filters)


# ---- full-text index ----------------------------------------------------------
def ensure_fts(db, lock, meta, log=print):
    """Trigram FTS5 tables mirroring each chunk table (kept in sync by triggers). Built once in the background."""
    with lock:
        for table, fts in CORPORA.values():
            db.executescript(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {fts} USING fts5(text, content='{table}', content_rowid='id', tokenize='trigram');
            CREATE TRIGGER IF NOT EXISTS {fts}_ai AFTER INSERT ON {table} BEGIN
              INSERT INTO {fts}(rowid, text) VALUES (new.id, new.text); END;
            CREATE TRIGGER IF NOT EXISTS {fts}_ad AFTER DELETE ON {table} BEGIN
              INSERT INTO {fts}({fts}, rowid, text) VALUES('delete', old.id, old.text); END;
            CREATE TRIGGER IF NOT EXISTS {fts}_au AFTER UPDATE OF text ON {table} BEGIN
              INSERT INTO {fts}({fts}, rowid, text) VALUES('delete', old.id, old.text);
              INSERT INTO {fts}(rowid, text) VALUES (new.id, new.text); END;
            """)
        db.commit()
        if meta("fts") == FTS_VERSION:
            return False
    for table, fts in CORPORA.values():
        t = time.time()
        with lock:
            db.execute(f"INSERT INTO {fts}({fts}) VALUES('rebuild')")
            db.commit()
        log(f"full-text index {fts}: {time.time() - t:.1f}s")
    meta("fts", FTS_VERSION)
    with lock:
        db.commit()
    return True


def _fts_query(phrases):
    return " AND ".join('"' + p.replace('"', '""') + '"' for p in phrases)


# ---- search ---------------------------------------------------------------------
class Searcher:
    def __init__(self, store, repos, files):
        self.store, self.repos, self.files = store, repos, files
        self.db, self.lock = store.db, store.lock

    def _index(self, kind):
        return {"file": self.files.index, "code": self.repos.index, "session": self.store.index}[kind]

    def find(self, q: str, limit=20, kinds=None):
        pq = parse(q)
        f = pq["filters"]
        kinds = [k for k in (kinds or CORPORA) if not f.get("kind") or f["kind"] == k]
        if f.get("ext"):
            kinds = [k for k in kinds if k != "session"]
        qv = embed([pq["semantic"]], query=True)[0] if pq["semantic"] else None
        hits = {}  # (kind, chunk id) -> score dict
        exact_short = [p for p in pq["exact"] if len(p) < 3]  # trigram needs 3+ chars: checked by substring instead
        exact_fts = [p for p in pq["exact"] if len(p) >= 3]
        for kind in kinds:
            table, fts = CORPORA[kind]
            if pq["exact"]:
                with self.lock:
                    if exact_fts:
                        rows = self.db.execute(f"SELECT rowid, bm25({fts}) FROM {fts} WHERE {fts} MATCH ? ORDER BY 2 LIMIT 400",
                                               (_fts_query(exact_fts),)).fetchall()
                    else:
                        like = " AND ".join("instr(lower(text), ?)>0" for _ in exact_short)
                        rows = self.db.execute(f"SELECT id, 0 FROM {table} WHERE {like} LIMIT 400",
                                               [p.lower() for p in exact_short]).fetchall()
                for cid, bm in rows:
                    hits[(kind, cid)] = dict(exact=True, bm25=-bm, sem=None)
                if kind == "file":  # file names count as an exact hit too
                    with self.lock:
                        for (cid,) in self.db.execute(
                                "SELECT MIN(c.id) FROM files f JOIN file_chunks c ON c.path=f.path WHERE "
                                + " AND ".join("instr(lower(f.name), ?)>0" for _ in pq["exact"]) + " GROUP BY f.path LIMIT 200",
                                [p.lower() for p in pq["exact"]]).fetchall():
                            hits.setdefault((kind, cid), dict(exact=True, bm25=5.0, sem=None, name=True))
            elif qv is not None:
                idx = self._index(kind)
                with self.lock:
                    has = self.db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                    if has:
                        sc, ids = idx.search(qv[None, :], k=max(60, limit * 4))
                        for s, cid in zip(sc[0], ids[0]):
                            hits[(kind, int(cid))] = dict(exact=False, bm25=0, sem=float(s))
        rows = self._rows(hits)
        if qv is not None and pq["exact"]:  # phrase matches ranked by meaning of the remaining words
            for key, r in rows.items():
                hits[key]["sem"] = float(np.frombuffer(r["vec"], np.float16).astype(np.float32) @ qv)
        out = []
        words = [w.lower() for w in pq["semantic"].split() if len(w) >= 3]
        for key, r in rows.items():
            h = hits[key]
            low = r["text"].lower()
            if any(p.lower() not in low and not h.get("name") for p in exact_short):
                continue
            where = f"{r['title']} {r['subtitle']} {r['path'] or ''}".lower()
            if any(x.lower() in low or x.lower() in where for x in pq["exclude"]):
                continue
            if f.get("ext") and not (r["path"] or "").lower().endswith("." + f["ext"]):
                continue
            if f.get("in") and f["in"] not in f"{r['path'] or ''} {r['subtitle']}".lower():
                continue
            if h["sem"] is not None:
                score = h["sem"] + (0.05 if any(w in r["title"].lower() for w in words) else 0)
            else:
                score = h["bm25"]
            snippet, hl = _snippet(r["text"], pq["exact"] or words)
            out.append(dict(id=r["id"], kind=key[0], title=r["title"], subtitle=r["subtitle"], path=r["path"],
                            line=r["line"], snippet=snippet, highlights=hl, score=round(score, 4), ext=r["ext"],
                            match="both" if pq["exact"] and qv is not None else "exact" if pq["exact"] else "semantic",
                            mtime=r.get("mtime")))
        out.sort(key=lambda x: (-x["score"], -_ts(x["mtime"])))
        best, seen = [], set()
        for x in out:  # best chunk per file / session (code ids are per chunk, so dedupe code by file)
            k = x["path"] if x["kind"] == "code" else x["id"]
            if k in seen:
                continue
            seen.add(k)
            best.append(x)
        return dict(query=pq, results=best[:limit])

    def _rows(self, hits):
        by = {}
        for kind, cid in hits:
            by.setdefault(kind, []).append(cid)
        out, home = {}, str(Path.home())
        with self.lock:
            for kind, ids in by.items():
                ph = ",".join("?" * len(ids))
                if kind == "file":
                    q = (f"SELECT c.id, c.text, c.vec, c.start, f.path, f.name, f.ext, f.mtime, f.kind FROM file_chunks c "
                         f"JOIN files f ON f.path=c.path WHERE c.id IN ({ph})")
                    for cid, text, vec, start, path, name, ext, mtime, k in self.db.execute(q, ids):
                        out[(kind, cid)] = dict(id=f"file:{path}", text=text, vec=vec, title=name, ext=ext, path=path,
                                                subtitle=str(Path(path).parent).replace(home, "~", 1), mtime=mtime,
                                                line=start if start and k in ("code", "doc", "data") else None)
                elif kind == "code":
                    q = (f"SELECT c.id, c.text, c.vec, c.start, c.path, r.root, r.name, c.last_ts, c.wt, w.branch FROM code_chunks c "
                         f"JOIN repos r ON r.root=c.repo LEFT JOIN worktrees w ON w.path=c.wt WHERE c.id IN ({ph})")
                    for cid, text, vec, start, path, root, rname, ts, wt, branch in self.db.execute(q, ids):
                        p = Path(path)
                        where = f"{rname}/{p.parent}".rstrip("/.") if str(p.parent) != "." else rname
                        out[(kind, cid)] = dict(id=f"code:{cid}", text=text, vec=vec, title=p.name, ext=p.suffix.lstrip("."),
                                                path=str(Path(wt or root) / path), line=start, mtime=ts,
                                                subtitle=where + (f" @ {branch or Path(wt).name}" if wt else ""))
                else:
                    q = (f"SELECT c.id, c.text, c.vec, s.id, s.title, s.source, s.project_name, s.updated FROM chunks c "
                         f"JOIN sessions s ON s.id=c.session_id WHERE c.id IN ({ph})")
                    for cid, text, vec, sid, title, src, proj, upd in self.db.execute(q, ids):
                        out[(kind, cid)] = dict(id=f"session:{sid}", text=text, vec=vec, title=title or sid, ext="",
                                                path=None, line=None, mtime=upd,
                                                subtitle=" · ".join(x for x in (src, proj, (upd or "")[:10]) if x))
        return out


def _ts(m):
    """mtime (epoch seconds) or ISO date -> epoch, for tie-breaking toward recent items."""
    if isinstance(m, (int, float)):
        return m
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(m).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def _snippet(text: str, terms, width=220):
    flat = re.sub(r"\s+", " ", text).strip()
    low = flat.lower()
    pos = min((i for i in (low.find(t.lower()) for t in terms) if i >= 0), default=0)
    start = max(0, pos - 60)
    if start:
        sp = flat.find(" ", start)
        start = sp + 1 if 0 <= sp < pos else start
    snip = flat[start:start + width]
    prefix = "…" if start else ""
    snip = prefix + snip + ("…" if start + width < len(flat) else "")
    hl, sl = [], snip.lower()
    for t in terms:
        t = t.lower()
        i = sl.find(t)
        while i >= 0 and t:
            hl.append([i, i + len(t)])
            i = sl.find(t, i + len(t))
    return snip, sorted(hl)
