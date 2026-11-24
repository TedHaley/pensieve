//! Agent sessions in the index: transcripts found on disk, cut into chunks of exchanges, embedded, laid out on the
//! session map and grouped into topics. The tables are `sessions`, `chunks`, `session_files`, `topics` and
//! `session_usage` of index.db.
//!
//! A transcript is append-only, so a session that grows is read from where its last parse stopped, and only its
//! new or changed chunks are embedded. A transcript that keeps growing is indexed at most every ACTIVE_SECONDS.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{LazyLock, Mutex, MutexGuard};
use std::time::Instant;

use regex::Regex;
use serde_json::{Value, json};
use store::rusqlite::{self, OptionalExtension, params, params_from_iter};

use crate::db::marks;
use crate::parsers::{self, Session};
use crate::text::sha1_hex;
use crate::{CHUNK_CHARS, Config, Index, layout, tokens, vectors};

pub const LAYOUT_VERSION: &str = "umap-v1";
/// A transcript that is still growing is indexed again at most this often.
pub const ACTIVE_SECONDS: f64 = 1.5;
/// Transcripts whose last parse stays in memory: the ones that grow now.
const PARSED_KEEP: usize = 16;
pub const MEMORY_GLOB: &str = "*/memory/*.md";
/// The folder a text-engine command runs in, and the first line of each of its prompts: never the user's sessions.
pub const ENGINE_TEMP_PREFIX: &str = "pensieve-ai-";
pub const ENGINE_MARK: &str = "[pensieve text job]";
/// The connection that MCP results found in transcripts belong to.
pub const SESSIONS_CONNECTION: &str = "agent_sessions";
const USAGE_BACKFILL: &str = "usage_backfill_v1";

/// Words that every agent session has, so they name no topic.
const TOPIC_STOP: [&str; 12] = ["memory", "memories", "user", "assistant", "session", "sessions", "agent", "claude",
    "codex", "conversation", "chat", "file"];
const ENGLISH_STOP: &str = "a about above across after afterwards again against all almost alone along already also \
although always am among amongst amoungst amount an and another any anyhow anyone anything anyway anywhere are around \
as at back be became because become becomes becoming been before beforehand behind being below beside besides between \
beyond bill both bottom but by call can cannot cant co con could couldnt cry de describe detail do done down due \
during each eg eight either eleven else elsewhere empty enough etc even ever every everyone everything everywhere \
except few fifteen fifty fill find fire first five for former formerly forty found four from front full further get \
give go had has hasnt have he hence her here hereafter hereby herein hereupon hers herself him himself his how however \
hundred i ie if in inc indeed interest into is it its itself keep last latter latterly least less ltd made many may me \
meanwhile might mill mine more moreover most mostly move much must my myself name namely neither never nevertheless \
next nine no nobody none noone nor not nothing now nowhere of off often on once one only onto or other others \
otherwise our ours ourselves out over own part per perhaps please put rather re same see seem seemed seeming seems \
serious several she should show side since sincere six sixty so some somehow someone something sometime sometimes \
somewhere still such system take ten than that the their them themselves then thence there thereafter thereby \
therefore therein thereupon these they thick thin third this those though three through throughout thru thus to \
together too top toward towards twelve twenty two un under until up upon us very via was we well were what whatever \
when whence whenever where whereafter whereas whereby wherein whereupon wherever whether which while whither who \
whoever whole whom whose why will with within without would yet you your yours yourself yourselves files";

/// One session that indexing read: its id, and the MCP calls found in the part of the transcript read this time
/// (empty unless the agent-sessions connection is on), with the session's last time.
#[derive(Debug, Clone)]
pub struct Indexed {
    pub id: String,
    pub mcp: Vec<Value>,
    pub at: String,
}

type Listener = Box<dyn Fn(&Indexed) + Send + Sync>;

#[derive(Default)]
struct Inner {
    indexed_at: HashMap<String, f64>,
    /// path -> its last parse; the newest last.
    parsed: Vec<(String, Session)>,
    waiting: BTreeSet<String>,
    /// Transcripts that are not the user's sessions, with the (mtime, size) they had.
    ignored: HashMap<String, (f64, i64)>,
}

#[derive(Default)]
pub struct State {
    inner: Mutex<Inner>,
    sync: Mutex<()>,
    layout: Mutex<()>,
    listeners: Mutex<Vec<Listener>>,
    /// Sessions being indexed now: {name, root, done, total: null}, for /api/syncing.
    pub progress: Mutex<Option<Value>>,
    /// The last `sync_files` ran out of its time budget with sessions left.
    pub behind: AtomicBool,
    /// The last sync left a growing transcript for ACTIVE_SECONDS: call `sync_paths` again after that.
    pub deferred: AtomicBool,
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

static TEMP: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(/private)?/(tmp|var/folders)/").expect("regex"));

/// The whole relative path matches the glob: `*` and `?` stay inside one folder, `**` matches any number of folders.
pub fn glob_match(rel: &str, pattern: &str) -> bool {
    glob_regex(pattern).is_some_and(|r| r.is_match(rel))
}

fn glob_regex(pattern: &str) -> Option<Regex> {
    let rx: Vec<String> = pattern.split('/').map(|part| {
        if part == "**" {
            "(?:[^/]+/)*".to_string()
        } else {
            regex::escape(part).replace(r"\*", "[^/]*").replace(r"\?", "[^/]") + "/"
        }
    }).collect();
    let rx = rx.concat();
    let rx = rx.strip_suffix('/').unwrap_or(&rx);
    Regex::new(&format!("^(?:{rx})$")).ok()
}

/// A system temp folder, when `skip_temp_sessions` is on: sessions that ran there are throwaway. The text engine's
/// own folders are skipped whatever the setting.
pub fn in_temp(path: &str, cfg: &Config) -> bool {
    !path.is_empty() && (path.contains(ENGINE_TEMP_PREFIX) || (TEMP.is_match(path) && cfg.flag("skip_temp_sessions")))
}

/// A session that the text engine ran: Pensieve's own notes must never come back as the user's sessions.
pub fn engine_session(s: &Session, cfg: &Config) -> bool {
    in_temp(&s.project, cfg)
        || s.turns.iter().take(2).any(|(r, t)| r == "user" && crate::text::head(t, 500).contains(ENGINE_MARK))
}

/// The files under `root` whose relative path matches `pattern`.
fn glob(root: &Path, pattern: &str) -> Vec<PathBuf> {
    let depth = if pattern.contains("**") { usize::MAX } else { pattern.split('/').count() };
    let Some(rx) = glob_regex(pattern) else { return Vec::new() };
    let mut out: Vec<PathBuf> = walkdir::WalkDir::new(root).min_depth(1).max_depth(depth).into_iter().flatten()
        .filter(|e| e.file_type().is_file() || e.path_is_symlink())
        .filter_map(|e| {
            let rel = e.path().strip_prefix(root).ok()?.to_string_lossy().into_owned();
            rx.is_match(&rel).then(|| e.into_path())
        })
        .collect();
    out.sort();
    out
}

/// (agent, path) of every transcript on disk in the folders that are on, and Claude Code's memory files (not
/// MEMORY.md, their index).
pub fn discover(cfg: &Config) -> Vec<(String, PathBuf)> {
    let mut out = Vec::new();
    for (sid, name, root, pattern) in cfg.session_sources() {
        if !(cfg.enabled(&sid) && cfg.enabled(&format!("agents/{name}")) && root.exists()) {
            continue;
        }
        for p in glob(&root, &pattern) {
            if p.is_file() && !p.components().any(|c| c.as_os_str() == "subagents") {
                out.push((name.clone(), p));
            }
        }
        if name == "claude" {
            for p in glob(&root, MEMORY_GLOB) {
                if p.is_file() && p.file_name().is_some_and(|n| n != "MEMORY.md") {
                    out.push((name.clone(), p));
                }
            }
        }
    }
    out
}

/// Exchanges (a user turn and the replies after it), each split on paragraph boundaries into chunks of at most
/// CHUNK_CHARS characters; a longer paragraph is cut hard.
pub fn chunk_session(turns: &[(String, String)]) -> Vec<String> {
    let mut exchanges: Vec<Vec<String>> = Vec::new();
    let mut cur: Vec<String> = Vec::new();
    for (role, text) in turns {
        if role == "user" && !cur.is_empty() {
            exchanges.push(std::mem::take(&mut cur));
        }
        let label = match role.as_str() { "user" => "User", "memory" => "Memory", _ => "Assistant" };
        cur.push(format!("{label}: {text}"));
    }
    if !cur.is_empty() {
        exchanges.push(cur);
    }
    let mut out = Vec::new();
    for ex in exchanges {
        let mut buf = String::new();
        let mut buf_len = 0usize;
        for para in ex.join("\n\n").split("\n\n") {
            let mut para: Vec<char> = para.chars().collect();
            while para.len() > CHUNK_CHARS {
                let rest = para.split_off(CHUNK_CHARS);
                if !buf.is_empty() {
                    out.push(std::mem::take(&mut buf));
                    buf_len = 0;
                }
                out.push(para.iter().collect());
                para = rest;
            }
            if !buf.is_empty() && buf_len + para.len() > CHUNK_CHARS {
                out.push(std::mem::take(&mut buf));
                buf_len = 0;
            }
            let p: String = para.iter().collect();
            if buf.is_empty() {
                buf = p;
                buf_len = para.len();
            } else {
                buf = format!("{buf}\n\n{p}");
                buf_len += 2 + para.len();
            }
        }
        if !buf.is_empty() {
            out.push(buf);
        }
    }
    out
}

static ROOTS: LazyLock<Mutex<HashMap<String, Option<String>>>> = LazyLock::new(Default::default);

/// The repo root of a folder: linked git worktrees and `.claude/worktrees` checkouts fold into their main repo.
pub fn project_root(cwd: &str, home: &Path) -> Option<String> {
    if let Some(r) = lock(&ROOTS).get(cwd) {
        return r.clone();
    }
    let mut p = PathBuf::from(if cwd.is_empty() { "/nonexistent" } else { cwd });
    let parts: Vec<String> = p.components().map(|c| c.as_os_str().to_string_lossy().into_owned()).collect();
    if let (Some(i), true) = (parts.iter().position(|x| x == ".claude"), parts.iter().any(|x| x == "worktrees")) {
        p = parts[..i].iter().collect();
    }
    let mut out = None;
    let mut q = Some(p.as_path());
    while let Some(dir) = q {
        let g = dir.join(".git");
        if g.is_file() {
            let gd = std::fs::read_to_string(&g).ok()
                .and_then(|t| t.split_once("gitdir:").map(|x| PathBuf::from(x.1.trim())));
            let main = gd.and_then(|gd| {
                let gp: Vec<_> = gd.components().collect();
                let w = gp.iter().position(|c| c.as_os_str() == "worktrees")?;
                gp[..w].iter().collect::<PathBuf>().parent().map(|m| m.to_string_lossy().into_owned())
            });
            out = Some(main.unwrap_or_else(|| dir.to_string_lossy().into_owned()));
            break;
        }
        if g.exists() {
            out = Some(dir.to_string_lossy().into_owned());
            break;
        }
        if dir == home {
            break;
        }
        q = dir.parent();
    }
    lock(&ROOTS).insert(cwd.to_string(), out.clone());
    out
}

fn base_name(p: &str) -> String {
    Path::new(p).file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
}

/// A stable project name for a working folder: the repo's folder name (worktrees fold into their repo).
pub fn project_name(cwd: &str, home: &Path) -> String {
    if cwd.is_empty() {
        return "(unknown)".into();
    }
    let p = Path::new(cwd);
    if p == home {
        return "(home)".into();
    }
    let parts: Vec<String> = p.components().map(|c| c.as_os_str().to_string_lossy().into_owned()).collect();
    if let (Some(i), true) = (parts.iter().position(|x| x == ".claude"), parts.iter().any(|x| x == "worktrees")) {
        return parts[..i].iter().collect::<PathBuf>().file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    }
    match project_root(cwd, home) {
        Some(root) => base_name(&root),
        None => base_name(cwd),
    }
}

/// Python's `str.title()`.
pub fn title_case(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut prev = false;
    for c in s.chars() {
        if prev { out.extend(c.to_lowercase()) } else { out.extend(c.to_uppercase()) }
        prev = c.is_alphabetic();
    }
    out
}

/// A list of strings as Python's `json.dumps` writes it (", " between items).
fn py_list(items: &[String]) -> String {
    format!("[{}]", items.iter().map(|s| Value::String(s.clone()).to_string()).collect::<Vec<_>>().join(", "))
}

fn file_stat(p: &Path) -> Option<(f64, i64)> {
    let m = p.metadata().ok()?;
    let t = m.modified().ok()?.duration_since(std::time::UNIX_EPOCH).ok()?;
    Some((t.as_secs() as f64 + t.subsec_nanos() as f64 * 1e-9, m.len() as i64))
}

/// The top 5 TF-IDF terms of each document (sklearn's TfidfVectorizer: English stop words and Pensieve's own,
/// at most 4000 terms, words of 3+ letters), joined with ", ".
pub fn keywords(docs: &[String]) -> Vec<String> {
    static WORD: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[A-Za-z][A-Za-z_\-]{2,}").expect("regex"));
    let stop: HashSet<&str> = ENGLISH_STOP.split_whitespace().chain(TOPIC_STOP).collect();
    let counts: Vec<HashMap<String, f64>> = docs.iter().map(|d| {
        let low = d.to_lowercase();
        let mut c = HashMap::new();
        for m in WORD.find_iter(&low) {
            if !stop.contains(m.as_str()) {
                *c.entry(m.as_str().to_string()).or_insert(0.0) += 1.0;
            }
        }
        c
    }).collect();
    let mut total: BTreeMap<&str, f64> = BTreeMap::new();
    for c in &counts {
        for (w, n) in c {
            *total.entry(w.as_str()).or_default() += n;
        }
    }
    if total.is_empty() {
        return vec![String::new(); docs.len()];
    }
    let mut vocab: Vec<&str> = total.keys().copied().collect();
    if vocab.len() > 4000 {
        let mut by: Vec<(&str, f64)> = total.iter().map(|(w, n)| (*w, *n)).collect();
        by.sort_by(|a, b| b.1.total_cmp(&a.1));
        vocab = by.into_iter().take(4000).map(|x| x.0).collect();
        vocab.sort();
    }
    let n = docs.len() as f64;
    let idf: Vec<f64> = vocab.iter().map(|w| {
        let df = counts.iter().filter(|c| c.contains_key(*w)).count() as f64;
        ((1.0 + n) / (1.0 + df)).ln() + 1.0
    }).collect();
    counts.iter().map(|c| {
        let row: Vec<f64> = vocab.iter().zip(&idf).map(|(w, i)| c.get(*w).copied().unwrap_or(0.0) * i).collect();
        let mut order: Vec<usize> = (0..row.len()).collect();
        order.sort_by(|a, b| row[*b].total_cmp(&row[*a]));
        order.iter().take(5).map(|&j| vocab[j]).collect::<Vec<_>>().join(", ")
    }).collect()
}

fn topic_name(name: Option<&str>, keywords: &str, id: i64) -> String {
    if let Some(n) = name.filter(|n| !n.is_empty()) {
        return n.to_string();
    }
    let first = title_case(keywords.split(',').next().unwrap_or("").trim());
    if first.is_empty() { format!("Topic {}", id + 1) } else { first }
}

/// Filters of a session search (0.21 `Store.search`). `since` / `until` are ISO times compared with the session's
/// last and first activity.
#[derive(Debug, Clone, Default)]
pub struct Filter {
    pub session_id: Option<String>,
    pub projects: Vec<String>,
    pub sources: Vec<String>,
    pub since: Option<String>,
    pub until: Option<String>,
    pub session_ids: Option<Vec<String>>,
}

impl Filter {
    fn empty(&self) -> bool {
        self.session_id.is_none() && self.projects.is_empty() && self.sources.is_empty() && self.since.is_none()
            && self.until.is_none() && self.session_ids.is_none()
    }
}

impl Index {
    /// Call `f` after each session is indexed (the card builder; collection of MCP results from transcripts).
    pub fn on_session_indexed(&self, f: impl Fn(&Indexed) + Send + Sync + 'static) {
        lock(&self.sessions.listeners).push(Box::new(f));
    }

    /// Index new and changed transcripts; returns how many sessions it touched. With `budget`, stops after that
    /// long and sets `behind`. `on_progress` gets the count every 8 sessions. Runs on a background thread.
    pub fn sync_sessions(&self, budget: Option<f64>, on_progress: Option<&dyn Fn(usize)>, log: &dyn Fn(&str)) -> usize {
        let _one = lock(&self.sessions.sync);
        let st = &self.sessions;
        let end = budget.map(|b| Instant::now() + std::time::Duration::from_secs_f64(b));
        st.behind.store(false, Ordering::Relaxed);
        st.deferred.store(false, Ordering::Relaxed);
        let cfg = self.config();
        let mut touched = self.purge_disabled(&cfg, log) + self.purge_own(&cfg, log) + self.purge_memories(log);
        let known: HashMap<String, (f64, i64)> = self.db.read(|c| {
            let mut s = c.prepare("SELECT path, mtime, size FROM sessions")?;
            let r = s.query_map([], |r| Ok((r.get::<_, Option<String>>(0)?.unwrap_or_default(),
                                            (r.get::<_, Option<f64>>(1)?.unwrap_or(0.0), r.get::<_, Option<i64>>(2)?.unwrap_or(0)))))?
                .flatten().collect();
            Ok(r)
        }).unwrap_or_default();
        for (source, path) in discover(&cfg) {
            if end.is_some_and(|e| Instant::now() > e) {
                st.behind.store(true, Ordering::Relaxed);
                break;
            }
            let Some(stat) = file_stat(&path) else { continue };
            let p = path.to_string_lossy().into_owned();
            if known.get(&p) == Some(&stat) {
                continue;
            }
            if known.contains_key(&p) && store::now() - lock(&st.inner).indexed_at.get(&p).copied().unwrap_or(0.0) < ACTIVE_SECONDS {
                st.deferred.store(true, Ordering::Relaxed);
                lock(&st.inner).waiting.insert(p);
                continue;
            }
            if self.index_one(&cfg, &source, &path, stat, log) {
                touched += 1;
                if touched >= 5 {
                    *lock(&st.progress) = Some(json!({"name": source, "root": path.parent().map(|x| x.to_string_lossy().into_owned()),
                                                      "done": touched, "total": null}));
                }
                if let Some(f) = on_progress {
                    if touched % 8 == 0 {
                        f(touched);
                    }
                }
            }
        }
        *lock(&st.progress) = None;
        touched
    }

    /// Index these transcripts now (the ones the file watch saw change), without the sweep of every folder. A
    /// transcript indexed less than ACTIVE_SECONDS ago waits, and sets `deferred`.
    pub fn sync_session_paths(&self, paths: &[String], log: &dyn Fn(&str)) -> usize {
        let _one = lock(&self.sessions.sync);
        let st = &self.sessions;
        st.deferred.store(false, Ordering::Relaxed);
        let cfg = self.config();
        let roots: Vec<(String, PathBuf, String)> = cfg.session_sources().into_iter()
            .filter(|(sid, name, _, _)| cfg.enabled(sid) && cfg.enabled(&format!("agents/{name}")))
            .map(|(_, n, r, p)| (n, r, p)).collect();
        let mut all: BTreeSet<String> = paths.iter().cloned().collect();
        all.extend(std::mem::take(&mut lock(&st.inner).waiting));
        let mut touched = 0;
        for p in all {
            let path = PathBuf::from(&p);
            let hit = roots.iter().find(|(_, r, pat)| {
                let r = r.to_string_lossy();
                p.starts_with(&format!("{r}/")) && glob_match(&p[r.len() + 1..], pat)
            });
            let Some((name, _, _)) = hit else { continue };
            if path.components().any(|c| c.as_os_str() == "subagents") || !path.is_file() {
                continue;
            }
            let Some(stat) = file_stat(&path) else { continue };
            let row: Option<(f64, i64)> = self.db.read(|c| c.query_row(
                "SELECT mtime, size FROM sessions WHERE path=?", [&p],
                |r| Ok((r.get::<_, Option<f64>>(0)?.unwrap_or(0.0), r.get::<_, Option<i64>>(1)?.unwrap_or(0)))).optional()).ok().flatten();
            if row == Some(stat) {
                continue;
            }
            if row.is_some() && store::now() - lock(&st.inner).indexed_at.get(&p).copied().unwrap_or(0.0) < ACTIVE_SECONDS {
                lock(&st.inner).waiting.insert(p);
                st.deferred.store(true, Ordering::Relaxed);
                continue;
            }
            touched += self.index_one(&cfg, name, &path, stat, log) as usize;
        }
        touched
    }

    /// Index one changed transcript; false when it is not the user's work or it failed.
    fn index_one(&self, cfg: &Config, source: &str, path: &Path, stat: (f64, i64), log: &dyn Fn(&str)) -> bool {
        let p = path.to_string_lossy().into_owned();
        {
            let mut g = lock(&self.sessions.inner);
            g.indexed_at.insert(p.clone(), store::now());
            if g.ignored.get(&p) == Some(&stat) {
                return false;
            }
        }
        let t0 = Instant::now();
        match self.index_session(cfg, source, path, stat) {
            Ok(true) => {
                let stem: String = path.file_stem().map(|s| s.to_string_lossy().chars().take(8).collect()).unwrap_or_default();
                log(&format!("indexed {source}:{stem} in {:.0} ms", t0.elapsed().as_secs_f64() * 1000.0));
                true
            }
            Ok(false) => {
                lock(&self.sessions.inner).ignored.insert(p, stat);
                false
            }
            Err(e) => {
                log(&format!("skip {p}: {e}"));
                false
            }
        }
    }

    fn index_session(&self, cfg: &Config, source: &str, path: &Path, stat: (f64, i64)) -> Result<bool, String> {
        let p = path.to_string_lossy().into_owned();
        let old_s = {
            let mut g = lock(&self.sessions.inner);
            g.parsed.iter().position(|(k, _)| *k == p).map(|i| g.parsed.remove(i).1)
        };
        let size = path.metadata().map(|m| m.len()).unwrap_or(0);
        let mcp_done = old_s.as_ref()
            .filter(|o| o.path == p && size >= o.offset && !path.extension().is_some_and(|x| x == "md"))
            .map(|o| o.mcp.len()).unwrap_or(0);
        let s = parsers::parse_from(source, path, old_s).map_err(|e| e.to_string())?;
        {
            let mut g = lock(&self.sessions.inner);
            g.parsed.push((p.clone(), s.clone()));
            while g.parsed.len() > PARSED_KEEP {
                g.parsed.remove(0);
            }
        }
        if engine_session(&s, cfg) {
            return Ok(false);
        }
        let chunks = chunk_session(&s.turns);
        let hashes: Vec<String> = chunks.iter().map(|c| sha1_hex(c)).collect();
        let old: HashMap<(i64, String), i64> = self.db.read(|c| {
            let mut st = c.prepare("SELECT id, idx, hash FROM chunks WHERE session_id=?")?;
            let r = st.query_map([&s.id], |r| Ok(((r.get::<_, Option<i64>>(1)?.unwrap_or(-1), r.get::<_, Option<String>>(2)?.unwrap_or_default()), r.get::<_, i64>(0)?)))?
                .flatten().collect();
            Ok(r)
        }).map_err(|e| e.to_string())?;
        let keep: HashSet<(i64, String)> = hashes.iter().enumerate().map(|(i, h)| (i as i64, h.clone())).collect();
        let stale: Vec<i64> = old.iter().filter(|(k, _)| !keep.contains(*k)).map(|(_, id)| *id).collect();
        let new: Vec<usize> = (0..chunks.len()).filter(|&i| !old.contains_key(&(i as i64, hashes[i].clone()))).collect();
        let texts: Vec<String> = new.iter().map(|&i| chunks[i].clone()).collect();
        let vecs = if texts.is_empty() { Vec::new() } else { self.embed_docs(&texts, None) };
        let home = cfg.home.clone();
        let usage = tokens::rows(&s.state.tok);
        let ids = self.db.tx(|t| {
            t.execute("DELETE FROM session_files WHERE session_id=?", [&s.id])?;
            let mut files: Vec<(&str, i64)> = s.touched.iter().map(|(p, e)| (p.as_str(), *e as i64)).collect();
            if files.is_empty() {
                files.push(("", 0));
            }
            for (fp, e) in files {
                t.execute("INSERT OR REPLACE INTO session_files VALUES(?,?,?)", params![s.id, fp, e])?;
            }
            for cid in &stale {
                t.execute("DELETE FROM chunks WHERE id=?", [cid])?;
            }
            let mut ids = Vec::new();
            for (&i, v) in new.iter().zip(&vecs) {
                t.execute("INSERT INTO chunks(session_id, idx, hash, text, vec) VALUES(?,?,?,?,?)",
                          params![s.id, i as i64, hashes[i], chunks[i], vectors::to_blob(v)])?;
                ids.push(t.last_insert_rowid());
            }
            t.execute(
                "INSERT INTO sessions(id,source,project,project_name,title,path,mtime,size,started,updated,n_chunks) \
                 VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET source=excluded.source, \
                 project=excluded.project, project_name=excluded.project_name, title=excluded.title, path=excluded.path, \
                 mtime=excluded.mtime, size=excluded.size, started=excluded.started, updated=excluded.updated, \
                 n_chunks=excluded.n_chunks",
                params![s.id, source, s.project, project_name(&s.project, &home), s.title, p, stat.0, stat.1,
                        s.started, s.updated, chunks.len() as i64])?;
            if !new.is_empty() || !stale.is_empty() {
                t.execute("UPDATE sessions SET summary=NULL, tags=NULL WHERE id=?", [&s.id])?;
            }
            tokens::replace_session(t, &s.id, &usage)?;
            Ok(ids)
        }).map_err(|e| e.to_string())?;
        self.session_vecs.remove_many(&stale);
        self.session_vecs.add(&ids.into_iter().zip(vecs).collect::<Vec<_>>());
        if !s.id.contains(":memory/") {
            self.store_exchanges(&s).map_err(|e| e.to_string())?;
        }
        let mcp = if s.mcp.len() > mcp_done && cfg.enabled(&format!("connections/{SESSIONS_CONNECTION}")) {
            s.mcp[mcp_done..].to_vec()
        } else {
            Vec::new()
        };
        let done = Indexed { id: s.id.clone(), mcp, at: if s.updated.is_empty() { s.started.clone() } else { s.updated.clone() } };
        for f in lock(&self.sessions.listeners).iter() {
            f(&done);
        }
        Ok(true)
    }

    /// Drop sessions from agents, or session folders, that were switched off or removed in Sources.
    fn purge_disabled(&self, cfg: &Config, log: &dyn Fn(&str)) -> usize {
        let mut roots: HashMap<String, Vec<String>> = HashMap::new();
        for (sid, name, root, _) in cfg.session_sources() {
            if cfg.enabled(&sid) {
                roots.entry(name).or_default().push(format!("{}/", root.to_string_lossy()));
            }
        }
        let rows: Vec<(String, String, String)> = self.db.read(|c| {
            let mut st = c.prepare("SELECT id, source, path FROM sessions")?;
            let r = st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?.unwrap_or_default(),
                                             r.get::<_, Option<String>>(2)?.unwrap_or_default())))?.flatten().collect();
            Ok(r)
        }).unwrap_or_default();
        let off: BTreeSet<String> = rows.iter().map(|r| r.1.clone()).filter(|s| !cfg.enabled(&format!("agents/{s}"))).collect();
        let mut n = 0;
        for src in &off {
            n += self.delete_sessions(&rows.iter().filter(|r| &r.1 == src).map(|r| r.0.clone()).collect::<Vec<_>>());
            log(&format!("removed {src} sessions (source switched off)"));
        }
        let gone: Vec<String> = rows.iter()
            .filter(|(_, src, path)| !off.contains(src) && !path.is_empty()
                && !roots.get(src).is_some_and(|rs| rs.iter().any(|r| path.starts_with(r))))
            .map(|r| r.0.clone()).collect();
        if !gone.is_empty() {
            n += self.delete_sessions(&gone);
            log(&format!("removed {} sessions from session folders that were switched off", gone.len()));
        }
        n
    }

    /// Drop sessions that ran in temp folders (such as Pensieve's own AI calls through an agent's command line).
    fn purge_own(&self, cfg: &Config, log: &dyn Fn(&str)) -> usize {
        let own: Vec<String> = self.db.read(|c| {
            let mut st = c.prepare("SELECT id, project FROM sessions WHERE project LIKE '/tmp/%' OR project LIKE '/private/%' \
                                    OR project LIKE '/var/folders/%'")?;
            let r = st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?.unwrap_or_default())))?
                .flatten().filter(|(_, p)| in_temp(p, cfg)).map(|x| x.0).collect();
            Ok(r)
        }).unwrap_or_default();
        let n = self.delete_sessions(&own);
        if n > 0 {
            log(&format!("removed {n} sessions that ran in temp folders"));
        }
        n
    }

    /// Drop memory files that were deleted: a deleted memory was usually wrong or out of date.
    fn purge_memories(&self, log: &dyn Fn(&str)) -> usize {
        let gone: Vec<String> = self.db.read(|c| {
            let mut st = c.prepare("SELECT id, path FROM sessions WHERE id LIKE 'claude:memory/%'")?;
            let r = st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?.unwrap_or_default())))?
                .flatten().filter(|(_, p)| !Path::new(p).exists()).map(|x| x.0).collect();
            Ok(r)
        }).unwrap_or_default();
        let n = self.delete_sessions(&gone);
        if n > 0 {
            log(&format!("removed {n} deleted memory files"));
        }
        n
    }

    /// Delete sessions with their chunks, files, token rows and vectors. Returns how many.
    pub fn delete_sessions(&self, sids: &[String]) -> usize {
        for part in sids.chunks(500) {
            let q = marks(part.len());
            let res = self.db.tx(|t| {
                let ids: Vec<i64> = t.prepare(&format!("SELECT id FROM chunks WHERE session_id IN ({q})"))?
                    .query_map(params_from_iter(part), |r| r.get(0))?.flatten().collect();
                for table in ["chunks", "session_files", "session_usage", "exchanges", "sessions"] {
                    let col = if table == "sessions" { "id" } else { "session_id" };
                    t.execute(&format!("DELETE FROM {table} WHERE {col} IN ({q})"), params_from_iter(part))?;
                }
                Ok(ids)
            });
            if let Ok(ids) = res {
                self.session_vecs.remove_many(&ids);
            }
        }
        sids.len()
    }

    /// Token rows for the sessions indexed before `session_usage` existed. Once per data folder.
    pub fn backfill_usage(&self, log: &dyn Fn(&str)) -> usize {
        if self.meta(USAGE_BACKFILL).is_some() {
            return 0;
        }
        let t0 = Instant::now();
        let have = self.sessions_with_usage();
        let todo: Vec<(String, String, String)> = self.db.read(|c| {
            let mut st = c.prepare("SELECT id, source, path FROM sessions")?;
            let r = st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?.unwrap_or_default(),
                                             r.get::<_, Option<String>>(2)?.unwrap_or_default())))?
                .flatten().filter(|(sid, _, p)| !have.contains_key(sid) && p.ends_with(".jsonl")).collect();
            Ok(r)
        }).unwrap_or_default();
        let (mut batch, mut n) = (Vec::new(), 0);
        for (sid, src, path) in &todo {
            let usage = tokens::rows(&tokens::scan(src, Path::new(path)));
            if !usage.is_empty() {
                batch.push((sid.clone(), usage));
                n += 1;
            }
            if batch.len() >= 25 {
                let _ = self.write_usage(&std::mem::take(&mut batch));
            }
        }
        let _ = self.write_usage(&batch);
        self.set_meta(USAGE_BACKFILL, &(store::now() as i64).to_string());
        log(&format!("token use of {n} of {} sessions read in {:.1} s", todo.len(), t0.elapsed().as_secs_f64()));
        n
    }

    pub fn sessions_need_layout(&self) -> bool {
        self.db.read(|c| c.query_row("SELECT 1 FROM chunks WHERE x IS NULL LIMIT 1", [], |_| Ok(())).optional())
            .ok().flatten().is_some()
    }

    /// The session map: a full UMAP fit and new topics when the chunks grew or shrank a lot (or on the first run),
    /// else new points placed by their neighbours. Returns whether anything moved. Runs on a background thread
    /// (0.21's map worker).
    pub fn reproject_sessions(&self, force: bool) -> bool {
        let _one = lock(&self.sessions.layout);
        let rows: Vec<(i64, String, Option<[f64; 3]>)> = self.db.read(|c| {
            let mut st = c.prepare("SELECT id, session_id, x, y, z FROM chunks")?;
            let r = st.query_map([], |r| {
                let x: Option<f64> = r.get(2)?;
                let p = match x { Some(x) => Some([x, r.get::<_, Option<f64>>(3)?.unwrap_or(0.0), r.get::<_, Option<f64>>(4)?.unwrap_or(0.0)]), None => None };
                Ok((r.get(0)?, r.get::<_, Option<String>>(1)?.unwrap_or_default(), p))
            })?.flatten().collect();
            Ok(r)
        }).unwrap_or_default();
        let fit_n: f64 = self.meta("session_fit_n").and_then(|v| v.parse().ok()).unwrap_or(0.0);
        let version_ok = self.meta("layout").as_deref() == Some(LAYOUT_VERSION);
        let found = self.session_vecs.take(&rows.iter().map(|r| r.0).collect::<Vec<_>>());
        let (rows, vecs): (Vec<_>, Vec<_>) = rows.into_iter().zip(found).filter_map(|(r, v)| v.map(|v| (r, v))).unzip();
        if rows.len() < 4 {
            return false;
        }
        let x = layout::unit(&vecs);
        let n = rows.len();
        let have: Vec<bool> = rows.iter().map(|r| r.2.is_some()).collect();
        let n_have = have.iter().filter(|h| **h).count();
        let full = force || !version_ok || (n_have as f64) < 0.5 * n as f64 || (n as f64 - fit_n).abs() > (0.15 * fit_n).max(60.0);
        let sessions: Vec<String> = rows.iter().map(|r| r.1.clone()).collect::<BTreeSet<_>>().into_iter().collect();
        let pos: HashMap<&str, usize> = sessions.iter().enumerate().map(|(i, s)| (s.as_str(), i)).collect();
        let dim = x[0].len();
        let mut sums = vec![vec![0f32; dim]; sessions.len()];
        let mut cnt = vec![0usize; sessions.len()];
        for (r, v) in rows.iter().zip(&x) {
            let j = pos[r.1.as_str()];
            cnt[j] += 1;
            for (s, a) in sums[j].iter_mut().zip(v) {
                *s += a;
            }
        }
        let c: Vec<Vec<f32>> = sums.iter().zip(&cnt).map(|(s, &k)| vectors::unit(&s.iter().map(|v| v / k as f32).collect::<Vec<_>>())).collect();
        if full {
            let p = layout::fit3d(&x, 15, 0.1);
            let sp = layout::fit3d(&c, 10, 0.35);
            let lab = layout::cluster(&c, 4, 8);
            let assign: Vec<(String, usize)> = sessions.iter().cloned().zip(lab.iter().copied()).collect();
            let kw = self.topic_keywords(&group(&assign));
            let res = self.db.tx(|t| {
                let mut up = t.prepare("UPDATE chunks SET x=?, y=?, z=? WHERE id=?")?;
                for (q, r) in p.iter().zip(&rows) {
                    up.execute(params![q[0] as f64, q[1] as f64, q[2] as f64, r.0])?;
                }
                let mut up = t.prepare("UPDATE sessions SET x=?, y=?, z=?, cluster=? WHERE id=?")?;
                for ((q, l), s) in sp.iter().zip(&lab).zip(&sessions) {
                    up.execute(params![q[0] as f64, q[1] as f64, q[2] as f64, *l as i64, s])?;
                }
                save_topics(t, &assign, &kw)?;
                crate::db::set_meta(t, "session_fit_n", &n.to_string())?;
                crate::db::set_meta(t, "layout", LAYOUT_VERSION)
            });
            return res.is_ok();
        }
        if n_have < n {
            let known: Vec<Vec<f32>> = x.iter().zip(&have).filter(|(_, h)| **h).map(|(v, _)| v.clone()).collect();
            let p_old: Vec<layout::P3> = rows.iter().filter_map(|r| r.2.map(|p| [p[0] as f32, p[1] as f32, p[2] as f32])).collect();
            let new: Vec<Vec<f32>> = x.iter().zip(&have).filter(|(_, h)| !**h).map(|(v, _)| v.clone()).collect();
            let p_new = layout::place_new(&known, &p_old, &new, 4, 0.03);
            let ids: Vec<i64> = rows.iter().zip(&have).filter(|(_, h)| !**h).map(|(r, _)| r.0).collect();
            let _ = self.db.tx(|t| {
                let mut up = t.prepare("UPDATE chunks SET x=?, y=?, z=? WHERE id=?")?;
                for (q, id) in p_new.iter().zip(&ids) {
                    up.execute(params![q[0] as f64, q[1] as f64, q[2] as f64, id])?;
                }
                Ok(())
            });
        }
        let placed: HashMap<String, ([f32; 3], Option<i64>)> = self.db.read(|cn| {
            let mut st = cn.prepare("SELECT id, x, y, z, cluster FROM sessions WHERE x IS NOT NULL")?;
            let r = st.query_map([], |r| Ok((r.get::<_, String>(0)?, ([r.get::<_, f64>(1)? as f32, r.get::<_, Option<f64>>(2)?.unwrap_or(0.0) as f32,
                                                                       r.get::<_, Option<f64>>(3)?.unwrap_or(0.0) as f32], r.get(4)?))))?
                .flatten().collect();
            Ok(r)
        }).unwrap_or_default();
        let missing: Vec<usize> = (0..sessions.len()).filter(|&i| !placed.contains_key(&sessions[i])).collect();
        if !missing.is_empty() && !placed.is_empty() {
            let known: Vec<usize> = (0..sessions.len()).filter(|&i| placed.contains_key(&sessions[i])).collect();
            let ck: Vec<Vec<f32>> = known.iter().map(|&i| c[i].clone()).collect();
            let pk: Vec<layout::P3> = known.iter().map(|&i| placed[&sessions[i]].0).collect();
            let cm: Vec<Vec<f32>> = missing.iter().map(|&i| c[i].clone()).collect();
            let pn = layout::place_new(&ck, &pk, &cm, 3, 0.05);
            let near = layout::nearest(&cm, &ck);
            let _ = self.db.tx(|t| {
                for (j, &i) in missing.iter().enumerate() {
                    let cl = placed[&sessions[known[near[j]]]].1;
                    t.execute("UPDATE sessions SET x=?, y=?, z=?, cluster=? WHERE id=?",
                              params![pn[j][0] as f64, pn[j][1] as f64, pn[j][2] as f64, cl, sessions[i]])?;
                }
                Ok(())
            });
        }
        !missing.is_empty() || n_have < n
    }

    /// Topic id -> its top TF-IDF terms over its sessions' titles, tags and summaries.
    fn topic_keywords(&self, by: &BTreeMap<usize, Vec<String>>) -> HashMap<usize, String> {
        let info: HashMap<String, (Option<String>, Option<String>, Option<String>)> = self.db.read(|c| {
            let mut st = c.prepare("SELECT id, title, tags, summary FROM sessions")?;
            let r = st.query_map([], |r| Ok((r.get(0)?, (r.get(1)?, r.get(2)?, r.get(3)?))))?.flatten().collect();
            Ok(r)
        }).unwrap_or_default();
        let ids: Vec<usize> = by.keys().copied().collect();
        let docs: Vec<String> = ids.iter().map(|c| by[c].iter().filter_map(|s| info.get(s)).map(|(t, g, m)| {
            format!("{} {} {}", t.as_deref().unwrap_or("None"), g.as_deref().unwrap_or(""), m.as_deref().unwrap_or(""))
        }).collect::<Vec<_>>().join(" ")).collect();
        ids.into_iter().zip(keywords(&docs)).collect()
    }

    pub fn session_topics(&self) -> Vec<Value> {
        self.db.read(|c| {
            let mut st = c.prepare("SELECT id, name, description, keywords, members FROM topics ORDER BY id")?;
            let r = st.query_map([], |r| {
                let (id, name, desc, kw, mem): (i64, Option<String>, Option<String>, Option<String>, Option<String>) =
                    (r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?);
                let kw = kw.unwrap_or_default();
                let sessions: Value = serde_json::from_str(mem.as_deref().unwrap_or("[]")).unwrap_or(json!([]));
                Ok(json!({"id": id, "name": topic_name(name.as_deref(), &kw, id), "named": name.as_deref().is_some_and(|n| !n.is_empty()),
                          "description": desc.unwrap_or_default(), "keywords": kw, "sessions": sessions}))
            })?.flatten().collect();
            Ok(r)
        }).unwrap_or_default()
    }

    /// Topics clustered again within some sessions (one repo, say). A topic keeps the name the text engine gave
    /// the same members before (meta `tname:<hash>`); an unnamed one has a keyword name and `named: false`.
    pub fn scoped_topics(&self, sids: &[String]) -> Vec<Value> {
        let (ids, c) = self.session_centroids();
        let want: HashSet<&str> = sids.iter().map(String::as_str).collect();
        let idx: Vec<usize> = (0..ids.len()).filter(|&i| want.contains(ids[i].as_str())).collect();
        if idx.is_empty() {
            return Vec::new();
        }
        let sub: Vec<Vec<f32>> = idx.iter().map(|&i| c[i].clone()).collect();
        let lab = if sub.len() >= 6 { layout::cluster(&sub, 2, 6) } else { vec![0; sub.len()] };
        let assign: Vec<(String, usize)> = idx.iter().map(|&i| ids[i].clone()).zip(lab).collect();
        let by = group(&assign);
        let kw = self.topic_keywords(&by);
        by.iter().map(|(c, mem)| {
            let mut mem = mem.clone();
            mem.sort();
            let key = format!("tname:{}", &sha1_hex(&mem.join(","))[..16]);
            let named: Option<Value> = self.meta(&key).and_then(|v| serde_json::from_str(&v).ok()).filter(|v: &Value| !v.is_null());
            let k = kw.get(c).cloned().unwrap_or_default();
            let fallback = {
                let f = title_case(k.split(',').next().unwrap_or("").trim());
                if f.is_empty() { format!("Topic {}", c + 1) } else { f }
            };
            json!({"id": c, "key": key, "named": named.is_some(), "keywords": k, "sessions": mem,
                   "name": named.as_ref().and_then(|n| n.get("name").cloned()).unwrap_or(json!(fallback)),
                   "description": named.as_ref().and_then(|n| n.get("description").cloned()).unwrap_or(json!(""))})
        }).collect()
    }

    /// The map's points: one per session (`level` "session") or per chunk ("chunk").
    pub fn session_points(&self, level: &str) -> Value {
        let pts: Vec<Value> = self.db.read(|c| {
            if level == "chunk" {
                let mut st = c.prepare(
                    "SELECT c.id, c.session_id, s.title, s.source, s.cluster, c.x, c.y, c.z, s.project_name, c.summary, \
                     s.started, c.idx FROM chunks c JOIN sessions s ON s.id=c.session_id WHERE c.x IS NOT NULL")?;
                let r = st.query_map([], |r| Ok(json!({
                    "id": r.get::<_, i64>(0)?, "session": r.get::<_, Option<String>>(1)?, "title": r.get::<_, Option<String>>(2)?,
                    "source": r.get::<_, Option<String>>(3)?, "cluster": r.get::<_, Option<i64>>(4)?,
                    "p": [r.get::<_, Option<f64>>(5)?, r.get::<_, Option<f64>>(6)?, r.get::<_, Option<f64>>(7)?],
                    "project": r.get::<_, Option<String>>(8)?, "summary": r.get::<_, Option<String>>(9)?,
                    "started": r.get::<_, Option<String>>(10)?, "idx": r.get::<_, Option<i64>>(11)?})))?.flatten().collect();
                return Ok(r);
            }
            let mut st = c.prepare(
                "SELECT id, title, source, project_name, cluster, started, updated, n_chunks, x, y, z, summary, tags \
                 FROM sessions WHERE x IS NOT NULL AND n_chunks>0")?;
            let r = st.query_map([], |r| Ok(json!({
                "id": r.get::<_, String>(0)?, "session": r.get::<_, String>(0)?, "title": r.get::<_, Option<String>>(1)?,
                "source": r.get::<_, Option<String>>(2)?, "project": r.get::<_, Option<String>>(3)?,
                "cluster": r.get::<_, Option<i64>>(4)?, "started": r.get::<_, Option<String>>(5)?,
                "updated": r.get::<_, Option<String>>(6)?, "n": r.get::<_, Option<i64>>(7)?,
                "p": [r.get::<_, Option<f64>>(8)?, r.get::<_, Option<f64>>(9)?, r.get::<_, Option<f64>>(10)?],
                "summary": r.get::<_, Option<String>>(11)?, "tags": r.get::<_, Option<String>>(12)?})))?.flatten().collect();
            Ok(r)
        }).unwrap_or_default();
        json!({"level": level, "points": pts})
    }

    /// The chunk ids that pass the filters, or None when there are none.
    fn session_allow(&self, f: &Filter) -> Option<Vec<i64>> {
        if f.empty() {
            return None;
        }
        let mut sql = "SELECT c.id FROM chunks c JOIN sessions s ON s.id=c.session_id WHERE 1=1".to_string();
        let mut args: Vec<String> = Vec::new();
        if let Some(sid) = &f.session_id {
            sql += " AND c.session_id=?";
            args.push(sid.clone());
        }
        if let Some(ids) = &f.session_ids {
            sql += &format!(" AND c.session_id IN ({})", marks(ids.len()));
            args.extend(ids.iter().cloned());
        }
        if !f.projects.is_empty() {
            sql += &format!(" AND s.project_name IN ({})", marks(f.projects.len()));
            args.extend(f.projects.iter().cloned());
        }
        if !f.sources.is_empty() {
            sql += &format!(" AND s.source IN ({})", marks(f.sources.len()));
            args.extend(f.sources.iter().cloned());
        }
        if let Some(s) = &f.since {
            sql += " AND s.updated >= ?";
            args.push(s.clone());
        }
        if let Some(u) = &f.until {
            sql += " AND s.started <= ?";
            args.push(u.clone());
        }
        let ids: Vec<i64> = self.db.read(|c| {
            let r = c.prepare(&sql)?.query_map(params_from_iter(&args), |r| r.get(0))?.flatten().collect();
            Ok(r)
        }).unwrap_or_default();
        Some(ids.into_iter().filter(|i| self.session_vecs.contains(*i)).collect())
    }

    /// The `k` session chunks closest to the query by meaning, within the filters.
    pub fn search_sessions(&self, query: &str, k: usize, f: &Filter) -> Vec<Value> {
        let q = self.embed_query(query);
        let allow = self.session_allow(f);
        if allow.as_ref().is_some_and(|a| a.is_empty()) || self.session_vecs.is_empty() {
            return Vec::new();
        }
        let hits = self.session_vecs.search(&q, k, allow.as_deref());
        self.db.read(|c| {
            let mut st = c.prepare(
                "SELECT c.id, c.session_id, c.idx, c.text, s.title, s.source, s.project_name, c.summary, s.started \
                 FROM chunks c JOIN sessions s ON s.id=c.session_id WHERE c.id=?")?;
            let mut out = Vec::new();
            for (cid, sc) in &hits {
                let r = st.query_row([cid], |r| Ok(json!({
                    "chunk": r.get::<_, i64>(0)?, "session": r.get::<_, Option<String>>(1)?, "idx": r.get::<_, Option<i64>>(2)?,
                    "text": r.get::<_, Option<String>>(3)?, "title": r.get::<_, Option<String>>(4)?,
                    "source": r.get::<_, Option<String>>(5)?, "project": r.get::<_, Option<String>>(6)?,
                    "summary": r.get::<_, Option<String>>(7)?, "started": r.get::<_, Option<String>>(8)?, "score": *sc as f64}))).optional()?;
                out.extend(r);
            }
            Ok(out)
        }).unwrap_or_default()
    }

    /// Session ids and the unit mean vector of each session's chunks.
    pub fn session_centroids(&self) -> (Vec<String>, Vec<Vec<f32>>) {
        let rows: Vec<(i64, String)> = self.db.read(|c| {
            let r = c.prepare("SELECT id, session_id FROM chunks")?
                .query_map([], |r| Ok((r.get(0)?, r.get::<_, Option<String>>(1)?.unwrap_or_default())))?.flatten().collect();
            Ok(r)
        }).unwrap_or_default();
        let mut order: Vec<String> = Vec::new();
        let mut by: HashMap<String, Vec<i64>> = HashMap::new();
        for (cid, s) in rows {
            by.entry(s.clone()).or_insert_with(|| {
                order.push(s);
                Vec::new()
            }).push(cid);
        }
        let groups: Vec<(String, Vec<i64>)> = order.into_iter().map(|s| { let v = by.remove(&s).unwrap_or_default(); (s, v) }).collect();
        vectors::centroids(&groups, &self.session_vecs)
    }

    /// The `k` sessions most like one session, by the mean of their chunks.
    pub fn similar_sessions(&self, sid: &str, k: usize) -> Vec<Value> {
        let (ids, c) = self.session_centroids();
        let Some(i) = ids.iter().position(|s| s == sid) else { return Vec::new() };
        let mut sims: Vec<(usize, f32)> = c.iter().enumerate().map(|(j, v)| (j, vectors::dot(v, &c[i]))).collect();
        sims.sort_by(|a, b| b.1.total_cmp(&a.1));
        self.db.read(|cn| {
            let mut st = cn.prepare("SELECT title, project_name, source, started, summary FROM sessions WHERE id=?")?;
            let mut out = Vec::new();
            for &(j, sc) in sims.iter().skip(1).take(k) {
                let r = st.query_row([&ids[j]], |r| Ok(json!({
                    "id": ids[j], "title": r.get::<_, Option<String>>(0)?, "project": r.get::<_, Option<String>>(1)?,
                    "source": r.get::<_, Option<String>>(2)?, "started": r.get::<_, Option<String>>(3)?,
                    "summary": r.get::<_, Option<String>>(4)?, "score": sc as f64}))).optional()?;
                out.extend(r);
            }
            Ok(out)
        }).unwrap_or_default()
    }

    /// One session with its turns, read again from its transcript; None when the index has no such session or the
    /// transcript cannot be read.
    pub fn session(&self, sid: &str) -> Option<Value> {
        type Row = (String, Option<String>, Option<String>, Option<String>, Option<String>, Option<String>,
                    Option<String>, Option<String>, Option<String>, Option<i64>, Option<String>);
        let r: Row = self.db.read(|c| c.query_row(
            "SELECT id,source,project_name,title,path,started,updated,summary,tags,cluster,project FROM sessions WHERE id=?",
            [sid], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?,
                           r.get(8)?, r.get(9)?, r.get(10)?))).optional()).ok().flatten()?;
        let s = parsers::parse(r.1.as_deref().unwrap_or(""), Path::new(r.4.as_deref().unwrap_or(""))).ok()?;
        let turns: Vec<Value> = s.turns.into_iter().map(|(role, text)| json!({"role": role, "text": text})).collect();
        Some(json!({"id": r.0, "source": r.1, "project": r.2, "title": r.3, "started": r.5, "updated": r.6,
                    "summary": r.7, "tags": r.8, "cluster": r.9, "cwd": r.10, "turns": turns}))
    }

    /// GET /api/points. `session_scope`: session id -> {project, dirs} from the repo index (0.21
    /// `repos.session_scope()`): a session in a repo shows under the repo's name, with its folder kept in `folder`.
    pub fn api_points(&self, level: &str, session_scope: &HashMap<String, Value>) -> Value {
        let mut res = self.session_points(level);
        if let Some(pts) = res.get_mut("points").and_then(Value::as_array_mut) {
            for p in pts {
                let Some(v) = p.get("session").and_then(Value::as_str).and_then(|s| session_scope.get(s)).cloned() else { continue };
                if v.is_null() || v.as_object().is_some_and(|o| o.is_empty()) {
                    continue;
                }
                let folder = p.get("project").cloned().unwrap_or(Value::Null);
                p["folder"] = folder;
                p["project"] = v.get("project").cloned().unwrap_or(Value::Null);
                if level == "session" {
                    p["dirs"] = v.get("dirs").cloned().unwrap_or(Value::Null);
                }
            }
        }
        res
    }

    /// GET /api/topics.
    pub fn api_topics(&self) -> Value {
        Value::Array(self.session_topics())
    }

    /// POST /api/topics/scoped. The app names the topics that come back with `named: false` (text engine) and
    /// stores each name under its `key` in meta.
    pub fn api_topics_scoped(&self, session_ids: &[String]) -> Value {
        Value::Array(self.scoped_topics(session_ids))
    }

    /// GET /api/session/{sid}: Null when there is no such session (0.21 answered 404).
    pub fn api_session(&self, sid: &str) -> Value {
        self.session(sid).unwrap_or(Value::Null)
    }

    /// GET /api/similar/{sid}.
    pub fn api_similar(&self, sid: &str) -> Value {
        Value::Array(self.similar_sessions(sid, 6))
    }

    /// GET /api/search?q&k&projects&sources&since&until (comma lists; empty means no filter).
    pub fn api_search(&self, q: &str, k: usize, projects: &str, sources: &str, since: &str, until: &str) -> Value {
        let csv = |v: &str| v.split(',').filter(|x| !x.is_empty()).map(String::from).collect::<Vec<_>>();
        let f = Filter {
            projects: csv(projects),
            sources: csv(sources),
            since: Some(since.to_string()).filter(|s| !s.is_empty()),
            until: Some(until.to_string()).filter(|s| !s.is_empty()),
            ..Default::default()
        };
        Value::Array(self.search_sessions(q, k, &f))
    }
}

/// Members of each cluster, in cluster order.
fn group(assign: &[(String, usize)]) -> BTreeMap<usize, Vec<String>> {
    let mut by: BTreeMap<usize, Vec<String>> = BTreeMap::new();
    for (s, c) in assign {
        by.entry(*c).or_default().push(s.clone());
    }
    by
}

/// Store topic membership; a topic that mostly matches an earlier one (Jaccard 0.5 or more) keeps its name.
fn save_topics(t: &rusqlite::Transaction, assign: &[(String, usize)], kw: &HashMap<usize, String>) -> rusqlite::Result<()> {
    let old: Vec<(HashSet<String>, Option<String>, Option<String>)> = t
        .prepare("SELECT members, name, description FROM topics")?
        .query_map([], |r| Ok((r.get::<_, Option<String>>(0)?, r.get(1)?, r.get(2)?)))?
        .flatten()
        .map(|(m, n, d)| (serde_json::from_str::<Vec<String>>(m.as_deref().unwrap_or("[]")).unwrap_or_default().into_iter().collect(), n, d))
        .collect();
    t.execute("DELETE FROM topics", [])?;
    for (c, mem) in group(assign) {
        let mem: HashSet<String> = mem.into_iter().collect();
        let jac = |o: &HashSet<String>| mem.intersection(o).count() as f64 / mem.union(o).count().max(1) as f64;
        let mut best: Option<&(HashSet<String>, Option<String>, Option<String>)> = None;
        for o in &old {
            if best.is_none_or(|b| jac(&o.0) > jac(&b.0)) {
                best = Some(o);
            }
        }
        let (name, desc) = match best {
            Some(b) if jac(&b.0) >= 0.5 => (b.1.clone(), b.2.clone()),
            _ => (None, None),
        };
        let mut sorted: Vec<String> = mem.into_iter().collect();
        sorted.sort();
        t.execute("INSERT INTO topics VALUES(?,?,?,?,?)",
                  params![c as i64, name, desc, kw.get(&c).cloned().unwrap_or_default(), py_list(&sorted)])?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn t(role: &str, text: &str) -> (String, String) {
        (role.into(), text.into())
    }

    #[test]
    fn globs_as_0_21() {
        assert!(glob_match("proj/abc.jsonl", "*/*.jsonl"));
        assert!(!glob_match("proj/sub/abc.jsonl", "*/*.jsonl"));
        assert!(glob_match("2026/01/02/rollout-x.jsonl", "**/rollout-*.jsonl"));
        assert!(glob_match("rollout-x.jsonl", "**/rollout-*.jsonl"));
        assert!(glob_match("p/chats/a.jsonl", "*/chats/*.jsonl"));
    }

    #[test]
    fn chunks_exchanges_on_paragraphs() {
        let long = "x".repeat(CHUNK_CHARS + 10);
        let c = chunk_session(&[t("user", "hi"), t("assistant", "a\n\nb"), t("user", &long), t("memory", "m")]);
        assert_eq!(c[0], "User: hi\n\nAssistant: a\n\nb");
        assert_eq!(c[1].chars().count(), CHUNK_CHARS);
        assert_eq!(c.len(), 3);
        assert!(c[2].ends_with("\n\nMemory: m"));
    }

    #[test]
    fn keywords_pick_distinct_terms() {
        let k = keywords(&["parser parser lexer the user".into(), "deploy cluster cluster helm".into()]);
        assert!(k[0].starts_with("parser, lexer"));
        assert!(k[1].starts_with("cluster"));
        assert_eq!(title_case("rate-limit don't"), "Rate-Limit Don'T");
    }

    #[test]
    fn sweeps_transcript_folders_and_defers_growing_ones() {
        let home = std::env::temp_dir().join(format!("sessions-sweep-{}", std::process::id()));
        let proj = home.join(".claude/projects/-work-app");
        std::fs::create_dir_all(proj.join("memory")).unwrap();
        let line = |t: &str, text: &str| format!(r#"{{"type":"{t}","cwd":"/work/app","timestamp":"2026-01-01T00:00:00Z","message":{{"content":"{text}"}}}}"#);
        std::fs::write(proj.join("a.jsonl"), format!("{}\n", line("user", "first prompt"))).unwrap();
        std::fs::write(proj.join("memory/MEMORY.md"), "index").unwrap();
        std::fs::write(proj.join("memory/style.md"), "---\nname: style\ndescription: short answers\n---\nKeep it short.").unwrap();
        let ix = Index::memory(std::sync::Arc::new(crate::testing::Words(64)), Config::new(json!({}), home.clone())).unwrap();
        assert_eq!(ix.sync_sessions(None, None, &|_| {}), 2);
        let m = ix.api_session("claude:memory/-work-app/style");
        assert_eq!(m["title"], "Memory: short answers");
        std::fs::write(proj.join("a.jsonl"), format!("{}\n{}\n", line("user", "first prompt"), line("user", "second"))).unwrap();
        assert_eq!(ix.sync_sessions(None, None, &|_| {}), 0);
        assert!(ix.sessions.deferred.load(Ordering::Relaxed));
        lock(&ix.sessions.inner).indexed_at.clear();
        let p = proj.join("a.jsonl").to_string_lossy().into_owned();
        assert_eq!(ix.sync_session_paths(&[p], &|_| {}), 1);
        assert_eq!(ix.api_session("claude:a")["turns"].as_array().map(|a| a.len()), Some(2));
        let _ = std::fs::remove_dir_all(home);
    }

    #[test]
    fn indexes_searches_and_deletes_a_session() {
        let ix = crate::testing::index();
        let dir = std::env::temp_dir().join(format!("sessions-test-{}", std::process::id()));
        let proj = dir.join("proj");
        std::fs::create_dir_all(&proj).unwrap();
        let p = proj.join("s1.jsonl");
        std::fs::write(&p, concat!(
            r#"{"type":"user","cwd":"/work/app","timestamp":"2026-01-01T00:00:00Z","message":{"content":"rotate the signing keys"}}"#, "\n",
            r#"{"type":"assistant","timestamp":"2026-01-01T00:01:00Z","message":{"content":[{"type":"text","text":"keys rotated"}]}}"#, "\n")).unwrap();
        let cfg = ix.config();
        let stat = file_stat(&p).unwrap();
        assert!(ix.index_one(&cfg, "claude", &p, stat, &|_| {}));
        let hits = ix.search_sessions("signing keys", 3, &Filter::default());
        assert_eq!(hits[0]["session"], "claude:s1");
        assert_eq!(ix.api_session("claude:s1")["turns"].as_array().map(|a| a.len()), Some(2));
        assert_eq!(ix.delete_sessions(&["claude:s1".into()]), 1);
        assert!(ix.session_vecs.is_empty());
        let _ = std::fs::remove_dir_all(dir);
    }
}
