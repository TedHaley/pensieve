"""Where indexing and search time goes, on a scratch index (never ~/.pensieve).

  PENSIEVE_DATA=/tmp/x .venv/bin/python bench/speed.py index ~/Desktop/anastomo [more repos]
  PENSIEVE_DATA=/tmp/x .venv/bin/python bench/speed.py search [--tasks bench/out/*/tasks-v2.jsonl] [--memdb] [--profile]
  PENSIEVE_DATA=/tmp/x .venv/bin/python bench/speed.py proto       # vector search and embedding micro-benchmarks

Times the real code path (Repos._sync_repo, Searcher.find) with wrappers around the model (embed), the vector index
(turbovec), git blame and every SQLite call. Wrapped totals are summed across threads.
"""
import argparse
import glob
import json
import os
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

if "PENSIEVE_DATA" not in os.environ or ".pensieve" in os.environ["PENSIEVE_DATA"]:
    sys.exit("set PENSIEVE_DATA to a scratch folder")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

T = defaultdict(float)  # phase -> seconds (summed across threads)
N = defaultdict(int)
_tl = threading.Lock()
SQL_SPLIT = bool(os.environ.get("SQL_SPLIT"))
TOTALS = defaultdict(float)  # with SQL_SPLIT: seconds per statement over the whole run


def add(name, dt):
    with _tl:
        if name.startswith("sql: "):
            TOTALS[name] += dt
            return
        T[name] += dt
        N[name] += 1


def timed(name, fn):
    def wrap(*a, **k):
        t = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            add(name, time.perf_counter() - t)
    return wrap


class Cur:
    def __init__(self, c):
        self.c = c

    def __getattr__(self, k):
        return getattr(self.c, k)

    def fetchone(self):
        return timed("sql", self.c.fetchone)()

    def fetchall(self):
        return timed("sql", self.c.fetchall)()

    def __iter__(self):
        it = iter(self.c)
        while True:
            t = time.perf_counter()
            try:
                r = next(it)
            except StopIteration:
                add("sql", time.perf_counter() - t)
                return
            add("sql", time.perf_counter() - t)
            yield r


class DB:
    """sqlite3 connection proxy timing execute + row fetches as 'sql'."""

    def __init__(self, conn):
        self.conn = conn

    def __getattr__(self, k):
        return getattr(self.conn, k)

    def execute(self, *a):
        t = time.perf_counter()
        c = self.conn.execute(*a)
        dt = time.perf_counter() - t
        add("sql", dt)
        if SQL_SPLIT:  # execute() runs the statement to its first row, so ORDER BY / bm25 cost lands here
            add("sql: " + " ".join(str(a[0]).split())[:70], dt)
        return Cur(c)

    def executemany(self, *a):
        return Cur(timed("sql", self.conn.executemany)(*a))

    def executescript(self, *a):
        return timed("sql", self.conn.executescript)(*a)

    def commit(self):
        return timed("sql_commit", self.conn.commit)()


class Idx:
    def __init__(self, ix):
        self.ix = ix

    def __getattr__(self, k):
        return getattr(self.ix, k)

    def search(self, *a, **k):
        return timed("ann_search", self.ix.search)(*a, **k)

    def add_with_ids(self, *a, **k):
        return timed("ann_add", self.ix.add_with_ids)(*a, **k)

    def sync(self, *a, **k):
        return timed("ann_sync", self.ix.sync)(*a, **k)


def build():
    from pensieve import indexer, repos as repos_mod, search as search_mod
    from pensieve.files import Files
    from pensieve.indexer import Store
    from pensieve.repos import Repos
    store = Store()
    repos, files = Repos(store), Files(store)
    search_mod.ensure_fts(store.db, store.lock, store.meta, log=lambda m: None)
    store.db = repos.db = files.db = DB(store.db)
    for o in (store, repos, files):
        o.index = Idx(o.index)
    repos_mod.embed = timed("embed", repos_mod.embed)
    repos_mod.blame_lines = timed("blame", repos_mod.blame_lines)
    repos_mod.chunk_code = timed("read_chunk", repos_mod.chunk_code)
    search_mod.embed = timed("embed", indexer.embed)
    search_mod._snippet = timed("snippet", search_mod._snippet)  # runs for every candidate, before the cut to `limit`
    searcher = search_mod.Searcher(store, repos, files)
    return store, repos, files, searcher


def cmd_index(a):
    from pensieve import repos as repos_mod
    repos_mod.worktree_list = lambda root: []  # main checkouts only
    t0 = time.perf_counter()
    store, repos, files, _ = build()
    from pensieve.indexer import embed
    embed(["warm up"])
    print(f"startup + model load: {time.perf_counter() - t0:.1f}s")
    for root in a.repos:
        root = str(Path(root).expanduser().resolve())
        T.clear(); N.clear()
        t = time.perf_counter()
        n = repos._sync_repo(root, "", print)
        wall = time.perf_counter() - t
        with store.lock:
            chunks = store.db.execute("SELECT COUNT(*) FROM code_chunks WHERE repo=?", (root,)).fetchone()[0]
        t2 = time.perf_counter()
        repos.save()  # map layout (UMAP) + write code.tv
        save = time.perf_counter() - t2
        print(json.dumps(dict(repo=root, files=n, chunks=chunks, wall_s=round(wall, 1), save_s=round(save, 1),
                              files_per_min=round(n / wall * 60), chunks_per_s=round(chunks / wall, 1),
                              phases={k: round(v, 2) for k, v in sorted(T.items())}, calls=dict(N))))


def load_queries(paths):
    qs = []
    for p in paths:
        for ln in open(p):
            t = json.loads(ln)
            if "bucket" in t:  # a query set from query_set.py: one query per line
                qs.append((t["style"], t["query"]))
                continue
            for style in ("query", "ident", "literal", "mixed"):
                if t.get(style):
                    qs.append((style, t[style]))
    return qs


def pct(xs, p):
    return float(np.percentile(xs, p)) * 1000


def summary(rows, label):
    tot = np.array([r["total"] for r in rows])
    e = np.array([r["embed"] for r in rows])
    s = np.array([r["ann_search"] for r in rows])
    q = np.array([r["sql"] for r in rows])
    o = tot - e - s - q
    print(f"{label}: n={len(rows)} total p50 {pct(tot, 50):.1f} p95 {pct(tot, 95):.1f} ms | "
          f"embed p50 {pct(e, 50):.1f} p95 {pct(e, 95):.1f} ({np.mean([r['n_embed'] for r in rows]):.1f} calls) | "
          f"ann p50 {pct(s, 50):.2f} p95 {pct(s, 95):.2f} | sql p50 {pct(q, 50):.1f} p95 {pct(q, 95):.1f} | "
          f"python/other p50 {pct(o, 50):.1f} p95 {pct(o, 95):.1f} | share of total time: embed "
          f"{e.sum() / tot.sum():.0%} ann {s.sum() / tot.sum():.0%} sql {q.sum() / tot.sum():.0%} other {o.sum() / tot.sum():.0%}"
          f" | of other, snippets p50 {pct([r['snippet'] for r in rows], 50):.1f} ms "
          f"({np.mean([r['n_snippet'] for r in rows]):.0f} per search)")


def cmd_search(a):
    store, repos, files, searcher = build()
    from pensieve.indexer import embed
    if a.separate_embeds:  # the baseline: one model call per instruction instead of one batched call
        from pensieve import search as search_mod
        batched = search_mod.embed

        def separate(texts, query=False):
            if isinstance(query, list):
                return np.concatenate([batched([t], query=p) for t, p in zip(texts, query)])
            return batched(texts, query=query)
        search_mod.embed = separate
    embed(["warm up"])
    if a.memdb:  # the whole database copied into RAM: what an in-memory store would buy
        mem = sqlite3.connect(":memory:", check_same_thread=False)
        t = time.perf_counter()
        store.db.conn.backup(mem)
        from pensieve.search import code_words, repo_token
        mem.create_function("code_words", 1, code_words, deterministic=True)
        mem.create_function("repo_token", 1, repo_token, deterministic=True)
        store.db.conn = mem
        print(f"copied db to :memory: in {time.perf_counter() - t:.2f}s")
    searcher._folder_index(wait=True)
    qs = load_queries(a.tasks)
    with store.lock:
        n = store.db.execute("SELECT COUNT(*) FROM code_chunks").fetchone()[0]
    print(f"{len(qs)} queries over {n} code chunks")
    first = rows = None
    for rnd in range(a.rounds + 1):
        rows, prof = [], None
        if a.profile and rnd == a.rounds:
            import cProfile
            prof = cProfile.Profile()
            prof.enable()
        dump = []
        for style, q in qs:
            T.clear(); N.clear()
            t = time.perf_counter()
            res = searcher.find(q, limit=10)
            tot = time.perf_counter() - t
            dump.append([q, [{k: x.get(k) for k in ("id", "score", "snippet", "highlights")} for x in res["results"]]])
            rows.append(dict(style=style, total=tot, **{k: T[k] for k in ("embed", "ann_search", "sql", "snippet")},
                             n_embed=N["embed"], n_snippet=N["snippet"]))
        if prof:
            prof.disable()
            import pstats
            pstats.Stats(prof).sort_stats("tottime").print_stats(18)
        if rnd == 0:
            first = rows
    if a.dump:  # the last round's results, to check that a speed change returns the same results
        Path(a.dump).write_text(json.dumps(dump, indent=1))
    summary(first, "first pass (fresh process)")
    summary(rows, "warm pass")
    for st in ("query", "ident", "literal", "mixed"):
        summary([r for r in rows if r["style"] == st], f"  warm {st}")
    for k, v in sorted(TOTALS.items(), key=lambda x: -x[1])[:8]:
        print(f"{v / (len(qs) * (a.rounds + 1)) * 1000:6.2f} ms/query  {k}")


def cmd_proto(a):
    """Micro-benchmarks: turbovec vs exact numpy over the same vectors; query embedding split into tokenize vs model."""
    from turbovec import IdMapIndex
    from pensieve import config
    from pensieve.indexer import embedder, embed, CODE_QUERY
    db = sqlite3.connect(config.DB_PATH)
    ids, V = [], []
    for cid, v in db.execute("SELECT id, vec FROM code_chunks"):
        ids.append(cid); V.append(np.frombuffer(v, np.float16))
    V = np.stack(V).astype(np.float32)
    ix = IdMapIndex.load(str(config.DATA_DIR / "code.tv"))
    qtexts = [json.loads(ln)["query"] for p in a.tasks for ln in open(p)]
    Q = embed(qtexts, query=CODE_QUERY)

    def run(fn):
        ts = []
        for q in Q:
            t = time.perf_counter(); fn(q); ts.append(time.perf_counter() - t)
        return pct(ts, 50), pct(ts, 95)

    for scale in (1, 10):
        Vs = np.concatenate([V] * scale) if scale > 1 else V
        if scale > 1:  # a 10x larger corpus (copies), to see how each scales
            tix = IdMapIndex(dim=config.EMBED_DIM, bit_width=4)
            tix.add_with_ids(Vs, np.arange(1, len(Vs) + 1, dtype=np.uint64))
        else:
            tix = ix
        tv = run(lambda q: tix.search(q[None, :], k=400))
        npy = run(lambda q: np.argpartition(-(Vs @ q), 400)[:400])
        print(f"N={len(Vs)} vectors: turbovec k=400 p50 {tv[0]:.2f} p95 {tv[1]:.2f} ms | numpy fp32 exact p50 "
              f"{npy[0]:.2f} p95 {npy[1]:.2f} ms | fp32 matrix {Vs.nbytes / 1e6:.0f} MB")
    rec = []
    for q in Q:
        _, got = ix.search(q[None, :], k=50)
        exact = {ids[i] for i in np.argsort(-(V @ q))[:50]}
        rec.append(len(exact & set(int(x) for x in got[0])) / 50)
    print(f"turbovec (4-bit) recall@50 vs exact fp32: {np.mean(rec):.3f}")
    m = embedder()
    tok = getattr(m, "tok", None)
    ts_tok, ts_all = [], []
    for q in qtexts:
        if tok is not None:
            t = time.perf_counter()
            tok([CODE_QUERY + q], padding=True, truncation=True, max_length=512, return_tensors="np")
            ts_tok.append(time.perf_counter() - t)
        t = time.perf_counter(); embed([q], query=CODE_QUERY); ts_all.append(time.perf_counter() - t)
    print(f"query embed p50 {pct(ts_all, 50):.1f} p95 {pct(ts_all, 95):.1f} ms; tokenizer alone p50 "
          f"{pct(ts_tok, 50) if ts_tok else float('nan'):.2f} ms; embedder={type(m).__name__}")
    texts = [r[0] for r in db.execute("SELECT path || char(10) || text FROM code_chunks ORDER BY random() LIMIT 256")]
    for bs in (1, 8, 16, 32):
        t = time.perf_counter()
        m.encode(texts, batch_size=bs, normalize_embeddings=True)
        print(f"bulk embed batch {bs}: {len(texts) / (time.perf_counter() - t):.1f} chunks/s")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("index")
    p.add_argument("repos", nargs="+")
    default = sorted(glob.glob(str(Path(__file__).parent / "out" / "*" / "tasks-v2.jsonl")))
    for name in ("search", "proto"):
        p = sub.add_parser(name)
        p.add_argument("--tasks", nargs="+", default=default)
        p.add_argument("--rounds", type=int, default=1)
        p.add_argument("--memdb", action="store_true")
        p.add_argument("--profile", action="store_true")
        p.add_argument("--dump", help="search: write each query's results to this JSON file")
        p.add_argument("--separate-embeds", action="store_true", help="search: embed each query instruction in its own call")
    a = ap.parse_args()
    {"index": cmd_index, "search": cmd_search, "proto": cmd_proto}[a.cmd](a)


if __name__ == "__main__":
    main()
