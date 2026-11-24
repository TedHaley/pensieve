//! The file watch (FSEvents on macOS, through `notify`): it tells the indexers what changed, so that edits show up
//! in seconds. A change is sorted into an agent transcript, a document in an indexed folder, or a repo checkout
//! (with a linked worktree and its .git metadata: commits, checkouts, staging). The indexers still sweep every few
//! minutes, so a missed event only delays an update.
//!
//! The app owns the loop that drains it: `set_wake` gives the function that wakes that loop, and the loop calls the
//! `take_*` methods.

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::{Arc, Mutex, MutexGuard, RwLock};

use notify::{EventKind, RecursiveMode, Watcher};

use crate::Index;

const IGNORE_PARTS: [&str; 14] = ["node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".next", ".turbo", ".gradle", ".idea", "dist", "build", "target"];
const GIT_META: [&str; 5] = ["/index", "/HEAD", "/refs/", "/packed-refs", "/worktrees/"];

type Wake = Arc<dyn Fn() + Send + Sync>;

/// What changed since the loop last looked.
#[derive(Default)]
pub struct Pending {
    pub sessions: bool,
    pub session_paths: HashSet<String>,
    pub files: HashSet<String>,
    pub repos: HashSet<String>,
    pub full_files: bool,
    pub full_repos: bool,
    /// Checkout folder -> its canonical repo root.
    checkouts: HashMap<String, String>,
    folders: Vec<String>,
    sources: Vec<String>,
    data_dir: String,
}

#[derive(Default)]
pub struct State {
    pending: Arc<Mutex<Pending>>,
    wake: Arc<RwLock<Option<Wake>>>,
    watcher: Mutex<Option<notify::RecommendedWatcher>>,
    watched: Mutex<Vec<String>>,
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

fn wake(w: &RwLock<Option<Wake>>) {
    if let Some(f) = w.read().unwrap_or_else(|e| e.into_inner()).as_ref() {
        f();
    }
}

impl Pending {
    fn repo_for(&self, path: &str) -> Option<String> {
        let best = self.checkouts.iter()
            .filter(|(co, _)| path == co.as_str() || path.starts_with(&format!("{co}/")))
            .max_by_key(|(co, _)| co.len());
        if let Some((_, repo)) = best {
            return Some(repo.clone());
        }
        let i = path.find("/.git/")?;
        (i > 0).then(|| self.checkouts.get(&path[..i]).cloned()).flatten()
    }

    /// Sort one changed path. Returns whether the indexer loop should wake.
    fn note(&mut self, path: &str, is_dir: bool) -> bool {
        let parts: Vec<&str> = path.split('/').collect();
        if parts.iter().any(|p| IGNORE_PARTS.contains(p))
            || (!self.data_dir.is_empty() && path.contains(&self.data_dir)) {
            return false;
        }
        if self.sources.iter().any(|s| path.starts_with(&format!("{s}/"))) {
            self.sessions = true;
            if !is_dir {
                self.session_paths.insert(path.to_string());
            }
            return true;
        }
        if let Some(repo) = self.repo_for(path) {
            let hit = match path.find("/.git") {
                Some(i) if path.contains("/.git/") || path.ends_with("/.git") => GIT_META.iter().any(|m| path[i..].contains(m)),
                _ => !path.ends_with('~') && ![".swp", ".swx", ".tmp"].iter().any(|e| path.ends_with(e)) && !path.contains("/.git"),
            };
            if hit {
                self.repos.insert(repo);
            }
            return hit;
        }
        if self.folders.iter().any(|f| path.starts_with(&format!("{f}/"))) && !parts.iter().any(|p| p.starts_with('.')) {
            if is_dir {
                self.full_files = true;
            } else {
                self.files.insert(path.to_string());
            }
            return true;
        }
        false
    }
}

impl State {
    /// The function that wakes the app's indexer loop; called after each change that matters.
    pub fn set_wake(&self, f: impl Fn() + Send + Sync + 'static) {
        *self.wake.write().unwrap_or_else(|e| e.into_inner()) = Some(Arc::new(f));
    }

    /// Sort one changed path (also for tests and for changes the app learns of itself, such as a hook's edit).
    pub fn note(&self, path: &str, is_dir: bool) {
        if lock(&self.pending).note(path, is_dir) {
            wake(&self.wake);
        }
    }

    /// The pending transcript and document changes: (sessions changed, document paths, full files pass wanted).
    /// Repo changes are taken by the repo loop.
    pub fn take(&self) -> (bool, HashSet<String>, bool) {
        let mut p = lock(&self.pending);
        let out = (p.sessions, std::mem::take(&mut p.files), p.full_files);
        p.sessions = false;
        p.full_files = false;
        out
    }

    /// The transcripts that changed since the last call.
    pub fn take_sessions(&self) -> HashSet<String> {
        std::mem::take(&mut lock(&self.pending).session_paths)
    }

    /// (changed canonical repo roots, whether a full repo sweep was asked for).
    pub fn take_repos(&self) -> (HashSet<String>, bool) {
        let mut p = lock(&self.pending);
        let full = std::mem::replace(&mut p.full_repos, false);
        (std::mem::take(&mut p.repos), full)
    }

    /// Only the changed repo roots: a pending full-sweep request stays.
    pub fn take_changed_repos(&self) -> HashSet<String> {
        std::mem::take(&mut lock(&self.pending).repos)
    }

    pub fn requeue_repos(&self, roots: impl IntoIterator<Item = String>) {
        lock(&self.pending).repos.extend(roots);
        wake(&self.wake);
    }

    /// Sweep the document folders on the next pass (a folder came into use).
    pub fn force_files(&self) {
        lock(&self.pending).full_files = true;
        wake(&self.wake);
    }

    /// Settings changed: sweep everything on the next pass.
    pub fn force_full(&self) {
        {
            let mut p = lock(&self.pending);
            p.full_files = true;
            p.full_repos = true;
        }
        wake(&self.wake);
    }

    /// Only the changed document paths: session changes and a full-pass request stay.
    pub fn take_files(&self) -> HashSet<String> {
        std::mem::take(&mut lock(&self.pending).files)
    }

    pub fn requeue_files(&self, paths: impl IntoIterator<Item = String>) {
        lock(&self.pending).files.extend(paths);
    }
}

impl Index {
    /// (Re)start watching the transcript folders, the indexed folders and every repo checkout (`checkouts`: checkout
    /// folder -> canonical repo root, from the repo indexer). Call at start and whenever these change. Watching a
    /// folder covers everything below it, so nested folders are watched once.
    pub fn watch_configure(&self, checkouts: &HashMap<String, String>) {
        let w = &self.watch;
        let folders: Vec<String> = self.files_roots().iter().map(|p| p.to_string_lossy().into_owned()).collect();
        let sources: Vec<String> = self.config().session_sources().into_iter()
            .filter(|s| s.2.is_dir()).map(|s| s.2.to_string_lossy().into_owned()).collect();
        let checkouts: HashMap<String, String> =
            checkouts.iter().map(|(k, v)| (k.trim_end_matches('/').to_string(), v.clone())).collect();
        let mut want: Vec<String> = sources.iter().chain(&folders).chain(checkouts.keys()).cloned().collect();
        want.sort();
        want.dedup();
        let roots: Vec<String> = want.iter()
            .filter(|p| !want.iter().any(|q| q != *p && p.starts_with(&format!("{q}/")))).cloned().collect();
        {
            let mut p = lock(&w.pending);
            p.checkouts = checkouts;
            p.folders = folders;
            p.sources = sources;
            p.data_dir = store::data_dir().to_string_lossy().into_owned();
        }
        let mut watcher = lock(&w.watcher);
        if *lock(&w.watched) == roots && watcher.is_some() {
            return;
        }
        *watcher = None;
        let (pending, wk) = (w.pending.clone(), w.wake.clone());
        let handler = move |res: notify::Result<notify::Event>| {
            let Ok(ev) = res else { return };
            if matches!(ev.kind, EventKind::Access(_)) {
                return;
            }
            let mut hit = false;
            {
                let mut p = lock(&pending);
                for path in &ev.paths {
                    let s = path.to_string_lossy();
                    hit |= p.note(&s, Path::new(path).is_dir());
                }
            }
            if hit {
                wake(&wk);
            }
        };
        let Ok(mut nw) = notify::recommended_watcher(handler) else { return };
        for r in &roots {
            let _ = nw.watch(Path::new(r), RecursiveMode::Recursive);
        }
        *watcher = Some(nw);
        *lock(&w.watched) = roots;
        drop(watcher);
        wake(&w.wake);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sorts_changes() {
        let mut p = Pending {
            checkouts: [("/w/app".to_string(), "/w/app".to_string()), ("/w/app-feature".to_string(), "/w/app".to_string())].into(),
            folders: vec!["/h/Documents".into()],
            sources: vec!["/h/.claude/projects".into()],
            ..Default::default()
        };
        assert!(p.note("/h/.claude/projects/x/s.jsonl", false));
        assert!(p.note("/w/app-feature/src/a.rs", false));
        assert!(p.note("/w/app/.git/refs/heads/main", false));
        assert!(!p.note("/w/app/.git/objects/ab/cd", false));
        assert!(!p.note("/w/app/node_modules/x.js", false));
        assert!(p.note("/h/Documents/plan.md", false));
        assert!(!p.note("/h/Documents/.hidden/plan.md", false));
        assert!(p.note("/h/Documents/new", true));
        assert_eq!(p.repos.len(), 1);
        assert!(p.files.contains("/h/Documents/plan.md") && p.full_files);
        assert_eq!(p.session_paths.len(), 1);
    }
}
