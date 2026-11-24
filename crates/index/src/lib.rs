//! Developer mode's index: chunks of agent sessions, files, code and collected documents in index.db, their word
//! indexes and vectors, the maps, the file watch, and search over all of it.
//!
//! The app opens one `Index` with an embedder (`Embed`) and the settings (`Config`). Indexing runs on background
//! threads that the app starts; a search reads SQLite through its own read-only connection and the vectors under a
//! read lock, so it never waits for indexing. Each REST route and MCP tool that this crate backs is a method named
//! `api_*` or `mcp_*` that returns the JSON that 0.21 returned.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};

use serde_json::{Value, json};
use store::Db;
use store::rusqlite;

pub mod db;
pub mod layout;
pub mod text;
pub mod vectors;

pub mod digest;
pub mod parsers;
pub mod segment;
pub mod sessions;
pub mod thoughts;
pub mod tokens;

pub mod docindex;
pub mod extract;
pub mod files;
pub mod fileuse;
pub mod places;
pub mod watch;

pub mod repos;

pub mod actions;
pub mod scopes;
pub mod search;

pub use vectors::VecIndex;

/// The embedding model, which the app plugs in.
pub trait Embed: Send + Sync {
    /// One unit vector per text. `query`: the texts are search queries, and the model adds its own query
    /// instruction. Else each text is a document exactly as the model reads it: callers format it with `doc_text`.
    fn embed(&self, texts: &[&str], query: bool) -> Vec<Vec<f32>>;
    fn dim(&self) -> usize;
    /// A vector for an image file, in the space of the text vectors. None when the model has no image part or the
    /// file cannot be decoded.
    fn embed_image(&self, _path: &Path) -> Option<Vec<f32>> {
        None
    }
    /// A video or sound file as (start, seconds, "video" | "audio", vector), one per window.
    fn embed_media(&self, _path: &Path, _video: bool, _sound: bool) -> Vec<(f64, f64, String, Vec<f32>)> {
        Vec::new()
    }
}

/// A document as EmbeddingGemma reads it, with its title (a path) when it has one.
pub fn doc_text(text: &str, title: Option<&str>) -> String {
    format!("title: {} | text: {text}", title.filter(|t| !t.is_empty()).unwrap_or("none"))
}

/// Texts per model call while indexing: a search waits for one slice at most.
pub const BULK_SLICE: usize = 8;
pub const EMBED_DIM: usize = 768;
pub const CHUNK_CHARS: usize = 1800;

/// The settings this crate reads (0.21's settings.json, with its defaults for missing keys), and facts that the app
/// knows and this crate does not.
#[derive(Debug, Clone)]
pub struct Config {
    pub settings: Value,
    pub home: PathBuf,
    /// Connection ids that Pensieve holds documents from: such a connection counts as on (settings.enabled).
    pub connections_with_data: HashSet<String>,
}

pub fn defaults() -> Value {
    json!({
        "folders": ["~/Documents", "~/Desktop", "~/Downloads"],
        "sweep_roots": ["~"],
        "sweep_depth": 4,
        "max_repo_files": 5000,
        "repos": [],
        "exclude": ["node_modules", ".venv", "venv", "__pycache__", "Library", ".Trash", "dist", "build", "target",
                    ".cache", "Applications", "Pictures", "Music", "Movies"],
        "exclude_files": ["*emergency kit*", "*recovery*code*", "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx",
                          "*.keychain*", ".env*", "*credential*", "*secret*", "*password*", "id_rsa*", "id_ed25519*",
                          "*.ovpn"],
        "disabled": [],
        "max_file_mb": 20,
        // Images, video and sound that are switched on are read by content only when in use (crate::fileuse).
        "media_by_use": true,
        "file_types": {"text": {"index": "content", "map": true}, "pdf": {"index": "content", "map": true},
                       "office": {"index": "content", "map": true}, "data": {"index": "content", "map": true},
                       "code": {"index": "content", "map": true}, "images": {"index": "content", "map": true},
                       "video": {"index": "content", "map": true}, "audio": {"index": "content", "map": true},
                       "other": {"index": "name", "map": false}},
        "off_map": [],
        "chunk_chars": 1500,
        "max_chunks": 2000,
        "skip_temp_sessions": true,
        "session_folders": [],
        "scopes": {},
        "default_scope": "",
        "editor": "default",
        "terminal": "Terminal",
        "connections_on": [],
        "mode": "session"
    })
}

pub const SESSION_GLOB: [(&str, &str); 3] = [("claude", "*/*.jsonl"), ("qwen", "*/chats/*.jsonl"), ("codex", "**/rollout-*.jsonl")];

impl Config {
    /// `settings` over 0.21's defaults.
    pub fn new(settings: Value, home: PathBuf) -> Config {
        let mut s = defaults();
        if let (Some(base), Value::Object(over)) = (s.as_object_mut(), settings) {
            for (k, v) in over {
                base.insert(k, v);
            }
        }
        Config { settings: s, home, connections_with_data: HashSet::new() }
    }

    pub fn get(&self, key: &str) -> &Value {
        self.settings.get(key).unwrap_or(&Value::Null)
    }

    pub fn strs(&self, key: &str) -> Vec<String> {
        self.get(key).as_array().map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect()).unwrap_or_default()
    }

    pub fn int(&self, key: &str) -> i64 {
        self.get(key).as_i64().or_else(|| self.get(key).as_f64().map(|f| f as i64)).unwrap_or(0)
    }

    pub fn flag(&self, key: &str) -> bool {
        self.get(key).as_bool().unwrap_or(false)
    }

    /// A source is on unless it, or its category, is in `disabled`. A connection is on once it is in
    /// `connections_on` or Pensieve holds data from it.
    pub fn enabled(&self, source_id: &str) -> bool {
        let off = self.strs("disabled");
        let cat = source_id.split('/').next().unwrap_or("");
        if off.iter().any(|d| d == source_id || d == cat) {
            return false;
        }
        match source_id.split_once('/') {
            Some(("connections", sid)) if !sid.is_empty() => {
                self.strs("connections_on").iter().any(|c| c == source_id) || self.connections_with_data.contains(sid)
            }
            _ => true,
        }
    }

    /// Shown on the map unless it, or its category, is in `off_map`.
    pub fn on_map(&self, source_id: &str) -> bool {
        let off = self.strs("off_map");
        let cat = source_id.split('/').next().unwrap_or("");
        !off.iter().any(|d| d == source_id || d == cat)
    }

    /// `~/x` as an absolute path.
    pub fn expand(&self, p: &str) -> PathBuf {
        match p.strip_prefix("~") {
            Some(rest) => self.home.join(rest.trim_start_matches('/')),
            None => PathBuf::from(p),
        }
    }

    pub fn developer(&self) -> bool {
        self.get("mode").as_str() == Some("developer")
    }

    /// (source id, agent, folder, glob) of each folder of agent transcripts: the default ones and the ones added in
    /// Sources (`session_folders`, "agent:path").
    pub fn session_sources(&self) -> Vec<(String, String, PathBuf, String)> {
        let glob = |name: &str| SESSION_GLOB.iter().find(|(n, _)| *n == name).map(|(_, g)| g.to_string());
        let mut out: Vec<(String, String, PathBuf, String)> = [
            ("claude", self.home.join(".claude/projects")),
            ("qwen", self.home.join(".qwen/projects")),
            ("codex", self.home.join(".codex/sessions")),
        ]
        .into_iter()
        .map(|(n, root)| (format!("agents/{n}"), n.to_string(), root, glob(n).unwrap_or_default()))
        .collect();
        for entry in self.strs("session_folders") {
            if let Some((name, p)) = entry.split_once(':') {
                if let (Some(g), false) = (glob(name), p.is_empty()) {
                    out.push((format!("agents/{name}@{p}"), name.to_string(), self.expand(p), g));
                }
            }
        }
        out
    }
}

/// index.db, the vectors of each kind of chunk, and the state of each indexer.
pub struct Index {
    pub db: Db,
    pub embedder: Arc<dyn Embed>,
    cfg: RwLock<Arc<Config>>,
    pub session_vecs: VecIndex,
    pub code_vecs: VecIndex,
    pub file_vecs: VecIndex,
    pub doc_vecs: VecIndex,
    pub sessions: sessions::State,
    pub repos: repos::State,
    pub files: files::State,
    pub file_use: fileuse::State,
    pub docs: docindex::State,
    pub search: search::State,
    pub thoughts: thoughts::State,
    pub watch: watch::State,
}

impl Index {
    /// Open (or make) index.db at `path` and load every vector into memory.
    pub fn open(path: &Path, embedder: Arc<dyn Embed>, cfg: Config) -> rusqlite::Result<Index> {
        let db = Db::open(path, db::SCHEMA, db::VERSION)?;
        Self::with_db(db, embedder, cfg)
    }

    /// An index in memory, for tests.
    pub fn memory(embedder: Arc<dyn Embed>, cfg: Config) -> rusqlite::Result<Index> {
        let db = Db::memory("")?;
        db::functions(&db.write())?;
        db.write().execute_batch(db::SCHEMA)?;
        Self::with_db(db, embedder, cfg)
    }

    fn with_db(db: Db, embedder: Arc<dyn Embed>, cfg: Config) -> rusqlite::Result<Index> {
        db::functions(&db.write())?;
        let dim = embedder.dim();
        let ix = Index {
            db,
            embedder,
            cfg: RwLock::new(Arc::new(cfg)),
            session_vecs: VecIndex::new(dim),
            code_vecs: VecIndex::new(dim),
            file_vecs: VecIndex::new(dim),
            doc_vecs: VecIndex::new(dim),
            sessions: Default::default(),
            repos: Default::default(),
            files: Default::default(),
            file_use: Default::default(),
            docs: Default::default(),
            search: Default::default(),
            thoughts: Default::default(),
            watch: Default::default(),
        };
        ix.db.read(|c| {
            ix.session_vecs.load(c, "chunks")?;
            ix.code_vecs.load(c, "code_chunks")?;
            ix.file_vecs.load(c, "file_chunks")?;
            ix.doc_vecs.load(c, "doc_chunks")
        })?;
        Ok(ix)
    }

    pub fn config(&self) -> Arc<Config> {
        self.cfg.read().unwrap_or_else(|e| e.into_inner()).clone()
    }

    /// New settings from the app (after the user changed them).
    pub fn set_config(&self, cfg: Config) {
        *self.cfg.write().unwrap_or_else(|e| e.into_inner()) = Arc::new(cfg);
    }

    pub fn meta(&self, key: &str) -> Option<String> {
        self.db.read(|c| Ok(db::meta(c, key))).ok().flatten()
    }

    pub fn set_meta(&self, key: &str, value: &str) {
        let _ = db::set_meta(&self.db.write(), key, value);
    }

    /// Vectors for documents (indexing). Each text with its title as the model reads it (`doc_text`), BULK_SLICE
    /// texts per model call, and a text that comes twice is embedded once.
    pub fn embed_docs(&self, texts: &[String], titles: Option<&[String]>) -> Vec<Vec<f32>> {
        let model: Vec<String> = texts.iter().enumerate()
            .map(|(i, t)| doc_text(t, titles.and_then(|ts| ts.get(i)).map(|s| s.as_str())))
            .collect();
        let mut first: std::collections::HashMap<&str, usize> = Default::default();
        let mut todo = Vec::new();
        for (i, t) in model.iter().enumerate() {
            if !first.contains_key(t.as_str()) {
                first.insert(t, i);
                todo.push(i);
            }
        }
        todo.sort_by_key(|&i| std::cmp::Reverse(model[i].len()));
        let mut out = vec![Vec::new(); model.len()];
        for part in todo.chunks(BULK_SLICE) {
            let batch: Vec<&str> = part.iter().map(|&i| model[i].as_str()).collect();
            for (&i, v) in part.iter().zip(self.embedder.embed(&batch, false)) {
                out[i] = vectors::unit(&v);
            }
        }
        for i in 0..model.len() {
            let j = first[model[i].as_str()];
            if j != i {
                out[i] = out[j].clone();
            }
        }
        out
    }

    /// A search query's vector, under the model's query instruction.
    pub fn embed_query(&self, q: &str) -> Vec<f32> {
        self.embedder.embed(&[q], true).into_iter().next().map(|v| vectors::unit(&v)).unwrap_or_default()
    }
}

#[cfg(test)]
pub mod testing {
    //! An embedder for tests: a bag of hashed words, so that texts that share words are near.
    use super::*;

    pub struct Words(pub usize);

    impl Embed for Words {
        fn embed(&self, texts: &[&str], _query: bool) -> Vec<Vec<f32>> {
            texts.iter().map(|t| {
                let mut v = vec![0f32; self.0];
                for w in t.to_lowercase().split(|c: char| !c.is_alphanumeric()).filter(|w| w.len() > 2) {
                    let h = w.bytes().fold(5381u64, |h, b| h.wrapping_mul(33) ^ b as u64);
                    v[(h % self.0 as u64) as usize] += 1.0;
                }
                vectors::unit(&v)
            }).collect()
        }
        fn dim(&self) -> usize {
            self.0
        }
    }

    pub fn index() -> Index {
        Index::memory(Arc::new(Words(64)), Config::new(json!({}), PathBuf::from("/home/test"))).expect("index")
    }
}
