"""One map of everything: documents, code files and agent sessions laid out together by meaning.

Each point is a file-like item: a document, a file in a git repo (the main checkout; worktree versions sit with it
in search, not on the map), or an agent session. Positions come from one joint UMAP over item centroids, kept in
`data_points` and refit when the set grows; new items are placed beside their nearest neighbours in between."""
import json
from collections import Counter
from pathlib import Path

import numpy as np

from .indexer import LAYOUT_VERSION
from .layout import cluster, fit3d, place_new, unit

VERSION = LAYOUT_VERSION + "+data1"
AGENT = {"claude": "Claude Code", "codex": "Codex", "qwen": "Qwen Code"}


class DataMap:
    def __init__(self, store, repos, files):
        self.store, self.repos, self.files = store, repos, files
        self.db, self.lock = store.db, store.lock
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS data_points(id TEXT PRIMARY KEY, x REAL, y REAL, z REAL, cluster INTEGER)")
            self.db.commit()
        self._cache = (None, None)

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
        return self.store.meta("data_layout_fp") != json.dumps(self.fingerprint())

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
            lab = cluster(C, kmin=4, kmax=8)
            rows = [(i, *map(float, p), int(c)) for i, p, c in zip(ids, P, lab)]
            with self.lock:
                self.db.execute("DELETE FROM data_points")
                self.db.executemany("INSERT INTO data_points VALUES(?,?,?,?,?)", rows)
                self.store.meta("data_fit_n", n)
                self.store.meta("data_layout", VERSION)
        else:
            idx = {i: k for k, i in enumerate(ids)}
            new = [i for i in ids if i not in placed]
            gone = [i for i in placed if i not in idx]
            known = [i for i in ids if i in placed]
            rows = []
            if new and known:
                Kk = [idx[i] for i in known]
                Pk = np.array([placed[i][:3] for i in known], dtype=np.float32)
                Pn = place_new(C[Kk], Pk, C[[idx[i] for i in new]], k=3, jitter=0.03)
                near = (C[[idx[i] for i in new]] @ C[Kk].T).argmax(1)
                rows = [(i, *map(float, p), placed[known[j]][3]) for i, p, j in zip(new, Pn, near)]
            with self.lock:
                self.db.executemany("INSERT OR REPLACE INTO data_points VALUES(?,?,?,?,?)", rows)
                self.db.executemany("DELETE FROM data_points WHERE id=?", [(i,) for i in gone])
        with self.lock:
            self.store.meta("data_layout_fp", fp)
            self.db.commit()
        self._cache = (None, None)
        return True

    # ---- points ---------------------------------------------------------------
    def points(self):
        fp = self.fingerprint()
        if self._cache[0] == fp:
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
        for repo, path, first_id, ts, n, authors in code:
            k = f"code:{repo}:{path}"
            if k not in pos:
                continue
            blame = Counter()
            for a in (authors or "").split("\x1f"):
                for who, lines in json.loads(a or "{}").items():
                    blame[who.split(" <")[0]] += lines
            full = f"{repo}/{path}"
            out.append(dict(id=k, type="code", open=f"code:{first_id}", title=Path(path).name, path=full,
                            display=full.replace(home, "~", 1), repo=repo_names.get(repo, Path(repo).name), repo_root=repo,
                            ext=Path(path).suffix.lstrip("."), mtime=ts, chunks=n,
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
