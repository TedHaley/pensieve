import os
from pathlib import Path

HOME = Path.home()
DATA_DIR = Path(os.environ.get("PENSIEVE_DATA", HOME / ".pensieve")).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "pensieve.db"
INDEX_PATH = DATA_DIR / "index.tv"
PORT = int(os.environ.get("PENSIEVE_PORT", 8765))
URL = f"http://127.0.0.1:{PORT}"

# Session sources: (name, root, glob). Add more agents here.
SOURCES = [
    ("claude", HOME / ".claude" / "projects", "*/*.jsonl"),
    ("qwen", HOME / ".qwen" / "projects", "*/chats/*.jsonl"),
    ("codex", HOME / ".codex" / "sessions", "**/rollout-*.jsonl"),
]

EMBED_MODEL = os.environ.get("PENSIEVE_EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B")
EMBED_DIM = 1024
EMBED_MAX_TOKENS = 512

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
