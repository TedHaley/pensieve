//! Where the user's agents work: the git repos and folders that their sessions ran in, and the files those sessions
//! read and edited (`session_files`). Ranked by use, these show what is most worth indexing: setup and Sources
//! suggest the ones that are not indexed, and the repo sweep indexes the busiest first.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde_json::{Value, json};

use crate::files::fnmatch;
use crate::{Index, SESSION_GLOB};

const SKIP: [&str; 8] = ["/tmp/", "/private/", "/var/", "/System/", "/Library/", "/Applications/", "/opt/", "/usr/"];
/// Seconds a computed list is used again.
pub const TTL: f64 = 300.0;

/// One row of the places query: (session id, its folder, last activity, a file it touched, whether it edited it).
pub type Row = (String, String, String, Option<String>, Option<bool>);

pub const ROWS: &str = "SELECT s.id, s.project, s.updated, f.path, f.edited FROM sessions s \
                        LEFT JOIN session_files f ON f.session_id = s.id WHERE s.n_chunks > 0";

#[derive(Default)]
pub struct Cache(Mutex<Option<(f64, Vec<Value>)>>);

/// The repo root of a folder: linked worktrees and `.claude/worktrees` checkouts fold into the main repo.
pub fn project_root(cwd: &str, home: &Path) -> Option<String> {
    let mut p = PathBuf::from(if cwd.is_empty() { "/nonexistent" } else { cwd });
    let parts: Vec<String> = p.components().map(|c| c.as_os_str().to_string_lossy().into_owned()).collect();
    if let (Some(i), true) = (parts.iter().position(|x| x == ".claude"), parts.iter().any(|x| x == "worktrees")) {
        p = parts[..i].iter().collect();
    }
    let mut q = Some(p.as_path());
    while let Some(dir) = q {
        let g = dir.join(".git");
        if g.is_file() {
            if let Ok(t) = std::fs::read_to_string(&g) {
                if let Some((_, gd)) = t.split_once("gitdir:") {
                    let gd = PathBuf::from(gd.trim());
                    let gp: Vec<_> = gd.components().collect();
                    if let Some(w) = gp.iter().position(|c| c.as_os_str() == "worktrees") {
                        let main: PathBuf = gp[..w].iter().collect();
                        if let Some(m) = main.parent() {
                            return Some(m.to_string_lossy().into_owned());
                        }
                    }
                }
            }
            return Some(dir.to_string_lossy().into_owned());
        }
        if g.exists() {
            return Some(dir.to_string_lossy().into_owned());
        }
        if dir == home {
            break;
        }
        q = dir.parent();
    }
    None
}

/// A place that is not a repo: the folder two levels under home (~/Desktop/notes), or None for what is not worth
/// suggesting (home itself, ~/Library, dot folders).
fn folder(d: &Path, home: &Path) -> Option<PathBuf> {
    let rel = d.strip_prefix(home).ok()?;
    let parts: Vec<String> = rel.components().map(|c| c.as_os_str().to_string_lossy().into_owned()).collect();
    if parts.is_empty() || parts[0].starts_with('.') || parts[0] == "Library" {
        return None;
    }
    Some(parts.iter().take(2).fold(home.to_path_buf(), |p, x| p.join(x)))
}

/// Every place the agents worked: {root, name, kind (repo | folder), sessions, files, edited, last, state,
/// indexed_as}, busiest first. `state(root, kind)` says whether a place is indexed: {state, root?}.
pub fn compute(rows: &[Row], home: &Path, state: &dyn Fn(&str, &str) -> Value) -> Vec<Value> {
    compute_skipping(rows, home, state, &SKIP)
}

/// `skip`: folder prefixes that are never places (system and temp folders).
pub fn compute_skipping(rows: &[Row], home: &Path, state: &dyn Fn(&str, &str) -> Value, skip: &[&str]) -> Vec<Value> {
    struct Acc {
        root: String,
        kind: String,
        sessions: HashSet<String>,
        files: HashSet<String>,
        edited: HashSet<String>,
        last: String,
    }
    let mut wheres: HashMap<String, Option<(String, String)>> = HashMap::new();
    let mut place = |d: &str| -> Option<(String, String)> {
        wheres.entry(d.to_string()).or_insert_with(|| {
            if d.is_empty() || skip.iter().any(|s| d.starts_with(s)) || !d.starts_with('/') {
                return None;
            }
            match project_root(d, home) {
                Some(r) => Some((r, "repo".into())),
                None => folder(Path::new(d), home).map(|f| (f.to_string_lossy().into_owned(), "folder".into())),
            }
        }).clone()
    };
    let mut order: Vec<(String, String)> = Vec::new();
    let mut out: HashMap<(String, String), Acc> = HashMap::new();
    for (sid, cwd, updated, path, edited) in rows {
        let parent = path.as_ref().map(|p| Path::new(p).parent().map(|x| x.to_string_lossy().into_owned()).unwrap_or_default());
        for (d, file) in [(Some(cwd.clone()), None), (parent, path.clone())] {
            let Some(d) = d.filter(|d| !d.is_empty()) else { continue };
            let Some(p) = place(&d) else { continue };
            let o = out.entry(p.clone()).or_insert_with(|| {
                order.push(p.clone());
                Acc { root: p.0.clone(), kind: p.1.clone(), sessions: HashSet::new(), files: HashSet::new(),
                      edited: HashSet::new(), last: String::new() }
            });
            o.sessions.insert(sid.clone());
            if *updated > o.last {
                o.last = updated.clone();
            }
            if let Some(f) = file {
                o.files.insert(f.clone());
                if edited.unwrap_or(false) {
                    o.edited.insert(f.clone());
                }
            }
        }
    }
    let mut res: Vec<(usize, usize, String, Value)> = Vec::new();
    for key in order {
        let o = &out[&key];
        if !Path::new(&o.root).is_dir() {
            continue;
        }
        let st = state(&o.root, &o.kind);
        let indexed_as = st.get("root").and_then(Value::as_str).filter(|r| *r != o.root).map(String::from);
        let name = Path::new(&o.root).file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        res.push((o.sessions.len(), o.files.len(), o.last.clone(), json!({
            "root": o.root, "name": name, "kind": o.kind, "sessions": o.sessions.len(), "files": o.files.len(),
            "edited": o.edited.len(), "last": o.last, "state": st.get("state").cloned().unwrap_or(Value::Null),
            "indexed_as": indexed_as,
        })));
    }
    res.sort_by(|a, b| (b.0, b.1, &b.2).cmp(&(a.0, a.1, &a.2)));
    res.into_iter().map(|x| x.3).collect()
}

/// A Claude Code project folder name back to a folder that exists: each "-" is "/", "-" or "." (the longest folder
/// first); "" when no folder on disk fits.
pub fn decode(name: &str) -> String {
    let parts: Vec<&str> = name.trim_start_matches('-').split('-').collect();
    let mut cur = PathBuf::from("/");
    let mut i = 0;
    'outer: while i < parts.len() {
        for j in (i + 1..=parts.len()).rev() {
            let mut hit = [parts[i..j].join("-"), parts[i..j].join(".")].into_iter()
                .map(|n| cur.join(n)).find(|p| p.is_dir());
            if hit.is_none() && parts[i].is_empty() && j > i + 1 {
                let p = cur.join(format!(".{}", parts[i + 1..j].join("-")));
                hit = p.is_dir().then_some(p);
            }
            if let Some(h) = hit {
                cur = h;
                i = j;
                continue 'outer;
            }
        }
        return String::new();
    }
    cur.to_string_lossy().into_owned()
}

/// The files under `root` that a pathlib glob such as `*/*.jsonl` or `**/rollout-*.jsonl` matches.
pub fn glob(root: &Path, pattern: &str) -> Vec<PathBuf> {
    fn go(d: &Path, segs: &[&str], out: &mut Vec<PathBuf>) {
        let Some((first, rest)) = segs.split_first() else { return };
        if *first == "**" {
            go(d, rest, out);
            if let Ok(rd) = std::fs::read_dir(d) {
                for e in rd.flatten() {
                    if e.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                        go(&e.path(), segs, out);
                    }
                }
            }
            return;
        }
        let Ok(rd) = std::fs::read_dir(d) else { return };
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().into_owned();
            if !fnmatch(&name, first) {
                continue;
            }
            if rest.is_empty() {
                out.push(e.path());
            } else if e.path().is_dir() {
                go(&e.path(), rest, out);
            }
        }
    }
    let segs: Vec<&str> = pattern.split('/').collect();
    let mut out = Vec::new();
    go(root, &segs, &mut out);
    out
}

/// Which agent's transcripts a folder holds, and the folder its glob starts from (a config folder such as ~/.claude
/// holds them in projects/, ~/.codex in sessions/).
pub fn detect_sessions(folder: &Path) -> Option<(String, PathBuf)> {
    for (name, sub) in [("codex", "sessions"), ("qwen", "projects"), ("claude", "projects")] {
        let pat = SESSION_GLOB.iter().find(|(n, _)| *n == name).map(|(_, g)| *g).unwrap_or("");
        for root in [folder.join(sub), folder.to_path_buf()] {
            if root.is_dir() && !glob(&root, pat).is_empty() {
                return Some((name.to_string(), root));
            }
        }
    }
    None
}

impl Index {
    /// ROWS's shape from the transcript files alone, before anything is indexed (setup): one row per session with
    /// its folder and time, and no files.
    pub fn places_transcript_rows(&self) -> Vec<Row> {
        let cfg = self.config();
        let mut folders: HashMap<String, String> = HashMap::new();
        let mut rows = Vec::new();
        let skips: Vec<String> = SKIP.iter().map(|s| format!("{}-", s.trim_end_matches('/').replace('/', "-"))).collect();
        for (_, name, root, pattern) in cfg.session_sources() {
            for f in glob(&root, &pattern) {
                if f.components().any(|c| c.as_os_str() == "subagents") {
                    continue;
                }
                let cwd = if name == "codex" {
                    std::fs::read_to_string(&f).ok().and_then(|t| {
                        let first = t.lines().next().unwrap_or("{}").to_string();
                        serde_json::from_str::<Value>(&first).ok()
                    }).and_then(|v| v.get("payload").and_then(|p| p.get("cwd")).and_then(Value::as_str).map(String::from))
                        .unwrap_or_default()
                } else {
                    let proj = f.strip_prefix(&root).ok().and_then(|r| r.components().next())
                        .map(|c| c.as_os_str().to_string_lossy().into_owned()).unwrap_or_default();
                    folders.entry(proj.clone()).or_insert_with(|| {
                        if skips.iter().any(|s| proj.starts_with(s.as_str())) { String::new() } else { decode(&proj) }
                    }).clone()
                };
                let when = std::fs::metadata(&f).ok().and_then(|m| m.modified().ok())
                    .map(|t| chrono::DateTime::<chrono::Utc>::from(t).to_rfc3339_opts(chrono::SecondsFormat::AutoSi, false))
                    .unwrap_or_default();
                rows.push((f.to_string_lossy().into_owned(), cwd, when, None, None));
            }
        }
        rows
    }

    fn place_rows(&self) -> Vec<Row> {
        self.db.read(|c| {
            let mut st = c.prepare(ROWS)?;
            st.query_map([], |r| Ok((
                r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?.unwrap_or_default(),
                r.get::<_, Option<String>>(2)?.unwrap_or_default(), r.get::<_, Option<String>>(3)?,
                r.get::<_, Option<i64>>(4)?.map(|e| e != 0),
            )))?.collect()
        }).unwrap_or_default()
    }

    /// Whether a folder place is indexed: an enabled folder source covers it.
    pub fn place_folder_state(&self, root: &str) -> Value {
        let cfg = self.config();
        let p = PathBuf::from(root);
        let on = cfg.enabled("files");
        for f in cfg.strs("folders") {
            let fp = cfg.expand(&f);
            if p == fp || p.starts_with(&fp) && p != fp {
                let ok = on && cfg.enabled(&format!("files/{f}"));
                return json!({"state": if ok { "indexed" } else { "declined" }});
            }
        }
        json!({"state": "unknown"})
    }

    /// GET /api/places: where the agents work, busiest first, with whether each place is indexed. `repo_state`
    /// answers for a repo as GET /api/repo-check does (the repo indexer's check); folders are answered here. The
    /// list is used again for TTL seconds unless `fresh`.
    pub fn api_places(&self, fresh: bool, repo_state: &dyn Fn(&str) -> Value) -> Value {
        let home = self.config().home.clone();
        let list = {
            let mut c = self.files.places.0.lock().unwrap_or_else(|e| e.into_inner());
            match &*c {
                Some((at, l)) if !fresh && store::now() - at <= TTL => l.clone(),
                _ => {
                    let state = |root: &str, kind: &str| if kind == "repo" { repo_state(root) } else { self.place_folder_state(root) };
                    let l = compute(&self.place_rows(), &home, &state);
                    *c = Some((store::now(), l.clone()));
                    l
                }
            }
        };
        json!({"places": list, "home": home.to_string_lossy()})
    }

    /// POST /api/places/index for a folder: the settings to change (key -> new value) so that the folder is indexed
    /// as documents and its source is on. The app saves them, then answers with `api_places(true, ..)`. A repo
    /// place goes through the repo choice instead (0.21 `repo_choice`).
    pub fn places_index_folder(&self, root: &str) -> Result<Value, String> {
        let cfg = self.config();
        if !cfg.expand(root).is_dir() {
            return Err(format!("not a folder: {root}"));
        }
        let mut folders = cfg.strs("folders");
        if !folders.iter().any(|f| f == root) {
            folders.push(root.to_string());
        }
        let sid = format!("files/{root}");
        let disabled: Vec<String> = cfg.strs("disabled").into_iter().filter(|d| d != "files" && *d != sid).collect();
        Ok(json!({"folders": folders, "disabled": disabled}))
    }
}

/// `roots` with the ones used most in `ranked` (the places list) first; the rest keep their order.
pub fn busiest_first(roots: &[String], ranked: &[Value]) -> Vec<String> {
    let rank: HashMap<&str, usize> = ranked.iter().enumerate()
        .filter_map(|(i, o)| o.get("root").and_then(Value::as_str).map(|r| (r, i))).collect();
    let mut out = roots.to_vec();
    out.sort_by_key(|r| rank.get(r.as_str()).copied().unwrap_or(rank.len()));
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ranks_places_by_use() {
        let dir = std::env::temp_dir().join(format!("places-test-{}", std::process::id()));
        let home = dir.join("home");
        let repo = home.join("work/app");
        std::fs::create_dir_all(repo.join(".git")).ok();
        std::fs::create_dir_all(home.join("Desktop/notes/deep")).ok();
        let r = |s: &str, cwd: &Path, f: Option<&str>| (s.to_string(), cwd.to_string_lossy().into_owned(), "2026-01-02".to_string(),
                                                       f.map(String::from), Some(true));
        let rows = vec![
            r("a", &repo.join("src"), Some(&repo.join("src/x.rs").to_string_lossy())),
            r("b", &repo, None),
            r("c", &home.join("Desktop/notes/deep"), None),
            r("d", Path::new("/tmp/x"), None),
        ];
        let out = compute_skipping(&rows, &home, &|_, _| json!({"state": "unknown"}), &["/tmp/"]);
        assert_eq!(out.len(), 2);
        assert_eq!(out[0]["kind"], "repo");
        assert_eq!(out[0]["sessions"], 2);
        assert_eq!(out[0]["edited"], 1);
        assert_eq!(out[1]["root"], home.join("Desktop/notes").to_string_lossy().as_ref());
        assert_eq!(busiest_first(&["z".into(), repo.to_string_lossy().into_owned()], &out)[0], repo.to_string_lossy());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn globs_like_pathlib() {
        let dir = std::env::temp_dir().join(format!("glob-test-{}", std::process::id()));
        std::fs::create_dir_all(dir.join("p/chats")).ok();
        std::fs::create_dir_all(dir.join("2026/01")).ok();
        std::fs::write(dir.join("p/s.jsonl"), "").ok();
        std::fs::write(dir.join("p/chats/q.jsonl"), "").ok();
        std::fs::write(dir.join("2026/01/rollout-1.jsonl"), "").ok();
        assert_eq!(glob(&dir, "*/*.jsonl").len(), 1);
        assert_eq!(glob(&dir, "*/chats/*.jsonl").len(), 1);
        assert_eq!(glob(&dir, "**/rollout-*.jsonl").len(), 1);
        assert_eq!(detect_sessions(&dir).map(|x| x.0), Some("codex".to_string()));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
