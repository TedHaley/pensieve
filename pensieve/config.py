import os
from pathlib import Path

HOME = Path.home()
DATA_DIR = Path(os.environ.get("PENSIEVE_DATA", HOME / ".pensieve")).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)

V2_DIR = DATA_DIR / "v2"  # storage v2 (docs/design/storage.md); the v1 files are in DATA_DIR until migrate removes them
VECTORS_DIR = V2_DIR / "vectors"
DB_PATH = V2_DIR / "index.db"  # sessions, files and code, with the other derived tables (store.index_db)
INDEX_PATH = VECTORS_DIR / "sessions.tv"
V1_DB_PATH = DATA_DIR / "pensieve.db"  # read only by migrate.py
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

GEMMA = "google/embeddinggemma-2"
EMBED_MODEL = GEMMA  # the one embedding model: every vector in the index is EmbeddingGemma 2's
EMBED_LABEL = "EmbeddingGemma 2"
EMBED_DIM = 768
EMBED_RATE = 55  # chunks per second of a first index on an M4 Max, until this Mac has measured its own
EMBED_MAX_TOKENS = 512

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
