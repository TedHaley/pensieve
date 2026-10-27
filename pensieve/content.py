"""What the search model reads besides text: images, video and sound. Each is off until the user switches it on;
then its part of EmbeddingGemma 2 downloads (the install), and once it is here, Pensieve reads those files by what
they show or say. A file of a kind that is off, or not installed yet, is found by its name.

A kind's switch is its file type's `index` setting (settings.file_types): content = on."""
import subprocess
import sys
import threading
from pathlib import Path

from pensieve import config, settings

# kind -> the model parts it needs (gemma_mlx.PARTS), whether it needs the video and sound decoder, and what it is
KINDS = {
    "images": dict(parts=["vision"], decoder=False, label="Images",
                   description="Screenshots, diagrams and photos, by what they show."),
    "video": dict(parts=["vision"], decoder=True, label="Video",
                  description="Screen recordings and videos, by what they show, 32 seconds at a time. With audio on, "
                              "by what is said in them too."),
    "audio": dict(parts=["audio"], decoder=True, label="Audio",
                  description="Voice notes, meetings and other sound, by what is said or heard, 30 seconds at a time."),
}
PART_MB = {"vision": 335, "audio": 611}  # EmbeddingGemma 2's image and sound encoders
_jobs = {}  # part -> {done, total, error}: a download that runs or failed
_lock = threading.Lock()
on_installed = []  # callables run when a part finishes downloading (the server re-sweeps)


def model_folder():
    from huggingface_hub import try_to_load_from_cache
    cfg = try_to_load_from_cache(config.GEMMA, "config.json")
    return Path(cfg).parent if isinstance(cfg, str) else None


def part_here(part) -> bool:
    from pensieve.gemma_mlx import part_file
    folder = model_folder()
    return bool(folder and part_file(folder, part))


def switched_on(kind) -> bool:
    return settings.file_type(kind)["index"] == "content"


def decoder() -> str | None:
    from pensieve import media
    return media.decoder()


def ready(kind) -> bool:
    """Whether files of `kind` are read by content now: switched on, its model parts are downloaded and, for video
    and sound, a decoder is here."""
    k = KINDS[kind]
    return (switched_on(kind) and all(part_here(p) for p in k["parts"])
            and (not k["decoder"] or decoder() is not None))


def status() -> list[dict]:
    """Each kind: on, ready, and what stands in the way (needs, installing with its progress, error)."""
    out = [dict(id="text", label="Text", on=True, ready=True, size_mb=0, locked=True,
                description="Code, documents, notes and agent sessions.")]
    for kind, k in KINDS.items():
        missing = [p for p in k["parts"] if not part_here(p)]
        jobs = {p: _jobs[p] for p in k["parts"] if p in _jobs}
        o = dict(id=kind, label=k["label"], description=k["description"], on=switched_on(kind), ready=ready(kind),
                 size_mb=sum(PART_MB[p] for p in k["parts"]), installed=not missing)
        if k["decoder"] and decoder() is None:
            o["needs"] = "decoder"
        err = next((j["error"] for j in jobs.values() if j.get("error")), None)
        if err:
            o["error"] = err
        elif missing and any(not j.get("error") for j in jobs.values()):
            done = sum(j["done"] for j in jobs.values())
            total = sum(j["total"] or PART_MB[p] << 20 for p, j in jobs.items())
            o["installing"] = dict(done=done, total=total)
        out.append(o)
    return out


def install(kind):
    """Download the parts `kind` needs that aren't here, in the background. Returns at once."""
    for part in KINDS[kind]["parts"]:
        with _lock:
            running = part in _jobs and not _jobs[part].get("error")
            if part_here(part) or running:
                continue
            _jobs[part] = dict(done=0, total=0, error=None)
        threading.Thread(target=_download, args=(part,), daemon=True, name=f"install-{part}").start()


def _download(part):
    """One part, in a child process that may go online (this one is offline once the index's model is here)."""
    import os
    env = {k: v for k, v in os.environ.items() if k != "HF_HUB_OFFLINE"}
    try:
        p = subprocess.Popen([sys.executable, "-m", "pensieve.gemma_mlx", config.GEMMA, part], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for line in p.stdout:
            if line.startswith("progress "):
                _, d, t = line.split()
                _jobs[part].update(done=int(d), total=int(t))
        err = p.stderr.read()
        if p.wait() != 0:
            raise OSError((err.strip().splitlines() or ["the download failed"])[-1])
        with _lock:
            _jobs.pop(part, None)
        for f in on_installed:
            f()
    except Exception as e:
        with _lock:
            _jobs[part] = dict(done=0, total=0, error=str(e))


def switch(kind, on: bool):
    """Switch a kind on (and install what it needs) or off (its files are found by name again)."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; one of: {', '.join(KINDS)}")
    with _lock:  # a failed download is tried again
        for p in KINDS[kind]["parts"]:
            if _jobs.get(p, {}).get("error"):
                _jobs.pop(p)
    settings.update({"file_types": {kind: {"index": "content" if on else "name", "map": on}}})
    if on:
        install(kind)


def remove(part):
    """Delete a downloaded part that no switched-on kind needs, to free its space."""
    from pensieve.gemma_mlx import PARTS
    if any(switched_on(k) and part in v["parts"] for k, v in KINDS.items()):
        raise ValueError(f"{part} is in use")
    folder = model_folder()
    f = folder / PARTS[part][0] if folder else None
    if f and f.exists():
        f.unlink()


def resume():
    """At start: install what switched-on kinds still need (a download that was cut off, or a new Mac)."""
    for kind in KINDS:
        if switched_on(kind):
            install(kind)
