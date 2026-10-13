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
SESSION_GLOB = {"claude": "*/*.jsonl", "qwen": "*/chats/*.jsonl", "codex": "**/rollout-*.jsonl",
                "opencode": "opencode.db"}
_XDG_DATA = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local" / "share")).expanduser()
_OPENCODE_DB = Path(os.environ.get("OPENCODE_DB", "opencode.db")).expanduser()
if not _OPENCODE_DB.is_absolute():
    _OPENCODE_DB = _XDG_DATA / "opencode" / _OPENCODE_DB
SOURCES = [
    ("claude", HOME / ".claude" / "projects", SESSION_GLOB["claude"]),
    ("qwen", HOME / ".qwen" / "projects", SESSION_GLOB["qwen"]),
    ("codex", HOME / ".codex" / "sessions", SESSION_GLOB["codex"]),
    ("opencode", _OPENCODE_DB.parent, _OPENCODE_DB.name),
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
    for name, sub in (("opencode", "opencode"), ("codex", "sessions"), ("qwen", "projects"),
                      ("claude", "projects")):
        for root in (folder / sub, folder):
            if root.is_dir() and next(root.glob(SESSION_GLOB[name]), None):
                return name, root
    return None

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
