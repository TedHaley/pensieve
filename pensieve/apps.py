"""App sources: content that lives inside Mac apps, made searchable through the file indexer.

Each app either points at folders that already hold its content (Obsidian vaults) or exports its content as
Markdown into ~/.pensieve/apps/<App>/ (Apple Notes), which is then indexed like any other folder."""
import json
import re
import subprocess
import time
from pathlib import Path

from . import config

EXPORT_DIR = config.DATA_DIR / "apps"
_state = {}  # app id -> {"error": str | None, "count": int, "at": float}

NOTES_JXA = r"""
const Notes = Application('Notes');
const out = [];
for (const acc of Notes.accounts()) {
  for (const f of acc.folders()) {
    const folder = f.name();
    if (folder === 'Recently Deleted') continue;
    try {
      const n = f.notes;
      const ids = n.id(), names = n.name(), mods = n.modificationDate(), texts = n.plaintext();
      for (let i = 0; i < ids.length; i++)
        out.push({id: ids[i], name: names[i], folder: folder, account: acc.name(), mod: mods[i].getTime() / 1000, text: texts[i]});
    } catch (e) {}
  }
}
JSON.stringify(out);
"""


def _obsidian_vaults():
    cfg = Path.home() / "Library/Application Support/obsidian/obsidian.json"
    try:
        vaults = json.loads(cfg.read_text()).get("vaults", {})
        return [Path(v["path"]) for v in vaults.values() if Path(v.get("path", "")).is_dir()]
    except (OSError, ValueError, KeyError):
        return []


def _safe(name: str) -> str:
    return re.sub(r'[/\\:*?"<>|\n\r\t]+', " ", name).strip()[:80] or "Untitled"


def export_apple_notes():
    """Write every note to ~/.pensieve/apps/Apple Notes/<account>/<folder>/<title>.md (mtime = the note's).
    The first run asks macOS for permission to control Notes."""
    root = EXPORT_DIR / "Apple Notes"
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", NOTES_JXA], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        msg = r.stderr.strip()
        if "-1743" in msg or "not allowed" in msg.lower():
            msg = "Allow Pensieve to control Notes: System Settings → Privacy & Security → Automation."
        _state["apple_notes"] = dict(error=msg[:300], count=0, at=time.time())
        return
    notes = json.loads(r.stdout or "[]")
    index, want = {}, set()
    for n in notes:
        p = root / _safe(n["account"]) / _safe(n["folder"]) / f"{_safe(n['name'])} ({n['id'][-6:]}).md"
        want.add(p)
        index[str(p)] = n["id"]
        if p.exists() and abs(p.stat().st_mtime - n["mod"]) < 1:
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(n["text"] or "")
        import os
        os.utime(p, (n["mod"], n["mod"]))
    if root.exists():
        for p in root.rglob("*.md"):
            if p not in want:
                p.unlink()
    (EXPORT_DIR / "apple_notes.json").write_text(json.dumps(index))
    _state["apple_notes"] = dict(error=None, count=len(notes), at=time.time())


def note_id(path: str):
    """Apple Notes id for an exported note file, so 'open' can show the real note."""
    try:
        return json.loads((EXPORT_DIR / "apple_notes.json").read_text()).get(path)
    except (OSError, ValueError):
        return None


APPS = {
    "obsidian": dict(label="Obsidian", detail="Notes in your Obsidian vaults",
                     available=lambda: bool(_obsidian_vaults()), roots=_obsidian_vaults, refresh=None),
    "apple_notes": dict(label="Apple Notes", detail="Your notes, exported to Markdown inside ~/.pensieve (asks for permission)",
                        available=lambda: Path("/System/Applications/Notes.app").exists(),
                        roots=lambda: [EXPORT_DIR / "Apple Notes"], refresh=export_apple_notes),
}


def refresh(enabled):
    """Re-export apps that need it (called before each full file sweep)."""
    for app_id, a in APPS.items():
        if a["refresh"] and enabled(f"apps/{app_id}") and a["available"]():
            try:
                a["refresh"]()
            except Exception as e:
                _state[app_id] = dict(error=repr(e)[:300], count=0, at=time.time())


def roots(enabled):
    out = []
    for app_id, a in APPS.items():
        if enabled(f"apps/{app_id}") and a["available"]():
            out += [p for p in a["roots"]() if p.is_dir()]
    return out


def state(app_id):
    return _state.get(app_id, {})
