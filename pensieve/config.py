import os
from pathlib import Path

HOME = Path.home()
DATA_DIR = Path(os.environ.get("PENSIEVE_DATA", HOME / ".pensieve")).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "pensieve.db"
INDEX_PATH = DATA_DIR / "index.tv"

# Session sources: (name, root, glob). Add more agents here.
SOURCES = [
    ("claude", HOME / ".claude" / "projects", "*/*.jsonl"),
    ("qwen", HOME / ".qwen" / "projects", "*/chats/*.jsonl"),
    ("codex", HOME / ".codex" / "sessions", "**/rollout-*.jsonl"),
]

# Repos are discovered from the folders your agent sessions ran in. These add more: repo roots, or parent
# folders that are searched (3 levels deep) for git repos. Set via `pensieve --repos` or PENSIEVE_REPOS.
EXTRA_REPOS = [Path(p).expanduser() for p in os.environ.get("PENSIEVE_REPOS", "").split(os.pathsep) if p.strip()]

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

LLM_URL = os.environ.get("PENSIEVE_LLM_URL", "http://localhost:1234/v1")
LLM_MODEL = os.environ.get("PENSIEVE_LLM_MODEL", "qwen/qwen3.5-9b")

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
