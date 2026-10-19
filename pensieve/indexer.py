"""SQLite metadata + turbovec ANN index + embedding + 3D projection."""
import hashlib
import json
import re
import sqlite3
import threading
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from pensieve import config, connections, parsers, settings, vectors
from pensieve.layout import cluster, fit3d, nearest, place_new, unit
from pensieve.projects import project_name

LAYOUT_VERSION = "umap-v1"
ACTIVE_SECONDS = 30  # re-index a transcript that's still growing at most this often

_embedder = None
_embed_lock = threading.Lock()


def load_embedder(name):
    """Embedding model `name` on MLX where it can run there (Apple Silicon), else on sentence-transformers."""
    # one at a time, and not while a model embeds: transformers' first import isn't thread-safe (a second thread got
    # an ImportError), and loading weights onto the GPU beside another model's work crashed Metal
    ensure_downloaded(name)  # before the lock: a first download takes minutes, and searches must not wait for it
    with _embed_lock:
        return _load_embedder(name)


def ensure_downloaded(name):
    """Download model `name` if it isn't cached. In a child process: config turns Hugging Face offline once the
    index's own model is cached, and this process can't turn it back on."""
    import os
    import subprocess
    import sys
    from huggingface_hub import try_to_load_from_cache
    cfg = try_to_load_from_cache(name, "config.json")
    env = {k: v for k, v in os.environ.items() if k != "HF_HUB_OFFLINE"}
    if name == config.GEMMA:  # its text weights only (gemma_mlx.download_text), unless the whole model is here
        from pensieve.gemma_mlx import TEXT_WEIGHTS
        if isinstance(cfg, str) and any((Path(cfg).parent / f).exists() for f in (TEXT_WEIGHTS, "model.safetensors")):
            return
        subprocess.run([sys.executable, "-m", "pensieve.gemma_mlx", name], env=env, check=True)
        return
    if isinstance(cfg, str):
        return
    subprocess.run([sys.executable, "-c", "import sys; from huggingface_hub import snapshot_download; "
                    "snapshot_download(sys.argv[1])", name], env=env, check=True)


def _load_embedder(name):
    try:
        if name == config.GEMMA:
            from pensieve.gemma_mlx import MLXGemmaEmbedder
            return MLXGemmaEmbedder(name, config.EMBED_MAX_TOKENS)
        return MLXEmbedder(name, config.EMBED_MAX_TOKENS)
    except (ImportError, ValueError) as e:
        print(f"embedding model {name}: no MLX ({e!r}); using sentence-transformers", flush=True)
        from sentence_transformers import SentenceTransformer
        kw = {"config_kwargs": {"vision_config": None, "audio_config": None}} if name == config.GEMMA else {}
        m = SentenceTransformer(name, device=config.device(), **kw)
        m.max_seq_length = config.EMBED_MAX_TOKENS
        return m


def embedder():
    """The index's embedding model (config.EMBED_MODEL), loaded once."""
    global _embedder
    if _embedder is None or _embedder[0] != config.EMBED_MODEL:
        _embedder = (config.EMBED_MODEL, load_embedder(config.EMBED_MODEL))
    return _embedder[1]


def model_texts(texts, query=False, titles=None, name=None, m=None):
    """The texts as model `name` (the index's model by default) reads them. A query gets the model's instruction:
    `query` is True (general search), an instruction string such as CODE_QUERY, or one of those per text. A document
    gets the model's document format, with its title (a path) when there is one."""
    name = name or config.EMBED_MODEL
    gemma = name == config.GEMMA
    general = ""
    if (any(query) if isinstance(query, list) else query):  # only a query needs the loaded model, for its prompt
        general = (getattr(m or embedder(), "prompts", None) or {}).get("query", "")

    def instruction(p):
        if p is True or (gemma and p == CODE_QUERY):  # EmbeddingGemma: its search query beat its code query
            return general
        return p or None

    ps = [instruction(p) for p in query] if isinstance(query, list) else [instruction(query)] * len(texts)
    out = []
    for i, (p, t) in enumerate(zip(ps, texts)):
        title = titles[i] if titles else None
        if p is not None:
            out.append(p + t)
        elif gemma:
            out.append(f"title: {title or 'none'} | text: {t}")
        else:
            out.append(f"{title}\n{t}" if title else t)
    return out


class MLXEmbedder:
    """Qwen3-Embedding on Apple's MLX, without torch. Same vectors as sentence-transformers (cosine 0.998 or more
    on code chunks), so an index built with either one keeps working. `encode` takes the arguments that
    SentenceTransformer.encode takes here."""

    def __init__(self, name, max_tokens):
        import mlx.core as mx
        from huggingface_hub import snapshot_download
        from mlx_lm.models import qwen3
        from transformers import AutoTokenizer
        path = Path(snapshot_download(name))
        cfg = json.loads((path / "config.json").read_text())
        if cfg.get("model_type") != "qwen3":
            raise ValueError(f"MLX embedder only runs qwen3 models, not {cfg.get('model_type')}")
        st = path / "config_sentence_transformers.json"
        self.prompts = json.loads(st.read_text()).get("prompts", {}) if st.exists() else {}
        self.mx, self.max_tokens = mx, max_tokens
        self.model = qwen3.Qwen3Model(qwen3.ModelArgs.from_dict(cfg))
        self.model.load_weights(list(mx.load(str(path / "model.safetensors")).items()))
        mx.eval(self.model.parameters())
        self.tok = AutoTokenizer.from_pretrained(str(path))
        self.tok.padding_side = "left"  # the last position is then every text's last token

    def get_sentence_embedding_dimension(self):
        return self.model.args.hidden_size

    def encode(self, texts, batch_size=16, normalize_embeddings=True, show_progress_bar=False, prompt=None,
               prompt_name=None):
        mx = self.mx
        prompt = prompt if prompt is not None else self.prompts.get(prompt_name, "") if prompt_name else ""
        texts = [prompt + t for t in texts]
        order = sorted(range(len(texts)), key=lambda i: -len(texts[i]))  # similar lengths: less padding
        out = np.zeros((len(texts), self.get_sentence_embedding_dimension()), np.float32)
        for i in range(0, len(order), batch_size):
            idx = order[i:i + batch_size]
            enc = self.tok([texts[j] for j in idx], padding=True, truncation=True, max_length=self.max_tokens,
                           return_tensors="np")
            h = self.model.embed_tokens(mx.array(enc["input_ids"]))
            mask = self._mask(mx.array(enc["attention_mask"]), h.dtype)
            for layer in self.model.layers:
                h = layer(h, mask, None)
            v = self.model.norm(h)[:, -1, :].astype(mx.float32)
            if normalize_embeddings:
                v = v / mx.linalg.norm(v, axis=-1, keepdims=True)
            out[idx] = np.array(v)
        mx.clear_cache()  # MLX keeps freed buffers for reuse; between calls they only hold memory
        return out

    def _mask(self, attn, dtype):
        """Causal attention that skips the left padding. Each position also sees itself, so padding rows stay finite."""
        mx = self.mx
        n = attn.shape[1]
        keep = mx.tril(mx.ones((n, n), dtype=mx.bool_))[None, None] & attn.astype(mx.bool_)[:, None, None, :]
        keep = keep | mx.eye(n, dtype=mx.bool_)[None, None]
        return mx.where(keep, 0.0, -mx.inf).astype(dtype)


CODE_QUERY = "Instruct: Given a description of what some code does, retrieve the source code that implements it\nQuery:"


class _Count:
    """A thread-safe counter (`+=` on a global int can lose updates between threads)."""

    def __init__(self):
        self.n, self._lock = 0, threading.Lock()

    def add(self, d):
        with self._lock:
            self.n += d

    def __bool__(self):
        return self.n > 0


queries_waiting = _Count()   # searches waiting for the model; indexing lets them go first
searches_running = _Count()  # searches in progress: indexing threads pause between files so a search gets the interpreter


def _wait_while(count, limit=1.0):
    t = time.monotonic()
    while count and time.monotonic() - t < limit:
        time.sleep(0.003)


def yield_to_searches():
    """Called by indexing threads between units of work. Waits while a search runs, at most a second."""
    _wait_while(searches_running)
BULK_SLICE = 8  # indexing texts per model call: 16 runs ~10% faster, but a search could then wait twice as long


def embed(texts, query=False, titles=None, model=None) -> np.ndarray:
    """`query`: True for the model's general search instruction, an instruction string (e.g. CODE_QUERY), or a list
    with one of those per text, so a search gets its differently-instructed vectors from one model call. `titles`:
    a document's path, for the models that take it apart. `model`: (name, loaded model) instead of the index's.
    Indexing embeds its texts BULK_SLICE at a time and steps aside between slices while a search waits, so a search
    during indexing waits for one slice (a quarter second at most), not for a whole batch of files."""
    searching = bool(query) if not isinstance(query, list) else any(query)
    name, m = model if model else (config.EMBED_MODEL, None)
    kw = dict(batch_size=16, normalize_embeddings=True, show_progress_bar=False)
    if searching:
        m = m or embedder()
        texts = model_texts(texts, query, titles, name, m)
        queries_waiting.add(1)
        try:
            with _embed_lock:
                return np.asarray(m.encode(texts, **kw), dtype=np.float32)
        finally:
            queries_waiting.add(-1)
    texts = model_texts(texts, query, titles, name, m)
    out = np.zeros((len(texts), config.EMBED_MODELS[name]["dim"] if name in config.EMBED_MODELS else config.EMBED_DIM),
                   np.float32)
    # each distinct text is embedded once: a text in the cache, or twice in this call, is not embedded again
    from pensieve import embedcache
    cache = embedcache.get()
    keys = [embedcache.key(t) for t in texts]
    have = cache.get(name, keys) if cache else {}
    first = {}  # key -> position of the first text with it that is not in the cache
    for i, k in enumerate(keys):
        if k in have:
            out[i] = have[k]
        else:
            first.setdefault(k, i)
    todo = list(first.values())
    if cache:
        cache.hits += len(texts) - len(todo)
        cache.misses += len(todo)
    if todo:
        m = m or embedder()
    order = sorted(todo, key=lambda i: -len(texts[i]))  # similar lengths in a slice: less padding
    for i in range(0, len(order), BULK_SLICE):
        _wait_while(queries_waiting)
        idx = order[i:i + BULK_SLICE]
        with _embed_lock:
            out[idx] = m.encode([texts[j] for j in idx], **kw)
    if cache and todo:
        cache.put(name, [(keys[i], out[i]) for i in todo])
    for i, k in enumerate(keys):  # copies within this call
        if k not in have and first[k] != i:
            out[i] = out[first[k]]
    return out


def embed_images(paths) -> list:
    """One vector per image file, in the same space as the text vectors, or None for a file that can't be decoded.
    The model's image part must be downloaded (content.install). Steps aside for a waiting search between images."""
    m = embedder()
    out = []
    for p in paths:
        _wait_while(queries_waiting)
        try:
            with _embed_lock:
                out.append(m.encode_images([p])[0])
        except Exception as e:  # truncated, damaged or huge images, and a HEIC that sips can't convert
            print(f"image {p}: not read ({e!r})", flush=True)
            out.append(None)
    return out


def embed_media(path, video: bool, sound: bool) -> list:
    """A video or sound file as [(start, seconds, kind, vector)]: one vector per window of what it shows (video) and
    one per window of its sound (sound), each window as media.segments cuts it. Decodes a window, then embeds it, so
    a search waits for one window at most. Raises OSError when the file can't be decoded."""
    from pensieve import media
    m = embedder()
    have = media.info(path)
    out = []
    if video and have["video"]:
        for start, secs in media.segments(have["duration"], media.VIDEO_SEGMENT):
            frames = media.frames(path, start, secs)
            if frames:
                _wait_while(queries_waiting)
                with _embed_lock:
                    out.append((start, secs, "video", m.encode_video(frames)))
    if sound and have["audio"]:
        for start, secs in media.segments(have["duration"], media.AUDIO_SEGMENT):
            s = media.samples(path, start, secs)
            if s.size >= media.AUDIO_RATE // 10:  # a tenth of a second at least
                _wait_while(queries_waiting)
                with _embed_lock:
                    out.append((start, secs, "audio", m.encode_audio(s)))
    return out


def chunk_session(turns):
    """Group into user->assistant exchanges, splitting long ones on paragraph boundaries."""
    exchanges, cur = [], []
    for role, text in turns:
        if role == "user" and cur:
            exchanges.append(cur)
            cur = []
        cur.append(f"{'User' if role == 'user' else 'Memory' if role == 'memory' else 'Assistant'}: {text}")
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



_TEMP = re.compile(r"^(/private)?/(tmp|var/folders)/")


def in_temp(path: str) -> bool:
    """A system temp folder, when skip_temp_sessions is on: sessions that ran there are throwaway (Pensieve's own AI
    calls through the Claude Code CLI, scripts, tests), not the user's work."""
    return bool(path) and bool(_TEMP.match(path)) and settings.get("skip_temp_sessions")

class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self._indexed_at = {}
        self.behind = False  # the last sync_files ran out of time with sessions left to index
        self.progress = None  # sessions being indexed now: {name: agent, root, done, total=None}
        self._ignored = {}  # transcript path -> (mtime, size) of files that are not the user's sessions
        self.db = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        # write-ahead log: a commit appends instead of rewriting a journal, and NORMAL syncs at checkpoints rather
        # than every commit (still crash-safe; a power cut can lose only the last few commits, which re-index)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        from pensieve.search import code_words, repo_token
        self.db.create_function("code_words", 1, code_words, deterministic=True)  # used by the code keyword index
        self.db.create_function("repo_token", 1, repo_token, deterministic=True)
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
        self.index = vectors.Index(config.INDEX_PATH, config.EMBED_DIM)
        self.index.load(self.db, "chunks")

    # ---- ingestion -------------------------------------------------------
    def sync_files(self, log=lambda m: print(m, flush=True), on_progress=None, budget=None) -> int:
        """Index new/changed transcript files. Returns number of sessions touched. With `budget` (seconds), stops
        after that long and sets `behind`: the caller sweeps folders and calls again for the rest."""
        import time
        end = time.monotonic() + budget if budget else None
        self.behind = False
        touched = self._purge_disabled(log) + self._purge_own(log) + self._purge_memories(log)
        with self.lock:
            known = {r[0]: (r[1], r[2]) for r in self.db.execute("SELECT path, mtime, size FROM sessions")}
        for source, path in parsers.discover():
            if end and time.monotonic() > end:
                self.behind = True
                break
            st = path.stat()
            if known.get(str(path)) == (st.st_mtime, st.st_size):
                continue
            # transcripts are append-only JSONL (a half-written last line is skipped), so no need to wait for quiet;
            # a session that's still being written re-indexes at most every ACTIVE_SECONDS (new turns only)
            if str(path) in known and time.time() - self._indexed_at.get(str(path), 0) < ACTIVE_SECONDS:
                continue
            self._indexed_at[str(path)] = time.time()
            if self._ignored.get(str(path)) == (st.st_mtime, st.st_size):
                continue
            try:
                if not self._index_session(source, path, st):
                    self._ignored[str(path)] = (st.st_mtime, st.st_size)
                    continue
                touched += 1
                log(f"indexed {source}:{path.stem[:8]}")
                if touched >= 5:  # a first index or a batch of new sessions, not one live session's new turns
                    self.progress = dict(name=source, root=str(path.parent), done=touched, total=None)
                if on_progress and touched % 8 == 0:
                    on_progress(touched)
            except Exception as e:  # one bad file shouldn't stop the sweep
                log(f"skip {path}: {e!r}")
        self.progress = None
        if touched:  # the map catches up later (server.map_worker)
            self.save()
        return touched

    def _purge_disabled(self, log):
        """Drop sessions from agents, or session folders, that were switched off or removed in Sources."""
        roots = defaultdict(list)
        for sid, name, root, _ in config.session_sources():
            if settings.enabled(sid):
                roots[name].append(str(root) + "/")
        with self.lock:
            off = [s for (s,) in self.db.execute("SELECT DISTINCT source FROM sessions") if not settings.enabled(f"agents/{s}")]
            n = 0
            for src in off:
                n += self._delete_sessions([sid for (sid,) in self.db.execute("SELECT id FROM sessions WHERE source=?", (src,))])
                log(f"removed {src} sessions (source switched off)")
            gone = [sid for sid, src, path in self.db.execute("SELECT id, source, path FROM sessions")
                    if src not in off and path and not any(path.startswith(r) for r in roots[src])]
            if gone:
                n += self._delete_sessions(gone)
                log(f"removed {len(gone)} sessions from session folders that were switched off")
        return n

    def _purge_own(self, log):
        """Drop sessions that ran in temp folders (e.g. Pensieve's own AI calls through the Claude Code CLI)."""
        with self.lock:
            own = [sid for sid, proj in self.db.execute(
                "SELECT id, project FROM sessions WHERE project LIKE '/tmp/%' OR project LIKE '/private/%' "
                "OR project LIKE '/var/folders/%'") if in_temp(proj)]
            n = self._delete_sessions(own)
        if n:
            log(f"removed {n} sessions that ran in temp folders")
        return n

    def _purge_memories(self, log):
        """Drop memory files that were deleted. Transcripts stay after their file goes, but a deleted memory was
        usually wrong or out of date."""
        with self.lock:
            gone = [sid for sid, path in self.db.execute("SELECT id, path FROM sessions WHERE id LIKE 'claude:memory/%'")
                    if not Path(path).exists()]
        n = self._delete_sessions(gone)
        if n:
            log(f"removed {n} deleted memory files")
        return n

    def _delete_sessions(self, sids):
        with self.lock:
            for i in range(0, len(sids), 500):
                part = sids[i:i + 500]
                q = ",".join("?" * len(part))
                for (cid,) in self.db.execute(f"SELECT id FROM chunks WHERE session_id IN ({q})", part).fetchall():
                    try:
                        self.index.remove(cid)
                    except Exception:
                        pass
                self.db.execute(f"DELETE FROM chunks WHERE session_id IN ({q})", part)
                self.db.execute(f"DELETE FROM session_files WHERE session_id IN ({q})", part)
                self.db.execute(f"DELETE FROM sessions WHERE id IN ({q})", part)
            if sids:
                self.db.commit()
        return len(sids)

    def save_session_files(self, sid, touched: dict):
        with self.lock:
            self.db.execute("DELETE FROM session_files WHERE session_id=?", (sid,))
            rows = [(sid, p, int(e)) for p, e in touched.items()] or [(sid, "", 0)]
            self.db.executemany("INSERT OR REPLACE INTO session_files VALUES(?,?,?)", rows)
            self.db.commit()

    def _index_session(self, source, path: Path, st):
        """Index one transcript; returns False for one that ran in a temp folder (not the user's work)."""
        s = parsers.parse(source, path)
        if in_temp(s.project):
            return False
        self.save_session_files(s.id, s.touched)
        if s.mcp and settings.enabled(f"connections/{connections.SESSIONS}"):
            connections.from_session(s.id, s.mcp, s.updated or s.started)
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
        return True

    def save(self, force=False):
        """Write the session vector index to its file: at most every vectors.SAVE_SECONDS, unless `force`."""
        with self.lock:
            return self.index.save(force)

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
        """UMAP refit and new topics when the corpus grew or shrank meaningfully (or on first run); otherwise place
        new points by neighbours. Shrinking counts too: after a big removal the old topics describe what's gone."""
        with self.lock:
            rows = self.db.execute("SELECT id, session_id, x, y, z FROM chunks").fetchall()
            fit_n = int(self.meta("session_fit_n") or 0)
            version_ok = self.meta("layout") == LAYOUT_VERSION
        found, V = self.index.take([r[0] for r in rows])
        rows = [r[:2] + (None,) + r[2:] for r, ok in zip(rows, found) if ok]  # r[3:6]: x, y, z as before
        if len(rows) < 4:
            return False
        cid = np.array([r[0] for r in rows])
        sid = np.array([r[1] for r in rows])
        X = unit(V.astype(np.float32))
        have = np.array([r[3] is not None for r in rows])
        n = len(rows)
        full = force or not version_ok or have.sum() < 0.5 * n or abs(n - fit_n) > max(60, 0.15 * fit_n)
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
                near = nearest(C[missing], C[known])
                for j, i in enumerate(missing):
                    cl = placed[sessions[known[near[j]]]][3]
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
            rows = self.db.execute("SELECT id, session_id FROM chunks").fetchall()
        by = {}
        for cid, s in rows:
            by.setdefault(s, []).append(cid)
        return vectors.centroids(by, self.index)

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
