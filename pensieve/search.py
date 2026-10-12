"""One search box over files, code and agent sessions, Google-style:

    pricing model                 -> by meaning (semantic)
    "rate limit"                  -> must contain those exact words (case-insensitive substring)
    "rate limit" retries          -> contains the phrase, ranked by meaning of 'retries'
    -draft  -"old notes"          -> exclude
    kind:code  ext:pdf  in:anastomo   -> filters (kind is file, code or session; in matches the path or project)
"""
import bisect
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from pensieve import indexer
from pensieve.indexer import CODE_QUERY, embed
from pensieve.layout import unit

CORPORA = {  # kind -> (chunk table, fts table)
    "file": ("file_chunks", "fts_files"),
    "code": ("code_chunks", "fts_code"),
    "session": ("chunks", "fts_sessions"),
}
FTS_VERSION = "trigram-v1"
WORDS_VERSION = "words-v3"  # v3: a `repo` column holds the chunk's repo_token; it filters, and bm25 gives it no weight
WORDS_FTS = "fts_code_words"  # code split into words (camelCase and snake_case too), for keyword ranking


_WORDS_OF = "code_words({r}.path || ' ' || {r}.text), repo_token({r}.repo)"  # one chunk's (words, repo) columns


def repo_token(root):
    """A made-up word for one repo, in the keyword index's `repo` column, so a search scoped to a repo asks SQLite for
    "these words AND this repo" rather than scoring every repo's matches. Letters only, so FTS5 keeps it whole."""
    return "zzrepo" + "".join(chr(97 + int(c, 16)) for c in hashlib.sha1(str(root).encode()).hexdigest()[:12])
_TOKEN = re.compile(r'(-?)"([^"]*)"?|(\S+)')
_FILTER = re.compile(r"^(kind|ext|in|by):(.+)$", re.I)
_KINDS = {"file": "file", "files": "file", "doc": "doc", "docs": "doc", "document": "doc", "documents": "doc",
          "code": "code", "session": "session", "sessions": "session", "chat": "session", "agent": "session",
          "folder": "folder", "folders": "folder", "dir": "folder", "dirs": "folder", "directory": "folder",
          "repo": "repo", "repos": "repo", "repository": "repo", "person": "person", "people": "person",
          "who": "person", "author": "person"}
# which chunk corpora each kind: searches (file = documents and code); folder/repo/person are results of their own
_KIND_CORPORA = {"file": ["file", "code"], "doc": ["file"], "code": ["code"], "session": ["session"],
                 "folder": [], "repo": [], "person": []}


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
        elif re.match(r"-[A-Za-z_]\w\w", word):  # -word (3+ letters) excludes; "-p", "-rf", "--flag", "-1" are text
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
        words_fresh = meta("fts_words") == WORDS_VERSION
        if not words_fresh:  # the table's columns changed: CREATE ... IF NOT EXISTS would keep the old ones
            db.executescript(f"DROP TABLE IF EXISTS {WORDS_FTS}_vocab; DROP TABLE IF EXISTS {WORDS_FTS};")
        db.executescript(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {WORDS_FTS} USING fts5(words, repo, content='', contentless_delete=1);
        CREATE VIRTUAL TABLE IF NOT EXISTS {WORDS_FTS}_vocab USING fts5vocab({WORDS_FTS}, 'row');
        DROP TRIGGER IF EXISTS {WORDS_FTS}_ai;
        DROP TRIGGER IF EXISTS {WORDS_FTS}_au;
        CREATE TRIGGER {WORDS_FTS}_ai AFTER INSERT ON code_chunks BEGIN
          INSERT INTO {WORDS_FTS}(rowid, words, repo) VALUES (new.id, {_WORDS_OF.format(r="new")}); END;
        CREATE TRIGGER IF NOT EXISTS {WORDS_FTS}_ad AFTER DELETE ON code_chunks BEGIN
          DELETE FROM {WORDS_FTS} WHERE rowid=old.id; END;
        CREATE TRIGGER {WORDS_FTS}_au AFTER UPDATE OF text ON code_chunks BEGIN
          DELETE FROM {WORDS_FTS} WHERE rowid=old.id;
          INSERT INTO {WORDS_FTS}(rowid, words, repo) VALUES (new.id, {_WORDS_OF.format(r="new")}); END;
        """)
        db.commit()
        fresh = meta("fts") == FTS_VERSION
    if fresh and words_fresh:
        return False
    if not words_fresh:
        t = time.time()
        with lock:
            db.execute(f"INSERT INTO {WORDS_FTS}(rowid, words, repo) SELECT id, {_WORDS_OF.format(r='code_chunks')} "
                       f"FROM code_chunks")
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


class _Laps:
    """How long each phase of a search took (milliseconds), in `ms` for the usage log; with PENSIEVE_TRACE set, also
    printed."""

    def __init__(self, q):
        self.on = bool(os.environ.get("PENSIEVE_TRACE"))
        self.q, self.t, self.ms = q, time.perf_counter(), {}

    def __call__(self, name, done=False):
        now = time.perf_counter()
        self.ms[name] = round(self.ms.get(name, 0) + (now - self.t) * 1000, 2)
        self.t = now
        if done and self.on:
            print(f"trace find {self.q[:40]!r}: " + ", ".join(f"{k} {v:.0f}" for k, v in self.ms.items()), flush=True)


FOLDER_REBUILD = 30  # seconds between background rebuilds of the folder index while the index keeps changing
COMMON_WORD = 0.05  # keyword ranking skips query words in more than this share of code chunks (see _rare_terms)
_NO_FOLDERS = dict(fp=None, at=0, keys=[], dirs={}, C=np.zeros((0, 1), np.float32), ids=[], people=Counter(),
                   by_word={}, words=[], authors={}, doc_author={})  # until the first folder index is built
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
        self._fbuilding = False

    def _index(self, kind):
        return {"file": self.files.index, "code": self.repos.index, "session": self.store.index}[kind]

    def find(self, q: str, limit=20, kinds=None, scope=None, retry=True):
        """`scope`: a scopes.Scope (None = everything); every corpus is filtered to it. `kinds`: chunk corpora
        (file = documents, code, session). In the query, kind:file|doc|code|session|folder|repo|person picks what
        comes back (file = documents and code; a repo is a folder too) and by:<name> keeps what that person wrote.
        Edits the file watcher saw are indexed first, so a search right after an edit finds the new text."""
        indexer.searches_running.add(1)
        try:
            return self._find(q, limit, kinds, scope, retry)
        finally:
            indexer.searches_running.add(-1)

    def _catch_up(self, roots=(), paths=()):
        """Index pending edits now. This search stops counting as running meanwhile: the indexing it waits for
        would otherwise step aside for it."""
        indexer.searches_running.add(-1)
        try:
            return (self.repos.refresh(roots) or 0) + (self.files.refresh(paths) or 0)
        finally:
            indexer.searches_running.add(1)

    def _find(self, q, limit, kinds, scope, retry):
        from pensieve import scopes
        lap = _Laps(q)
        if retry:
            self._catch_up()
            lap("catch-up")
        pq = parse(q)
        f = pq["filters"]
        kf = f.get("kind")
        kinds = [k for k in (kinds or CORPORA) if not kf or k in _KIND_CORPORA.get(kf, [kf])]
        if f.get("ext"):
            kinds = [k for k in kinds if k != "session"]
        by = f.get("by")
        if by:  # what that person wrote: code by blame, documents by author, sessions by agent
            kinds = [k for k in kinds if k != "session" or any(by in a for a in ("claude", "codex", "qwen"))]
        qv = code_qv = None  # the query under the general instruction, and under CODE_QUERY (code, folders, people)
        if pq["semantic"]:
            if "code" in kinds or kf in (None, "folder", "repo", "person"):
                qv, code_qv = embed([pq["semantic"]] * 2, query=[True, CODE_QUERY])
            else:
                qv = embed([pq["semantic"]], query=True)[0]
        lap("embed")
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
                if by:
                    allow = self._by_allow(by, allow)
                    if not len(allow):
                        continue
                for cid, sc in self._code(pq["semantic"], allow, roots=scope.repos if scope is not None else None,
                                          qv=code_qv).items():
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
        lap("corpora")
        rows = self._rows(hits)
        lap("rows")
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
            if by and not self._by_ok(by, key[0], r):
                continue
            if key in code_ranked:
                score = code_ranked[key]
            elif h["sem"] is not None:
                score = h["sem"] + (0.05 if any(w in r["title"].lower() for w in words) else 0)
            else:
                score = h["bm25"]
            out.append(dict(id=r["id"], kind=key[0], title=r["title"], subtitle=r["subtitle"], path=r["path"],
                            line=r["line"], row=key, score=round(score, 4), ext=r["ext"],
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
        lap("rank")
        extra = []
        if not pq["semantic"] and kf == "repo":  # kind:repo alone: every repo, biggest first
            extra += self._folders("", scope, repos_only=True, limit=limit, by=by, ext=f.get("ext"), within=f.get("in"))
        if pq["semantic"] and kf in (None, "folder", "repo"):
            extra += self._folders(pq["semantic"], scope, repos_only=kf == "repo", strong_only=kf is None, code_qv=code_qv,
                                   limit=limit, by=by, ext=f.get("ext"), within=f.get("in"))
        if pq["semantic"] and kf in (None, "person") and not by:
            extra += self._people(pq["semantic"], scope, named_only=kf is None, limit=8, code_qv=code_qv)
        if kf in ("folder", "repo", "person"):
            best = extra
        elif extra:  # a folder or person the query names outright goes first
            best = extra + [x for x in best if x["id"] not in {e["id"] for e in extra}]
        lap("folders+people")
        results, roots, paths = self._on_disk(best, rows, limit)
        for x in results:  # snippets for what is shown only, not for every candidate
            if "row" in x:
                x["snippet"], x["highlights"] = _snippet(rows[x.pop("row")]["text"], pq["exact"] or words)
        lap("on-disk")
        if retry and (roots or paths):  # changed after it was indexed and the watcher hasn't caught it yet
            n = self._catch_up(roots, paths)
            if n:
                return self.find(q, limit, kinds, scope, retry=False)
        shown = 0
        for x in results:
            if x["kind"] == "code" and x.get("line"):
                text = rows[("code", int(x["id"].partition(":")[2]))]["text"]
                off, x["definition"], x["focus"] = _focus(text, pq["semantic"], pq["exact"])
                x["focus_line"] = x["line"] + off
                x["chunk_line"], x["line"] = x["line"], x["focus_line"]  # apps open code at the line that matched
                if shown < 3:  # enough of the best hits to judge them without opening the file
                    shown += 1
                    x["excerpt"] = _excerpt(text, x["chunk_line"], pq["semantic"], pq["exact"])
        lap("lines", done=True)
        return dict(query=pq, results=results, scope=scopes.describe(scope), timing=lap.ms)

    @staticmethod
    def _on_disk(best, rows, limit):
        """The first `limit` results that still exist on disk, and the repos and documents of results whose file
        changed since it was indexed. Those are marked `stale`; a code hit whose text only moved gets its new line."""
        out, roots, paths = [], set(), set()
        for x in best:
            if len(out) == limit:
                break
            kind, path = x["kind"], x.get("path")
            if kind not in ("code", "file") or not path or x.get("is_dir"):
                out.append(x)
                continue
            try:
                if kind == "file":
                    if os.stat(path).st_mtime != x["mtime"]:
                        x["stale"] = True
                        paths.add(path)
                else:
                    r = rows[("code", int(x["id"].partition(":")[2]))]
                    text = Path(path).read_bytes().decode("utf8", "replace")
                    lines, n = text.split("\n"), r["text"].count("\n") + 1
                    if "\n".join(lines[x["line"] - 1:x["line"] - 1 + n]) != r["text"]:
                        x["stale"] = True
                        roots.add(r["root"])
                        i = text.find(r["text"])
                        if i >= 0:
                            x["line"] = text.count("\n", 0, i) + 1
            except FileNotFoundError:
                if kind == "file":
                    paths.add(path)
                else:
                    roots.add(rows[("code", int(x["id"].partition(":")[2]))]["root"])
                continue
            except OSError:
                pass
            out.append(x)
        return out, roots, paths

    def _rare_terms(self, terms):
        """The query words worth keyword-ranking on. A word in more than COMMON_WORD of all code chunks weighs almost
        nothing in BM25, yet SQLite scores every chunk that contains it (about 0.1 s for a word like "data"), so it
        is left out; the two rarest words are kept when every word is that common. Call with self.lock held."""
        now = time.time()
        if not hasattr(self, "_df") or now - self._df_at > 300:
            self._df, self._df_at = {}, now
            self._n_chunks = self.db.execute("SELECT COUNT(*) FROM code_chunks").fetchone()[0] or 1
        try:
            for t in terms:
                if t not in self._df:
                    r = self.db.execute(f"SELECT doc FROM {WORDS_FTS}_vocab WHERE term=?", (t,)).fetchone()
                    self._df[t] = r[0] if r else 0
        except sqlite3.OperationalError:  # the vocabulary table comes with ensure_fts, which runs in the background
            return terms
        keep = [t for t in terms if self._df[t] <= COMMON_WORD * self._n_chunks]
        return keep or sorted(terms, key=self._df.get)[:2]

    def _fragments(self, q):
        """The fixed text of a message as it shows up at runtime ("company_id[20879] not found in directory"): the
        query split at numbers and at words no code contains (the values filled into a format string), fragments of
        8+ characters, and whether there was anything to split at. Call with self.lock held, after _rare_terms has
        looked up the query's words."""
        out = []
        for part in re.split(r"\d+", q):
            cur = ""
            for tok in re.findall(r"\s+|\S+", part):
                terms = code_words(tok).split()
                if terms and all(self._df.get(t, 1) == 0 for t in terms):
                    out.append(cur)
                    cur = ""
                else:
                    cur += tok
            out.append(cur)
        holes = len(out) > 1
        out = [f.strip() for f in out if len(f.strip()) >= 8]
        # A value that is also a word in the code ("jdoe" in "Writing workers to jdoe") is not split out above, so
        # also offer each fragment with one word removed: the words before it, and the words after it.
        # Values are rare in code, so the words the fewest code chunks contain are removed first.
        more = []
        for f in out:
            toks = re.findall(r"\S+", f)
            for i, tok in enumerate(toks):
                df = min((self._df.get(t, 0) for t in code_words(tok).split()), default=0)
                more += [(df, -len(p), p) for p in (" ".join(toks[:i]), " ".join(toks[i + 1:]))]
        seen, res = set(), []
        for f in sorted(out, key=len, reverse=True) + [p for _, _, p in sorted(more)]:
            if len(f) >= 8 and f != q and f not in seen:
                seen.add(f)
                res.append(f)
        return res, holes

    def _code(self, q, allow, k=400, roots=None, qv=None):
        """Code search tuned on bench/ (agent-style queries: descriptions, names, error strings, mixes of these).

        Candidates: the top-k chunks by meaning and by keywords (identifier-aware), plus chunks containing the
        whole query or a code name from it. Each file is scored by its best chunk:
            z(meaning) + 0.2 z(keywords) - 0.5 [test/spec/schema file]
            + 3 [contains the whole query] + log(1 + times) + [same case]       (not for a bare name)
            + 2 [contains a code name from the query] / log2(1 + files that do)
            + 2 [defines that name, and at most 3 files do]
        returned on the cosine scale (best chunk similarity + sigma * the rest) so it merges with other corpora.
        Returns {chunk id: score} with the best chunk of each file. `qv`: q already embedded under CODE_QUERY."""
        allowed = set(allow.tolist()) if allow is not None else None
        ok = (lambda c: c in allowed) if allowed is not None else (lambda c: True)
        if qv is None:
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
            terms = self._rare_terms(sorted(set(code_words(q).split())))
            if terms:
                fq = " OR ".join(f'"{t}"' for t in terms)
                scoped = bool(roots) and self.store.meta("fts_words") == WORDS_VERSION  # the index has repo tokens
                if scoped:
                    fq = f"words : ({fq}) AND repo : ({' OR '.join(repo_token(r) for r in roots)})"
                for cid, bm in self.db.execute(f"SELECT rowid, bm25({WORDS_FTS}, 1.0, 0.0) FROM {WORDS_FTS} WHERE {WORDS_FTS} "
                                               f"MATCH ? ORDER BY 2 LIMIT ?", (fq, k if allowed is None or scoped else 20000)):
                    if ok(cid) and len(kw) < k:
                        kw[cid] = -bm
            cand.update(kw)
            ql = q.strip()
            phrase = set()
            if len(ql) >= 8:
                phrase = {c for (c,) in self.db.execute("SELECT rowid FROM fts_code WHERE fts_code MATCH ? LIMIT ?",
                                                        (_fts_query([ql]), 500 if allowed is None else 5000)) if ok(c)}
            names = code_names(ql)
            frags, pieces = {}, []  # fragment -> chunks containing it
            if len(ql) >= 8 and not phrase and names != [ql]:
                pieces, holes = self._fragments(ql)
                # A number or a word no code contains marks a message: probe the index for up to 3 fragments. A
                # query without one is more likely a description, and its fragments are only looked for in the
                # candidates below.
                for f in pieces[:8] if holes else []:
                    if any(f in g for g in frags):
                        continue
                    hits = {c for (c,) in self.db.execute("SELECT rowid FROM fts_code WHERE fts_code MATCH ? LIMIT ?",
                                                          (_fts_query([f]), 500 if allowed is None else 5000)) if ok(c)}
                    if hits:
                        frags[f] = hits
                        cand |= hits
                        if len(frags) == 3:
                            break
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
                for cid, text, vec, path, repo, wt in self.db.execute(
                        f"SELECT id, text, vec, path, repo, wt FROM code_chunks WHERE id IN ({','.join('?' * len(part))})",
                        part):
                    rows[cid] = (text, vec, f"{repo}:{path}", path, wt)
        if pieces and not frags:
            low = {cid: r[0].lower() for cid, r in rows.items()}
            for f in pieces:
                if not any(f in g for g in frags):
                    hits = {cid for cid, t in low.items() if f.lower() in t}
                    if hits:
                        frags[f] = hits
                        if len(frags) == 3:
                            break
        files, best, sem, kwf, main = {}, {}, {}, {}, {}
        for cid, (text, vec, fkey, path, wt) in rows.items():
            s_ = float(np.frombuffer(vec, np.float16).astype(np.float32) @ qv)
            files.setdefault(fkey, path)
            # a linked worktree's copy shares the file's key: the main checkout's chunks stand for the file when
            # any are candidates (the agent is shown worktree copies last)
            if (not wt, s_) > (main.get(fkey, False), sem.get(fkey, -9)):
                sem[fkey], best[fkey], main[fkey] = s_, cid, not wt
            kwf[fkey] = max(kwf.get(fkey, 0.0), kw.get(cid, 0.0))
        keys = list(files)
        S = np.array([sem[f] for f in keys], np.float32)
        K = np.array([kwf[f] for f in keys], np.float32)
        z = lambda x: (x - x.mean()) / (x.std() + 1e-9)
        extra = 0.2 * z(K) - 0.5 * np.array([bool(_TESTY.search(files[f])) for f in keys], np.float32)
        pos = {f: i for i, f in enumerate(keys)}
        if phrase:
            hit, cased = np.zeros(len(keys)), np.zeros(len(keys))
            cnt = np.zeros((2, len(keys)))  # matches in the main checkout, in linked worktrees' copies
            for c in phrase:
                if c in rows:
                    j = pos[rows[c][2]]
                    hit[j] = 1
                    cnt[int(bool(rows[c][4])), j] += rows[c][0].lower().count(ql.lower())
                    cased[j] = max(cased[j], float(ql in rows[c][0]))
            cnt = np.where(cnt[0] > 0, cnt[0], cnt[1])  # a file's copies would count each match twice
            extra += 3 * hit + ((np.log1p(cnt) + cased) if names != [ql] else 0)
        tier = hit if phrase else None  # what grep would find: files that contain the query's text
        if any(frags.values()):
            # A file counts when its fragments cover at least half of the query: a message with a few values
            # filled in, not a description that shares a common phrase with the file.
            spans, cnt = defaultdict(set), np.zeros(len(keys))
            for f, cids in frags.items():
                at = ql.lower().find(f.lower())
                for j in {pos[rows[c][2]] for c in cids if c in rows}:
                    spans[j].update(range(at, at + len(f)))
                for c in cids:
                    if c in rows and not rows[c][4]:
                        cnt[pos[rows[c][2]]] += rows[c][0].lower().count(f.lower())
            got = np.zeros(len(keys))
            for j, s in spans.items():
                got[j] = len(s) / len(ql) if len(s) >= len(ql) / 2 else 0
            extra += 3 * got
            tier = got
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
        if tier is not None and names != [ql] and tier.any():
            # Files are sorted by how much of the query's text they contain, then by match count as grep ranks them,
            # then by score. The existing scores are reassigned in that order, so the scale that merges code with
            # other kinds of results does not change.
            order = sorted(range(len(keys)), key=lambda i: (-tier[i], -cnt[i], -total[i]))
            total = np.empty_like(total)
            total[order] = np.sort(S + S.std() * extra)[::-1]
        return {best[f]: float(total[i]) for i, f in enumerate(keys)}

    # ---- folders, repos, people ----
    def _folder_index(self, wait=False):
        """Every folder of the indexed repos (main checkouts) and document folders: path words, files, and the mean
        vector of what's in it. Kept in memory and built in the background (the server starts it at launch): when the
        index changes it is rebuilt at most every FOLDER_REBUILD seconds and not during a sweep. A search never
        waits for it; until the first build is done it gets no folders or people. `wait`: build now if there is none
        (by:<person> needs the authors to filter at all)."""
        with self.lock:
            fp = self.db.execute("SELECT (SELECT MAX(id) FROM code_chunks), (SELECT MAX(id) FROM file_chunks), "
                                 "(SELECT COUNT(*) FROM repos)").fetchone()
        cur = getattr(self, "_fidx", None)
        if cur is None and wait:
            return self._build_folder_index(fp)
        sweeping = getattr(self.repos, "_sweeping", False) or getattr(self.files, "_sweeping", False)
        # a rebuild walks every chunk in Python; during a sweep it would run back to back and slow searches down
        if cur is None or (cur["fp"] != fp and not sweeping and time.time() - cur["at"] > FOLDER_REBUILD):
            if self._fbuilding:
                return cur or _NO_FOLDERS
            self._fbuilding = True

            def rebuild():
                try:
                    self._build_folder_index(fp)
                finally:
                    self._fbuilding = False
            threading.Thread(target=rebuild, daemon=True, name="folder-index").start()
        return cur or _NO_FOLDERS

    def _build_folder_index(self, fp):
        home = str(Path.home())
        with self.lock:
            names = dict(self.db.execute("SELECT root, name FROM repos").fetchall())
            code = self.db.execute("SELECT id, repo, path, vec, authors FROM code_chunks WHERE wt=''").fetchall()
            docs = self.db.execute("SELECT c.id, f.path, c.vec, f.author, f.root FROM file_chunks c JOIN files f ON f.path=c.path "
                                   "WHERE f.kind!='folder'").fetchall()
        dirs, ids, vecs = {}, [], []
        def add(key, **kw):
            d = dirs.get(key)
            if d is None:
                d = dirs[key] = dict(kw, idx=[], files=Counter())
            return d
        for i, (cid, repo, path, vec, _) in enumerate(code):
            ids.append(("code", cid)); vecs.append(vec)
            parts = path.split("/")[:-1]
            for depth in range(len(parts) + 1):
                rel = "/".join(parts[:depth])
                d = add(f"{repo}/{rel}".rstrip("/"), repo=repo, name=names.get(repo, Path(repo).name), rel=rel, root=not rel)
                d["idx"].append(i); d["files"][path.rsplit("/", 1)[-1]] += 1
        for j, (cid, path, vec, _, root) in enumerate(docs):
            ids.append(("file", cid)); vecs.append(vec)
            parent = str(Path(path).parent)
            base = str(Path(root).expanduser()) if root else home
            if not parent.startswith(base):
                continue
            parts = parent[len(base):].strip("/").split("/") if parent != base else []
            for depth in range(1, len(parts) + 1):
                rel = "/".join(parts[:depth])
                d = add(f"{base}/{rel}", repo=None, name=Path(base).name, rel=rel, root=False, base=base)
                d["idx"].append(len(code) + j); d["files"][Path(path).name] += 1
        V = unit(np.stack([np.frombuffer(v, np.float16) for v in vecs]).astype(np.float32)) if vecs else np.zeros((0, 1))
        keys = [k for k, d in dirs.items() if len(d["files"]) >= 1]
        C = unit(np.stack([V[dirs[k]["idx"]].mean(0) for k in keys])) if keys else np.zeros((0, 1))
        by_word = defaultdict(set)  # path word -> folders (positions in keys) whose path has it
        for j, k in enumerate(keys):  # words of the path, by segment (repo name first)
            d = dirs[k]
            d["segs"] = [set(code_words(x).split()) for x in [d["name"], *(d["rel"].split("/") if d["rel"] else [])]]
            for seg in d["segs"]:
                for w in seg:
                    by_word[w].add(j)
        people = Counter()
        for c in code:
            for who, n in json.loads(c[4] or "{}").items():
                people[who.split(" <")[0]] += n
        self._fidx = dict(fp=fp, at=time.time(), keys=keys, dirs=dirs, C=C, ids=ids, people=people,
                          by_word=by_word, words=sorted(by_word),
                          authors={("code", c[0]): (c[4] or "").lower() for c in code},
                          doc_author={("file", c[0]): (c[3] or "").lower() for c in docs})
        return self._fidx

    @staticmethod
    def _path_match(words, segs):
        """How well the query's words name this folder: the share of words found in its path (a word may be a prefix
        of a path word, 'pipeline' ~ 'pipelines'), more when they sit at the end, less for each level below them."""
        if not words:
            return 0.0, 0.0
        last, found = -1, 0
        for w in words:
            at = [i for i, seg in enumerate(segs) if any(x == w or (len(w) >= 4 and x.startswith(w)) for x in seg)]
            if at:
                found += 1; last = max(last, at[-1])
        cover = found / len(words)
        if not found:
            return 0.0, 0.0
        below = len(segs) - 1 - last
        return cover, cover + (0.15 if below == 0 else 0) - 0.08 * below

    def _folders(self, q, scope, repos_only=False, strong_only=False, limit=20, by=None, ext=None, within=None,
                 code_qv=None):
        """Folder (and repo) results: path words and what the folder holds both count. strong_only: only folders
        whose path names every word of the query (shown above files in a plain search)."""
        ix = self._folder_index()
        if not ix["keys"]:
            return []
        words = sorted(set(code_words(q).split()))
        qv = None if q.strip() else np.zeros(ix["C"].shape[1], np.float32)  # no words: rank by size
        home = str(Path.home())
        by_ok = None
        if by:
            by_ok = {i for i, k in enumerate(ix["ids"]) if by in (ix["authors"].get(k) or ix["doc_author"].get(k) or "").lower()}
        out = []
        candidates = range(len(ix["keys"]))
        if strong_only and words:  # only folders whose path has every word (the check below), found by word
            candidates = None
            for w in words:
                hit = set(ix["by_word"].get(w, ()))
                if len(w) >= 4:  # a word may be a prefix of a path word: 'pipeline' ~ 'pipelines'
                    i = bisect.bisect_left(ix["words"], w)
                    while i < len(ix["words"]) and ix["words"][i].startswith(w):
                        hit |= ix["by_word"][ix["words"][i]]
                        i += 1
                candidates = hit if candidates is None else candidates & hit
                if not candidates:
                    return []
            candidates = sorted(candidates)
        for j in candidates:
            key = ix["keys"][j]
            d = ix["dirs"][key]
            if repos_only and not d["root"]:
                continue
            if scope is not None:
                if d["repo"] and scope.repos is not None and d["repo"] not in scope.repos:
                    continue
                if not d["repo"] and scope.folders is not None and not any(key.startswith(str(Path(x).expanduser())) for x in scope.folders):
                    continue
            if within and within not in key.lower():
                continue
            if ext and not any(n.lower().endswith("." + ext) for n in d["files"]):
                continue
            if by_ok is not None and not any(i in by_ok for i in d["idx"]):
                continue
            cover, path = self._path_match(words, d["segs"])
            if strong_only and (cover < 1 or path < 1):
                continue
            if qv is None:  # embedded only once some folder is in the running
                qv = code_qv if code_qv is not None else embed([q], query=CODE_QUERY)[0]
            score = path + 0.8 * float(ix["C"][j] @ qv) + (0 if q.strip() else len(d["files"]) / 1e6)
            out.append((score, key, d, cover))
        out.sort(key=lambda x: -x[0])
        res = []
        for score, key, d, cover in out[:(2 if strong_only else limit)]:
            files = d["files"]
            where = (d["name"] + ("/" + d["rel"].rsplit("/", 1)[0] if "/" in d["rel"] else "")) if d["repo"] else \
                str(Path(key).parent).replace(home, "~", 1)
            title = d["name"] if d["root"] else d["rel"].rsplit("/", 1)[-1]
            res.append(dict(id=f"folder:{key}", kind="repo" if d["root"] else "folder", title=title,
                            subtitle=("Repository · " if d["root"] else f"Folder in {where} · ") +
                            f"{len(files):,} file{'' if len(files) == 1 else 's'}",
                            path=key, line=None, is_dir=True, ext="", mtime=None, author=None, highlights=[],
                            snippet="Mostly " + ", ".join(n for n, _ in files.most_common(4)),
                            score=round(1.0 + 0.1 * score if strong_only else score, 4), match="folder"))
        return res

    def _by_allow(self, by, allow):
        """Code chunk ids that `by` (a name, any part, case-insensitive) has blamed lines in, within `allow`."""
        with self.lock:
            ids = np.array([r[0] for r in self.db.execute("SELECT id FROM code_chunks WHERE lower(authors) LIKE ?",
                                                         (f"%{by}%",))], dtype=np.uint64)
        return ids if allow is None else np.intersect1d(ids, allow)

    def _by_ok(self, by, kind, r):
        if kind == "session":
            return by in (r.get("subtitle") or "").lower()
        if kind == "file":
            return by in (r.get("author") or "").lower()
        return by in self._folder_index(wait=True)["authors"].get(("code", int(r["id"].split(":")[1])), "")

    def _people(self, q, scope, named_only=False, limit=8, code_qv=None):
        """People for a query: someone it names outright, or (kind:person) whoever wrote most of the matching code."""
        from pensieve import scopes
        names = self._folder_index()["people"]
        ql = q.strip().lower()
        named = [w for w in names if len(ql) >= 3 and (w.lower() == ql or w.lower().startswith(ql + " ")
                                                         or ql in w.lower().split())]
        out = []
        if named:
            for w in sorted(named, key=lambda w: -names[w])[:3]:
                out.append(dict(id=f"person:{w}", kind="person", title=w, subtitle=f"Person · {names[w]:,} lines written",
                                path=None, line=None, is_dir=False, ext="", mtime=None, author=w, highlights=[],
                                snippet="Open to see what they know about", score=1.5, match="person"))
            return out
        if named_only:
            return []
        allow = scopes.allowlist(scope, "code", self.store, self.repos, self.repos.index) if scope else None
        top = sorted(self._code(q, allow, qv=code_qv).items(), key=lambda x: -x[1])[:40]
        for e in self.repos._experts_from([(cid, sc) for cid, sc in top])[:limit]:
            w = e["author"].split(" <")[0]
            out.append(dict(id=f"person:{w}", kind="person", title=w,
                            subtitle=f"Person · wrote {round(e['share'] * 100)}% of the code about this",
                            path=None, line=None, is_dir=False, ext="", mtime=e["last_active"], author=w, highlights=[],
                            snippet="Main files: " + ", ".join(Path(x).name for x in e["files"][:3]),
                            score=round(e["share"], 4), match="person"))
        return out

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
                                                path=str(Path(wt or root) / path), line=start, mtime=ts, key=f"{root}:{path}", root=root,
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


_STOP = set("the and for with from that this into when where what how all any each its are was has have not but "
             "you your can will use used uses using code file files function method class which then than them they".split())


def _stems(text):
    return {w[:6] for w in code_words(text).split() if len(w) >= 3 and w not in _STOP}


def _line_scores(lines, query, exact=()):
    """How well each line matches: shared words (identifiers split), and much more for an exact phrase or a code
    name from the query (`ICON_CANVAS`, `retryWebhook`) used on that line."""
    want, phrases = _stems(query), [p.lower() for p in exact]
    names = [re.compile(r"(?<![\w])" + re.escape(n) + r"(?![\w])") for n in code_names(query) if len(n) >= 3]
    return [0.0 if not ln.strip() else
            len(want & _stems(ln)) + 3 * sum(p in ln.lower() for p in phrases) + 4 * sum(bool(rx.search(ln)) for rx in names)
            for ln in lines]


def _focus(text, query, exact=()):
    """The line of a code chunk that best matches the query, and the definition it sits in, so an agent can tell
    from one result line whether it found the right code. Returns (line offset in the chunk, definition line or
    None, best line)."""
    lines = text.split("\n")
    sc = _line_scores(lines, query, exact)
    best = max(range(len(lines)), key=lambda i: (sc[i], -i)) if lines else 0
    if not sc or sc[best] == 0:  # nothing matched word for word: the chunk's first line of substance
        best = next((i for i, ln in enumerate(lines) if ln.strip()), 0)
    sym = next((lines[j] for j in range(best, -1, -1) if _SYMBOL.match(lines[j])), None)
    if sym is None:
        sym = next((ln for ln in lines if _SYMBOL.match(ln)), None)
    return best, (sym.strip() if sym and sym.strip() != lines[best].strip() else None), lines[best].strip()


def _excerpt(text, start, query, exact=()):
    """A few numbered lines that show why a chunk matched: its definition line and its two best-matching lines."""
    lines = text.split("\n")
    sc = _line_scores(lines, query, exact)
    top = sorted((i for i in range(len(lines)) if sc[i] > 0), key=lambda i: (-sc[i], i))[:2]
    if not top:
        top = [next((i for i, ln in enumerate(lines) if ln.strip()), 0)]
    sym = next((j for j in range(min(top), -1, -1) if _SYMBOL.match(lines[j])), None)
    keep = sorted(set(top) | ({sym} if sym is not None else set()))
    return [(start + j, lines[j][:120]) for j in keep]


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
