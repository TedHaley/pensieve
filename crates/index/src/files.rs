//! The files of the indexed folders (setting `folders`), chunked and embedded like sessions. Each file type (TYPES)
//! is read for its content, indexed by name only, or skipped, and shown on the map or not, as `file_types` says. A
//! folder is an item too, found by its name and what it holds. Git repos inside these folders are left to the repo
//! indexer.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, MutexGuard, RwLock};
use std::time::{Duration, Instant, UNIX_EPOCH};

use serde_json::{Value, json};
use store::rusqlite::{self, params};

use crate::extract::{self, TEXT_EXT, ext_lower};
use crate::{Config, Index, layout, vectors};

pub const FILES_VERSION: &str = "files-v3";
pub const LAYOUT_VERSION: &str = "umap-v1";
pub const MEDIA_VERSION: &str = "m2";
/// Per sweep of one folder: name-only entries beyond this are skipped (a huge Downloads folder).
pub const NAME_ONLY_MAX: usize = 4000;
pub const PROGRESS_MIN_FILES: usize = 10;
/// A file changed more recently than this can be half written: it waits for the next pass.
pub const SETTLE_SECONDS: f64 = 1.0;
pub const IMAGE_MAX_BYTES: u64 = 50_000_000;
/// Upper edges of the size chart in Settings, in chunks of 1500 characters.
pub const SIZE_BINS: [u32; 12] = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000];

const KIND: &[(&str, &str)] = &[
    (".md", "doc"), (".markdown", "doc"), (".rst", "doc"), (".org", "doc"), (".txt", "doc"), (".tex", "doc"),
    (".rtf", "doc"), (".doc", "doc"), (".odt", "doc"), (".pages", "doc"), (".eml", "doc"), (".srt", "doc"),
    (".vtt", "doc"), (".docx", "doc"), (".pdf", "pdf"), (".pptx", "slides"), (".key", "slides"), (".csv", "data"),
    (".tsv", "data"), (".json", "data"), (".xlsx", "data"), (".numbers", "data"), (".ipynb", "notebook"),
];
pub const IMAGE_EXT: &[&str] = &[".png", ".jpg", ".jpeg", ".gif", ".heic", ".heif", ".webp", ".tif", ".tiff", ".bmp",
    ".svg", ".psd", ".raw", ".cr2", ".nef", ".dng"];
/// Images the model can decode; the others are indexed by name.
pub const READ_IMAGE_EXT: &[&str] = &[".png", ".jpg", ".jpeg", ".gif", ".heic", ".heif", ".webp", ".tif", ".tiff", ".bmp"];
pub const VIDEO_EXT: &[&str] = &[".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"];
pub const AUDIO_EXT: &[&str] = &[".mp3", ".m4a", ".wav", ".aiff", ".aif", ".flac", ".ogg", ".caf", ".aac"];
/// Kinds that the model reads by what they show or say, once their part of the model is installed.
pub const MEDIA_KINDS: [&str; 3] = ["images", "video", "audio"];
/// Folders that macOS shows as one file: never walked into.
pub const PACKAGES: [&str; 8] = [".app", ".photoslibrary", ".bundle", ".framework", ".pages", ".key", ".numbers", ".rtfd"];

/// (id, label, what it covers, extensions), in the order Settings shows them.
pub const TYPES: [(&str, &str, &str, &[&str]); 9] = [
    ("text", "Text and notes", "Plain text, Markdown, RTF, Word 97, OpenDocument, mail, subtitles",
     &[".txt", ".md", ".markdown", ".rst", ".org", ".tex", ".rtf", ".doc", ".odt", ".eml", ".srt", ".vtt"]),
    ("pdf", "PDFs", "PDF documents (text only, no scanned images)", &[".pdf"]),
    ("office", "Office and iWork", "Word, PowerPoint, Excel, Pages, Keynote, Numbers",
     &[".docx", ".pptx", ".xlsx", ".pages", ".key", ".numbers"]),
    ("data", "Data", "CSV, TSV, JSON, notebooks", &[".csv", ".tsv", ".json", ".ipynb"]),
    ("code", "Code and config", "Source and config files outside git repos (repos are indexed as repos)", &[]),
    ("images", "Images", "Photos, screenshots and diagrams, by what they show (Settings → Models)", IMAGE_EXT),
    ("video", "Video", "Screen recordings and videos, by what they show (Settings → Models)", VIDEO_EXT),
    ("audio", "Audio", "Voice notes, meetings and other sound, by what is said or heard (Settings → Models)", AUDIO_EXT),
    ("other", "Other files", "Archives, apps, installers and everything else, by name only", &[]),
];

/// What the files indexer keeps between passes, and what the app tells it.
pub struct State {
    sync_lock: Mutex<()>,
    last: Mutex<f64>,
    sweeping: AtomicBool,
    refreshing: AtomicBool,
    pub progress: Mutex<Option<Value>>,
    /// Media kinds (images, video, audio) that are read by content now: switched on, model part installed, decoder
    /// found. The app sets it (0.21 `content.ready`).
    pub content_ready: RwLock<HashSet<String>>,
    /// The embedding model's name, part of a media file's index mode.
    pub embed_model: RwLock<String>,
    /// The last places list (crate::places), kept for its TTL.
    pub(crate) places: crate::places::Cache,
}

impl Default for State {
    fn default() -> State {
        State {
            sync_lock: Mutex::new(()),
            last: Mutex::new(0.0),
            sweeping: AtomicBool::new(false),
            refreshing: AtomicBool::new(false),
            progress: Mutex::new(None),
            content_ready: RwLock::new(HashSet::new()),
            embed_model: RwLock::new("google/embeddinggemma-2".into()),
            places: Default::default(),
        }
    }
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

pub fn kind_of(path: &Path) -> &'static str {
    let e = ext_lower(path);
    if let Some((_, k)) = KIND.iter().find(|(x, _)| *x == e) {
        return k;
    }
    if TEXT_EXT.contains(&e.as_str()) { "code" } else { "other" }
}

/// The TYPES id of a path, or of a bare extension such as `md`.
pub fn type_of(path: &str) -> &'static str {
    let s = path.to_lowercase();
    let e = if s.contains('/') || s.contains('.') { extract::suffix(Path::new(&s)) } else { format!(".{s}") };
    if let Some((t, ..)) = TYPES.iter().find(|(_, _, _, exts)| exts.contains(&e.as_str())) {
        return t;
    }
    if TEXT_EXT.contains(&e.as_str()) { "code" } else { "other" }
}

pub fn readable(t: &str) -> bool {
    t != "other"
}

/// The settings of one file type: {index: content | name | off, map: bool}.
pub fn file_type(cfg: &Config, t: &str) -> Value {
    let defaults = crate::defaults();
    let mine = cfg.get("file_types");
    let base = defaults["file_types"].get(t).cloned().unwrap_or(json!({"index": "name", "map": false}));
    let over = if MEDIA_KINDS.contains(&t) && mine.get(t).is_none() && mine.get("media").is_some() {
        mine.get("media")
    } else {
        mine.get(t)
    };
    let mut out = base;
    if let (Some(o), Some(Value::Object(m))) = (out.as_object_mut(), over) {
        for (k, v) in m {
            o.insert(k.clone(), v.clone());
        }
    }
    out
}

/// Seconds since 1970 of a file's change, as Python's `st_mtime` computes it, so stored values compare equal.
pub fn mtime_of(m: &std::fs::Metadata) -> f64 {
    m.modified().ok().and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_secs() as f64 + d.subsec_nanos() as f64 * 1e-9).unwrap_or(0.0)
}

/// Python's fnmatch on one name: `*`, `?` and `[...]` (`[!...]` negates).
pub fn fnmatch(name: &str, pat: &str) -> bool {
    let n: Vec<char> = name.chars().collect();
    let p: Vec<char> = pat.chars().collect();
    fn class(p: &[char], c: char) -> Option<(bool, usize)> {
        let mut i = 1;
        let neg = p.get(1) == Some(&'!');
        if neg {
            i += 1;
        }
        let start = i;
        let mut hit = false;
        while i < p.len() && (p[i] != ']' || i == start) {
            if i + 2 < p.len() && p[i + 1] == '-' && p[i + 2] != ']' {
                hit |= p[i] <= c && c <= p[i + 2];
                i += 3;
            } else {
                hit |= p[i] == c;
                i += 1;
            }
        }
        (i < p.len()).then_some((hit != neg, i + 1))
    }
    fn go(n: &[char], p: &[char]) -> bool {
        match p.first() {
            None => n.is_empty(),
            Some('*') => (0..=n.len()).any(|i| go(&n[i..], &p[1..])),
            Some('?') => !n.is_empty() && go(&n[1..], &p[1..]),
            Some('[') => match (n.first(), class(p, *n.first().unwrap_or(&'\0'))) {
                (Some(_), Some((ok, used))) => ok && go(&n[1..], &p[used..]),
                (Some(c), None) => *c == '[' && go(&n[1..], &p[1..]),
                (None, _) => false,
            },
            Some(c) => n.first() == Some(c) && go(&n[1..], &p[1..]),
        }
    }
    go(&n, &p)
}

/// A name that looks like a secret (key files, password exports): never indexed.
pub fn secret(name: &str, patterns: &[String]) -> bool {
    let n = name.to_lowercase();
    patterns.iter().any(|p| fnmatch(&n, &p.to_lowercase()))
}

fn is_package(name: &str) -> bool {
    PACKAGES.iter().any(|p| name.ends_with(p))
}

/// One step of a walk: a folder with its children (folders first, sorted, then files), or a file.
pub enum Entry {
    Folder(PathBuf, Vec<String>),
    File(PathBuf),
}

/// Every folder and file under `root`, as 0.21's walk (os.walk top-down): hidden folders, excluded names, packages,
/// secret-looking files and git repos are skipped. A folder comes before its files, and its files before its
/// subfolders.
pub fn walk(root: &Path, exclude: &HashSet<String>, exclude_files: &[String], f: &mut dyn FnMut(Entry)) {
    walk_dir(root, true, exclude, exclude_files, f);
}

fn walk_dir(d: &Path, top: bool, exclude: &HashSet<String>, exclude_files: &[String], f: &mut dyn FnMut(Entry)) {
    let Ok(rd) = std::fs::read_dir(d) else { return };
    let (mut dirs, mut files) = (Vec::new(), Vec::new());
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        let is_dir = match e.file_type() {
            Ok(t) if t.is_symlink() => std::fs::metadata(e.path()).map(|m| m.is_dir()).unwrap_or(false),
            Ok(t) => t.is_dir(),
            Err(_) => false,
        };
        if is_dir { dirs.push(name) } else { files.push(name) }
    }
    if !top && dirs.iter().chain(&files).any(|n| n == ".git") {
        return;
    }
    let dirs: Vec<String> = dirs.into_iter()
        .filter(|x| !x.starts_with('.') && !exclude.contains(x) && !is_package(x)).collect();
    let files: Vec<String> = files.into_iter()
        .filter(|x| !x.starts_with('.') && !x.starts_with("~$") && !secret(x, exclude_files)).collect();
    let mut sorted = dirs.clone();
    sorted.sort();
    f(Entry::Folder(d.to_path_buf(), sorted.into_iter().chain(files.iter().cloned()).collect()));
    for name in &files {
        f(Entry::File(d.join(name)));
    }
    for name in &dirs {
        walk_dir(&d.join(name), false, exclude, exclude_files, f);
    }
}

/// (start line, end line, text) chunks broken on blank lines near `size` characters; at most `cap` (0 = all).
pub fn chunk_text(text: &str, size: usize, cap: usize) -> Vec<(usize, usize, String)> {
    let lines: Vec<&str> = text.split('\n').collect();
    let mut out: Vec<(usize, usize, String)> = Vec::new();
    let (mut start, mut n) = (0usize, 0usize);
    let soft = size * 2 / 5;
    let mut broke = false;
    for (i, ln) in lines.iter().enumerate() {
        n += ln.chars().count() + 1;
        if n >= size || (n > soft && ln.trim().is_empty()) {
            let mut body: Vec<char> = lines[start..=i].join("\n").chars().collect();
            while body.len() > size * 2 {
                out.push((start + 1, i + 1, body[..size].iter().collect()));
                body = body[size..].to_vec();
            }
            out.push((start + 1, i + 1, body.into_iter().collect()));
            start = i + 1;
            n = 0;
            if cap > 0 && out.len() >= cap * 2 {
                broke = true;
                break;
            }
        }
    }
    if !broke && start < lines.len() {
        out.push((start + 1, lines.len(), lines[start..].join("\n")));
    }
    let mut out: Vec<_> = out.into_iter().filter(|c| !c.2.trim().is_empty()).collect();
    if cap > 0 {
        out.truncate(cap);
    }
    out
}

/// A window of a video or sound file as m:ss-m:ss.
pub fn media_label(start: f64, seconds: f64) -> String {
    let t = |s: f64| {
        let s = s as i64;
        if s >= 3600 { format!("{}:{:02}:{:02}", s / 3600, s / 60 % 60, s % 60) } else { format!("{}:{:02}", s / 60, s % 60) }
    };
    format!("{}-{}", t(start), t(start + seconds))
}

fn is_relative_to(p: &Path, q: &Path) -> bool {
    p.starts_with(q)
}

fn sq(p: &Path) -> String {
    p.to_string_lossy().into_owned()
}

/// Python's json.dumps of a string (ASCII only, as it escapes).
fn py_json_str(s: &str) -> String {
    let mut out = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let mut b = [0u16; 2];
                for u in c.encode_utf16(&mut b) {
                    out.push_str(&format!("\\u{:04x}", u));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// A row of `files` for the map and the file view.
struct FileRow {
    path: String,
    root: String,
    name: String,
    ext: String,
    kind: String,
    mtime: Option<f64>,
    size: Option<i64>,
    n: Option<i64>,
    p: [Option<f64>; 3],
    cluster: Option<i64>,
    author: Option<String>,
    author_source: Option<String>,
}

impl Index {
    fn files_content_ready(&self, kind: &str) -> bool {
        self.files.content_ready.read().unwrap_or_else(|e| e.into_inner()).contains(kind)
    }

    /// content | name | off for this file under the current `file_types` settings.
    pub fn files_level_of(&self, path: &Path) -> String {
        let cfg = self.config();
        let t = type_of(&sq(path));
        let lv = file_type(&cfg, t)["index"].as_str().unwrap_or("name").to_string();
        if lv == "content" && MEDIA_KINDS.contains(&t) {
            let decodable = t != "images" || READ_IMAGE_EXT.contains(&ext_lower(path).as_str());
            let used = cfg.get("media_by_use") == &Value::Bool(false) || self.files_in_use(path);
            return if decodable && self.files_content_ready(t) && used { "content".into() } else { "name".into() };
        }
        if lv == "content" && t == "other" { "name".into() } else { lv }
    }

    /// What a file was indexed with; a file whose stored mode differs is indexed again.
    pub fn files_mode_of(&self, level: &str, path: &Path) -> String {
        if level != "content" {
            return level.to_string();
        }
        let model = self.files.embed_model.read().unwrap_or_else(|e| e.into_inner()).clone();
        match type_of(&sq(path)) {
            "images" => format!("image/{model}"),
            "video" => format!("video/{model}/{MEDIA_VERSION}{}", if self.files_content_ready("audio") { "+audio" } else { "" }),
            "audio" => format!("audio/{model}/{MEDIA_VERSION}"),
            _ => {
                let cfg = self.config();
                format!("content/{}/{}/{FILES_VERSION}", cfg.int("chunk_chars"), cfg.int("max_chunks"))
            }
        }
    }

    /// The folders to index: the enabled `folders` that exist, resolved; a folder inside another one folds into it.
    pub fn files_roots(&self) -> Vec<PathBuf> {
        let cfg = self.config();
        let mut cand: Vec<PathBuf> = Vec::new();
        for f in cfg.strs("folders") {
            let p = cfg.expand(&f);
            if cfg.enabled(&format!("files/{f}")) && p.is_dir() {
                let r = p.canonicalize().unwrap_or(p);
                if !cand.contains(&r) {
                    cand.push(r);
                }
            }
        }
        cand.iter().filter(|p| !cand.iter().any(|q| q != *p && is_relative_to(p, q))).cloned().collect()
    }

    /// Index new and changed files of the folders and drop the ones that are gone. At most every 30 seconds unless
    /// `force`. Returns the files touched. Blocking: runs on the app's indexing thread.
    pub fn files_sync(&self, force: bool, log: &dyn Fn(&str)) -> usize {
        let _g = lock(&self.files.sync_lock);
        self.files.sweeping.store(true, Ordering::SeqCst);
        let n = self.files_sweep(force, log);
        self.files.sweeping.store(false, Ordering::SeqCst);
        *lock(&self.files.progress) = None;
        n
    }

    fn files_sweep(&self, force: bool, log: &dyn Fn(&str)) -> usize {
        {
            let mut last = lock(&self.files.last);
            if !force && store::now() - *last < 30.0 {
                return 0;
            }
            *last = store::now();
        }
        self.files_refresh_use();
        let cfg = self.config();
        let roots = self.files_roots();
        let exclude: HashSet<String> = cfg.strs("exclude").into_iter().collect();
        let exclude_files = cfg.strs("exclude_files");
        let max_bytes = cfg.int("max_file_mb").max(0) as u64 * 1_000_000;
        let mut known: HashMap<String, (f64, i64)> = HashMap::new();
        let mut modes: HashMap<String, Option<String>> = HashMap::new();
        let _ = self.db.read(|c| {
            let mut st = c.prepare("SELECT path, mtime, size, mode FROM files")?;
            let mut rows = st.query([])?;
            while let Some(r) = rows.next()? {
                let p: String = r.get(0)?;
                known.insert(p.clone(), (r.get::<_, Option<f64>>(1)?.unwrap_or(f64::NAN), r.get::<_, Option<i64>>(2)?.unwrap_or(-1)));
                modes.insert(p, r.get(3)?);
            }
            Ok(())
        });
        let stale = self.meta("files_v").as_deref() != Some(FILES_VERSION);
        let mut seen: HashSet<String> = HashSet::new();
        let mut touched = 0usize;
        let mut levels: HashMap<String, String> = HashMap::new();
        for root in &roots {
            let mut name_only = 0usize;
            walk(root, &exclude, &exclude_files, &mut |e| match e {
                Entry::Folder(p, children) => {
                    let sp = sq(&p);
                    seen.insert(sp.clone());
                    let Ok(m) = std::fs::metadata(&p) else { return };
                    let mt = mtime_of(&m);
                    if stale || known.get(&sp) != Some(&(mt, children.len() as i64)) {
                        match self.files_index_folder(&p, root, mt, &children) {
                            Ok(()) => touched += 1,
                            Err(e) => log(&format!("skip folder {}: {e}", p.display())),
                        }
                    }
                }
                Entry::File(p) => {
                    let Ok(st) = std::fs::metadata(&p) else { return };
                    let ext = ext_lower(&p);
                    // A media file's level depends on its use, not only on its extension.
                    let lv = if MEDIA_KINDS.contains(&type_of(&sq(&p))) {
                        self.files_level_of(&p)
                    } else {
                        levels.entry(ext).or_insert_with(|| self.files_level_of(&p)).clone()
                    };
                    if lv == "off" {
                        return;
                    }
                    if lv == "name" {
                        name_only += 1;
                        if name_only > NAME_ONLY_MAX {
                            return;
                        }
                    }
                    let sp = sq(&p);
                    seen.insert(sp.clone());
                    let mt = mtime_of(&st);
                    if known.get(&sp).is_some_and(|k| k.0 != mt) {
                        self.files_note_edit(&sp, mt);
                    }
                    let same = known.get(&sp) == Some(&(mt, st.len() as i64))
                        && modes.get(&sp).cloned().flatten().as_deref() == Some(self.files_mode_of(&lv, &p).as_str());
                    if (!stale && same) || store::now() - mt < SETTLE_SECONDS {
                        return;
                    }
                    match self.files_index_file(&p, root, &st, kind_of(&p), max_bytes, &lv) {
                        Ok(()) => {
                            touched += 1;
                            if touched >= PROGRESS_MIN_FILES {
                                let name = root.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
                                *lock(&self.files.progress) = Some(json!({"name": name, "root": sq(root), "done": touched, "total": null}));
                            }
                        }
                        Err(e) => log(&format!("skip {}: {e}", p.display())),
                    }
                    if touched > 0 && touched % 50 == 0 {
                        log(&format!("files: {touched} indexed"));
                    }
                }
            });
        }
        if stale {
            self.set_meta("files_v", FILES_VERSION);
        }
        self.files_backfill_authors(400);
        let gone: Vec<String> = known.keys().filter(|p| !seen.contains(*p)).cloned().collect();
        for p in &gone {
            self.files_drop(p);
        }
        touched + gone.len()
    }

    /// Index `paths` and the watcher's queue of changed files now, also files saved a moment ago (a search
    /// catching up). Returns the files changed; 0 when a sweep holds the index, and the paths go back in the queue.
    pub fn files_refresh(&self, paths: &[String]) -> usize {
        let mut all: HashSet<String> = paths.iter().cloned().collect();
        all.extend(self.watch.take_files());
        let wait = Duration::from_millis(1500);
        if all.is_empty() {
            if self.files.refreshing.load(Ordering::SeqCst) {
                drop(self.try_lock_for(wait));
            }
            return 0;
        }
        if self.files.sweeping.load(Ordering::SeqCst) {
            self.watch.requeue_files(all);
            return 0;
        }
        let Some(g) = self.try_lock_for(wait) else {
            self.watch.requeue_files(all);
            return 0;
        };
        let list: Vec<String> = all.into_iter().collect();
        let n = self.files_sync_paths_locked(&list, 0.0, &|_| {}).0;
        drop(g);
        n
    }

    fn try_lock_for(&self, wait: Duration) -> Option<MutexGuard<'_, ()>> {
        let end = Instant::now() + wait;
        loop {
            match self.files.sync_lock.try_lock() {
                Ok(g) => return Some(g),
                Err(std::sync::TryLockError::Poisoned(e)) => return Some(e.into_inner()),
                Err(_) if Instant::now() >= end => return None,
                Err(_) => std::thread::sleep(Duration::from_millis(10)),
            }
        }
    }

    /// Index or drop just these paths (from the file watcher). Returns (files changed, paths to try again later:
    /// changed less than `settle` seconds ago, maybe still being written).
    pub fn files_sync_paths(&self, paths: &[String], settle: f64, log: &dyn Fn(&str)) -> (usize, Vec<String>) {
        let _g = lock(&self.files.sync_lock);
        self.files_sync_paths_locked(paths, settle, log)
    }

    fn files_sync_paths_locked(&self, paths: &[String], settle: f64, log: &dyn Fn(&str)) -> (usize, Vec<String>) {
        self.files.refreshing.store(true, Ordering::SeqCst);
        let cfg = self.config();
        let roots = self.files_roots();
        let exclude: HashSet<String> = cfg.strs("exclude").into_iter().collect();
        let exclude_files = cfg.strs("exclude_files");
        let max_bytes = cfg.int("max_file_mb").max(0) as u64 * 1_000_000;
        let (mut changed, mut retry) = (0, Vec::new());
        for sp in paths {
            let p = Path::new(sp);
            let Some(root) = roots.iter().find(|r| is_relative_to(p, r)) else { continue };
            if !p.exists() {
                let like = format!("{sp}/%");
                let gone: Vec<String> = self.db.read(|c| {
                    let mut st = c.prepare("SELECT path FROM files WHERE path=? OR path LIKE ?")?;
                    st.query_map(params![sp, like], |r| r.get(0))?.collect()
                }).unwrap_or_default();
                for g in &gone {
                    self.files_drop(g);
                }
                changed += gone.len();
                continue;
            }
            if !p.is_file() || !eligible(p, root, &exclude, &exclude_files) {
                continue;
            }
            let Ok(st) = std::fs::metadata(p) else { continue };
            let mt = mtime_of(&st);
            if store::now() - mt < settle {
                retry.push(sp.clone());
                continue;
            }
            let lv = self.files_level_of(p);
            let row: Option<(Option<f64>, Option<i64>, Option<String>)> = self.db.read(|c| {
                c.query_row("SELECT mtime, size, mode FROM files WHERE path=?", [sp], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
                    .map(Some).or_else(|e| if e == rusqlite::Error::QueryReturnedNoRows { Ok(None) } else { Err(e) })
            }).unwrap_or(None);
            if row.as_ref().is_some_and(|r| r.0 != Some(mt)) && self.files_note_edit(sp, mt) {
                self.watch.force_files();
            }
            if lv == "off" {
                if row.is_some() {
                    self.files_drop(sp);
                    changed += 1;
                }
                continue;
            }
            if row == Some((Some(mt), Some(st.len() as i64), Some(self.files_mode_of(&lv, p)))) {
                continue;
            }
            match self.files_index_file(p, root, &st, kind_of(p), max_bytes, &lv) {
                Ok(()) => {
                    changed += 1;
                    log(&format!("file updated: {}", p.file_name().map(|n| n.to_string_lossy()).unwrap_or_default()));
                }
                Err(e) => log(&format!("skip {sp}: {e}")),
            }
        }
        self.files.refreshing.store(false, Ordering::SeqCst);
        (changed, retry)
    }

    fn files_index_folder(&self, p: &Path, root: &Path, mtime: f64, children: &[String]) -> rusqlite::Result<()> {
        let base = root.parent().unwrap_or(Path::new("/"));
        let rel = p.strip_prefix(base).unwrap_or(p);
        let name = p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        let shown: Vec<&str> = children.iter().take(80).map(|s| s.as_str()).collect();
        let text = format!("{name}\nFolder {}\nContains: {}", rel.display(), shown.join(", "));
        let vec = self.embed_docs(std::slice::from_ref(&text), None).pop().unwrap_or_default();
        let sp = sq(p);
        let (old, id) = self.db.tx(|t| {
            let old = drop_rows(t, &sp)?;
            t.execute("INSERT INTO file_chunks(path, idx, start, end, text, vec) VALUES(?,?,?,?,?,?)",
                      params![sp, 0, 0, 0, text, vectors::to_blob(&vec)])?;
            let id = t.last_insert_rowid();
            t.execute("INSERT OR REPLACE INTO files(path, root, name, ext, kind, mtime, size, n_chunks) VALUES(?,?,?,?,?,?,?,?)",
                      params![sp, sq(root), name, "", "folder", mtime, children.len() as i64, 1])?;
            Ok((old, id))
        })?;
        self.file_vecs.remove_many(&old);
        self.file_vecs.add(&[(id, vec)]);
        Ok(())
    }

    fn files_index_file(&self, p: &Path, root: &Path, st: &std::fs::Metadata, kind: &str, max_bytes: u64, level: &str)
                  -> rusqlite::Result<()> {
        let base = root.parent().unwrap_or(Path::new("/"));
        let rel = if base != p { p.strip_prefix(base).unwrap_or(p) } else { p };
        let t = type_of(&sq(p));
        let pictures = level == "content" && MEDIA_KINDS.contains(&t);
        let cfg = self.config();
        let text = if level == "content" && !pictures && st.len() <= max_bytes { extract::extract(p, max_bytes) } else { String::new() };
        let mut chunks = if text.trim().is_empty() {
            Vec::new()
        } else {
            chunk_text(&text, cfg.int("chunk_chars").max(1) as usize, cfg.int("max_chunks").max(0) as usize)
        };
        let name = p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        let parent = p.parent().map(sq).unwrap_or_default();
        if chunks.is_empty() {
            chunks = vec![(0, 0, format!("{name}\n{parent}"))];
        }
        let mut vecs: Option<Vec<Vec<f32>>> = None;
        if pictures && t == "images" && st.len() <= IMAGE_MAX_BYTES {
            vecs = self.embedder.embed_image(p).map(|v| vec![vectors::unit(&v)]);
        } else if pictures && (t == "video" || t == "audio") {
            let parts = self.embedder.embed_media(p, t == "video", t == "audio" || self.files_content_ready("audio"));
            if !parts.is_empty() {
                chunks = parts.iter().map(|(a, s, k, _)| {
                    let said = if k == "video" { "shows" } else { "sound" };
                    (*a as usize, (a + s) as usize, format!("{name} {} ({said})\n{parent}", media_label(*a, *s)))
                }).collect();
                vecs = Some(parts.into_iter().map(|(.., v)| vectors::unit(&v)).collect());
            }
        }
        let vecs = vecs.unwrap_or_else(|| {
            let texts: Vec<String> = chunks.iter().map(|c| c.2.clone()).collect();
            let titles = vec![rel.to_string_lossy().into_owned(); texts.len()];
            self.embed_docs(&texts, Some(&titles))
        });
        let sp = sq(p);
        let chars = if text.trim().is_empty() { None } else { Some(text.chars().count() as i64) };
        let mode = self.files_mode_of(level, p);
        let ext = ext_lower(p).trim_start_matches('.').to_string();
        let (old, ids) = self.db.tx(|tx| {
            let old = drop_rows(tx, &sp)?;
            let mut ids = Vec::new();
            for (i, ((a, b, text), v)) in chunks.iter().zip(&vecs).enumerate() {
                tx.execute("INSERT INTO file_chunks(path, idx, start, end, text, vec) VALUES(?,?,?,?,?,?)",
                           params![sp, i as i64, *a as i64, *b as i64, text, vectors::to_blob(v)])?;
                ids.push(tx.last_insert_rowid());
            }
            tx.execute("INSERT OR REPLACE INTO files(path, root, name, ext, kind, mtime, size, n_chunks, chars, mode) \
                        VALUES(?,?,?,?,?,?,?,?,?,?)",
                       params![sp, sq(root), name, ext, kind, mtime_of(st), st.len() as i64, chunks.len() as i64, chars, mode])?;
            Ok((old, ids))
        })?;
        self.file_vecs.remove_many(&old);
        self.file_vecs.add(&ids.into_iter().zip(vecs).collect::<Vec<_>>());
        Ok(())
    }

    fn files_drop(&self, path: &str) {
        if let Ok(old) = self.db.tx(|t| drop_rows(t, path)) {
            self.file_vecs.remove_many(&old);
        }
    }

    /// Fill in who wrote documents: a bounded batch per sweep, so the first run does not stall indexing.
    pub fn files_backfill_authors(&self, limit: usize) {
        let todo: Vec<(String, String)> = self.db.read(|c| {
            let mut st = c.prepare("SELECT path, kind FROM files WHERE author_source IS NULL AND kind!='folder' LIMIT ?")?;
            st.query_map([limit as i64], |r| Ok((r.get(0)?, r.get::<_, Option<String>>(1)?.unwrap_or_default())))?.collect()
        }).unwrap_or_default();
        let rows: Vec<(Option<String>, String, String)> = todo.into_iter().map(|(p, k)| {
            let (a, src) = extract::author_of(Path::new(&p), &k);
            (a, src.unwrap_or_else(|| "none".into()), p)
        }).collect();
        if !rows.is_empty() {
            let _ = self.db.tx(|t| {
                for (a, s, p) in &rows {
                    t.execute("UPDATE files SET author=?, author_source=? WHERE path=?", params![a, s, p])?;
                }
                Ok(())
            });
        }
    }

    /// Changes whenever a map switch changes, so a layout made under other switches is made again. The same string
    /// as 0.21's, so a copied index keeps its map.
    pub fn files_map_sig(&self) -> String {
        let cfg = self.config();
        let mut items: Vec<(String, bool, String)> = TYPES.iter().map(|(t, ..)| {
            let ft = file_type(&cfg, t);
            (t.to_string(), ft["map"].as_bool().unwrap_or(false), ft["index"].as_str().unwrap_or("").to_string())
        }).collect();
        items.sort();
        let mut off = cfg.strs("off_map");
        off.sort();
        let a: Vec<String> = items.iter()
            .map(|(t, m, i)| format!("[{}, {}, {}]", py_json_str(t), m, py_json_str(i))).collect();
        let b: Vec<String> = off.iter().map(|s| py_json_str(s)).collect();
        format!("[[{}], [{}]]", a.join(", "), b.join(", "))
    }

    /// (path, root) -> whether the file is a map point: its type is read for content and on the map, and its folder
    /// is not in `off_map`.
    fn files_map_filter(&self) -> impl Fn(&str, &str) -> bool + use<'_> {
        let cfg = self.config();
        let shown: HashMap<&str, bool> = TYPES.iter().map(|(t, ..)| {
            let ft = file_type(&cfg, t);
            (*t, ft["map"].as_bool().unwrap_or(false) && ft["index"] == "content" && readable(t))
        }).collect();
        let folders: HashMap<String, String> = cfg.strs("folders").into_iter().map(|f| {
            let p = cfg.expand(&f);
            (sq(&p.canonicalize().unwrap_or(p)), format!("files/{f}"))
        }).collect();
        let src: Mutex<HashMap<String, bool>> = Mutex::new(HashMap::new());
        move |path: &str, root: &str| {
            if !shown.get(type_of(path)).copied().unwrap_or(false) {
                return false;
            }
            *lock(&src).entry(root.to_string())
                .or_insert_with(|| cfg.on_map(folders.get(root).map(|s| s.as_str()).unwrap_or("files")))
        }
    }

    /// Paths and unit mean vectors of the files on the map.
    pub fn files_centroids(&self) -> (Vec<String>, Vec<Vec<f32>>) {
        let rows: Vec<(String, String, i64)> = self.db.read(|c| {
            let mut st = c.prepare("SELECT c.path, f.root, c.id FROM file_chunks c JOIN files f ON f.path=c.path WHERE f.kind!='folder'")?;
            st.query_map([], |r| Ok((r.get(0)?, r.get::<_, Option<String>>(1)?.unwrap_or_default(), r.get(2)?)))?.collect()
        }).unwrap_or_default();
        let ok = self.files_map_filter();
        let mut order: Vec<(String, Vec<i64>)> = Vec::new();
        let mut at: HashMap<String, usize> = HashMap::new();
        for (p, root, cid) in rows {
            if !ok(&p, &root) {
                continue;
            }
            let i = *at.entry(p.clone()).or_insert_with(|| {
                order.push((p.clone(), Vec::new()));
                order.len() - 1
            });
            order[i].1.push(cid);
        }
        vectors::centroids(&order, &self.file_vecs)
    }

    pub fn files_needs_layout(&self) -> bool {
        if self.meta("files_map").as_deref() != Some(self.files_map_sig().as_str()) {
            return true;
        }
        let ok = self.files_map_filter();
        let rows: Vec<(String, String)> = self.db.read(|c| {
            let mut st = c.prepare("SELECT path, root FROM files WHERE x IS NULL AND kind!='folder'")?;
            st.query_map([], |r| Ok((r.get(0)?, r.get::<_, Option<String>>(1)?.unwrap_or_default())))?.collect()
        }).unwrap_or_default();
        rows.iter().any(|(p, r)| ok(p, r))
    }

    /// One map point per file: a full fit when the set grew or shrank a lot, else new files go next to their
    /// neighbours. Returns whether points moved. Blocking (the app's map worker).
    pub fn files_reproject(&self, force: bool) -> bool {
        let sig = self.files_map_sig();
        let mut force = force;
        if self.meta("files_map").as_deref() != Some(sig.as_str()) {
            force = true;
            let _ = self.db.tx(|t| {
                t.execute("UPDATE files SET x=NULL, y=NULL, z=NULL", [])?;
                crate::db::set_meta(t, "files_map", &sig)
            });
        }
        let (paths, c) = self.files_centroids();
        if paths.len() < 4 {
            return false;
        }
        let placed: HashMap<String, ([f32; 3], Option<i64>)> = self.db.read(|c| {
            let mut st = c.prepare("SELECT path, x, y, z, cluster FROM files WHERE x IS NOT NULL")?;
            st.query_map([], |r| Ok((r.get(0)?, ([r.get::<_, f64>(1)? as f32, r.get::<_, Option<f64>>(2)?.unwrap_or(0.0) as f32,
                                                  r.get::<_, Option<f64>>(3)?.unwrap_or(0.0) as f32], r.get(4)?))))?.collect()
        }).unwrap_or_default();
        let fit_n: f64 = self.meta("files_fit_n").and_then(|s| s.parse().ok()).unwrap_or(0.0);
        let ok = self.meta("files_layout").as_deref() == Some(LAYOUT_VERSION);
        let n = paths.len() as f64;
        if force || !ok || (placed.len() as f64) < 0.5 * n || (n - fit_n).abs() > 60f64.max(0.15 * fit_n) {
            let pts = layout::fit3d(&c, 12, 0.2);
            let lab = layout::cluster(&c, 4, 8);
            let _ = self.db.tx(|t| {
                let mut st = t.prepare("UPDATE files SET x=?, y=?, z=?, cluster=? WHERE path=?")?;
                for ((q, l), p) in pts.iter().zip(&lab).zip(&paths) {
                    st.execute(params![q[0] as f64, q[1] as f64, q[2] as f64, *l as i64, p])?;
                }
                crate::db::set_meta(t, "files_fit_n", &paths.len().to_string())?;
                crate::db::set_meta(t, "files_layout", LAYOUT_VERSION)
            });
            return true;
        }
        let missing: Vec<usize> = (0..paths.len()).filter(|&i| !placed.contains_key(&paths[i])).collect();
        if missing.is_empty() {
            return false;
        }
        let known: Vec<usize> = (0..paths.len()).filter(|&i| placed.contains_key(&paths[i])).collect();
        let ck: Vec<Vec<f32>> = known.iter().map(|&i| c[i].clone()).collect();
        let cm: Vec<Vec<f32>> = missing.iter().map(|&i| c[i].clone()).collect();
        let pk: Vec<[f32; 3]> = known.iter().map(|&i| placed[&paths[i]].0).collect();
        let pn = layout::place_new(&ck, &pk, &cm, 3, 0.04);
        let near = layout::nearest(&cm, &ck);
        let _ = self.db.tx(|t| {
            for (j, &i) in missing.iter().enumerate() {
                let cl = placed[&paths[known[near[j]]]].1;
                t.execute("UPDATE files SET x=?, y=?, z=?, cluster=? WHERE path=?",
                          params![pn[j][0] as f64, pn[j][1] as f64, pn[j][2] as f64, cl, paths[i]])?;
            }
            Ok(())
        });
        true
    }

    fn files_query(&self, sql: &str, args: &[&dyn rusqlite::ToSql]) -> Vec<FileRow> {
        self.db.read(|c| {
            let mut st = c.prepare(sql)?;
            st.query_map(args, |r| Ok(FileRow {
                path: r.get(0)?,
                root: r.get::<_, Option<String>>(1)?.unwrap_or_default(),
                name: r.get::<_, Option<String>>(2)?.unwrap_or_default(),
                ext: r.get::<_, Option<String>>(3)?.unwrap_or_default(),
                kind: r.get::<_, Option<String>>(4)?.unwrap_or_default(),
                mtime: r.get(5)?,
                size: r.get(6)?,
                n: r.get(7)?,
                p: [r.get(8)?, r.get(9)?, r.get(10)?],
                cluster: r.get(11)?,
                author: r.get(12)?,
                author_source: r.get(13)?,
            }))?.collect()
        }).unwrap_or_default()
    }

    /// GET /api/files/points: the files on the map.
    pub fn api_files_points(&self) -> Value {
        let home = sq(&self.config().home);
        let ok = self.files_map_filter();
        let rows = self.files_query("SELECT path, root, name, ext, kind, mtime, size, n_chunks, x, y, z, cluster, author, author_source \
                                   FROM files WHERE x IS NOT NULL AND kind!='folder'", &[]);
        let out: Vec<Value> = rows.into_iter().filter(|r| ok(&r.path, &r.root)).map(|r| {
            let base = Path::new(&r.root).parent().map(sq).unwrap_or_default();
            let rel = Path::new(&r.path).strip_prefix(&base).map(sq).unwrap_or_else(|_| r.path.clone());
            let folder = Path::new(&rel).parent().map(sq).unwrap_or_default();
            let root_name = Path::new(&r.root).file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
            json!({"id": format!("file:{}", r.path), "path": r.path, "rel": rel, "folder": folder, "root": root_name,
                   "name": r.name, "ext": r.ext, "kind": r.kind, "mtime": r.mtime, "size": r.size, "n": r.n, "p": r.p,
                   "cluster": r.cluster, "author": r.author,
                   "author_source": r.author_source.filter(|s| s != "none"),
                   "display": r.path.replacen(&home, "~", 1)})
        }).collect();
        Value::Array(out)
    }

    /// One file and its chunks (0.21 `Files.file`), or None.
    pub fn files_file(&self, path: &str) -> Option<Value> {
        let r = self.files_query("SELECT path, root, name, ext, kind, mtime, size, n_chunks, NULL, NULL, NULL, NULL, author, author_source \
                                FROM files WHERE path=?", &[&path]).into_iter().next()?;
        let chunks: Vec<Value> = self.db.read(|c| {
            let mut st = c.prepare("SELECT idx, start, end, text FROM file_chunks WHERE path=? ORDER BY idx")?;
            st.query_map([path], |r| Ok((r.get::<_, Option<i64>>(0)?, r.get::<_, Option<i64>>(1)?, r.get::<_, Option<i64>>(2)?,
                                         r.get::<_, Option<String>>(3)?)))?
                .collect::<rusqlite::Result<Vec<_>>>()
        }).unwrap_or_default().into_iter().filter(|c| c.2.unwrap_or(0) != 0)
            .map(|c| json!({"idx": c.0, "start": c.1, "end": c.2, "text": c.3})).collect();
        let home = sq(&self.config().home);
        Some(json!({"id": format!("file:{}", r.path), "path": r.path, "root": r.root, "name": r.name, "ext": r.ext,
                    "kind": r.kind, "mtime": r.mtime, "size": r.size, "n": r.n, "display": r.path.replacen(&home, "~", 1),
                    "author": r.author, "author_source": r.author_source.filter(|s| s != "none"),
                    "is_dir": r.kind == "folder", "chunks": chunks}))
    }

    /// The files nearest in meaning to one file (by their mean vectors).
    pub fn files_similar(&self, path: &str, k: usize) -> Vec<Value> {
        let (paths, c) = self.files_centroids();
        let Some(i) = paths.iter().position(|p| p == path) else { return Vec::new() };
        let mut sims: Vec<(usize, f32)> = c.iter().enumerate().map(|(j, v)| (j, vectors::dot(v, &c[i]))).collect();
        sims.sort_by(|a, b| b.1.total_cmp(&a.1));
        let home = sq(&self.config().home);
        sims.into_iter().skip(1).take(k).map(|(j, s)| {
            let p = &paths[j];
            json!({"id": format!("file:{p}"), "path": p,
                   "name": Path::new(p).file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default(),
                   "display": p.replacen(&home, "~", 1), "score": s as f64})
        }).collect()
    }

    /// GET /api/file?path=: the file with the files most like it. None for a path the index does not have (404).
    pub fn api_file(&self, path: &str) -> Option<Value> {
        let mut r = self.files_file(path)?;
        r["similar"] = Value::Array(self.files_similar(path, 8));
        Some(r)
    }

    /// GET /api/filetypes and MCP file_types: each file type with its settings, its files, and a histogram of their
    /// text length.
    pub fn api_filetypes(&self) -> Value {
        let cfg = self.config();
        let rows: Vec<(String, Option<i64>)> = self.db.read(|c| {
            let mut st = c.prepare("SELECT path, chars FROM files WHERE kind!='folder'")?;
            st.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect()
        }).unwrap_or_default();
        let size = cfg.int("chunk_chars");
        let cap = cfg.int("max_chunks");
        let mut out: Vec<(String, Value)> = TYPES.iter().map(|(t, label, desc, exts)| {
            let mut e: Vec<String> = exts.iter().map(|x| x.trim_start_matches('.').to_string()).collect();
            e.sort();
            e.truncate(12);
            let ft = file_type(&cfg, t);
            (t.to_string(), json!({"id": t, "label": label, "description": desc, "exts": e, "readable": readable(t),
                                   "index": ft["index"], "map": ft["map"], "files": 0, "read": 0,
                                   "bins": vec![0; SIZE_BINS.len() + 1], "cut": 0}))
        }).collect();
        for (path, chars) in rows {
            let t = type_of(&path);
            let Some((_, o)) = out.iter_mut().find(|(id, _)| id == t) else { continue };
            o["files"] = json!(o["files"].as_i64().unwrap_or(0) + 1);
            let Some(chars) = chars.filter(|c| *c != 0) else { continue };
            o["read"] = json!(o["read"].as_i64().unwrap_or(0) + 1);
            let n = chars as f64 / 1500.0;
            let bin = SIZE_BINS.iter().position(|b| n <= *b as f64).unwrap_or(SIZE_BINS.len());
            o["bins"][bin] = json!(o["bins"][bin].as_i64().unwrap_or(0) + 1);
            if cap > 0 && chars > cap * size {
                o["cut"] = json!(o["cut"].as_i64().unwrap_or(0) + 1);
            }
        }
        json!({"types": out.into_iter().map(|(_, v)| v).collect::<Vec<_>>(), "bin_edges": SIZE_BINS,
               "unit_chars": 1500, "chunk_chars": size, "max_chunks": cap})
    }

    pub fn mcp_file_types(&self) -> Value {
        self.api_filetypes()
    }

    /// PUT /api/filetypes: the new `file_types` setting with one type changed, for the app to save (then it calls
    /// `api_filetypes` for the answer). Err with 0.21's message for a bad change (400).
    pub fn put_filetype(&self, id: &str, index: Option<&str>, map: Option<bool>) -> Result<Value, String> {
        let cfg = self.config();
        let defaults = crate::defaults()["file_types"].clone();
        if defaults.get(id).is_none() {
            let valid: Vec<&str> = TYPES.iter().map(|t| t.0).collect();
            return Err(format!("unknown file type {id:?}; valid: {}", valid.join(", ")).replace('"', "'"));
        }
        if index.is_some_and(|i| !["content", "name", "off"].contains(&i)) {
            return Err(format!("file_types.{id}: index is content, name or off; map is true or false"));
        }
        let mut merged = cfg.get("file_types").clone();
        if !merged.is_object() {
            merged = json!({});
        }
        let mut cur = merged.get(id).cloned().unwrap_or_else(|| defaults[id].clone());
        if let Some(i) = index {
            cur["index"] = json!(i);
        }
        if let Some(m) = map {
            cur["map"] = json!(m);
        }
        if id == "other" && cur["index"] == "content" {
            return Err(format!("{id} files have no text to read: index them by name or switch them off"));
        }
        merged[id] = cur;
        Ok(merged)
    }

    /// MCP add_folder: the new `folders` setting (unchanged when the folder is in it), for the app to save. The
    /// answer is `{"folders": <that list>}`.
    pub fn mcp_add_folder(&self, path: &str) -> Value {
        let mut cur = self.config().strs("folders");
        if !cur.iter().any(|f| f == path) {
            cur.push(path.to_string());
        }
        json!({"folders": cur})
    }

    /// MCP remove_folder: the new `folders` setting, for the app to save; also the answer.
    pub fn mcp_remove_folder(&self, path: &str) -> Value {
        let cur: Vec<String> = self.config().strs("folders").into_iter().filter(|f| f != path).collect();
        json!({"folders": cur})
    }

    /// MCP index_path: index a folder from now on, whatever it holds (a git repo, a folder of repos, a folder of
    /// agent sessions, or documents). Returns (the tool's answer, the settings to change: key -> new value). The
    /// app saves the settings and asks the watcher for a full pass. `setup_done`: the setting of that name.
    pub fn mcp_index_path(&self, path: &str, cwd: Option<&str>) -> Result<(Value, Value), String> {
        let cfg = self.config();
        let mut p = cfg.expand(path);
        if !p.is_absolute() {
            p = PathBuf::from(cwd.unwrap_or(".")).join(p);
        }
        let p = p.canonicalize().unwrap_or(p);
        if !p.is_dir() {
            return Err(format!("not a folder: {path}"));
        }
        let home = sq(&cfg.home);
        let tilde = |q: &Path| sq(q).replacen(&home, "~", 1);
        let exclude: HashSet<String> = cfg.strs("exclude").into_iter().collect();
        let git = p.join(".git").exists();
        let mut changes = serde_json::Map::new();
        let (kind, what, sid);
        if git || !find_repos(&p, 2, &exclude).is_empty() {
            kind = "repos";
            what = if git { "git repo" } else { "folder of git repos" };
            let mut cur = cfg.strs("repos");
            if !cur.iter().any(|r| cfg.expand(r) == p) {
                cur.push(tilde(&p));
                changes.insert("repos".into(), json!(cur));
            }
            sid = format!("repos/{}", sq(&p));
        } else if let Some((name, root)) = crate::places::detect_sessions(&p) {
            kind = "agents";
            what = "agent sessions";
            let known = cfg.session_sources();
            let mut s = known.iter().find(|(_, n, r, _)| *n == name && *r == root).map(|x| x.0.clone());
            if s.is_none() {
                let entry = format!("{name}:{}", tilde(&root));
                let mut cur = cfg.strs("session_folders");
                cur.push(entry.clone());
                changes.insert("session_folders".into(), json!(cur));
                s = Some(format!("agents/{name}@{}", tilde(&root)));
            }
            sid = s.unwrap_or_default();
        } else {
            kind = "files";
            what = "documents";
            let t = tilde(&p);
            let mut cur = cfg.strs("folders");
            if !cur.contains(&t) {
                cur.push(t.clone());
                changes.insert("folders".into(), json!(cur));
            }
            sid = format!("files/{t}");
        }
        let mut off: Vec<String> = cfg.strs("disabled").into_iter().filter(|d| *d != sid).collect();
        if let Some(i) = off.iter().position(|d| d == kind) {
            off.remove(i);
            let others: Vec<String> = match kind {
                "files" => cfg.strs("folders").into_iter().map(|f| format!("files/{f}")).collect(),
                "agents" => cfg.session_sources().into_iter().map(|s| s.0).collect(),
                _ => vec!["repos/discover".into()],
            };
            for o in others {
                if o != sid && !off.contains(&o) {
                    off.push(o);
                }
            }
        }
        changes.insert("disabled".into(), json!(off));
        let note = if cfg.flag("setup_done") {
            "Indexing starts within seconds; search covers it as it goes."
        } else {
            "Pensieve isn't set up yet, so nothing is indexed until setup is done (setup_guide)."
        };
        Ok((json!({"id": sid, "path": sq(&p), "indexed_as": what, "note": note}), Value::Object(changes)))
    }

    /// How many files of each kind (0.21 `Files.stats`).
    pub fn files_stats(&self) -> Value {
        let rows: Vec<(Option<String>, i64)> = self.db.read(|c| {
            let mut st = c.prepare("SELECT kind, COUNT(*) FROM files GROUP BY kind")?;
            st.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect()
        }).unwrap_or_default();
        Value::Object(rows.into_iter().map(|(k, n)| (k.unwrap_or_else(|| "null".into()), json!(n))).collect())
    }

    /// The folder being indexed now: {name, root, done, total}.
    pub fn files_progress(&self) -> Option<Value> {
        lock(&self.files.progress).clone()
    }

    /// Media kinds that are read by content now (images, video, audio), from the app's model installs.
    pub fn set_files_content_ready(&self, kinds: &[&str]) {
        *self.files.content_ready.write().unwrap_or_else(|e| e.into_inner()) = kinds.iter().map(|s| s.to_string()).collect();
    }
}

/// Drop one path's chunks and row; returns the chunk ids for the vector index.
fn drop_rows(t: &rusqlite::Connection, path: &str) -> rusqlite::Result<Vec<i64>> {
    let old: Vec<i64> = {
        let mut st = t.prepare("SELECT id FROM file_chunks WHERE path=?")?;
        st.query_map([path], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?
    };
    t.execute("DELETE FROM file_chunks WHERE path=?", [path])?;
    t.execute("DELETE FROM files WHERE path=?", [path])?;
    Ok(old)
}

/// Whether a file under `root` is one the walk would index: no hidden, excluded or package folder on its way, not
/// hidden or secret itself, and not inside a git repo below the root.
pub fn eligible(p: &Path, root: &Path, exclude: &HashSet<String>, exclude_files: &[String]) -> bool {
    let Ok(rel) = p.strip_prefix(root) else { return false };
    let parts: Vec<String> = rel.components().map(|c| c.as_os_str().to_string_lossy().into_owned()).collect();
    if parts.len() > 1 && parts[..parts.len() - 1].iter().any(|x| x.starts_with('.') || exclude.contains(x) || is_package(x)) {
        return false;
    }
    let name = p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    if name.starts_with('.') || name.starts_with("~$") || secret(&name, exclude_files) {
        return false;
    }
    let mut d = p.parent();
    while let Some(dir) = d {
        if dir == root || !dir.starts_with(root) {
            break;
        }
        if dir.join(".git").exists() {
            return false;
        }
        d = dir.parent();
    }
    true
}

/// Git repo roots at or below `base`, `depth` folder levels deep at most, not looking inside a repo once found
/// (0.21 `repos.find_repos`).
pub fn find_repos(base: &Path, depth: usize, exclude: &HashSet<String>) -> Vec<String> {
    if !base.is_dir() {
        return Vec::new();
    }
    if base.join(".git").exists() {
        return vec![sq(&base.canonicalize().unwrap_or(base.to_path_buf()))];
    }
    let mut out = Vec::new();
    fn go(d: &Path, left: usize, exclude: &HashSet<String>, out: &mut Vec<String>) {
        let Ok(rd) = std::fs::read_dir(d) else { return };
        let mut dirs = Vec::new();
        let mut has_git = false;
        for e in rd.flatten() {
            let n = e.file_name().to_string_lossy().into_owned();
            if n == ".git" {
                has_git = true;
            } else if e.path().is_dir() {
                dirs.push(n);
            }
        }
        if has_git {
            out.push(sq(&d.canonicalize().unwrap_or(d.to_path_buf())));
            return;
        }
        if left == 0 {
            return;
        }
        for n in dirs {
            if !n.starts_with('.') && !exclude.contains(&n) {
                go(&d.join(n), left - 1, exclude, out);
            }
        }
    }
    go(base, depth, exclude, &mut out);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chunks_break_on_blank_lines_and_long_lines() {
        let text = format!("{}\n\n{}\n{}", "a".repeat(700), "b".repeat(50), "c".repeat(5000));
        let c = chunk_text(&text, 1500, 0);
        assert_eq!(c[0], (1, 2, format!("{}\n", "a".repeat(700))));
        assert_eq!(c[1].0, 3);
        assert_eq!(c[1].2.chars().count(), 1500);
        assert!(c.iter().all(|x| !x.2.trim().is_empty()));
        assert_eq!(chunk_text("one\ntwo", 1500, 0), vec![(1, 2, "one\ntwo".to_string())]);
        assert_eq!(chunk_text(&text, 1500, 1).len(), 1);
    }

    #[test]
    fn types_and_kinds() {
        assert_eq!(type_of("/a/b/report.PDF"), "pdf");
        assert_eq!(type_of("md"), "text");
        assert_eq!(type_of("/a/x.py"), "code");
        assert_eq!(type_of("/a/x.zip"), "other");
        assert_eq!(kind_of(Path::new("/a/x.xlsx")), "data");
        assert_eq!(media_label(65.0, 32.0), "1:05-1:37");
    }

    #[test]
    fn secrets_match_like_fnmatch() {
        let pats = vec!["*.pem".to_string(), ".env*".to_string(), "*recovery*code*".to_string(), "id_rsa*".into()];
        assert!(secret("Server.PEM", &pats));
        assert!(secret("my recovery codes.txt", &pats));
        assert!(!secret("notes.md", &pats));
        assert!(fnmatch("a1", "a[0-9]"));
        assert!(!fnmatch("ab", "a[!b]"));
    }

    #[test]
    fn walks_and_indexes_a_folder() {
        let dir = std::env::temp_dir().join(format!("files-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("notes/sub")).ok();
        std::fs::create_dir_all(dir.join("repo/.git")).ok();
        std::fs::create_dir_all(dir.join(".hidden")).ok();
        std::fs::write(dir.join("notes/plan.md"), "pricing model for the launch\n\nmore text").ok();
        std::fs::write(dir.join("notes/sub/server.pem"), "x").ok();
        std::fs::write(dir.join("repo/a.py"), "x = 1").ok();
        let mut seen = Vec::new();
        walk(&dir, &HashSet::new(), &["*.pem".to_string()], &mut |e| match e {
            Entry::Folder(p, _) => seen.push(format!("d:{}", p.strip_prefix(&dir).map(sq).unwrap_or_default())),
            Entry::File(p) => seen.push(format!("f:{}", p.strip_prefix(&dir).map(sq).unwrap_or_default())),
        });
        seen.sort();
        assert_eq!(seen, vec!["d:", "d:notes", "d:notes/sub", "f:notes/plan.md"]);
        assert!(!eligible(&dir.join("repo/a.py"), &dir, &HashSet::new(), &[]));

        let ix = crate::testing::index();
        let mut s = ix.config().settings.clone();
        s["folders"] = json!([sq(&dir)]);
        ix.set_config(Config::new(s, PathBuf::from("/home/test")));
        std::thread::sleep(Duration::from_millis(1100));
        let n = ix.files_sync(true, &|_| {});
        assert!(n >= 3, "touched {n}");
        let f = ix.files_file(&sq(&dir.canonicalize().unwrap_or(dir.clone()).join("notes/plan.md")));
        assert!(f.is_some_and(|f| f["chunks"].as_array().map(|a| a.len()) == Some(1)));
        assert!(ix.file_vecs.len() >= 3);
        assert_eq!(ix.files_sync(true, &|_| {}), 0);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
