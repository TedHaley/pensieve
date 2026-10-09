"""One map of everything: documents, code files and agent sessions laid out together by meaning.

Each point is a file-like item: a document, a file in a git repo (the main checkout; worktree versions sit with it
in search, not on the map), or an agent session. Positions come from one joint UMAP over item centroids, kept in
`data_points` and refit when the set grows; new items are placed beside their nearest neighbours in between.

Topics are KMeans clusters of the same centroids (kept in `data_points.cluster`). Each gets keyword names at once and,
when an AI engine is on, a written name and description later (`data_topics`, reset whenever the clusters change)."""
import hashlib
import json
import math
import os
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from .indexer import LAYOUT_VERSION
from .layout import cluster, fit3d, nearest, place_new, unit

VERSION = LAYOUT_VERSION + "+data1"
TOPIC_VERSION = "t4"
# words that say where or what kind of file something is, not what it is about
STOP = set("""desktop documents downloads library pycharmprojects users home applications application support agent agents
sessions session src lib libs py js ts tsx jsx md txt json yaml yml toml cfg ini lock html css csv pdf docx xlsx pptx png jpg
jpeg init index main test tests spec utils util helpers helper common core misc tmp temp private var file files new old copy
readme license changelog dist build module modules package packages app apps makefile dockerfile dockerignore gitignore
variables outputs locals inputs title page meta""".split())
AGENT = {"claude": "Claude Code", "codex": "Codex", "qwen": "Qwen Code"}


class DataMap:
    def __init__(self, store, repos, files):
        self.store, self.repos, self.files = store, repos, files
        self.db, self.lock = store.db, store.lock
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS data_points(id TEXT PRIMARY KEY, x REAL, y REAL, z REAL, cluster INTEGER)")
            self.db.execute("CREATE TABLE IF NOT EXISTS data_topics(id INTEGER PRIMARY KEY, sig TEXT, name TEXT, description TEXT, "
                            "keywords TEXT, reps TEXT)")
            self.db.commit()
        self._cache = (None, None)
        self._ctimes = {}  # repo root -> (HEAD, {path: last commit time})
        self._vec, self._vec_at = (None, None, None), 0.0  # (fingerprint, ids, centroids), when built
        self._scoped = {}  # (fingerprint, folders) -> scoped topics

    # ---- items + vectors -------------------------------------------------------
    def _vectors(self):
        """id -> (unit centroid). Ids: 'file:<abs>', 'code:<repo root>:<path>', 'session:<sid>'."""
        by = {}
        with self.lock:
            for path, v in self.db.execute("SELECT c.path, c.vec FROM file_chunks c JOIN files f ON f.path=c.path "
                                           "WHERE f.kind!='folder'"):
                by.setdefault(f"file:{path}", []).append(v)
            for repo, path, v in self.db.execute("SELECT repo, path, vec FROM code_chunks WHERE wt=''"):
                by.setdefault(f"code:{repo}:{path}", []).append(v)
            for sid, v in self.db.execute("SELECT session_id, vec FROM chunks"):
                by.setdefault(f"session:{sid}", []).append(v)
        ids = list(by)
        if not ids:
            return [], np.zeros((0, 1024), np.float32)
        C = np.stack([np.mean([np.frombuffer(b, np.float16).astype(np.float32) for b in by[i]], axis=0) for i in ids])
        return ids, unit(C)

    def fingerprint(self):
        with self.lock:
            return self.db.execute(
                "SELECT (SELECT COUNT(*) FROM file_chunks), (SELECT MAX(id) FROM file_chunks), "
                "(SELECT COUNT(*) FROM code_chunks WHERE wt=''), (SELECT MAX(id) FROM code_chunks), "
                "(SELECT COUNT(*) FROM chunks), (SELECT MAX(id) FROM chunks)").fetchone()

    def needs_layout(self):
        return (self.store.meta("data_layout_fp") != json.dumps(self.fingerprint())
                or self.store.meta("data_topic_v") != TOPIC_VERSION)

    def reproject(self, force=False):
        """Joint layout. Full UMAP when new or grown >15%; otherwise place new items next to their neighbours."""
        fp = json.dumps(self.fingerprint())
        ids, C = self._vectors()
        if len(ids) < 4:
            return False
        with self.lock:
            placed = {r[0]: r[1:] for r in self.db.execute("SELECT id, x, y, z, cluster FROM data_points")}
            fit_n = int(self.store.meta("data_fit_n") or 0)
            ok = self.store.meta("data_layout") == VERSION
        n = len(ids)
        if force or not ok or len([i for i in ids if i in placed]) < 0.5 * n or n - fit_n > max(100, 0.15 * fit_n):
            P = fit3d(C, n_neighbors=20, min_dist=0.1)
            lab = self._cluster(C)
            rows = [(i, *map(float, p), int(c)) for i, p, c in zip(ids, P, lab)]
            with self.lock:
                self.db.execute("DELETE FROM data_points")
                self.db.executemany("INSERT INTO data_points VALUES(?,?,?,?,?)", rows)
                self.store.meta("data_fit_n", n)
                self.store.meta("data_layout", VERSION)
            self._save_topics(ids, C, lab)
        elif self.store.meta("data_topic_v") != TOPIC_VERSION or not self._has_topics():  # topics changed, positions kept
            self._place(ids, C, placed)
            lab = self._cluster(C)
            with self.lock:
                self.db.executemany("UPDATE data_points SET cluster=? WHERE id=?", [(int(c), i) for i, c in zip(ids, lab)])
            self._save_topics(ids, C, lab)
        else:
            self._place(ids, C, placed)
        with self.lock:
            self.store.meta("data_layout_fp", fp)
            self.db.commit()
        self._cache = (None, None)
        return True

    def _place(self, ids, C, placed):
        """Add new items beside their nearest placed neighbours (in their topic) and drop removed ones."""
        idx = {i: k for k, i in enumerate(ids)}
        new = [i for i in ids if i not in placed]
        gone = [i for i in placed if i not in idx]
        known = [i for i in ids if i in placed]
        rows = []
        if new and known:
            Kk = [idx[i] for i in known]
            Pk = np.array([placed[i][:3] for i in known], dtype=np.float32)
            Pn = place_new(C[Kk], Pk, C[[idx[i] for i in new]], k=3, jitter=0.03)
            near = nearest(C[[idx[i] for i in new]], C[Kk])
            rows = [(i, *map(float, p), placed[known[j]][3]) for i, p, j in zip(new, Pn, near)]
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO data_points VALUES(?,?,?,?,?)", rows)
            self.db.executemany("DELETE FROM data_points WHERE id=?", [(i,) for i in gone])

    # ---- topics ---------------------------------------------------------------
    @staticmethod
    def _cluster(C):
        """A size-scaled number of topics, at most 8 so each has its own palette color on the dots, labels and legend."""
        k = max(2, min(8, round(math.sqrt(len(C)) / 6), len(C) // 8)) if len(C) >= 16 else 1
        return cluster(C, kmin=k, kmax=k) if k > 1 else np.zeros(len(C), int)

    def _has_topics(self):
        with self.lock:
            return self.db.execute("SELECT 1 FROM data_topics LIMIT 1").fetchone() is not None

    def _save_topics(self, ids, C, lab):
        """Keywords and representative items per topic; written names are cleared since the members changed."""
        pts = self.points(fresh=True)["points"]
        texts = {p["id"]: self._text(p) for p in pts}
        exts = {e.lower() for p in pts for e in [p.get("ext")] if e}  # "tfvars", "wav"… say what kind, not what about
        by = defaultdict(list)
        for k, (i, c) in enumerate(zip(ids, lab)):
            by[int(c)].append(k)
        kw = self._keywords({c: [texts.get(ids[k], "") for k in ks] for c, ks in by.items()}, exts)
        rows = []
        for c, ks in sorted(by.items()):
            cen = C[ks].mean(0)
            reps = [ids[ks[j]] for j in np.argsort(-(C[ks] @ cen))[:24]]
            sig = hashlib.sha1(("|".join(sorted(ids[k] for k in ks[:500])) + TOPIC_VERSION).encode()).hexdigest()[:16]
            rows.append((c, sig, None, None, kw.get(c, ""), json.dumps(reps)))
        with self.lock:
            self.db.execute("DELETE FROM data_topics")
            self.db.executemany("INSERT INTO data_topics VALUES(?,?,?,?,?,?)", rows)
            self.store.meta("data_topic_v", TOPIC_VERSION)
            self.db.commit()

    def _text(self, p):
        home = str(Path.home())
        bits = [p.get("title") or "", (p.get("path") or "").replace(home, ""), p.get("repo") or "", p.get("summary") or ""]
        t = " ".join(bits)
        t = re.sub(r"([a-z])([A-Z])", r"\1 \2", t)  # camelCase -> camel Case
        return re.sub(r"[_\-./\\]+", " ", t).lower()

    @staticmethod
    def _keywords(docs_by, extra_stop=()):
        from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer
        ids = sorted(docs_by)
        docs = [" ".join(docs_by[c]) for c in ids]
        import warnings
        warnings.filterwarnings("ignore", message="Your stop_words may be inconsistent")
        try:
            vec = TfidfVectorizer(stop_words=list(STOP | ENGLISH_STOP_WORDS | set(extra_stop)),
                                  sublinear_tf=True, max_df=0.6 if len(ids) > 3 else 1.0, max_features=8000,
                                  token_pattern=r"(?u)\b[a-z][a-z0-9]{2,}\b")
            M = vec.fit_transform(docs)
            terms = np.array(vec.get_feature_names_out())
            return {c: ", ".join(terms[np.argsort(-M[i].toarray()[0])[:6]]) for i, c in enumerate(ids)}
        except ValueError:
            return {c: "" for c in ids}

    def topics(self):
        with self.lock:
            rows = self.db.execute("SELECT id, sig, name, description, keywords, reps FROM data_topics ORDER BY id").fetchall()
        return [dict(id=c, sig=sig, named=bool(name), keywords=kw or "", reps=json.loads(reps or "[]"),
                     name=name or self._kw_name(kw or "", c),
                     description=desc or "") for c, sig, name, desc, kw, reps in rows]

    def _cached_vectors(self):
        """All item centroids, refreshed at most every two minutes (building them reads every chunk vector)."""
        import time
        fp = self.fingerprint()
        if self._vec[0] != fp and (self._vec[0] is None or time.time() - self._vec_at > 120):
            self._vec = (fp, *self._vectors())
            self._vec_at = time.time()
        return self._vec[1], self._vec[2]

    @staticmethod
    def _kw_name(kw, c):
        """Two distinctive words, skipping one that's a variant of the other ("Component · Components")."""
        words = []
        for w in (x.strip() for x in kw.split(",")):
            if w and not any(w.startswith(v[:5]) or v.startswith(w[:5]) for v in words):
                words.append(w)
        return " · ".join(w.title() for w in words[:2]) or f"Topic {c + 1}"

    def scoped_topics(self, folders):
        """Topics among just the items under `folders` (absolute paths): what the map is about once it's filtered
        to a repo or folder. Kept while the folder's items change by less than a tenth (new ones join the nearest
        topic), so names stay put while indexing carries on. Keyword names at once; AI names are cached by
        membership (meta 'dscope:<sig>')."""
        ids, C = self._cached_vectors()
        pos = {i: k for k, i in enumerate(ids)}
        path = {p["id"]: p.get("path") or "" for p in self.points()["points"]}
        pre = [f.rstrip("/") + "/" for f in folders]
        sub = [i for i in ids if any(path.get(i, "").startswith(f) for f in pre)]
        key = tuple(sorted(folders))
        hit = self._scoped.get(key)
        if hit and abs(len(sub) - hit["n"]) <= max(5, 0.1 * hit["n"]):
            out = hit["out"]
            new = [i for i in sub if i not in out["of"]]
            if new and hit["cents"] is not None:  # newcomers join their nearest topic
                near = (C[[pos[i] for i in new]] @ hit["cents"].T).argmax(1)
                out["of"].update({i: int(hit["tids"][j]) for i, j in zip(new, near)})
            return self._fill_names(out)
        out, cents, tids = dict(topics=[], of={}), None, []
        if len(sub) >= 10:
            X = C[[pos[i] for i in sub]]
            k = max(2, min(8, round(math.sqrt(len(sub)) / 4)))
            lab = cluster(X, kmin=k, kmax=k)
            by = defaultdict(list)
            for j, c in enumerate(lab):
                by[int(c)].append(j)
            pts = self.points()["points"]
            texts = {p["id"]: self._text(p) for p in pts}
            exts = {(p.get("ext") or "").lower() for p in pts} - {""}
            kw = self._keywords({c: [texts.get(sub[j], "") for j in js] for c, js in by.items()}, exts)
            rows = []
            for c, js in sorted(by.items()):
                cen = X[js].mean(0)
                rows.append(cen)
                tids.append(c)
                reps = [sub[js[j]] for j in np.argsort(-(X[js] @ cen))[:24]]
                sig = hashlib.sha1(("|".join(sorted(sub[j] for j in js)) + TOPIC_VERSION).encode()).hexdigest()[:16]
                out["topics"].append(dict(id=c, sig=sig, key=f"dscope:{sig}", keywords=kw.get(c, ""), reps=reps,
                                          name=self._kw_name(kw.get(c, ""), c), description="", named=False))
            out["of"] = {sub[j]: int(c) for j, c in enumerate(lab)}
            cents = unit(np.stack(rows))
        if len(self._scoped) > 32:
            self._scoped.clear()
        self._scoped[key] = dict(out=out, n=len(sub), cents=cents, tids=tids)
        return self._fill_names(out)

    def _fill_names(self, out):
        for t in out["topics"]:
            if not t["named"] and (v := self.store.meta(t["key"])):
                d = json.loads(v)
                t.update(name=d["name"], description=d["description"], named=True)
        return out

    def set_scoped_name(self, topic, name, description):
        with self.lock:
            self.store.meta(topic["key"], json.dumps(dict(name=name.strip()[:48], description=description.strip())))
            self.db.commit()

    def pending_topics(self):
        return [t for t in self.topics() if not t["named"]]

    def set_topic_name(self, topic, name, description):
        with self.lock:  # only if the topic hasn't been reclustered while the AI was writing
            self.db.execute("UPDATE data_topics SET name=?, description=? WHERE id=? AND sig=?",
                            (name.strip()[:48], description.strip(), topic["id"], topic["sig"]))
            self.db.commit()

    def _commit_times(self, root):
        """path -> time of the last commit that touched it, from one `git log` (cached until HEAD moves)."""
        try:
            head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            head = ""
        if not head:
            return {}
        if self._ctimes.get(root, (None,))[0] == head:
            return self._ctimes[root][1]
        out = {}
        try:
            log = subprocess.run(["git", "-C", root, "log", "--format=%x1e%ct", "--name-only", "--no-renames"],
                                 capture_output=True, text=True, timeout=120, errors="replace").stdout
        except (OSError, subprocess.SubprocessError):
            log = ""
        for rec in log.split("\x1e"):  # newest first, so the first time a path appears is its last commit
            lines = rec.strip().split("\n")
            if not lines or not lines[0].isdigit():
                continue
            t = int(lines[0])
            for f in lines[1:]:
                if f and f not in out:
                    out[f] = t
        self._ctimes[root] = (head, out)
        return out

    # ---- points ---------------------------------------------------------------
    def points(self, fresh=False):
        fp = self.fingerprint()
        if self._cache[0] == fp and not fresh:
            return self._cache[1]
        home = str(Path.home())
        with self.lock:
            pos = {r[0]: r[1:] for r in self.db.execute("SELECT id, x, y, z, cluster FROM data_points")}
            files = self.db.execute("SELECT path, root, name, ext, kind, mtime, size, author, author_source FROM files "
                                    "WHERE kind!='folder'").fetchall()
            repo_names = dict(self.db.execute("SELECT root, name FROM repos").fetchall())
            code = self.db.execute("SELECT repo, path, MIN(id), MAX(last_ts), COUNT(*), GROUP_CONCAT(authors, '\x1f') "
                                   "FROM code_chunks WHERE wt='' GROUP BY repo, path").fetchall()
            sess = self.db.execute("SELECT id, title, source, project, project_name, updated, n_chunks, summary "
                                   "FROM sessions WHERE n_chunks>0").fetchall()
        out = []
        for path, root, name, ext, kind, mtime, size, author, src in files:
            k = f"file:{path}"
            if k in pos:
                out.append(dict(id=k, type="doc", open=k, title=name, path=path, display=path.replace(home, "~", 1),
                                ext=ext, kind=kind, mtime=mtime, size=size, author=author,
                                author_source=None if src == "none" else src, p=pos[k][:3], cluster=pos[k][3]))
        ctimes = {}
        for repo, path, first_id, ts, n, authors in code:
            k = f"code:{repo}:{path}"
            if k not in pos:
                continue
            if repo not in ctimes:
                ctimes[repo] = self._commit_times(repo)
            # when it was last committed; never committed (new, or a repo without commits): the file's own time
            when = ctimes[repo].get(path)
            if when is None:
                try:
                    when = os.stat(f"{repo}/{path}").st_mtime
                except OSError:
                    when = ts
            blame = Counter()
            for a in (authors or "").split("\x1f"):
                for who, lines in json.loads(a or "{}").items():
                    blame[who.split(" <")[0]] += lines
            full = f"{repo}/{path}"
            out.append(dict(id=k, type="code", open=f"code:{first_id}", title=Path(path).name, path=full,
                            display=full.replace(home, "~", 1), repo=repo_names.get(repo, Path(repo).name), repo_root=repo,
                            ext=Path(path).suffix.lstrip("."), mtime=when, committed=path in ctimes[repo], chunks=n,
                            author=blame.most_common(1)[0][0] if blame else None, author_source="git",
                            p=pos[k][:3], cluster=pos[k][3]))
        scope = self.repos.session_scope()
        for sid, title, src, cwd, proj, upd, n, summary in sess:
            k = f"session:{sid}"
            if k not in pos:
                continue
            sc = scope.get(sid, {})
            out.append(dict(id=k, type="session", open=k, title=title or sid, agent=AGENT.get(src, src), source=src,
                            # in the folder tree a session sits in its repo (clones/worktrees fold in) or where it ran
                            path=f"{sc.get('root') or cwd}/Agent sessions/{title or sid}" if (sc.get("root") or cwd) else None,
                            display=(cwd or "").replace(home, "~", 1), repo=sc.get("project") if sc.get("root") else None,
                            repo_root=sc.get("root"), mtime=upd, chunks=n, summary=summary, author=AGENT.get(src, src),
                            author_source="agent", p=pos[k][:3], cluster=pos[k][3]))
        res = dict(points=out, repos=[dict(root=r, name=nm) for r, nm in repo_names.items()], home=home)
        self._cache = (fp, res)
        return res
