"""Video and sound files read for search: decoded into the frames and samples that EmbeddingGemma 2 reads, in
segments of at most VIDEO_SEGMENT (video) or AUDIO_SEGMENT (sound) seconds, since the model reads 32 frames or
30 seconds of sound at most. Each segment becomes one chunk of the file, found by what it shows or what is said.

The decoder is the Mac app's `pensieve-media` (AVFoundation: every format macOS plays), found through
PENSIEVE_MEDIA_TOOL, an installed Pensieve.app or a build in mac/'s build folder; else PyAV when it is installed."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from pensieve.gemma_mlx import AUDIO_MAX_SECONDS, AUDIO_RATE, VIDEO_FPS, VIDEO_MAX_FRAMES

VIDEO_SEGMENT = VIDEO_MAX_FRAMES / VIDEO_FPS  # 32 s: one frame a second, as the model samples a video
AUDIO_SEGMENT = AUDIO_MAX_SECONDS
MAX_SEGMENTS = 240  # per file: two hours of sound, or of video at one segment per 32 s; the rest is left out
VERSION = "m2"  # part of a video or sound file's index mode: bump to read every such file again


def tool():
    """The pensieve-media decoder, or None."""
    home = Path.home()
    for p in (os.environ.get("PENSIEVE_MEDIA_TOOL"), "/Applications/Pensieve.app/Contents/MacOS/pensieve-media",
              home / "Applications/Pensieve.app/Contents/MacOS/pensieve-media",
              home / "Library/Caches/pensieve-mac-build/arm64-apple-macosx/release/pensieve-media"):
        if p and os.access(p, os.X_OK):
            return str(p)
    return None


def _pyav():
    try:
        import av
        return av
    except ImportError:
        return None


def decoder() -> str | None:
    """'pensieve-media', 'pyav', or None when no decoder is here."""
    return "pensieve-media" if tool() else "pyav" if _pyav() else None


def _run(*args, timeout=600):
    r = subprocess.run([tool(), *map(str, args)], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise OSError(r.stderr.strip() or f"pensieve-media {args[0]} failed")
    return r.stdout.strip()


def info(path) -> dict:
    """{duration (seconds), video, audio}: what the file has."""
    if tool():
        return json.loads(_run("info", path, timeout=60))
    av = _pyav()
    with av.open(str(path)) as c:
        d = c.duration / 1e6 if c.duration else 0.0
        return dict(duration=d, video=bool(c.streams.video), audio=bool(c.streams.audio))


def segments(duration, length, cap=MAX_SEGMENTS) -> list[tuple[float, float]]:
    """(start, seconds) windows that cover `duration`; the last one ends with the file. A file shorter than a window
    is one window, and so is a file whose duration is unknown."""
    if not duration:
        return [(0.0, length)]
    n = max(1, min(cap, -(-int(duration) // int(length))))
    return [(i * length, min(length, duration - i * length)) for i in range(n)]


def frames(path, start=0.0, seconds=VIDEO_SEGMENT):
    """The frames of a video window as RGB PIL images: VIDEO_FPS a second, at most VIDEO_MAX_FRAMES."""
    from PIL import Image
    if tool():
        with tempfile.TemporaryDirectory() as d:
            _run("frames", path, d, start, seconds, VIDEO_FPS, VIDEO_MAX_FRAMES)
            out = []
            for f in sorted(Path(d).glob("frame*.png")):
                with Image.open(f) as im:
                    out.append(im.convert("RGB"))
            return out
    av = _pyav()
    out, nxt = [], start
    with av.open(str(path)) as c:
        for fr in c.decode(video=0):
            if fr.time is None or fr.time + 1e-6 < nxt:
                continue
            if fr.time >= start + seconds:
                break
            out.append(fr.to_image())
            nxt += 1 / VIDEO_FPS
    if len(out) > VIDEO_MAX_FRAMES:
        out = [out[i] for i in np.linspace(0, len(out) - 1, VIDEO_MAX_FRAMES, dtype=int)]
    return out


def samples(path, start=0.0, seconds=AUDIO_SEGMENT) -> np.ndarray:
    """A window of the sound, mono float32 at AUDIO_RATE. Both decoders give the file's own rate; one resampler
    (resample) brings it to AUDIO_RATE, so the two give the same samples."""
    if tool():
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "sound.f32"
            rate = int(_run("audio", path, out, start, seconds).split()[0])
            return resample(np.fromfile(out, dtype="<f4"), rate)[:int(seconds * AUDIO_RATE)]
    av = _pyav()
    with av.open(str(path)) as c:
        if start:
            c.seek(int(start * 1e6))
        rate = c.streams.audio[0].sample_rate
        rs = av.AudioResampler(format="flt", layout="mono", rate=rate)
        parts, have = [], 0
        for fr in c.decode(audio=0):
            if fr.time is not None and fr.time + fr.samples / fr.sample_rate <= start:
                continue
            for r in rs.resample(fr):
                parts.append(r.to_ndarray().ravel())
                have += parts[-1].size
            if have >= seconds * rate:
                break
    s = np.concatenate(parts).astype(np.float32) if parts else np.zeros(0, np.float32)
    return resample(s[:int(seconds * rate)], rate)


def resample(s, rate) -> np.ndarray:
    """Samples at `rate` to AUDIO_RATE, with a polyphase filter."""
    if rate == AUDIO_RATE or not s.size:
        return s.astype(np.float32)
    from math import gcd
    from scipy.signal import resample_poly
    g = gcd(int(rate), AUDIO_RATE)
    return resample_poly(s, AUDIO_RATE // g, int(rate) // g).astype(np.float32)


def label(start, seconds) -> str:
    """A window as m:ss-m:ss, for a chunk's text."""
    def t(s):
        s = int(s)
        return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
    return f"{t(start)}-{t(start + seconds)}"
