import os
from pathlib import Path

HOME = Path.home()
DATA_DIR = Path(os.environ.get("PENSIEVE_DATA", HOME / ".pensieve")).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "pensieve.db"
INDEX_PATH = DATA_DIR / "index.tv"
PORT = int(os.environ.get("PENSIEVE_PORT", 8765))
URL = f"http://127.0.0.1:{PORT}"

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
EMBED_MODEL_FILE = DATA_DIR / "embed_model"  # the model the index was built with, written at the first start


DEFAULT_EMBED_MODEL = GEMMA  # for a new index; an index from before models could be chosen stays Qwen's


def _has_vectors() -> bool:
    import sqlite3
    if not DB_PATH.exists():
        return False
    try:
        db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
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
    if _has_vectors():
        name = QWEN
    else:  # a new or reset index: the model chosen in Settings, else the default (settings.json, read directly:
        try:  # importing settings here would be circular)
            import json
            name = json.loads((DATA_DIR / "settings.json").read_text()).get("embed_model", "")
        except (OSError, ValueError, AttributeError):
            name = ""
        name = name if name in EMBED_MODELS else DEFAULT_EMBED_MODEL
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

# Folders, repos, LLM endpoint and the other user-facing options live in settings.py (~/.pensieve/settings.json).

CHUNK_CHARS = 1800
POLL_SECONDS = 5
SETTLE_SECONDS = 3  # don't index a file modified more recently than this


def device():
    import torch
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"
