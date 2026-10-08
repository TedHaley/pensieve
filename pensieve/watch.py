"""File-system watcher (FSEvents on macOS) that tells the indexer what changed, so edits show up in seconds.

Changes are sorted into: agent transcripts, documents in the indexed folders, and repo checkouts (including
linked worktrees and their .git metadata: commits, checkouts, staging). The indexer still does a full sweep
every few minutes, so a missed event only delays an update."""
import os
import threading
from pathlib import Path

from . import config, settings

IGNORE_PARTS = {"node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache",
                ".next", ".turbo", ".gradle", ".idea", "dist", "build", "target"}
GIT_META = ("/index", "/HEAD", "/refs/", "/packed-refs", "/worktrees/")


class Watch:
    def __init__(self, wake):
        self.wake = wake               # callable, thread-safe: wakes the indexer loop
        self.lock = threading.Lock()
        self.sessions = False
        self.files: set[str] = set()
        self.repos: set[str] = set()   # canonical repo roots
        self.full_files = False
        self.full_repos = False
        self.observer = None
        self.watched: tuple = ()
        self.checkouts: dict[str, str] = {}
        self.folders: list[str] = []
        self.sources: list[str] = []

    def configure(self, checkouts: dict):
        """(Re)start watching the session folders, the indexed folders and every repo checkout."""
        self.checkouts = {k.rstrip("/"): v for k, v in checkouts.items()}
        from .files import Files
        self.folders = [str(p) for p in Files.roots()]
        self.sources = [str(root) for _, root, _ in config.SOURCES if root.is_dir()]
        want = sorted(set(self.sources + self.folders + list(self.checkouts)))
        # watching a folder covers everything below it
        roots = tuple(p for p in want if not any(p != q and p.startswith(q + "/") for q in want))
        if roots == self.watched and self.observer:
            return
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            return
        outer = self

        class H(FileSystemEventHandler):
            def on_any_event(self, ev):
                if ev.event_type in ("opened", "closed_no_write"):
                    return
                for p in (ev.src_path, getattr(ev, "dest_path", "")):
                    if p:
                        outer.note(os.fsdecode(p), ev.is_directory)

        if self.observer:  # FSEvents streams are global per path: the old ones must be gone before re-adding
            self.observer.stop()
            self.observer.join(timeout=5)
        self.observer = Observer()
        for r in roots:
            try:
                self.observer.schedule(H(), r, recursive=True)
            except (OSError, RuntimeError):
                pass
        self.observer.daemon = True
        self.observer.start()
        self.watched = roots
        self.wake()  # catch anything that changed while we weren't watching

    def note(self, path: str, is_dir=False):
        parts = path.split("/")
        if IGNORE_PARTS.intersection(parts) or str(config.DATA_DIR) in path:
            return
        hit = False
        with self.lock:
            if any(path.startswith(s + "/") for s in self.sources):
                self.sessions = hit = True
            else:
                repo = self._repo_for(path)
                if repo:
                    if "/.git/" in path or path.endswith("/.git"):
                        hit = any(m in path[path.index("/.git"):] for m in GIT_META)
                    else:
                        hit = not path.endswith(("~", ".swp", ".swx", ".tmp")) and "/.git" not in path
                    if hit:
                        self.repos.add(repo)
                elif any(path.startswith(f + "/") for f in self.folders) and not any(p.startswith(".") for p in parts):
                    if is_dir:
                        self.full_files = True
                    else:
                        self.files.add(path)
                    hit = True
        if hit:
            self.wake()

    def _repo_for(self, path):
        best = None
        for co, repo in self.checkouts.items():
            if (path == co or path.startswith(co + "/")) and (best is None or len(co) > len(best[0])):
                best = (co, repo)
        if best:
            return best[1]
        # a linked worktree's metadata lives in <main>/.git/worktrees/<name>/
        i = path.find("/.git/")
        if i > 0 and path[:i] in self.checkouts:
            return self.checkouts[path[:i]]
        return None

    def take(self):
        """Pending session/document changes (repo changes are taken separately by the repo loop)."""
        with self.lock:
            out = dict(sessions=self.sessions, files=self.files, full_files=self.full_files)
            self.sessions, self.files, self.full_files = False, set(), False
        return out

    def take_repos(self):
        """(changed canonical repo roots, whether a full repo sweep was requested)."""
        with self.lock:
            out, full, self.repos, self.full_repos = self.repos, self.full_repos, set(), False
        return out, full

    def take_changed_repos(self):
        """Just the changed repo roots (leaves a pending full-sweep request alone)."""
        with self.lock:
            out, self.repos = self.repos, set()
        return out

    def requeue_repos(self, roots):
        with self.lock:
            self.repos |= set(roots)
        self.wake()

    def force_full(self):
        """Settings changed: re-sweep everything on the next pass."""
        with self.lock:
            self.full_files = self.full_repos = True
        self.wake()

    def requeue_files(self, paths):
        with self.lock:
            self.files |= set(paths)
