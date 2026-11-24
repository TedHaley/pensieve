//! The index of sessions, files and code (crates/index) with EmbeddingGemma 2 (crates/embed). The daemon opens one
//! `Engine` at start. Its indexing runs on background threads, and a search never waits for them.
//!
//! The file watch and the transcript watch wake the loops. A loop that has no event sleeps for IDLE_WAIT, and a full
//! sweep runs every SWEEP_SECONDS as a safety net (GOALS.md, principle 3).

use std::path::Path;
use std::sync::atomic::Ordering;
use std::sync::{Arc, Condvar, Mutex, OnceLock};
use std::time::{Duration, Instant};

use embed::cache::EmbedCache;
use embed::{Embedder, Kind, MODEL, Part};
use index::{Config, Index};
use serde_json::{Value, json};

use crate::settings;

/// The safety sweep behind the file and transcript watches. It also finds new repos and folders.
const SWEEP_SECONDS: f64 = 6.0 * 3600.0;
/// A loop with no event wakes this often at most.
const IDLE_WAIT: Duration = Duration::from_secs(60);
/// A backlog of sessions is indexed in turns this long.
const SESSION_BATCH_SECONDS: f64 = 30.0;
const SETTLE_SECONDS: f64 = 1.0;
const ACTIVE_SECONDS: f64 = 1.5;
/// The folder index is built again at most this often when nothing new was indexed.
const FOLDER_WAIT: Duration = Duration::from_secs(600);
/// The thoughts are built again when no session changed for THOUGHT_QUIET, and THOUGHT_CAP after the first change
/// at the latest. A session in use changes every few seconds, and each build reads every exchange.
const THOUGHT_QUIET: Duration = Duration::from_secs(20);
const THOUGHT_CAP: Duration = Duration::from_secs(300);
const THOUGHT_BATCH_SECONDS: f64 = 30.0;

/// EmbeddingGemma 2 behind the index's `Embed`. The model loads at its first use, and its text part downloads when
/// it is missing. Document vectors go through the embedding cache.
pub struct Gemma {
    model: OnceLock<Result<Arc<Embedder>, String>>,
    loading: Mutex<()>,
    cache: Option<EmbedCache>,
    pub downloading: Mutex<Option<(u64, u64)>>,
}

impl Gemma {
    fn new(cache: Option<EmbedCache>) -> Gemma {
        Gemma { model: OnceLock::new(), loading: Mutex::new(()), cache, downloading: Mutex::new(None) }
    }

    /// The model, loaded (and its text part downloaded) on the first call.
    pub fn model(&self) -> Result<Arc<Embedder>, String> {
        if let Some(m) = self.model.get() {
            return m.clone();
        }
        let _one = self.loading.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(m) = self.model.get() {
            return m.clone();
        }
        let m = self.load();
        if let Err(e) = &m {
            eprintln!("embedding model: {e}");
            return m;
        }
        self.model.get_or_init(|| m).clone()
    }

    fn load(&self) -> Result<Arc<Embedder>, String> {
        if !embed::files::part_here(MODEL, Part::Text) {
            eprintln!("downloading the text part of {MODEL}");
            let mut progress = |done: u64, total: u64| {
                *self.downloading.lock().unwrap_or_else(|e| e.into_inner()) = Some((done, total));
            };
            let r = embed::files::download_part(MODEL, Part::Text, &mut progress);
            *self.downloading.lock().unwrap_or_else(|e| e.into_inner()) = None;
            r.map_err(|e| format!("{e:#}"))?;
        }
        Embedder::load_cached().map(Arc::new).map_err(|e| format!("{e:#}"))
    }
}

impl index::Embed for Gemma {
    fn embed(&self, texts: &[&str], query: bool) -> Vec<Vec<f32>> {
        let zero = || texts.iter().map(|_| vec![0f32; index::EMBED_DIM]).collect();
        let Ok(m) = self.model() else { return zero() };
        let out = if query {
            m.embed_texts(texts, Kind::Query)
        } else {
            let owned: Vec<String> = texts.iter().map(|t| t.to_string()).collect();
            embed::cache::embed(&m, self.cache.as_ref(), &owned)
        };
        out.unwrap_or_else(|e| {
            eprintln!("embed: {e:#}");
            zero()
        })
    }

    fn dim(&self) -> usize {
        index::EMBED_DIM
    }

    fn embed_image(&self, path: &Path) -> Option<Vec<f32>> {
        self.model().ok()?.embed_images(&[path]).into_iter().next()?.ok()
    }

    fn embed_media(&self, path: &Path, video: bool, sound: bool) -> Vec<(f64, f64, String, Vec<f32>)> {
        let Ok(m) = self.model() else { return Vec::new() };
        let mut out = Vec::new();
        if video {
            out.extend(m.embed_video(path).unwrap_or_default().into_iter().map(|(a, b, v)| (a, b, "video".to_string(), v)));
        }
        if sound {
            out.extend(m.embed_audio(path).unwrap_or_default().into_iter().map(|(a, b, v)| (a, b, "audio".to_string(), v)));
        }
        out
    }
}

/// A flag a loop sleeps on: `set` wakes it early.
#[derive(Default)]
pub struct Wake(Mutex<bool>, Condvar);

impl Wake {
    pub fn set(&self) {
        *self.0.lock().unwrap_or_else(|e| e.into_inner()) = true;
        self.1.notify_all();
    }

    /// Sleep until `set` or `timeout`, then clear the flag.
    pub fn wait(&self, timeout: Duration) {
        let g = self.0.lock().unwrap_or_else(|e| e.into_inner());
        let (mut g, _) = self.1.wait_timeout_while(g, timeout, |w| !*w).unwrap_or_else(|e| e.into_inner());
        *g = false;
    }

    /// Wait until no `set` came for `quiet` (at most `cap`), so that a burst of saves is indexed once.
    pub fn quiet(&self, quiet: Duration, cap: Duration) {
        let end = Instant::now() + cap;
        loop {
            let left = end.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return;
            }
            let g = self.0.lock().unwrap_or_else(|e| e.into_inner());
            let (mut g, r) = self.1.wait_timeout_while(g, quiet.min(left), |w| !*w).unwrap_or_else(|e| e.into_inner());
            *g = false;
            if r.timed_out() {
                return;
            }
        }
    }
}

pub struct Engine {
    pub ix: Arc<Index>,
    pub gemma: Arc<Gemma>,
    pub busy: Mutex<Vec<&'static str>>,
    wake_files: Wake,
    wake_repos: Wake,
    wake_sessions: Wake,
    wake_folders: Wake,
    wake_thoughts: Wake,
    /// Read every file again at the next pass of the files loop.
    resync_files: std::sync::atomic::AtomicBool,
    /// A download of model parts runs.
    fetching: std::sync::atomic::AtomicBool,
    /// The last counts of the index, and when they were made. The daemon reads the disk at low priority, so a
    /// count can take seconds while it indexes: /status answers from this copy and never waits for the disk.
    counts: Mutex<Option<(Instant, Value)>>,
    counting: std::sync::atomic::AtomicBool,
}

/// The age at which /status starts a new count in the background.
const COUNTS_SECONDS: u64 = 20;

pub fn config() -> Config {
    Config::new(Value::Object(settings::load()), store::home())
}

/// Background indexing is on unless PENSIEVE_NO_INDEX is set (route diffs against a copy of an index).
fn indexing_allowed() -> bool {
    std::env::var_os("PENSIEVE_NO_INDEX").is_none()
}

fn log(m: &str) {
    eprintln!("{m}");
}

impl Engine {
    /// Open index.db in the data folder. Loads every vector into memory: call it on a thread of its own.
    pub fn open() -> Result<Arc<Engine>, String> {
        let dir = store::data_dir();
        let t0 = Instant::now();
        let cache = EmbedCache::open(&dir.join("embed_cache.db")).map_err(|e| eprintln!("embed cache: {e}")).ok();
        let gemma = Arc::new(Gemma::new(cache));
        let ix = Arc::new(Index::open(&dir.join("index.db"), gemma.clone(), config()).map_err(|e| format!("index.db: {e}"))?);
        eprintln!("index open in {} ms ({} session, {} code, {} file vectors)", t0.elapsed().as_millis(),
                  ix.session_vecs.len(), ix.code_vecs.len(), ix.file_vecs.len());
        Ok(Arc::new(Engine { ix, gemma, busy: Mutex::new(Vec::new()), wake_files: Wake::default(), wake_repos: Wake::default(),
                             wake_sessions: Wake::default(), wake_folders: Wake::default(),
                             wake_thoughts: Wake::default(), resync_files: Default::default(), fetching: Default::default(),
                             counts: Mutex::new(None), counting: Default::default() }))
    }

    fn wake_all(&self) {
        self.wake_files.set();
        self.wake_repos.set();
        self.wake_sessions.set();
    }

    /// Save changed settings and index with them from now on.
    pub fn change_settings(&self, changes: &serde_json::Map<String, Value>) -> Result<Value, String> {
        let saved = settings::update(changes)?;
        self.ix.set_config(config());
        self.resync_files.store(true, std::sync::atomic::Ordering::Relaxed);
        self.wake_all();
        Ok(Value::Object(saved))
    }

    fn indexed(&self) {
        self.wake_folders.set();
    }

    /// Mark `what` busy while `f` runs.
    fn busy<T>(&self, what: &'static str, f: impl FnOnce() -> T) -> T {
        self.busy.lock().unwrap_or_else(|e| e.into_inner()).push(what);
        let out = f();
        self.busy.lock().unwrap_or_else(|e| e.into_inner()).retain(|w| *w != what);
        out
    }

    fn count(&self, sql: &str) -> i64 {
        self.ix.db.read(|c| c.query_row(sql, [], |r| r.get::<_, i64>(0))).unwrap_or(0)
    }

    fn recount(&self) {
        let counts = json!({
            "sessions": self.count("SELECT COUNT(*) FROM sessions WHERE n_chunks>0"),
            "files": self.count("SELECT COUNT(*) FROM files"),
            "repos": self.count("SELECT COUNT(*) FROM repos"),
            "code_chunks": self.count("SELECT COUNT(*) FROM code_chunks"),
            "thoughts": self.ix.thought_count(),
        });
        *self.counts.lock().unwrap_or_else(|e| e.into_inner()) = Some((Instant::now(), counts));
    }

    /// Start a count on a thread of its own when the last one is old, unless one runs.
    fn recount_if_old(self: &Arc<Self>) {
        let old = self.counts.lock().unwrap_or_else(|e| e.into_inner()).as_ref()
            .is_none_or(|(t, _)| t.elapsed().as_secs() >= COUNTS_SECONDS);
        if old && !self.counting.swap(true, Ordering::SeqCst) {
            let me = self.clone();
            spawn("count", move || {
                me.recount();
                me.counting.store(false, Ordering::SeqCst);
            });
        }
    }

    /// Answers from memory only. Before the first count is done, the counts are absent.
    pub fn status(self: &Arc<Self>) -> Value {
        self.recount_if_old();
        let mut out = json!({
            "model": MODEL,
            "in_use": {"files": self.ix.files_in_use_count().0, "folders": self.ix.files_in_use_count().1},
            "busy": *self.busy.lock().unwrap_or_else(|e| e.into_inner()),
        });
        if let Some((_, Value::Object(counts))) = &*self.counts.lock().unwrap_or_else(|e| e.into_inner()) {
            out.as_object_mut().expect("object").extend(counts.clone());
        }
        if let Some((done, total)) = *self.gemma.downloading.lock().unwrap_or_else(|e| e.into_inner()) {
            out["downloading"] = json!({"done": done, "total": total});
        }
        out
    }

    /// The media kinds that the file index reads by content: switched on in the settings, with their model part
    /// here and, for video and sound, the decoder. Returns the parts still missing for the kinds that are switched
    /// on and have a file in use: a Mac without such files never downloads them.
    fn set_content_ready(&self) -> Vec<Part> {
        let cfg = self.ix.config();
        let mut ready = Vec::new();
        let mut missing = Vec::new();
        for k in &embed::content::KINDS {
            let on = index::files::file_type(&cfg, k.id)["index"] == "content";
            if embed::content::Installer::ready(k, on) {
                ready.push(k.id);
            } else if on && self.ix.files_waiting(k.id) {
                for p in k.parts.iter().copied().filter(|p| !embed::files::part_here(MODEL, *p)) {
                    if !missing.contains(&p) {
                        missing.push(p);
                    }
                }
            }
        }
        eprintln!("media read by content: {}", if ready.is_empty() { "none".to_string() } else { ready.join(", ") });
        self.ix.set_files_content_ready(&ready);
        missing
    }

    /// Set the ready media kinds, and download the model parts that files in use wait for. Then read all files
    /// again, so that those files are read by content.
    fn check_media(self: &Arc<Self>) {
        if !indexing_allowed() || self.fetching.swap(true, Ordering::SeqCst) {
            return;
        }
        let parts = self.set_content_ready();
        if parts.is_empty() {
            self.fetching.store(false, Ordering::SeqCst);
            return;
        }
        let me = self.clone();
        spawn("model-parts", move || {
            for part in parts {
                eprintln!("downloading the {} part of {MODEL}", part.name());
                let mut progress = |done: u64, total: u64| {
                    *me.gemma.downloading.lock().unwrap_or_else(|e| e.into_inner()) = Some((done, total));
                };
                let r = me.busy("model", || embed::files::download_part(MODEL, part, &mut progress));
                *me.gemma.downloading.lock().unwrap_or_else(|e| e.into_inner()) = None;
                if let Err(e) = r {
                    eprintln!("the {} part of {MODEL}: {e:#}", part.name());
                }
            }
            me.set_content_ready();
            me.fetching.store(false, Ordering::SeqCst);
            me.resync_files.store(true, Ordering::Relaxed);
            me.wake_files.set();
        });
    }

    /// Start the background work: sessions, files, repos and the folder index.
    pub fn start(self: &Arc<Self>) {
        self.recount_if_old();
        self.set_content_ready();
        let me = self.clone();
        self.ix.watch.set_wake(move || me.wake_all());
        let ix = self.ix.clone();
        spawn("usage-backfill", move || {
            ix.backfill_usage(&log);
        });
        let loops: [(&str, fn(&Arc<Engine>)); 5] = [("sessions", Engine::session_loop), ("files", Engine::file_loop),
                                                      ("repos", Engine::repo_loop), ("folders", Engine::folder_loop),
                                                      ("thoughts", Engine::thought_loop)];
        for (name, f) in loops {
            if name != "folders" && !indexing_allowed() {
                continue;
            }
            let me = self.clone();
            spawn(name, move || f(&me));
        }
    }

    fn session_loop(self: &Arc<Self>) {
        let mut last_sweep = 0.0;
        loop {
            let paths: Vec<String> = self.ix.watch.take_sessions().into_iter().collect();
            let sweep = last_sweep == 0.0 || store::now() - last_sweep > SWEEP_SECONDS;
            let behind = self.ix.sessions.behind.load(Ordering::Relaxed);
            let n = if sweep || behind {
                self.busy("sessions", || self.ix.sync_sessions(Some(SESSION_BATCH_SECONDS), None, &log))
            } else if !paths.is_empty() {
                self.busy("sessions", || self.ix.sync_session_paths(&paths, &log))
            } else {
                0
            };
            if sweep {
                last_sweep = store::now();
            }
            if n > 0 {
                self.indexed();
                self.wake_thoughts.set();
            }
            if self.ix.sessions.deferred.load(Ordering::Relaxed) {
                let me = self.clone();
                spawn("session-later", move || {
                    std::thread::sleep(Duration::from_secs_f64(ACTIVE_SECONDS));
                    me.wake_sessions.set();
                });
            }
            if self.ix.sessions.behind.load(Ordering::Relaxed) {
                continue;
            }
            self.wake_sessions.wait(IDLE_WAIT);
            self.wake_sessions.quiet(Duration::from_millis(100), Duration::from_millis(500));
        }
    }

    fn file_loop(self: &Arc<Self>) {
        let mut last_full = 0.0;
        self.ix.watch_configure(&self.ix.checkouts());
        loop {
            let (_, files, full_files) = self.ix.watch.take();
            let mut changed = false;
            let resync = self.resync_files.swap(false, Ordering::Relaxed);
            if full_files || resync || store::now() - last_full > SWEEP_SECONDS {
                last_full = store::now();
                changed = self.busy("files", || self.ix.files_sync(true, &log)) > 0;
                self.check_media();
                self.ix.watch_configure(&self.ix.checkouts());
            } else if !files.is_empty() {
                let paths: Vec<String> = files.into_iter().collect();
                let (n, retry) = self.busy("files", || self.ix.files_sync_paths(&paths, SETTLE_SECONDS, &log));
                changed = n > 0;
                if !retry.is_empty() {
                    self.ix.watch.requeue_files(retry);
                    let me = self.clone();
                    spawn("files-later", move || {
                        std::thread::sleep(Duration::from_secs_f64(SETTLE_SECONDS + 0.5));
                        me.wake_files.set();
                    });
                }
            }
            if changed {
                self.indexed();
            }
            self.wake_files.wait(IDLE_WAIT);
            self.wake_files.quiet(Duration::from_millis(150), Duration::from_secs(1));
        }
    }

    fn repo_loop(self: &Arc<Self>) {
        let mut last_full = 0.0;
        loop {
            let (roots, asked) = self.ix.watch.take_repos();
            let full = asked || store::now() - last_full > SWEEP_SECONDS;
            if full || !roots.is_empty() {
                let n = self.busy("repos", || if full {
                    last_full = store::now();
                    self.ix.repos_sync(true, None, &log)
                } else {
                    self.ix.repos_refresh(&roots.into_iter().collect::<Vec<_>>())
                });
                self.ix.watch_configure(&self.ix.checkouts());
                if n > 0 {
                    self.indexed();
                }
            }
            self.ix.take_commit_events();
            self.wake_repos.wait(IDLE_WAIT);
            self.wake_repos.quiet(Duration::from_millis(150), Duration::from_secs(1));
        }
    }

    /// The folder index (`in:` filters and folder hits), built again after new content is indexed.
    fn folder_loop(self: &Arc<Self>) {
        loop {
            self.ix.folder_index_tick(self.ix.repos_sweeping());
            self.wake_folders.wait(FOLDER_WAIT);
        }
    }

    /// The thoughts (index::thoughts), built at start and again after sessions change. A build that ran out of its
    /// time continues after a pause, so that a model that does not load cannot make this loop spin.
    fn thought_loop(self: &Arc<Self>) {
        loop {
            let (_, left) = self.busy("thoughts", || self.ix.thoughts_rebuild(THOUGHT_BATCH_SECONDS, &log));
            if left {
                std::thread::sleep(Duration::from_secs(5));
                continue;
            }
            self.wake_thoughts.wait(Duration::from_secs_f64(SWEEP_SECONDS));
            self.wake_thoughts.quiet(THOUGHT_QUIET, THOUGHT_CAP);
        }
    }

    /// The scope of an MCP connection: a scope name, or "auto" (the repo or folder of `cwd`).
    pub fn scope(&self, name: &str, cwd: &str) -> Result<Option<index::scopes::Scope>, String> {
        let name = if name.is_empty() { None } else { Some(name) };
        let cwd = if cwd.is_empty() { None } else { Some(cwd) };
        self.ix.scope_param(name, cwd)
    }
}

/// A thread for background work. It runs in the Darwin background band: low CPU priority and throttled disk I/O.
/// Only these threads are in the band, so the request threads of the server stay fast while indexing runs.
fn spawn(name: &str, f: impl FnOnce() + Send + 'static) {
    let _ = std::thread::Builder::new().name(name.to_string()).spawn(move || {
        // SAFETY: changes only the priority of the calling thread
        unsafe { libc::setpriority(libc::PRIO_DARWIN_THREAD, 0, libc::PRIO_DARWIN_BG) };
        f()
    });
}
