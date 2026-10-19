"""The vectors of one kind of chunk (sessions, files, code): a turbovec index for search, and the same vectors as
float16 arrays in memory for the consumers that need the vectors themselves (the folder index, the maps, centroids,
team projects).

The arrays load once, from the chunk table, and then change with every add and remove. Before this module each of
those consumers read every vector BLOB from SQLite again, every 30 to 120 seconds while the index changed.

The turbovec file (`.tv`) is saved at most every SAVE_SECONDS, when nothing is indexed (the server's map worker) and
at shutdown. After a crash, `load` makes the index match the table again: it adds the vectors that are missing, and
builds the index again from the arrays when it holds ids that the table no longer has.
"""
import threading
import time
from pathlib import Path

import numpy as np

SAVE_SECONDS = 600  # longest time between saves of a .tv file while the index changes
MIN_ROWS = 1024


class Index:
    """Has the methods of turbovec's IdMapIndex that Pensieve uses (add_with_ids, remove, contains, search, len),
    and `take` / `matrix` for the vectors. turbovec is not safe for a write during a search, so each turbovec call
    takes `_tv`. The arrays have their own lock."""

    def __init__(self, path, dim, bit_width=4, fresh=False):
        """`fresh`: an empty index, whatever the file holds (a switch to a model with other dimensions)."""
        from turbovec import IdMapIndex
        self.path, self.dim = Path(path), dim
        if self.path.exists() and not fresh:
            self.tv = IdMapIndex.load(str(self.path))
        else:
            self.tv = IdMapIndex(dim=dim, bit_width=bit_width)
        self._bit_width = bit_width
        self._lock = threading.Lock()
        self._tv = threading.RLock()
        self._ids = np.zeros(MIN_ROWS, np.int64)
        self._V = np.zeros((MIN_ROWS, dim), np.float16)
        self.n = 0
        self._pos = {}  # id -> row in the arrays
        self.version = 0  # changes with every add and remove
        self.dirty = False  # changes since the last save of the .tv file
        self.saved_at = time.monotonic()

    def load(self, db, table) -> int:
        """Read every vector of `table` (id, vec) into the arrays, once, and make the turbovec index match the table.
        Returns the vectors that the index had to add or drop."""
        n = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        ids = np.zeros(max(n, MIN_ROWS), np.int64)
        V = np.zeros((max(n, MIN_ROWS), self.dim), np.float16)
        k = 0
        for cid, blob in db.execute(f"SELECT id, vec FROM {table}"):
            if k == len(ids):  # rows added since the count
                ids, V = np.resize(ids, 2 * k), np.resize(V, (2 * k, self.dim))
            if blob is None or len(blob) != 2 * self.dim:
                continue
            ids[k] = cid
            V[k] = np.frombuffer(blob, np.float16)
            k += 1
        with self._lock:
            self._ids, self._V, self.n = ids, V, k
            self._pos = {int(i): j for j, i in enumerate(ids[:k])}
            self.version += 1
        with self._tv:
            missing = [j for j in range(k) if not self.tv.contains(int(ids[j]))]
            fixed = len(missing)
            if missing:
                m = np.array(missing)
                self.tv.add_with_ids(V[m].astype(np.float32), ids[m].astype(np.uint64))
        if len(self) != k:  # ids of rows that are gone (removed after the last save): build it again
            fixed += abs(len(self) - k)
            self.rebuild()
        if fixed:
            self.dirty = True
            self.save(force=True)
        return fixed

    def rebuild(self):
        """A new turbovec index from the arrays (about a second for 300k vectors)."""
        from turbovec import IdMapIndex
        tv = IdMapIndex(dim=self.dim, bit_width=self._bit_width)
        with self._lock:
            for a in range(0, self.n, 50_000):  # in slices: one float32 copy of every vector is 1 GB at 300k
                b = min(a + 50_000, self.n)
                tv.add_with_ids(self._V[a:b].astype(np.float32), self._ids[a:b].astype(np.uint64))
        with self._tv:
            self.tv = tv
        self.dirty = True

    def add_with_ids(self, vecs, ids):
        vecs = np.asarray(vecs)
        ids = np.asarray(ids, dtype=np.uint64)
        with self._tv:
            self.tv.add_with_ids(vecs.astype(np.float32, copy=False), ids)
        with self._lock:
            need = self.n + len(ids)
            if need > len(self._ids):
                size = max(need, 2 * len(self._ids))
                self._ids = np.resize(self._ids, size)
                V = np.zeros((size, self.dim), np.float16)
                V[:self.n] = self._V[:self.n]
                self._V = V
            for j, i in enumerate(ids):
                self._pos[int(i)] = self.n + j
            self._ids[self.n:need] = ids.astype(np.int64)
            self._V[self.n:need] = vecs.astype(np.float16)
            self.n = need
            self.version += 1
        self.dirty = True

    def remove(self, cid) -> bool:
        with self._tv:
            found = self.tv.remove(int(cid))
        with self._lock:
            j = self._pos.pop(int(cid), None)
            if j is not None:  # the last row moves into its place
                last = self.n - 1
                if j != last:
                    self._ids[j] = self._ids[last]
                    self._V[j] = self._V[last]
                    self._pos[int(self._ids[j])] = j
                self.n = last
                self.version += 1
        self.dirty = True
        return found

    def contains(self, cid) -> bool:
        with self._tv:
            return self.tv.contains(int(cid))

    def search(self, q, k, allowlist=None):
        with self._tv:
            return self.tv.search(q, k=k, allowlist=allowlist) if allowlist is not None else self.tv.search(q, k=k)

    def __len__(self):
        with self._tv:
            return len(self.tv)

    def save(self, force=False) -> bool:
        """Write the turbovec index to its file when it changed, and SAVE_SECONDS have gone by since the last save
        (or `force`). turbovec appends what changed, but a sync after many removes rewrites the whole file."""
        if not self.dirty or (not force and time.monotonic() - self.saved_at < SAVE_SECONDS):
            return False
        self.dirty = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._tv:
            try:
                self.tv.sync(str(self.path))
            except OSError as e:  # the file changed after our last sync (seen in the field): write it whole
                print(f"vector index {self.path.name}: {e}; writing it whole", flush=True)
                self.tv.write(str(self.path))
        self.saved_at = time.monotonic()
        return True

    def take(self, ids):
        """(found, vectors): `found` is a bool mask over `ids` of the ids that have a vector; `vectors` holds their
        float16 vectors, in the order of `ids`."""
        with self._lock:
            rows = np.fromiter((self._pos.get(int(i), -1) for i in ids), np.int64, count=len(ids))
            found = rows >= 0
            return found, self._V[rows[found]]

    def matrix(self):
        """(ids, float16 vectors) of every vector: copies, so that a caller can use them without the lock."""
        with self._lock:
            return self._ids[:self.n].copy(), self._V[:self.n].copy()


def centroids(groups: dict, index: Index):
    """keys, unit float32 centroids: the mean vector of each key's chunk ids (`groups`: key -> [chunk id]). A key
    whose chunks have no vector is left out."""
    keys = list(groups)
    out_keys, parts, step = [], [], 20_000  # keys at a time: the float32 sums stay small
    for a in range(0, len(keys), step):
        part = keys[a:a + step]
        flat = np.fromiter((i for k in part for i in groups[k]), np.int64)
        owner = np.repeat(np.arange(len(part)), [len(groups[k]) for k in part])
        found, V = index.take(flat)
        owner = owner[found]  # sorted: the ids are in the order of the keys
        if not len(owner):
            continue
        starts = np.flatnonzero(np.r_[True, owner[1:] != owner[:-1]])
        C = np.add.reduceat(V, starts, axis=0, dtype=np.float32)
        C /= np.linalg.norm(C, axis=1, keepdims=True) + 1e-12  # the direction of the mean
        out_keys += [part[i] for i in owner[starts]]
        parts.append(C)
    if not parts:
        return [], np.zeros((0, index.dim), np.float32)
    return out_keys, np.concatenate(parts)
