//! Git repos as code chunks: discovery, the chunks of each checkout with their blame, commits, worktrees, the code
//! map, and what the code says about people (experts, owners, gaps, territory).
//!
//! `repos_sync` and `repos_refresh` run on the app's background threads. They read files and run git blame on
//! worker threads, embed outside any lock, and write each group of files in one short transaction, so a search
//! never waits for them.

pub mod answers;
pub mod git;
pub mod query;

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::Instant;

use serde_json::{Value, json};
use store::rusqlite::{self, OptionalExtension, params};

use crate::{Index, db, layout, vectors};
use git::{git, tree_state};

/// Files read, chunked and blamed ahead of the embedder.
pub const INDEX_WORKERS: usize = 4;
pub const INDEX_AHEAD: usize = 32;
/// Chunks gathered from several files before one embed call, so small files fill model batches.
pub const EMBED_GROUP: usize = 48;
/// Files to (re)index before blame goes through the commit-graph shadow (`git::blame_shadow`).
pub const SHADOW_MIN_FILES: usize = 20;
pub const PROGRESS_MIN_FILES: usize = 20;
pub const CHARS_PER_CHUNK: u64 = 1000;
pub const RATE_MIN_CHUNKS: usize = 2000;
pub const EMBED_RATE: f64 = 55.0;
pub const EMBED_MODEL: &str = "google/embeddinggemma-2";
/// The pseudo-repo of every indexed repo in one joint layout, paths prefixed with `<repo>/`.
pub const ALL: &str = "__all__";
pub const LAYOUT_VERSION: &str = "umap-v1";

/// Where a session belongs: its canonical repo and the repo folders it touched (`repo/dir/sub`).
#[derive(Debug, Clone, Default)]
pub struct SessionScope {
    pub project: String,
    pub root: Option<String>,
    pub folder: String,
    pub dirs: Vec<String>,
}

type Canon = (Instant, HashMap<String, String>, Vec<(String, String)>);

/// The code indexer's state between calls, and caches of what the queries derive from the chunks.
#[derive(Default)]
pub struct State {
    sync_lock: Mutex<()>,
    sweeping: AtomicBool,
    refreshing: AtomicBool,
    servicing: AtomicBool,
    /// The checkout being indexed now: {name, root, done, total, seconds_left}.
    pub progress: Mutex<Option<Value>>,
    removing: Mutex<HashSet<String>>,
    recheck: Mutex<HashSet<String>>,
    /// Roots with edits waiting (the file watch's queue).
    pending: Mutex<HashSet<String>>,
    /// (root, remote) of the repos the last sweep found.
    pub found: Mutex<Vec<(String, String)>>,
    /// Repos discovery skipped for their size: {root, files}.
    pub skipped: Mutex<Vec<Value>>,
    pub upstream: Mutex<BTreeMap<String, Value>>,
    /// Chunks whose vector was copied from an identical chunk instead of embedded.
    pub reused: AtomicUsize,
    unsaved: AtomicBool,
    last: Mutex<Option<Instant>>,
    /// Repos whose code changed since the last layout; None: all.
    layout_due: Mutex<Option<HashSet<String>>>,
    /// Roots whose commits or worktrees changed, for the app's card builder.
    commit_events: Mutex<BTreeSet<String>>,
    canon: Mutex<Option<Canon>>,
    scope: Mutex<Option<((i64, f64, i64), Instant, Arc<HashMap<String, SessionScope>>)>>,
    extra: Mutex<Option<(String, Instant, Vec<String>)>>,
    pub(crate) cache: Mutex<HashMap<String, Arc<query::Cached>>>,
    pub(crate) team: Mutex<HashMap<(String, i64), (Instant, Value)>>,
    protected_at: Mutex<HashMap<String, Instant>>,
    pub(crate) ctimes: Mutex<HashMap<String, (String, Arc<HashMap<String, f64>>)>>,
}

pub(crate) fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// A file prepared for embedding: its chunks and the blame of its lines.
struct Prepared {
    path: String,
    blob: String,
    chunks: Vec<(i64, i64, String)>,
    blame: HashMap<usize, (String, i64)>,
    image: bool,
}

fn file_name(p: &str) -> &str {
    git::file_name(p)
}

impl Index {
    /// The data folder for the blame shadows (next to index.db).
    fn data_dir(&self) -> PathBuf {
        self.db.path().parent().filter(|p| !p.as_os_str().is_empty()).map(Path::to_path_buf).unwrap_or_else(store::data_dir)
    }

    /// Whether images are indexed by what they show.
    fn images_on(&self) -> bool {
        self.config().get("file_types").get("images").and_then(|t| t.get("index")).and_then(Value::as_str) == Some("content")
    }

    /// Put repos with edits on the queue that `repos_refresh` takes (the file watch calls it).
    pub fn repos_queue(&self, roots: &[String]) {
        lock(&self.repos.pending).extend(roots.iter().cloned());
    }

    fn take_pending(&self) -> HashSet<String> {
        std::mem::take(&mut *lock(&self.repos.pending))
    }

    /// Roots whose commits or worktrees changed since the last call (0.21's commit_listeners: the card builder).
    pub fn take_commit_events(&self) -> Vec<String> {
        std::mem::take(&mut *lock(&self.repos.commit_events)).into_iter().collect()
    }

    pub fn repos_progress(&self) -> Option<Value> {
        lock(&self.repos.progress).clone()
    }

    /// A sweep holds the code index now (the folder index is not rebuilt during one).
    pub fn repos_sweeping(&self) -> bool {
        self.repos.sweeping.load(Ordering::Relaxed)
    }

    fn paths(&self, key: &str) -> Vec<PathBuf> {
        let cfg = self.config();
        cfg.strs(key).iter().map(|p| cfg.expand(p)).collect()
    }

    /// Repos beyond the ones sessions ran in: settings `repos`, and with repos/discover the repos found under the
    /// indexed folders and sweep roots (`sweep_depth` levels down). Found repos with more than `max_repo_files`
    /// tracked files are skipped. Cached for 10 minutes while the settings stay the same.
    fn extra_roots(&self) -> Vec<String> {
        let cfg = self.config();
        let key = cfg.settings.to_string();
        if let Some((k, at, v)) = lock(&self.repos.extra).as_ref() {
            if *k == key && at.elapsed().as_secs() < 600 {
                return v.clone();
            }
        }
        let depth = cfg.int("sweep_depth").max(0) as usize;
        let ex = cfg.strs("exclude");
        let mut out: Vec<String> = self.paths("repos").iter().flat_map(|b| git::find_repos(b, depth, &ex)).collect();
        let mut swept: Vec<String> = Vec::new();
        if cfg.enabled("repos/discover") {
            for b in self.paths("folders").into_iter().chain(self.paths("sweep_roots")) {
                for r in git::find_repos(&b, depth, &ex) {
                    if !swept.contains(&r) {
                        swept.push(r);
                    }
                }
            }
        }
        let sizes: Vec<usize> = std::thread::scope(|s| {
            let jobs: Vec<_> = swept.chunks(swept.len().div_ceil(8).max(1))
                .map(|part| s.spawn(move || part.iter().map(|r| git(r, &["ls-files"]).matches('\n').count()).collect::<Vec<_>>()))
                .collect();
            jobs.into_iter().flat_map(|j| j.join().unwrap_or_default()).collect()
        });
        let max = cfg.int("max_repo_files").max(0) as usize;
        let mut skipped = Vec::new();
        for (r, n) in swept.into_iter().zip(sizes) {
            if n > max {
                skipped.push(json!({"root": r, "files": n}));
            } else {
                out.push(r);
            }
        }
        let mut seen = HashSet::new();
        out.retain(|r| seen.insert(r.clone()));
        *lock(&self.repos.skipped) = skipped;
        *lock(&self.repos.extra) = Some((key, Instant::now(), out.clone()));
        out
    }

    fn project_root(&self, cwd: &str) -> Option<String> {
        git::project_root(cwd, &self.config().home)
    }

    /// (root, remote) of the repos to index: the ones sessions ran in (with repos/discover) and the extra roots,
    /// most used first; clones of one remote fold into the most used root.
    pub fn repos_discover(&self) -> Vec<(String, String)> {
        let cfg = self.config();
        if !cfg.enabled("repos") {
            return Vec::new();
        }
        let mut order: Vec<String> = Vec::new();
        let mut use_: HashMap<String, usize> = HashMap::new();
        if cfg.enabled("repos/discover") {
            let projects: Vec<Option<String>> = self.db.read(|c| {
                c.prepare("SELECT project FROM sessions")?.query_map([], |r| r.get(0))?.collect()
            }).unwrap_or_default();
            for p in projects {
                if let Some(r) = self.project_root(&p.unwrap_or_default()).filter(|r| Path::new(r).exists()) {
                    if !use_.contains_key(&r) {
                        order.push(r.clone());
                    }
                    *use_.entry(r).or_default() += 1;
                }
            }
        }
        for r in self.extra_roots() {
            let r = self.project_root(&r).unwrap_or(r);
            if !use_.contains_key(&r) {
                order.push(r.clone());
                use_.insert(r, 0);
            }
        }
        let mut ranked: Vec<(usize, String)> = order.into_iter().enumerate().collect();
        ranked.sort_by_key(|(i, r)| (std::cmp::Reverse(use_[r]), *i));
        let mut by_remote: Vec<(String, (String, String))> = Vec::new();
        for (_, r) in ranked {
            let rem = git::norm_remote(&git(&r, &["config", "--get", "remote.origin.url"]));
            let remote = if rem.is_empty() { r.clone() } else { rem };
            if !by_remote.iter().any(|(k, _)| *k == remote) {
                by_remote.push((remote.clone(), (r, remote)));
            }
        }
        by_remote.into_iter().map(|(_, v)| v).filter(|(r, _)| cfg.enabled(&format!("repos/{r}"))).collect()
    }

    /// Any session repo root -> canonical repo root (clones fold together), and the aliases (worktrees too) longest
    /// first. Cached for a minute.
    fn canon(&self) -> (HashMap<String, String>, Vec<(String, String)>) {
        if let Some((at, m, al)) = lock(&self.repos.canon).as_ref() {
            if at.elapsed().as_secs() < 60 {
                return (m.clone(), al.clone());
            }
        }
        let canon: HashMap<String, String> = self.repos_discover().into_iter().map(|(r, remote)| (remote, r)).collect();
        let projects: Vec<Option<String>> = self.db.read(|c| {
            c.prepare("SELECT DISTINCT project FROM sessions")?.query_map([], |r| r.get(0))?.collect()
        }).unwrap_or_default();
        let mut m = HashMap::new();
        for p in projects {
            if let Some(r) = self.project_root(&p.unwrap_or_default()) {
                let rem = git::norm_remote(&git(&r, &["config", "--get", "remote.origin.url"]));
                let remote = if rem.is_empty() { r.clone() } else { rem };
                if let Some(c) = canon.get(&remote) {
                    m.insert(r, c.clone());
                }
            }
        }
        let mut al: HashMap<String, String> = m.clone();
        for c in m.values().cloned().collect::<HashSet<_>>() {
            for ln in git(&c, &["worktree", "list", "--porcelain"]).split('\n') {
                if let Some(w) = ln.strip_prefix("worktree ") {
                    al.insert(w.to_string(), c.clone());
                }
            }
        }
        let mut al: Vec<(String, String)> = al.into_iter().collect();
        al.sort_by_key(|(a, _)| std::cmp::Reverse(a.len()));
        *lock(&self.repos.canon) = Some((Instant::now(), m.clone(), al.clone()));
        (m, al)
    }

    pub fn canon_map(&self) -> HashMap<String, String> {
        self.canon().0
    }

    /// Absolute path -> (canonical repo root, repo-relative path). Folds clones, linked worktrees,
    /// `.claude/worktrees` checkouts and deleted sibling clones like `<repo>-feature/` into their repo.
    pub fn locate(&self, path: &str) -> Option<(String, String)> {
        let (_, al) = self.canon();
        locate_in(&al, path)
    }

    /// sid -> where the session belongs. Cached until the sessions or their touched files change (and 2 minutes).
    pub fn session_scope(&self) -> Arc<HashMap<String, SessionScope>> {
        let read = self.db.read(|c| {
            let fp: (i64, f64, i64) = c.query_row(
                "SELECT (SELECT COUNT(*) FROM sessions), coalesce((SELECT MAX(mtime) FROM sessions), 0), (SELECT COUNT(*) FROM session_files)",
                [], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?;
            let files: Vec<(String, String)> = c.prepare("SELECT session_id, path FROM session_files WHERE path!=''")?
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<Result<_, _>>()?;
            let sess: Vec<(String, Option<String>, Option<String>)> = c.prepare("SELECT id, project, project_name FROM sessions")?
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?.collect::<Result<_, _>>()?;
            Ok((fp, files, sess))
        });
        let Ok((fp, file_rows, sess_rows)) = read else { return Arc::new(HashMap::new()) };
        if let Some((f, at, v)) = lock(&self.repos.scope).as_ref() {
            if *f == fp && at.elapsed().as_secs() < 120 {
                return v.clone();
            }
        }
        let (_, al) = self.canon();
        let mut files: HashMap<String, Vec<String>> = HashMap::new();
        for (sid, p) in file_rows {
            files.entry(sid).or_default().push(p);
        }
        let name = |r: &str| file_name(r).to_string();
        let mut out = HashMap::new();
        for (sid, cwd, folder) in sess_rows {
            let cwd = cwd.unwrap_or_default();
            let folder = folder.unwrap_or_default();
            let (mut root, rel_cwd) = if cwd.is_empty() { (None, None) } else {
                match locate_in(&al, &cwd) { Some((r, rel)) => (Some(r), Some(rel)), None => (None, None) }
            };
            let mut dirs = BTreeSet::new();
            if let (Some(r), Some(rel)) = (&root, &rel_cwd) {
                if !rel.is_empty() {
                    dirs.insert(format!("{}/{rel}", name(r)));
                }
            }
            let mut per_repo: Vec<(String, usize)> = Vec::new();
            for f in files.get(&sid).into_iter().flatten() {
                let Some((r, rel)) = locate_in(&al, f) else { continue };
                if rel.is_empty() {
                    continue;
                }
                match per_repo.iter_mut().find(|x| x.0 == r) {
                    Some(x) => x.1 += 1,
                    None => per_repo.push((r.clone(), 1)),
                }
                dirs.insert(match rel.rsplit_once('/') {
                    Some((d, _)) => format!("{}/{d}", name(&r)),
                    None => name(&r),
                });
            }
            if root.is_none() && !per_repo.is_empty() {
                let total: usize = per_repo.iter().map(|x| x.1).sum();
                let best = per_repo.iter().fold(&per_repo[0], |b, x| if x.1 > b.1 { x } else { b });
                if best.1 >= 2 && best.1 as f64 >= 0.5 * total as f64 {
                    root = Some(best.0.clone());
                }
            }
            out.insert(sid, SessionScope {
                project: root.as_deref().map(name).unwrap_or_else(|| folder.clone()),
                root, folder,
                dirs: dirs.into_iter().filter(|d| d.contains('/')).collect(),
            });
        }
        let out = Arc::new(out);
        *lock(&self.repos.scope) = Some((fp, Instant::now(), out.clone()));
        out
    }

    /// Bring every repo (or only `only`: canonical roots) up to date with what is on disk: committed, staged,
    /// modified and untracked files of the main checkout, and the files of each linked worktree that differ from
    /// it. A full sweep runs at most every 30 s unless `force`. Returns how many files and repos changed.
    pub fn repos_sync(&self, force: bool, only: Option<&[String]>, log: &dyn Fn(&str)) -> usize {
        let _g = lock(&self.repos.sync_lock);
        self.repos.sweeping.store(true, Ordering::Relaxed);
        let n = self.sync_inner(force, only, log);
        self.repos.sweeping.store(false, Ordering::Relaxed);
        n
    }

    fn sync_inner(&self, force: bool, only: Option<&[String]>, log: &dyn Fn(&str)) -> usize {
        if !force && only.is_none() {
            if let Some(t) = *lock(&self.repos.last) {
                if t.elapsed().as_secs() < 30 {
                    return 0;
                }
            }
        }
        if only.is_none() {
            *lock(&self.repos.last) = Some(Instant::now());
            self.backfill_session_files();
        }
        let mut touched = self.purge_removed(log);
        let found = self.repos_discover();
        *lock(&self.repos.found) = found.clone();
        if only.is_none() {
            let keep: HashSet<&String> = found.iter().map(|f| &f.0).collect();
            let roots: Vec<String> = self.db.read(|c| c.prepare("SELECT root FROM repos")?.query_map([], |r| r.get(0))?.collect())
                .unwrap_or_default();
            for r in roots {
                if !keep.contains(&r) && (!Path::new(&r).is_dir() || !self.switched_off(&r)) {
                    self.purge(&r);
                    log(&format!("repo {}: removed from the index", file_name(&r)));
                    touched += 1;
                }
            }
        }
        let found = self.busiest_first(found);
        for (root, remote) in found {
            if only.is_some_and(|o| !o.contains(&root)) {
                continue;
            }
            touched += self.service(&root, log);
            match self.sync_repo(&root, &remote, log) {
                Ok(n) => touched += n,
                Err(SyncError::SwitchedOff) => {
                    log(&format!("repo {}: switched off, stopped indexing", file_name(&root)));
                    touched += self.purge_removed(log);
                }
                Err(SyncError::Db(e)) => log(&format!("repo {root}: {e}")),
            }
        }
        let re = std::mem::take(&mut *lock(&self.repos.recheck));
        if !re.is_empty() {
            lock(&self.repos.pending).extend(re);
        }
        touched += self.purge_removed(log);
        self.repos.unsaved.store(false, Ordering::Relaxed);
        touched
    }

    /// The repos agents use most first, so that on a new index what is worth most is searchable soonest.
    fn busiest_first(&self, found: Vec<(String, String)>) -> Vec<(String, String)> {
        let scope = self.session_scope();
        let mut n: HashMap<&str, usize> = HashMap::new();
        for s in scope.values() {
            if let Some(r) = &s.root {
                *n.entry(r.as_str()).or_default() += 1;
            }
        }
        let mut out: Vec<(usize, (String, String))> = found.into_iter().enumerate().collect();
        out.sort_by_key(|(i, (r, _))| (std::cmp::Reverse(n.get(r.as_str()).copied().unwrap_or(0)), *i));
        out.into_iter().map(|x| x.1).collect()
    }

    /// Index the edits waiting in `roots` and on the queue now (a search catching up, or the file watch). Returns
    /// the files indexed again, or 0 when a sweep holds the index (the roots go back on the queue).
    pub fn repos_refresh(&self, roots: &[String]) -> usize {
        let mut want: HashSet<String> = roots.iter().cloned().collect();
        want.extend(self.take_pending());
        if want.is_empty() {
            return 0;
        }
        let guard = if self.repos.sweeping.load(Ordering::Relaxed) { None } else { self.repos.sync_lock.try_lock().ok() };
        let Some(_g) = guard else {
            lock(&self.repos.pending).extend(want);
            return 0;
        };
        self.repos.refreshing.store(true, Ordering::Relaxed);
        let remotes: HashMap<String, String> = self.db.read(|c| {
            c.prepare("SELECT root, coalesce(remote, '') FROM repos")?.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect()
        }).unwrap_or_default();
        let mut n = 0;
        for r in want {
            if let Some(remote) = remotes.get(&r) {
                if !self.switched_off(&r) {
                    n += self.sync_repo(&r, remote, &|_| {}).unwrap_or(0);
                }
            }
        }
        self.repos.refreshing.store(false, Ordering::Relaxed);
        if n > 0 {
            self.repos.unsaved.store(true, Ordering::Relaxed);
        }
        n
    }

    /// Switched off in Sources (the repo, the category, or discovery for a repo not added by hand).
    pub fn switched_off(&self, root: &str) -> bool {
        let cfg = self.config();
        if !(cfg.enabled("repos") && cfg.enabled(&format!("repos/{root}"))) {
            return true;
        }
        let added = self.paths("repos").iter().any(|p| {
            let p = p.to_string_lossy();
            root == p || root.starts_with(&format!("{p}/"))
        });
        !added && !cfg.enabled("repos/discover")
    }

    /// Remove a repo from the index (✕ in Sources); a sweep in progress removes it once it lets go of it.
    pub fn repos_remove(&self, root: &str) {
        lock(&self.repos.removing).insert(root.to_string());
        if let Ok(_g) = self.repos.sync_lock.try_lock() {
            self.purge_removed(&|_| {});
        }
    }

    fn purge_removed(&self, log: &dyn Fn(&str)) -> usize {
        let mut n = 0;
        loop {
            let next = {
                let mut g = lock(&self.repos.removing);
                let r = g.iter().next().cloned();
                if let Some(r) = &r {
                    g.remove(r);
                }
                r
            };
            let Some(r) = next else { break };
            self.purge(&r);
            log(&format!("repo {}: removed from the index", file_name(&r)));
            n += 1;
        }
        n
    }

    /// During a long sweep: sync other repos that changed meanwhile, so their edits do not wait for the sweep.
    fn service(&self, current: &str, log: &dyn Fn(&str)) -> usize {
        if self.repos.servicing.load(Ordering::Relaxed) {
            return 0;
        }
        let roots = self.take_pending();
        if roots.is_empty() {
            return 0;
        }
        self.repos.servicing.store(true, Ordering::Relaxed);
        let mut n = 0;
        if roots.contains(current) && self.config().developer() {
            lock(&self.repos.recheck).insert(current.to_string());
            let state = tree_state(current);
            let sig = git::sig(&state);
            if self.meta(&format!("tree:{current}")).as_deref() != Some(sig.as_str()) {
                n += self.index_checkout(current, "", &state, None, log, "").unwrap_or(0);
                self.set_meta(&format!("tree:{current}"), &sig);
            }
        }
        let remotes: HashMap<String, String> = self.db.read(|c| {
            c.prepare("SELECT root, coalesce(remote, '') FROM repos")?.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect()
        }).unwrap_or_default();
        for r in roots.iter().filter(|r| r.as_str() != current) {
            if let Some(remote) = remotes.get(r) {
                if !self.switched_off(r) {
                    n += self.sync_repo(r, remote, log).unwrap_or(0);
                }
            }
        }
        self.repos.servicing.store(false, Ordering::Relaxed);
        n
    }

    /// Every checkout path that is indexed -> its canonical repo root.
    pub fn checkouts(&self) -> HashMap<String, String> {
        self.db.read(|c| {
            let mut out: HashMap<String, String> = HashMap::new();
            for r in c.prepare("SELECT root FROM repos")?.query_map([], |r| r.get::<_, String>(0))? {
                let r = r?;
                out.insert(r.clone(), r);
            }
            for r in c.prepare("SELECT path, repo FROM worktrees")?.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))? {
                let (p, repo) = r?;
                out.insert(p, repo);
            }
            Ok(out)
        }).unwrap_or_default()
    }

    /// Fetch origin's default branch of every indexed repo (and once a day its protected branches), then record
    /// how far each local one is behind.
    pub fn check_upstream(&self, fetch: bool) -> BTreeMap<String, Value> {
        let roots: Vec<String> = self.db.read(|c| c.prepare("SELECT root FROM repos")?.query_map([], |r| r.get(0))?.collect())
            .unwrap_or_default();
        let mut out = BTreeMap::new();
        for root in roots {
            if !Path::new(&root).is_dir() {
                continue;
            }
            if fetch {
                git::fetch_default(&root);
                self.fetch_protected(&root);
            }
            if let Some(u) = git::upstream(&root) {
                out.insert(root, u);
            }
        }
        *lock(&self.repos.upstream) = out.clone();
        out
    }

    fn fetch_protected(&self, root: &str) -> usize {
        {
            let mut at = lock(&self.repos.protected_at);
            if at.get(root).is_some_and(|t| t.elapsed().as_secs() < 86400) {
                return 0;
            }
            at.insert(root.to_string(), Instant::now());
        }
        let def = git::default_branch(root);
        let branches: Vec<String> = git::protected_branches(root).into_iter().filter(|b| Some(b) != def.as_ref()).collect();
        if branches.is_empty() {
            return 0;
        }
        let specs: Vec<String> = branches.iter().map(|b| format!("+refs/heads/{b}:refs/remotes/origin/{b}")).collect();
        let mut args = vec!["fetch", "--quiet", "--no-tags", "origin"];
        args.extend(specs.iter().map(String::as_str));
        if git::quiet_git(root, &args, std::time::Duration::from_secs(120)).0 { branches.len() } else { 0 }
    }

    fn sync_repo(&self, root: &str, remote: &str, log: &dyn Fn(&str)) -> Result<usize, SyncError> {
        let name = file_name(root).to_string();
        let head = git(root, &["rev-parse", "HEAD"]).trim().to_string();
        let row: Option<Option<String>> = self.db.read(|c| {
            c.query_row("SELECT head FROM repos WHERE root=?", [root], |r| r.get(0)).optional()
        })?;
        if row.is_none() {
            self.db.write().execute("INSERT OR REPLACE INTO repos VALUES(?,?,?,?,?)",
                params![root, name, remote, Option::<String>::None, store::now()])?;
        }
        if row.as_ref().map(|h| h.as_deref() != Some(head.as_str())).unwrap_or(true) {
            self.load_commits(root)?;
        }
        let code = self.config().developer();
        let state = if code { tree_state(root) } else { BTreeMap::new() };
        let mut n = 0;
        let main_sig = git::sig(&state) + if self.images_on() { "+images" } else { "" };
        if code && self.meta(&format!("tree:{root}")).as_deref() != Some(main_sig.as_str()) {
            n += self.index_checkout(root, "", &state, None, log, "")?;
            self.set_meta(&format!("tree:{root}"), &main_sig);
        }
        let mut live: Vec<(String, String)> = Vec::new();
        for (wt, branch) in git::worktree_list(root) {
            if wt == root || !Path::new(&wt).is_dir() {
                continue;
            }
            n += self.service(root, log);
            live.push((wt.clone(), branch.clone()));
            if !code {
                continue;
            }
            let ws = tree_state(&wt);
            let sig = git::sig(&ws) + &main_sig;
            if self.meta(&format!("tree:{wt}")).as_deref() != Some(sig.as_str()) {
                n += self.index_checkout(root, &wt, &ws, Some(&state), log, &branch)?;
                self.set_meta(&format!("tree:{wt}"), &sig);
            }
        }
        let known: Vec<String> = self.db.read(|c| {
            c.prepare("SELECT path FROM worktrees WHERE repo=?")?.query_map([root], |r| r.get::<_, String>(0))?.collect()
        })?;
        let gone: Vec<String> = known.into_iter().filter(|p| !live.iter().any(|(w, _)| w == p)).collect();
        for wt in &gone {
            let paths: Vec<String> = self.db.read(|c| {
                c.prepare("SELECT path FROM checkout_files WHERE repo=? AND wt=?")?.query_map([root, wt.as_str()], |r| r.get(0))?.collect()
            })?;
            for p in paths {
                self.drop_file(root, &p, wt)?;
            }
            self.db.write().execute("DELETE FROM worktrees WHERE path=?", [wt])?;
            n += 1;
        }
        self.db.tx(|t| {
            for (w, b) in &live {
                t.execute("INSERT OR REPLACE INTO worktrees VALUES(?,?,?)", params![w, root, b])?;
            }
            t.execute("INSERT OR REPLACE INTO repos VALUES(?,?,?,?,?)", params![root, name, remote, head, store::now()])?;
            Ok(())
        })?;
        if n > 0 {
            lock(&self.repos.cache).clear();
            lock(&self.repos.team).clear();
            lock(&self.repos.commit_events).insert(root.to_string());
        }
        Ok(n)
    }

    /// Chunks this Mac embeds per second during an index: the last big index measured it, else EMBED_RATE.
    pub fn embed_rate(&self) -> f64 {
        self.meta(&format!("embed_rate:{EMBED_MODEL}")).and_then(|v| v.parse().ok()).unwrap_or(EMBED_RATE)
    }

    /// (path, text) -> the stored vector of each chunk of these files that this repo's index has already.
    fn known_vectors(&self, root: &str, paths: &[String]) -> HashMap<(String, String), Vec<u8>> {
        let mut out = HashMap::new();
        for part in paths.chunks(900) {
            let sql = format!("SELECT path, text, vec FROM code_chunks WHERE repo=? AND path IN ({})", db::marks(part.len()));
            let _ = self.db.read(|c| {
                let mut st = c.prepare(&sql)?;
                let args: Vec<&dyn rusqlite::ToSql> = std::iter::once(&root as &dyn rusqlite::ToSql)
                    .chain(part.iter().map(|p| p as &dyn rusqlite::ToSql)).collect();
                let mut rows = st.query(args.as_slice())?;
                while let Some(r) = rows.next()? {
                    if let (Ok(p), Ok(t), Ok(v)) = (r.get::<_, String>(0), r.get::<_, String>(1), r.get::<_, Vec<u8>>(2)) {
                        out.insert((p, t), v);
                    }
                }
                Ok(())
            });
        }
        out
    }

    /// What indexing the main checkout of `root` still has to do, from file sizes:
    /// {files (tracked), indexed (files indexed with their current content), chunks (still to embed)}.
    pub fn estimate_chunks(&self, root: &str) -> Value {
        let state = tree_state(root);
        let have: HashMap<String, String> = self.db.read(|c| {
            c.prepare("SELECT path, blob FROM checkout_files WHERE repo=? AND wt=''")?.query_map([root], |r| Ok((r.get(0)?, r.get(1)?)))?.collect()
        }).unwrap_or_default();
        let secrets = self.config().strs("exclude_files");
        let mut chunks = 0u64;
        for (path, blob) in &state {
            let name = file_name(path);
            let ext = git::suffix(path);
            if have.get(path) == Some(blob) || git::SKIP_EXT.contains(&ext.as_str()) || git::SKIP_NAMES.contains(&name)
                || name.contains(".min.") || git::secret(name, &secrets) {
                continue;
            }
            let f = Path::new(root).join(path);
            let Ok(size) = std::fs::metadata(&f).map(|m| m.len()) else { continue };
            if size == 0 || !(size <= git::MAX_BYTES as u64 || (size <= git::MAX_SOURCE_BYTES as u64 && git::SOURCE_EXT.contains(&ext.as_str()))) {
                continue;
            }
            let mut head = vec![0u8; 2000];
            let n = std::fs::File::open(&f).and_then(|mut h| std::io::Read::read(&mut h, &mut head)).unwrap_or(0);
            if head[..n].contains(&0) {
                continue;
            }
            chunks += size.div_ceil(CHARS_PER_CHUNK);
        }
        let indexed = state.iter().filter(|(p, b)| have.get(*p) == Some(*b)).count();
        json!({"files": state.len(), "indexed": indexed, "chunks": chunks})
    }

    /// Index one checkout. For a linked worktree (`wt`), only the files that differ from the main checkout (`base`).
    fn index_checkout(&self, root: &str, wt: &str, state: &BTreeMap<String, String>, base: Option<&BTreeMap<String, String>>,
                      log: &dyn Fn(&str), branch: &str) -> Result<usize, SyncError> {
        let imgs = self.images_on();
        let is_image = |p: &str| imgs && git::READ_IMAGE_EXT.contains(&git::suffix(p).as_str());
        let mut target: BTreeMap<String, String> = match base {
            None => state.clone(),
            Some(b) => state.iter().filter(|(p, h)| b.get(*p) != Some(*h)).map(|(p, h)| (p.clone(), h.clone())).collect(),
        };
        if !imgs {
            target.retain(|p, _| !git::READ_IMAGE_EXT.contains(&git::suffix(p).as_str()));
        }
        let have: HashMap<String, String> = self.db.read(|c| {
            c.prepare("SELECT path, blob FROM checkout_files WHERE repo=? AND wt=?")?
                .query_map([root, wt], |r| Ok((r.get(0)?, r.get(1)?)))?.collect()
        })?;
        let mut changed: Vec<String> = have.iter().filter(|(p, b)| target.get(*p) != Some(*b)).map(|(p, _)| p.clone()).collect();
        changed.sort();
        let prior = if !changed.is_empty() && changed.len() <= 500 { self.known_vectors(root, &changed) } else { HashMap::new() };
        for p in &changed {
            self.drop_file(root, p, wt)?;
        }
        let checkout = if wt.is_empty() { root.to_string() } else { wt.to_string() };
        let me = (git(&checkout, &["config", "user.name"]).trim().to_string(), git(&checkout, &["config", "user.email"]).trim().to_string());
        let secrets = self.config().strs("exclude_files");
        let label = format!("{}{}", file_name(root),
            if wt.is_empty() { String::new() } else { format!(" @ {}", if branch.is_empty() { file_name(wt) } else { branch }) });
        let todo: Vec<(String, String)> = target.iter().filter(|(p, b)| have.get(*p) != Some(*b)).filter(|(p, _)| {
            let name = file_name(p);
            is_image(p) || !(git::SKIP_EXT.contains(&git::suffix(p).as_str()) || git::SKIP_NAMES.contains(&name)
                || name.contains(".min.") || git::secret(name, &secrets))
        }).map(|(p, b)| (p.clone(), b.clone())).collect();
        let shadow = if todo.len() >= SHADOW_MIN_FILES { git::blame_shadow(&self.data_dir(), root, &checkout) } else { None };

        let prepare = |path: &str, blob: &str| -> Option<Prepared> {
            let full = Path::new(&checkout).join(path);
            if is_image(path) {
                let size = std::fs::metadata(&full).ok()?.len();
                if size == 0 || size > git::IMAGE_MAX_BYTES {
                    return None;
                }
                let last = git(&checkout, &["log", "-1", "--format=%an <%ae>%x00%at", "--", path]);
                let parts: Vec<&str> = last.trim().split('\0').collect();
                let mut bl = HashMap::new();
                if parts.len() == 2 {
                    bl.insert(1, (parts[0].to_string(), parts[1].parse().unwrap_or(0)));
                }
                return Some(Prepared { path: path.into(), blob: blob.into(), chunks: vec![(1, 1, format!("Image: {path}"))], blame: bl, image: true });
            }
            let raw = std::fs::read(&full).ok()?;
            let ws = |b: &u8| matches!(b, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c);
            if raw[..raw.len().min(2000)].contains(&0) || raw.iter().all(ws) || (raw.len() > git::MAX_BYTES && !git::written_source(&raw, path)) {
                return None;
            }
            let chunks = git::chunk_code(&String::from_utf8_lossy(&raw));
            if chunks.is_empty() {
                return None;
            }
            let blame = git::blame_lines(&checkout, path, Some(&me), shadow.as_ref());
            Some(Prepared { path: path.into(), blob: blob.into(), chunks, blame, image: false })
        };

        let big = todo.len() >= PROGRESS_MIN_FILES;
        let t0 = Instant::now();
        let mut n_new = 0usize;
        let mut embedded = 0usize;
        let mut done = 0usize;
        let mut group: Vec<Prepared> = Vec::new();
        let result = (|| -> Result<(), SyncError> {
            for batch in todo.chunks(INDEX_AHEAD) {
                if self.switched_off(root) || lock(&self.repos.removing).contains(root) {
                    return Err(SyncError::SwitchedOff);
                }
                let prepared: Vec<Option<Prepared>> = std::thread::scope(|s| {
                    let per = batch.len().div_ceil(INDEX_WORKERS).max(1);
                    let jobs: Vec<_> = batch.chunks(per)
                        .map(|part| s.spawn(|| part.iter().map(|(p, b)| prepare(p, b)).collect::<Vec<_>>()))
                        .collect();
                    jobs.into_iter().flat_map(|j| j.join().unwrap_or_default()).collect()
                });
                self.service(root, log);
                for r in prepared {
                    done += 1;
                    if big {
                        let left = t0.elapsed().as_secs_f64() / done as f64 * (todo.len() - done) as f64;
                        *lock(&self.repos.progress) = Some(json!({"name": label, "root": root, "done": done,
                            "total": todo.len(), "seconds_left": left.round() as i64}));
                    }
                    if let Some(r) = r {
                        group.push(r);
                        if group.iter().map(|g| g.chunks.len()).sum::<usize>() >= EMBED_GROUP {
                            let g = std::mem::take(&mut group);
                            let (e, k) = self.flush(root, wt, &checkout, &me, g, &prior)?;
                            embedded += e;
                            n_new += k;
                            if n_new % 100 < k {
                                log(&format!("  {label}: {n_new} files"));
                            }
                        }
                    }
                }
            }
            if !group.is_empty() {
                let (e, k) = self.flush(root, wt, &checkout, &me, std::mem::take(&mut group), &prior)?;
                embedded += e;
                n_new += k;
            }
            Ok(())
        })();
        if big {
            *lock(&self.repos.progress) = None;
        }
        result?;
        if embedded >= RATE_MIN_CHUNKS {
            let rate = (embedded as f64 / t0.elapsed().as_secs_f64() * 10.0).round() / 10.0;
            self.set_meta(&format!("embed_rate:{EMBED_MODEL}"), &rate.to_string());
        }
        if n_new > 0 {
            log(&format!("repo {label}: {n_new} files updated"));
        }
        Ok(n_new)
    }

    /// Embed one group of prepared files (vectors of identical chunks are copied, not embedded), then write their
    /// rows in one transaction. Returns (chunks embedded, files written).
    fn flush(&self, root: &str, wt: &str, checkout: &str, me: &(String, String), group: Vec<Prepared>,
             prior: &HashMap<(String, String), Vec<u8>>) -> Result<(usize, usize), SyncError> {
        let texts: Vec<String> = group.iter().flat_map(|g| g.chunks.iter().map(|c| c.2.clone())).collect();
        let titles: Vec<String> = group.iter().flat_map(|g| g.chunks.iter().map(|_| g.path.clone())).collect();
        let images: Vec<bool> = group.iter().flat_map(|g| g.chunks.iter().map(|_| g.image)).collect();
        let paths: Vec<String> = group.iter().map(|g| g.path.clone()).collect();
        let mut known = prior.clone();
        known.extend(self.known_vectors(root, &paths));
        let mut vecs: Vec<Vec<f32>> = vec![Vec::new(); texts.len()];
        let mut todo: Vec<usize> = Vec::new();
        for i in 0..texts.len() {
            match known.get(&(titles[i].clone(), texts[i].clone())) {
                Some(v) if !images[i] && v.len() == 2 * self.code_vecs.dim => vecs[i] = vectors::from_blob(v),
                _ => todo.push(i),
            }
        }
        let embedded = todo.len();
        self.repos.reused.fetch_add(texts.len() - todo.len(), Ordering::Relaxed);
        todo.retain(|&i| {
            if !images[i] {
                return true;
            }
            match self.embedder.embed_image(&Path::new(checkout).join(&titles[i])) {
                Some(v) => {
                    vecs[i] = vectors::unit(&v);
                    false
                }
                None => true,
            }
        });
        if !todo.is_empty() {
            let t: Vec<String> = todo.iter().map(|&i| texts[i].clone()).collect();
            let ti: Vec<String> = todo.iter().map(|&i| titles[i].clone()).collect();
            for (&i, v) in todo.iter().zip(self.embed_docs(&t, Some(&ti))) {
                vecs[i] = v;
            }
        }
        let now = store::now() as i64;
        let mine = format!("{} <{}>", me.0, me.1);
        let mut added: Vec<(i64, Vec<f32>)> = Vec::new();
        let mut k = 0;
        self.db.tx(|t| {
            let mut ins = t.prepare_cached("INSERT INTO code_chunks(repo,path,dir,start,end,text,vec,authors,last_ts,wt) VALUES(?,?,?,?,?,?,?,?,?,?)")?;
            for g in &group {
                let d = match g.path.split_once('/') { Some((first, _)) => first.to_string(), None => ".".into() };
                for (s, e, text) in &g.chunks {
                    let mut au: Vec<(String, i64)> = Vec::new();
                    let mut ts = 0i64;
                    for i in *s..=*e {
                        let (who, when) = match g.blame.get(&(i as usize)) {
                            Some((a, t)) => (a.clone(), *t),
                            None => (mine.clone(), now),
                        };
                        match au.iter_mut().find(|x| x.0 == who) {
                            Some(x) => x.1 += 1,
                            None => au.push((who, 1)),
                        }
                        ts = ts.max(when);
                    }
                    let authors = query::py_json_counter(&au);
                    let v = &vecs[k];
                    ins.execute(params![root, g.path, d, s, e, text, vectors::to_blob(v), authors, ts, wt])?;
                    added.push((t.last_insert_rowid(), v.clone()));
                    k += 1;
                }
                t.execute("INSERT OR REPLACE INTO checkout_files VALUES(?,?,?,?)", params![root, wt, g.path, g.blob])?;
            }
            Ok(())
        })?;
        self.code_vecs.add(&added);
        self.changed(root);
        Ok((embedded, group.len()))
    }

    /// The code of `root` changed: its map layout is due.
    fn changed(&self, root: &str) {
        if let Some(d) = lock(&self.repos.layout_due).as_mut() {
            d.insert(root.to_string());
        }
    }

    /// Drop a repo from the index: its chunks, files, commits, worktrees and tree signatures, and its blame shadow.
    pub fn purge(&self, root: &str) {
        self.changed(root);
        let ids: Vec<i64> = self.db.read(|c| c.prepare("SELECT id FROM code_chunks WHERE repo=?")?.query_map([root], |r| r.get(0))?.collect())
            .unwrap_or_default();
        self.code_vecs.remove_many(&ids);
        let _ = self.db.tx(|t| {
            for (tbl, col) in [("code_chunks", "repo"), ("checkout_files", "repo"), ("commits", "repo"), ("worktrees", "repo"), ("repos", "root")] {
                t.execute(&format!("DELETE FROM {tbl} WHERE {col}=?"), [root])?;
            }
            t.execute("DELETE FROM meta WHERE key LIKE ?", [format!("tree:{root}%")])?;
            Ok(())
        });
        lock(&self.repos.cache).clear();
        lock(&self.repos.team).clear();
        let _ = std::fs::remove_dir_all(git::shadow_dir(&self.data_dir(), root));
    }

    fn drop_file(&self, root: &str, path: &str, wt: &str) -> rusqlite::Result<()> {
        self.changed(root);
        let ids: Vec<i64> = self.db.read(|c| {
            c.prepare("SELECT id FROM code_chunks WHERE repo=? AND path=? AND wt=?")?.query_map([root, path, wt], |r| r.get(0))?.collect()
        })?;
        self.code_vecs.remove_many(&ids);
        self.db.tx(|t| {
            t.execute("DELETE FROM code_chunks WHERE repo=? AND path=? AND wt=?", [root, path, wt])?;
            t.execute("DELETE FROM checkout_files WHERE repo=? AND wt=? AND path=?", [root, wt, path])?;
            Ok(())
        })
    }

    fn load_commits(&self, root: &str) -> rusqlite::Result<()> {
        let out = git(root, &["log", "--format=%H%x1f%at%x1f%ae%x1f%s", "-n", "5000"]);
        let rows: Vec<Vec<&str>> = out.trim().split('\n').filter(|l| !l.is_empty()).map(|l| l.split('\x1f').collect()).collect();
        self.db.tx(|t| {
            let mut st = t.prepare_cached("INSERT OR REPLACE INTO commits VALUES(?,?,?,?,?)")?;
            for r in rows.iter().filter(|r| r.len() == 4) {
                st.execute(params![root, r[0], r[1].parse::<i64>().unwrap_or(0), r[2], r[3]])?;
            }
            Ok(())
        })?;
        lock(&self.repos.commit_events).insert(root.to_string());
        Ok(())
    }

    /// Lay out the code maps: one per repo and one joint map across repos. A layout is fitted again when it is new
    /// or the main checkout grew or shrank by more than 15%; else new chunks are placed next to their nearest
    /// neighbours. Returns whether any point moved.
    pub fn repos_reproject(&self, force: bool) -> bool {
        let Ok((mut roots, multi)) = self.db.read(|c| {
            let roots: Vec<String> = c.prepare("SELECT root FROM repos")?.query_map([], |r| r.get(0))?.collect::<Result<_, _>>()?;
            let multi: i64 = c.query_row("SELECT COUNT(DISTINCT repo) FROM code_chunks", [], |r| r.get(0))?;
            Ok((roots, multi > 1))
        }) else { return false };
        let due = std::mem::replace(&mut *lock(&self.repos.layout_due), Some(HashSet::new()));
        if !force {
            if let Some(d) = &due {
                roots.retain(|r| d.contains(r));
                if roots.is_empty() && d.is_empty() {
                    return false;
                }
            }
        }
        let mut changed = false;
        for root in &roots {
            changed |= self.layout(root, "WHERE repo=?", Some(root), ["x", "y", "z"], force);
        }
        if multi {
            changed |= self.layout(ALL, "", None, ["gx", "gy", "gz"], force);
        }
        changed
    }

    /// Some code chunks have no map position yet.
    pub fn repos_needs_layout(&self) -> bool {
        self.db.read(|c| {
            let multi: i64 = c.query_row("SELECT COUNT(DISTINCT repo) FROM code_chunks", [], |r| r.get(0))?;
            let a = c.query_row("SELECT 1 FROM code_chunks WHERE x IS NULL LIMIT 1", [], |_| Ok(())).optional()?.is_some();
            let b = multi > 1 && c.query_row("SELECT 1 FROM code_chunks WHERE gx IS NULL LIMIT 1", [], |_| Ok(())).optional()?.is_some();
            Ok(a || b)
        }).unwrap_or(false)
    }

    /// UMAP over the main checkout's code only; worktree copies are near-copies that would dissolve the clusters,
    /// so they are placed next to their nearest main-checkout code.
    fn layout(&self, key: &str, where_: &str, arg: Option<&String>, cols: [&str; 3], force: bool) -> bool {
        let [cx, cy, cz] = cols;
        let version = format!("{LAYOUT_VERSION}+mainfit");
        type Row = (i64, Option<f64>, Option<f64>, Option<f64>, String);
        let rows: Vec<Row> = self.db.read(|c| {
            let mut st = c.prepare(&format!("SELECT id, {cx}, {cy}, {cz}, coalesce(wt, '') FROM code_chunks {where_}"))?;
            let f = |r: &rusqlite::Row| -> rusqlite::Result<Row> { Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)) };
            match arg {
                Some(a) => st.query_map([a], f)?.collect(),
                None => st.query_map([], f)?.collect(),
            }
        }).unwrap_or_default();
        let fit_n: i64 = self.meta(&format!("code_fit_n:{key}")).and_then(|v| v.parse().ok()).unwrap_or(0);
        let ok = self.meta(&format!("code_layout:{key}")).as_deref() == Some(version.as_str());
        let vecs = self.code_vecs.take(&rows.iter().map(|r| r.0).collect::<Vec<_>>());
        let (rows, x): (Vec<Row>, Vec<Vec<f32>>) = rows.into_iter().zip(vecs).filter_map(|(r, v)| v.map(|v| (r, vectors::unit(&v)))).unzip();
        if rows.len() < 4 {
            return false;
        }
        let have: Vec<bool> = rows.iter().map(|r| r.1.is_some()).collect();
        let mut main: Vec<bool> = rows.iter().map(|r| r.4.is_empty()).collect();
        if main.iter().filter(|m| **m).count() < 4 {
            main.iter_mut().for_each(|m| *m = true);
        }
        let mut p: Vec<layout::P3> = rows.iter().map(|r| [r.1.unwrap_or(0.0) as f32, r.2.unwrap_or(0.0) as f32, r.3.unwrap_or(0.0) as f32]).collect();
        let n_main = main.iter().filter(|m| **m).count() as i64;
        let pick = |m: &[bool], want: bool| -> Vec<usize> { (0..m.len()).filter(|&i| m[i] == want).collect() };
        let sub = |idx: &[usize]| -> Vec<Vec<f32>> { idx.iter().map(|&i| x[i].clone()).collect() };
        let todo: Vec<usize>;
        if force || !ok || (n_main - fit_n).abs() as f64 > (200f64).max(0.15 * fit_n as f64) {
            let mi = pick(&main, true);
            let oi = pick(&main, false);
            let xm = sub(&mi);
            let pm = layout::fit3d(&xm, 20, 0.08);
            for (j, &i) in mi.iter().enumerate() {
                p[i] = pm[j];
            }
            if !oi.is_empty() {
                for (j, q) in layout::place_new(&xm, &pm, &sub(&oi), 3, 0.01).into_iter().enumerate() {
                    p[oi[j]] = q;
                }
            }
            todo = (0..rows.len()).collect();
            self.set_meta(&format!("code_fit_n:{key}"), &n_main.to_string());
            self.set_meta(&format!("code_layout:{key}"), &version);
        } else if have.iter().any(|h| !h) {
            let anchor: Vec<usize> = (0..rows.len()).filter(|&i| have[i] && main[i]).collect();
            todo = pick(&have, false);
            let xn = sub(&todo);
            let placed = if !anchor.is_empty() {
                let pa: Vec<layout::P3> = anchor.iter().map(|&i| p[i]).collect();
                layout::place_new(&sub(&anchor), &pa, &xn, 3, 0.01)
            } else {
                let hv = pick(&have, true);
                let pa: Vec<layout::P3> = hv.iter().map(|&i| p[i]).collect();
                layout::place_new(&sub(&hv), &pa, &xn, 4, 0.03)
            };
            for (j, q) in placed.into_iter().enumerate() {
                p[todo[j]] = q;
            }
        } else {
            return false;
        }
        let _ = self.db.tx(|t| {
            let mut st = t.prepare(&format!("UPDATE code_chunks SET {cx}=?, {cy}=?, {cz}=? WHERE id=?"))?;
            for &i in &todo {
                st.execute(params![p[i][0] as f64, p[i][1] as f64, p[i][2] as f64, rows[i].0])?;
            }
            Ok(())
        });
        lock(&self.repos.cache).clear();
        true
    }
}

fn locate_in(al: &[(String, String)], path: &str) -> Option<(String, String)> {
    static WT: std::sync::LazyLock<regex::Regex> =
        std::sync::LazyLock::new(|| regex::Regex::new(r"/\.claude/worktrees/[^/]+(/|$)").expect("regex"));
    let p = WT.replace_all(path.trim_end_matches('/'), "$1").trim_end_matches('/').to_string();
    for (a, c) in al {
        if p == *a {
            return Some((c.clone(), String::new()));
        }
        if let Some(rest) = p.strip_prefix(&format!("{a}/")) {
            return Some((c.clone(), rest.to_string()));
        }
    }
    for (_, c) in al {
        if let Some(rest) = p.strip_prefix(&format!("{c}-")) {
            let (head, tail) = rest.split_once('/').unwrap_or((rest, ""));
            if !head.is_empty() {
                return Some((c.clone(), tail.to_string()));
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Config, Embed};
    use std::process::Command;
    use std::sync::Arc;

    struct Zero;

    impl Embed for Zero {
        fn embed(&self, texts: &[&str], _query: bool) -> Vec<Vec<f32>> {
            texts.iter().map(|t| {
                let mut v = vec![0f32; 8];
                v[t.len() % 8] = 1.0;
                v
            }).collect()
        }
        fn dim(&self) -> usize {
            8
        }
    }

    fn sh(dir: &Path, args: &[&str]) {
        let ok = Command::new("git").arg("-C").arg(dir).args(args)
            .env("GIT_AUTHOR_DATE", "2026-01-02T03:04:05Z").env("GIT_COMMITTER_DATE", "2026-01-02T03:04:05Z")
            .output().map(|o| o.status.success()).unwrap_or(false);
        assert!(ok, "git {args:?}");
    }

    fn index_of(root: &str, data: &Path) -> Index {
        let cfg = Config::new(json!({"mode": "developer", "repos": [root], "disabled": ["repos/discover"], "folders": [],
            "sweep_roots": []}), PathBuf::from("/nonexistent-home"));
        Index::open(&data.join("index.db"), Arc::new(Zero), cfg).expect("index")
    }

    #[test]
    fn indexes_a_repo_with_blame_and_drops_deleted_files() {
        let base = std::env::temp_dir().join(format!("repos-test-{}", std::process::id()));
        let repo = base.join("app");
        std::fs::create_dir_all(repo.join("src")).expect("mkdir");
        sh(&repo, &["init", "-q"]);
        sh(&repo, &["config", "user.name", "Ann Example"]);
        sh(&repo, &["config", "user.email", "ann@example.org"]);
        std::fs::write(repo.join("src/main.py"), "def main():\n    return 1\n").expect("write");
        std::fs::write(repo.join("notes.md"), "# Notes\n").expect("write");
        std::fs::write(repo.join("secret.key"), "x").expect("write");
        sh(&repo, &["add", "."]);
        sh(&repo, &["commit", "-q", "-m", "first"]);
        std::fs::write(repo.join("new.py"), "x = 1\n").expect("write");
        let root = std::fs::canonicalize(&repo).expect("canon").to_string_lossy().into_owned();
        let ix = index_of(&root, &base);
        assert!(ix.repos_sync(true, None, &|_| {}) >= 3);
        let rows: Vec<(String, String, i64)> = ix.db.read(|c| {
            c.prepare("SELECT path, authors, last_ts FROM code_chunks ORDER BY path")?
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?.collect()
        }).expect("rows");
        assert_eq!(rows.iter().map(|r| r.0.as_str()).collect::<Vec<_>>(), vec!["new.py", "notes.md", "src/main.py"]);
        assert_eq!(rows[2].1, r#"{"Ann Example <ann@example.org>": 3}"#, "the empty last line goes to the user, as in 0.21");
        assert!(rows[2].2 > 1767323045);
        assert_eq!(ix.code_vecs.len(), 3);
        let repos = ix.api_repos();
        assert_eq!(repos[0]["chunks"], 3);
        std::fs::remove_file(repo.join("notes.md")).expect("rm");
        ix.repos_refresh(&[root.clone()]);
        assert_eq!(ix.code_vecs.len(), 2);
        let people = ix.api_repo_people(&root);
        assert_eq!(people[0]["author"], "Ann Example <ann@example.org>");
        let _ = std::fs::remove_dir_all(base);
    }

    /// Answers the repo queries over a copy of a 0.21 index ($PARITY_DB), as queries21.py does, into $PARITY_OUT.
    #[test]
    #[ignore]
    fn parity_queries() {
        let (Ok(dbp), Ok(out), Ok(root)) = (std::env::var("PARITY_DB"), std::env::var("PARITY_OUT"), std::env::var("PARITY_REPO")) else { return };
        struct NoModel;
        impl Embed for NoModel {
            fn embed(&self, texts: &[&str], _q: bool) -> Vec<Vec<f32>> {
                vec![vec![0.0; 768]; texts.len()]
            }
            fn dim(&self) -> usize {
                768
            }
        }
        let cfg = Config::new(json!({"mode": "developer", "repos": [root], "disabled": ["repos/discover"], "folders": [],
            "sweep_roots": []}), store::home());
        let ix = Index::open(Path::new(&dbp), Arc::new(NoModel), cfg).expect("index");
        let name = "pensieve";
        let cids: Vec<i64> = ix.db.read(|c| c.prepare("SELECT id FROM code_chunks WHERE wt='' ORDER BY id LIMIT 3000")?
            .query_map([], |r| r.get(0))?.collect()).expect("ids");
        let cids: Vec<i64> = cids.into_iter().step_by(500).collect();
        let qid = cids[2];
        let vec = ix.code_vecs.take(&[qid])[0].clone().expect("vec");
        let chunks: serde_json::Map<String, Value> = cids.iter().map(|c| (c.to_string(), ix.api_code(*c).unwrap_or(Value::Null))).collect();
        let res = json!({
            "list": ix.api_repos(), "owners": ix.api_repo_owners(name), "people": ix.api_repo_people(name),
            "gaps": ix.knowledge_gaps(name, 180, 150.0), "health": ix.area_health(name, 90, 180, 150.0),
            "overlaps": ix.api_repo_overlaps(name, 15), "team": ix.repo_team(name, 90), "chunks": chunks,
            "experts_vec": ix.experts(None, Some(&vec), Some(name), 40, None),
            "experts_path": ix.experts(Some("how does indexer.py work"), None, Some(name), 40, None),
            "territory": ix.territory(name, None, 8), "qid": qid,
        });
        std::fs::write(out, serde_json::to_string(&res).expect("json")).expect("write");
    }

    /// Lays out the code map of a copy of a 0.21 index ($PARITY_DB) and checks every chunk got a point.
    #[test]
    #[ignore]
    fn parity_layout() {
        let Ok(dbp) = std::env::var("PARITY_DB") else { return };
        let ix = Index::open(Path::new(&dbp), Arc::new(crate::testing::Words(768)), Config::new(json!({"mode": "developer"}), store::home()))
            .expect("index");
        let t = Instant::now();
        assert!(ix.repos_reproject(true));
        eprintln!("layout in {:?}", t.elapsed());
        assert!(!ix.repos_needs_layout());
        let pts = ix.api_repo_points("pensieve").expect("points");
        let n = pts["points"].as_array().map(|a| a.len()).unwrap_or(0);
        eprintln!("{n} points; first {}", pts["points"][0]);
        assert!(n > 0);
        assert!(!ix.repos_reproject(false));
    }

    /// Answers /api/repos, /api/repo-check and /api/sessioncode over a copy of the live index ($PARITY_DB) with the
    /// live settings ($PARITY_SETTINGS), for the cwds and session ids in $PARITY_ASK ({cwds, sids}), into $PARITY_OUT.
    #[test]
    #[ignore]
    fn parity_live() {
        let (Ok(dbp), Ok(sp), Ok(ask), Ok(out)) = (std::env::var("PARITY_DB"), std::env::var("PARITY_SETTINGS"),
            std::env::var("PARITY_ASK"), std::env::var("PARITY_OUT")) else { return };
        let settings: Value = serde_json::from_str(&std::fs::read_to_string(sp).expect("settings")).expect("json");
        let ask: Value = serde_json::from_str(&std::fs::read_to_string(ask).expect("ask")).expect("json");
        let ix = Index::open(Path::new(&dbp), Arc::new(Zero), Config::new(settings["settings"].clone(), store::home())).expect("index");
        let cwds: serde_json::Map<String, Value> = ask["cwds"].as_array().into_iter().flatten().filter_map(Value::as_str)
            .map(|c| (c.to_string(), ix.api_repo_check(c))).collect();
        let sids: serde_json::Map<String, Value> = ask["sids"].as_array().into_iter().flatten().filter_map(Value::as_str)
            .map(|s| (s.to_string(), ix.api_sessioncode(s).unwrap_or(Value::Null))).collect();
        let res = json!({"repos": ix.api_repos(), "check": cwds, "sessioncode": sids});
        std::fs::write(out, serde_json::to_string(&res).expect("json")).expect("write");
    }

    /// Indexes $PARITY_REPO into a fresh index and writes its rows to $PARITY_OUT, for the comparison with 0.21.
    #[test]
    #[ignore]
    fn parity_dump() {
        let (Ok(root), Ok(out)) = (std::env::var("PARITY_REPO"), std::env::var("PARITY_OUT")) else { return };
        let data = std::env::temp_dir().join(format!("repos-parity-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&data);
        let ix = index_of(&root, &data);
        let t = Instant::now();
        ix.repos_sync(true, None, &|m| eprintln!("{m}"));
        eprintln!("indexed in {:?}", t.elapsed());
        let rows: Vec<Value> = ix.db.read(|c| {
            c.prepare("SELECT repo, path, dir, start, end, text, authors, last_ts, wt FROM code_chunks ORDER BY path, start, wt")?
                .query_map([], |r| Ok(json!([r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?,
                    r.get::<_, i64>(3)?, r.get::<_, i64>(4)?, r.get::<_, String>(5)?, r.get::<_, String>(6)?,
                    r.get::<_, i64>(7)?, r.get::<_, String>(8)?])))?.collect()
        }).expect("rows");
        std::fs::write(out, serde_json::to_string(&rows).expect("json")).expect("write");
        let _ = std::fs::remove_dir_all(data);
    }
}

#[derive(Debug)]
enum SyncError {
    SwitchedOff,
    Db(rusqlite::Error),
}

impl From<rusqlite::Error> for SyncError {
    fn from(e: rusqlite::Error) -> Self {
        SyncError::Db(e)
    }
}
