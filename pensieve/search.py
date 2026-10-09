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

from .indexer import CODE_QUERY, embed

CORPORA = {  # kind -> (chunk table, fts table)
    "file": ("file_chunks", "fts_files"),
    "code": ("code_chunks", "fts_code"),
    "session": ("chunks", "fts_sessions"),
}
FTS_VERSION = "trigram-v1"
WORDS_VERSION = "words-v1"
WORDS_FTS = "fts_code_words"  # code split into words (camelCase and snake_case too), for keyword ranking
_TOKEN = re.compile(r'(-?)"([^"]*)"?|(\S+)')
_FILTER = re.compile(r"^(kind|ext|in):(.+)$", re.I)
_KINDS = {"file": "file", "files": "file", "doc": "file", "docs": "file", "code": "code", "repo": "code",
          "session": "session", "sessions": "session", "chat": "session", "agent": "session"}


def code_words(text):
    """Lowercase words of code text with identifiers split: 'fetchUserID_list' -> 'fetch user id list'."""
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9]*", text or ""):
        out += [p.lower() for p in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", w) if len(p) > 1]
    return " ".join(out)


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
        db.executescript(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {WORDS_FTS} USING fts5(words, content='', contentless_delete=1);
        CREATE TRIGGER IF NOT EXISTS {WORDS_FTS}_ai AFTER INSERT ON code_chunks BEGIN
          INSERT INTO {WORDS_FTS}(rowid, words) VALUES (new.id, code_words(new.path || ' ' || new.text)); END;
        CREATE TRIGGER IF NOT EXISTS {WORDS_FTS}_ad AFTER DELETE ON code_chunks BEGIN
          DELETE FROM {WORDS_FTS} WHERE rowid=old.id; END;
        CREATE TRIGGER IF NOT EXISTS {WORDS_FTS}_au AFTER UPDATE OF text ON code_chunks BEGIN
          DELETE FROM {WORDS_FTS} WHERE rowid=old.id;
          INSERT INTO {WORDS_FTS}(rowid, words) VALUES (new.id, code_words(new.path || ' ' || new.text)); END;
        """)
        db.commit()
        fresh, words_fresh = meta("fts") == FTS_VERSION, meta("fts_words") == WORDS_VERSION
    if fresh and words_fresh:
        return False
    if not words_fresh:
        t = time.time()
        with lock:
            db.execute(f"DELETE FROM {WORDS_FTS}")
            db.execute(f"INSERT INTO {WORDS_FTS}(rowid, words) SELECT id, code_words(path || ' ' || text) FROM code_chunks")
            meta("fts_words", WORDS_VERSION)
            db.commit()
        log(f"full-text index {WORDS_FTS}: {time.time() - t:.1f}s")
    if fresh:
        return True
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


_TESTY = re.compile(r"(^|/)(tests?|__tests__|spec|specs|__mocks__|mocks?|fixtures?|stories|e2e)/|"
                    r"([._-](test|spec|stories|mock)s?\.[a-z]+$)|(^|/)test_[^/]+$|/docs?/schema/")
_SYMBOL = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function|module|interface|type|struct|fn|func|"
                     r"const|let|CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|FUNCTION))\s+([A-Za-z_][\w.]*)", re.M | re.I)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def code_names(q):
    """Words in a query that look like code names (snake_case, camelCase, CONSTANT_CASE, name2), or the whole query
    when it is one word."""
    ws = _NAME.findall(q)
    if len(ws) == 1 and ws[0] == q.strip():
        return ws
    return [w for w in ws if "_" in w.strip("_") or re.search(r"[a-z][A-Z]|[A-Z]{2,}[a-z]|[a-z]\d|^[A-Z][a-z]+[A-Z]", w)]


def _fts_query(phrases):
    return " AND ".join('"' + p.replace('"', '""') + '"' for p in phrases)


# ---- search ---------------------------------------------------------------------
class Searcher:
    def __init__(self, store, repos, files):
        self.store, self.repos, self.files = store, repos, files
        self.db, self.lock = store.db, store.lock

    def _index(self, kind):
        return {"file": self.files.index, "code": self.repos.index, "session": self.store.index}[kind]

    def find(self, q: str, limit=20, kinds=None, scope=None):
        """`scope`: a scopes.Scope (None = everything); every corpus is filtered to it."""
        from . import scopes
        pq = parse(q)
        f = pq["filters"]
        kinds = [k for k in (kinds or CORPORA) if not f.get("kind") or f["kind"] == k]
        if f.get("ext"):
            kinds = [k for k in kinds if k != "session"]
        qv = embed([pq["semantic"]], query=True)[0] if pq["semantic"] else None
        code_ranked = {}  # (kind, chunk id) -> file-level score from _code
        hits = {}  # (kind, chunk id) -> score dict
        exact_short = [p for p in pq["exact"] if len(p) < 3]  # trigram needs 3+ chars: checked by substring instead
        exact_fts = [p for p in pq["exact"] if len(p) >= 3]
        for kind in kinds:
            table, fts = CORPORA[kind]
            allow = scopes.allowlist(scope, kind, self.store, self.repos, self._index(kind)) if scope else None
            if allow is not None and not len(allow):
                continue
            allowed = set(allow.tolist()) if allow is not None and pq["exact"] else None
            if pq["exact"]:
                with self.lock:
                    if exact_fts:
                        rows = self.db.execute(f"SELECT rowid, bm25({fts}) FROM {fts} WHERE {fts} MATCH ? ORDER BY 2 LIMIT ?",
                                               (_fts_query(exact_fts), 400 if allowed is None else 5000)).fetchall()
                    else:
                        like = " AND ".join("instr(lower(text), ?)>0" for _ in exact_short)
                        rows = self.db.execute(f"SELECT id, 0 FROM {table} WHERE {like} LIMIT 400",
                                               [p.lower() for p in exact_short]).fetchall()
                for cid, bm in rows:
                    if allowed is None or cid in allowed:
                        hits[(kind, cid)] = dict(exact=True, bm25=-bm, sem=None)
                if kind == "file":  # file names count as an exact hit too
                    with self.lock:
                        for (cid,) in self.db.execute(
                                "SELECT MIN(c.id) FROM files f JOIN file_chunks c ON c.path=f.path WHERE "
                                + " AND ".join("instr(lower(f.name), ?)>0" for _ in pq["exact"]) + " GROUP BY f.path LIMIT 200",
                                [p.lower() for p in pq["exact"]]).fetchall():
                            if allowed is None or cid in allowed:  # the name itself matches: rank above text matches
                                h = hits.setdefault((kind, cid), dict(exact=True, bm25=0, sem=None))
                                h.update(bm25=h["bm25"] + 50.0, name=True)
            elif qv is not None and kind == "code":
                for cid, sc in self._code(pq["semantic"], allow).items():
                    hits[(kind, cid)] = dict(exact=False, bm25=0, sem=sc)
                    code_ranked[(kind, cid)] = sc
            elif qv is not None:
                idx = self._index(kind)
                with self.lock:
                    has = self.db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                    if has:
                        k = max(60, limit * 4)
                        sc, ids = (idx.search(qv[None, :], k=min(k, len(allow)), allowlist=allow) if allow is not None
                                   else idx.search(qv[None, :], k=k))
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
            if key in code_ranked:
                score = code_ranked[key]
            elif h["sem"] is not None:
                score = h["sem"] + (0.05 if any(w in r["title"].lower() for w in words) else 0)
            else:
                score = h["bm25"]
            snippet, hl = _snippet(r["text"], pq["exact"] or words)
            out.append(dict(id=r["id"], kind=key[0], title=r["title"], subtitle=r["subtitle"], path=r["path"],
                            line=r["line"], snippet=snippet, highlights=hl, score=round(score, 4), ext=r["ext"],
                            match="both" if pq["exact"] and qv is not None else "exact" if pq["exact"] else "semantic",
                            mtime=r.get("mtime"), key=r.get("key"), is_dir=r.get("is_dir", False), author=r.get("author")))
        out.sort(key=lambda x: (-x["score"], -_ts(x["mtime"])))
        best, seen = [], set()
        for x in out:  # best chunk per file / session (code ids are per chunk, so dedupe code by file)
            k = x.pop("key", None) or x["id"]  # code: one result per repo file, across worktrees
            if k in seen:
                continue
            seen.add(k)
            best.append(x)
        return dict(query=pq, results=best[:limit], scope=scopes.describe(scope))

    def _code(self, q, allow, k=400):
        """Code search tuned on bench/ (agent-style queries: descriptions, names, error strings, mixes of these).

        Candidates: the top-k chunks by meaning and by keywords (identifier-aware), plus chunks containing the
        whole query or a code name from it. Each file is scored by its best chunk:
            z(meaning) + 0.2 z(keywords) - 0.5 [test/spec/schema file]
            + 3 [contains the whole query] + log(1 + times) + [same case]       (not for a bare name)
            + 2 [contains a code name from the query] / log2(1 + files that do)
            + 2 [defines that name, and at most 3 files do]
        returned on the cosine scale (best chunk similarity + sigma * the rest) so it merges with other corpora.
        Returns {chunk id: score} with the best chunk of each file."""
        allowed = set(allow.tolist()) if allow is not None else None
        ok = (lambda c: c in allowed) if allowed is not None else (lambda c: True)
        qv = embed([q], query=CODE_QUERY)[0]
        cand = set()
        idx = self.repos.index
        with self.lock:
            if not self.db.execute("SELECT 1 FROM code_chunks LIMIT 1").fetchone():
                return {}
            sc, ids = (idx.search(qv[None, :], k=min(k, len(allow)), allowlist=allow) if allow is not None
                       else idx.search(qv[None, :], k=k))
            cand.update(int(c) for c in ids[0])
            kw = {}
            terms = sorted(set(code_words(q).split()))
            if terms:
                fq = " OR ".join(f'"{t}"' for t in terms)
                for cid, bm in self.db.execute(f"SELECT rowid, bm25({WORDS_FTS}) FROM {WORDS_FTS} WHERE {WORDS_FTS} "
                                               f"MATCH ? ORDER BY 2 LIMIT ?", (fq, k if allowed is None else 20000)):
                    if ok(cid) and len(kw) < k:
                        kw[cid] = -bm
            cand.update(kw)
            ql = q.strip()
            phrase = set()
            if len(ql) >= 8:
                phrase = {c for (c,) in self.db.execute("SELECT rowid FROM fts_code WHERE fts_code MATCH ? LIMIT ?",
                                                        (_fts_query([ql]), 500 if allowed is None else 5000)) if ok(c)}
            names = code_names(ql)
            by_name = {}
            for n in names:
                if len(n) < 3:
                    continue
                by_name[n] = {c for (c,) in self.db.execute("SELECT rowid FROM fts_code WHERE fts_code MATCH ? LIMIT ?",
                                                            (_fts_query([n]), 500 if allowed is None else 5000)) if ok(c)}
                cand |= by_name[n]
            cand |= phrase
            if not cand:
                return {}
            rows = {}
            cl = list(cand)
            for i in range(0, len(cl), 900):
                part = cl[i:i + 900]
                for cid, text, vec, path, repo in self.db.execute(
                        f"SELECT id, text, vec, path, repo FROM code_chunks WHERE id IN ({','.join('?' * len(part))})", part):
                    rows[cid] = (text, vec, f"{repo}:{path}", path)
        files, best, sem, kwf = {}, {}, {}, {}
        for cid, (text, vec, fkey, path) in rows.items():
            s_ = float(np.frombuffer(vec, np.float16).astype(np.float32) @ qv)
            files.setdefault(fkey, path)
            if s_ > sem.get(fkey, -9):
                sem[fkey], best[fkey] = s_, cid
            kwf[fkey] = max(kwf.get(fkey, 0.0), kw.get(cid, 0.0))
        keys = list(files)
        S = np.array([sem[f] for f in keys], np.float32)
        K = np.array([kwf[f] for f in keys], np.float32)
        z = lambda x: (x - x.mean()) / (x.std() + 1e-9)
        extra = 0.2 * z(K) - 0.5 * np.array([bool(_TESTY.search(files[f])) for f in keys], np.float32)
        pos = {f: i for i, f in enumerate(keys)}
        if phrase:
            hit, cnt, cased = np.zeros(len(keys)), np.zeros(len(keys)), np.zeros(len(keys))
            for c in phrase:
                if c in rows:
                    j = pos[rows[c][2]]
                    hit[j] = 1
                    cnt[j] += rows[c][0].lower().count(ql.lower())
                    cased[j] = max(cased[j], float(ql in rows[c][0]))
            extra += 3 * hit + ((np.log1p(cnt) + cased) if names != [ql] else 0)
        for n, cids in by_name.items():
            rx = re.compile(r"(?<![\w])" + re.escape(n) + r"(?![\w])")
            has, defs = np.zeros(len(keys)), set()
            for c in cids:
                if c in rows and rx.search(rows[c][0]):
                    has[pos[rows[c][2]]] = 1
                    if any(m.group(1).split(".")[-1] == n for m in _SYMBOL.finditer(rows[c][0])):
                        defs.add(pos[rows[c][2]])
            if has.any():
                extra += 2 * has / np.log2(1 + has.sum())
            if 0 < len(defs) <= 3:
                extra[list(defs)] += 2
        total = S + S.std() * extra  # the tuned z-score order, kept on the cosine scale
        return {best[f]: float(total[i]) for i, f in enumerate(keys)}

    def _rows(self, hits):
        by = {}
        for kind, cid in hits:
            by.setdefault(kind, []).append(cid)
        out, home = {}, str(Path.home())
        with self.lock:
            for kind, ids in by.items():
                ph = ",".join("?" * len(ids))
                if kind == "file":
                    q = (f"SELECT c.id, c.text, c.vec, c.start, f.path, f.name, f.ext, f.mtime, f.kind, f.author FROM file_chunks c "
                         f"JOIN files f ON f.path=c.path WHERE c.id IN ({ph})")
                    for cid, text, vec, start, path, name, ext, mtime, k, author in self.db.execute(q, ids):
                        parent = str(Path(path).parent).replace(home, "~", 1)
                        out[(kind, cid)] = dict(id=f"file:{path}", text=text, vec=vec, title=name, ext=ext, path=path,
                                                subtitle=("Folder in " + parent) if k == "folder" else parent, mtime=mtime,
                                                is_dir=k == "folder", author=author,
                                                line=start if start and k in ("code", "doc", "data") else None)
                elif kind == "code":
                    q = (f"SELECT c.id, c.text, c.vec, c.start, c.path, r.root, r.name, c.last_ts, c.wt, w.branch FROM code_chunks c "
                         f"JOIN repos r ON r.root=c.repo LEFT JOIN worktrees w ON w.path=c.wt WHERE c.id IN ({ph})")
                    for cid, text, vec, start, path, root, rname, ts, wt, branch in self.db.execute(q, ids):
                        p = Path(path)
                        where = f"{rname}/{p.parent}".rstrip("/.") if str(p.parent) != "." else rname
                        out[(kind, cid)] = dict(id=f"code:{cid}", text=text, vec=vec, title=p.name, ext=p.suffix.lstrip("."),
                                                path=str(Path(wt or root) / path), line=start, mtime=ts, key=f"{root}:{path}",
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
