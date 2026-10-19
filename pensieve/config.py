import os
from pathlib import Path

HOME = Path.home()
DATA_DIR = Path(os.environ.get("PENSIEVE_DATA", HOME / ".pensieve")).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)

V2_DIR = DATA_DIR / "v2"  # storage v2 (docs/design/storage.md); the v1 files are in DATA_DIR until migrate removes them
VECTORS_DIR = V2_DIR / "vectors"
DB_PATH = V2_DIR / "index.db"  # sessions, files and code, with the other derived tables (store.index_db)
INDEX_PATH = VECTORS_DIR / "sessions.tv"
V1_DB_PATH = DATA_DIR / "pensieve.db"  # read only by migrate.py and the model choice below
PORT = int(os.environ.get("PENSIEVE_PORT", 8765))
URL = f"http://127.0.0.1:{PORT}"


def write_atomic(path: Path, text: str):
    """Write text to a file in one step: a reader sees the old file or the new one, never a part of it. The temporary
    file starts with a dot, so that the file indexer skips it."""
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

# Where each agent keeps its transcripts, and their file pattern below that folder. Add more agents here.
SESSION_GLOB = {"claude": "*/*.jsonl", "qwen": "*/chats/*.jsonl", "codex": "**/rollout-*.jsonl"}
SOURCES = [
    ("claude", HOME / ".claude" / "projects", SESSION_GLOB["claude"]),
    ("qwen", HOME / ".qwen" / "projects", SESSION_GLOB["qwen"]),
    ("codex", HOME / ".codex" / "sessions", SESSION_GLOB["codex"]),
]


def session_sources() -> list[tuple[str, str, Path, str]]:
    """(source id, agent, folder, glob) for the default folders and the ones added in Sources (`session_folders`,
    "agent:path" strings)."""
    from pensieve import settings
    out = [(f"agents/{name}", name, root, pattern) for name, root, pattern in SOURCES]
    for entry in settings.get("session_folders"):
        name, _, p = entry.partition(":")
        if name in SESSION_GLOB and p:
            out.append((f"agents/{name}@{p}", name, Path(p).expanduser(), SESSION_GLOB[name]))
    return out


def detect_sessions(folder: Path) -> tuple[str, Path] | None:
    """Which agent's transcripts a folder holds, and the folder the glob starts from (a config folder like ~/.claude
    holds them in projects/, ~/.codex in sessions/)."""
    for name, sub in (("codex", "sessions"), ("qwen", "projects"), ("claude", "projects")):
        for root in (folder / sub, folder):
            if root.is_dir() and next(root.glob(SESSION_GLOB[name]), None):
                return name, root
    return None

QWEN, GEMMA = "Qwen/Qwen3-Embedding-0.6B", "google/embeddinggemma-2"
EMBED_MODELS = {  # the embedding models Pensieve can index with; an index holds vectors of one model only
    # rate: chunks per second of a first index on an M4 Max, until this Mac has measured its own
    QWEN: dict(label="Qwen3 Embedding 0.6B", dim=1024, rate=30,
               about="The original model. 600M parameters: a 1.2 GB download the first time."),
    GEMMA: dict(label="EmbeddingGemma 2", dim=768, rate=55,
                about="About twice as fast to index and 25% smaller, with the same search quality in our tests. "
                      "270M parameters (its text part): a 550 MB download the first time."),
}
EMBED_MODEL_FILE = V2_DIR / "embed_model"  # the model the index was built with, written at the first start


DEFAULT_EMBED_MODEL = GEMMA  # for a new index; an index from before models could be chosen stays Qwen's


def _has_vectors(path: Path) -> bool:
    import sqlite3
    if not path.exists():
        return False
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return any(db.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()
                       for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                                              "('chunks', 'code_chunks', 'file_chunks')"))
        finally:
            db.close()
    except sqlite3.Error:
        return True  # can't tell: keep the model an older index has


def _index_model() -> str:
    """The model of the index in DATA_DIR: the one recorded with it; Qwen for an index from before models could be
    chosen; DEFAULT_EMBED_MODEL for a new one."""
    if os.environ.get("PENSIEVE_EMBED_MODEL"):
        return os.environ["PENSIEVE_EMBED_MODEL"]
    try:
        name = EMBED_MODEL_FILE.read_text().strip()
    except OSError:
        name = ""
    if name in EMBED_MODELS:
        return name
    try:  # the first start of v2 on v1 data: v1's model, so that the vectors of v1 fill the embed cache
        name = "" if _has_vectors(DB_PATH) else (DATA_DIR / "embed_model").read_text().strip()
    except OSError:
        name = ""
    if name in EMBED_MODELS:
        pass
    elif _has_vectors(DB_PATH) or _has_vectors(V1_DB_PATH):
        name = QWEN
    else:  # a new or reset index: the model chosen in Settings, else the default (settings.json, read directly:
        try:  # importing settings here would be circular)
            import json
            name = json.loads((DATA_DIR / "settings.json").read_text()).get("embed_model", "")
        except (OSError, ValueError, AttributeError):
            name = ""
        name = name if name in EMBED_MODELS else DEFAULT_EMBED_MODEL
    EMBED_MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    EMBED_MODEL_FILE.write_text(name + "\n")
    return name


EMBED_MODEL = _index_model()
EMBED_DIM = EMBED_MODELS.get(EMBED_MODEL, EMBED_MODELS[QWEN])["dim"]
EMBED_MAX_TOKENS = 512


def use_model(name: str):
    """Make `name` the index's model (after the index holds only its vectors)."""
    global EMBED_MODEL, EMBED_DIM
    EMBED_MODEL, EMBED_DIM = name, EMBED_MODELS[name]["dim"]
    EMBED_MODEL_FILE.write_text(name + "\n")

# Once the embedding model is cached, skip Hugging Face network checks (they can hang); on first run, download it.
if "HF_HUB_OFFLINE" not in os.environ:
    try:
        from huggingface_hub import try_to_load_from_cache
        if isinstance(try_to_load_from_cache(EMBED_MODEL, "config.json"), str):
            os.environ["HF_HUB_OFFLINE"] = "1"
    except Exception:
        pass

# Options that the user can change are in settings.py (~/.pensieve/settings.json).

CHUNK_CHARS = 1800
POLL_SECONDS = 5
SETTLE_SECONDS = 1  # don't index a file modified more recently than this (it can be half written)


def find_exe(name: str) -> str | None:
    """A program on this Mac by name, also in the folders that the app's PATH misses (~/.local/bin and the like)."""
    import shutil
    if not name:
        return None
    if shutil.which(name):
        return shutil.which(name)
    for d in (HOME / ".local/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"), HOME / ".npm-global/bin"):
        if os.access(d / name, os.X_OK):
            return str(d / name)
    return None


def device():
    import torch
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"
