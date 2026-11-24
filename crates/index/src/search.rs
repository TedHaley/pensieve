//! One search box over files, code, agent sessions and collected documents (`find`), and the MCP search tool's
//! one-line results. The query language is in `query`; text lookups in `words`; code ranking in `code`; folders,
//! repos and people in `folders`; result lines in `lines`.
//!
//! A search reads SQLite through its own read-only connection and the vectors under their read locks, so indexing
//! never holds it up. Edits that the file watch saw are indexed first, so a search right after an edit finds the new
//! text.

pub mod code;
pub mod folders;
pub mod lines;
pub mod query;
pub mod words;

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::{Arc, Mutex, RwLock};
use std::time::Instant;

use serde::Serialize;
use serde_json::{Value, json};
use store::rusqlite::{self, Connection, types::Value as Sql};

use crate::db::marks;
use crate::scopes::{self, Aliases, Scope};
use crate::vectors::from_blob;
use crate::Index;
pub use query::{Query, parse};

/// Chunk corpora in the order a search runs them.
pub const CORPORA: [&str; 3] = ["file", "code", "session"];

/// A collected document as Markdown (store.db's `documents.markdown`, which the app makes), for read.
pub type DocMarkdown = Arc<dyn Fn(i64) -> Option<String> + Send + Sync>;

/// What search keeps between calls.
#[derive(Default)]
pub struct State {
    pub(crate) aliases: Mutex<Option<(Instant, Arc<Aliases>)>>,
    pub(crate) allow_cache: Mutex<HashMap<String, (Instant, Option<Vec<i64>>)>>,
    pub(crate) df: Mutex<code::Df>,
    pub(crate) folders: RwLock<Option<Arc<folders::FolderIndex>>>,
    markdown: RwLock<Option<DocMarkdown>>,
}

/// One search result. Optional fields that 0.21 added only to some results are left out when empty.
#[derive(Debug, Clone, Default, Serialize)]
pub struct Hit {
    pub id: String,
    pub kind: String,
    pub title: String,
    pub subtitle: String,
    pub path: Option<String>,
    pub line: Option<i64>,
    pub score: f64,
    pub ext: String,
    #[serde(rename = "match")]
    pub matched: String,
    pub mtime: Value,
    pub is_dir: bool,
    pub author: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub private: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub snippet: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub highlights: Option<Vec<[usize; 2]>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stale: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub definition: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub focus: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub focus_line: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub chunk_line: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub excerpt: Option<Vec<(i64, String)>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub near_copy: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub own_differs: Option<bool>,
    #[serde(skip)]
    pub row: Option<(String, i64)>,
    #[serde(skip)]
    pub key: Option<String>,
}

/// A chunk as a result shows it.
#[derive(Debug, Clone, Default)]
pub struct Row {
    pub id: String,
    pub text: String,
    pub vec: Vec<u8>,
    pub title: String,
    pub subtitle: String,
    pub path: Option<String>,
    pub line: Option<i64>,
    pub mtime: Value,
    pub ext: String,
    pub key: Option<String>,
    pub is_dir: bool,
    pub author: Option<String>,
    pub root: Option<String>,
    pub url: Option<String>,
    pub private: Option<bool>,
}

#[derive(Debug, Clone, Default)]
struct Score {
    bm25: f64,
    sem: Option<f64>,
    name: bool,
}

/// Scores by (kind, chunk id), in the order they were first set.
#[derive(Default)]
struct Hits {
    v: Vec<((String, i64), Score)>,
    at: HashMap<(String, i64), usize>,
}

impl Hits {
    fn put(&mut self, k: (String, i64), s: Score) {
        match self.at.get(&k) {
            Some(&i) => self.v[i].1 = s,
            None => {
                self.at.insert(k.clone(), self.v.len());
                self.v.push((k, s));
            }
        }
    }

    fn get(&self, k: &(String, i64)) -> Option<Score> {
        self.at.get(k).map(|&i| self.v[i].1.clone())
    }
}

/// `round(x, 4)`.
pub fn round4(x: f64) -> f64 {
    (x * 1e4).round() / 1e4
}

/// mtime (seconds) or an ISO date as seconds, for the newest-first tie-break.
pub fn ts(m: &Value) -> f64 {
    match m {
        Value::Number(n) => n.as_f64().unwrap_or(0.0),
        Value::String(s) => crate::tokens::iso_seconds(&s.replace('Z', "+00:00")).unwrap_or(0.0),
        _ => 0.0,
    }
}

fn iso_utc(t: f64) -> String {
    chrono::DateTime::from_timestamp(t as i64, 0).map(|d| d.format("%Y-%m-%dT%H:%M:%S").to_string()).unwrap_or_default()
}

fn name_of(p: &str) -> String {
    Path::new(p).file_name().map(|x| x.to_string_lossy().into_owned()).unwrap_or_default()
}

fn ids_param(ids: &[i64]) -> Vec<Sql> {
    ids.iter().map(|i| Sql::Integer(*i)).collect()
}

/// Laps of one search, in milliseconds.
struct Laps {
    t: Instant,
    ms: serde_json::Map<String, Value>,
}

impl Laps {
    fn lap(&mut self, name: &str) {
        let now = Instant::now();
        let prev = self.ms.get(name).and_then(Value::as_f64).unwrap_or(0.0);
        self.ms.insert(name.into(), json!(((prev + (now - self.t).as_secs_f64() * 1000.0) * 100.0).round() / 100.0));
        self.t = now;
    }
}

impl Index {
    pub fn set_doc_markdown(&self, f: DocMarkdown) {
        *self.search.markdown.write().unwrap_or_else(|e| e.into_inner()) = Some(f);
    }

    pub fn doc_markdown(&self, id: i64) -> Option<String> {
        self.search.markdown.read().unwrap_or_else(|e| e.into_inner()).as_ref().and_then(|f| f(id))
    }

    /// The connection a collected document came from (`doc_meta` of the document index).
    pub fn doc_source(&self, id: i64) -> Option<String> {
        self.db.read(|c| c.query_row("SELECT source_id FROM doc_meta WHERE doc_id = ?", [id], |r| r.get(0))).ok()
    }

    /// GET /api/find: `kinds` are chunk corpora (file, code, session; doc for collected documents), `scope` a
    /// resolved scope (`scope_param`). Errors on a bad since:/until:.
    pub fn find(&self, q: &str, limit: usize, kinds: Option<&[String]>, scope: Option<&Scope>) -> Result<Value, String> {
        self.find_once(q, limit, kinds, scope, true)
    }

    /// GET /api/find?q=&limit=&kinds=&scope=&cwd=: `kinds` comma-separated, `scope` absent means the default_scope
    /// setting. Errors on an unknown scope (404) or a bad since:/until: (400).
    pub fn api_find(&self, q: &str, limit: usize, kinds: &str, scope: Option<&str>, cwd: Option<&str>) -> Result<Value, String> {
        let sc = self.scope_param(scope, cwd)?;
        let k: Vec<String> = kinds.split(',').map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect();
        self.find(q, limit, (!k.is_empty()).then_some(k.as_slice()), sc.as_ref())
    }

    /// Index the edits the watch saw (and `roots`, `paths`) now.
    fn catch_up(&self, roots: &[String], paths: &[String]) -> usize {
        self.repos_refresh(roots) + self.files_refresh(paths)
    }

    fn find_once(&self, q: &str, limit: usize, kinds_in: Option<&[String]>, scope: Option<&Scope>, retry: bool)
                 -> Result<Value, String> {
        let kinds = kinds_in;
        let mut lap = Laps { t: Instant::now(), ms: Default::default() };
        if retry {
            self.catch_up(&[], &[]);
            lap.lap("catch-up");
        }
        let pq = parse(q)?;
        let kf = pq.get("kind").map(String::from);
        let kf = kf.as_deref();
        let ext = pq.get("ext").map(String::from);
        let by = pq.get("by").map(String::from);
        let within = pq.get("in").map(String::from);
        let project = pq.get("project").map(String::from);
        let (lo, hi) = (pq.since, pq.until);
        let mut docs_too = kinds.is_none_or(|k| k.iter().any(|x| x == "doc")) && matches!(kf, None | Some("doc") | Some("file"))
            && ext.is_none() && by.is_none();
        let kind_ok = |k: &str| match kf {
            None => true,
            Some("file") => k == "file" || k == "code",
            Some("doc") => k == "file",
            Some("folder") | Some("repo") | Some("person") => false,
            Some(f) => k == f,
        };
        let base: Vec<String> = match kinds {
            Some(k) if !k.is_empty() => k.to_vec(),
            _ => CORPORA.iter().map(|s| s.to_string()).collect(),
        };
        let mut kinds: Vec<String> = base.into_iter().filter(|k| CORPORA.contains(&k.as_str()) && kind_ok(k)).collect();
        if ext.is_some() {
            kinds.retain(|k| k != "session");
        }
        if project.is_some() {
            kinds.retain(|k| k == "session");
            docs_too = false;
        }
        if let Some(b) = &by {
            let agent = ["claude", "codex", "qwen"].iter().any(|a| a.contains(b.as_str()));
            kinds.retain(|k| k != "session" || agent);
        }
        let qv: Option<Vec<f32>> = (!pq.semantic.is_empty()).then(|| self.embed_query(&pq.semantic));
        lap.lap("embed");
        let roots: Option<Vec<String>> = scope.and_then(|s| s.repos.as_ref()).map(|r| r.iter().cloned().collect());
        let mut code_ranked: HashMap<(String, i64), f64> = HashMap::new();
        let mut hits = Hits::default();
        let exact_short: Vec<String> = pq.exact.iter().filter(|p| p.chars().count() < 3).cloned().collect();
        for kind in &kinds {
            let (table, _) = words::corpus(kind);
            let mut allow = scope.and_then(|_| self.allowlist(scope, kind));
            if allow.as_ref().is_some_and(|a| a.is_empty()) {
                continue;
            }
            if kind == "session" && (lo.is_some() || hi.is_some() || project.is_some()) {
                let ids = self.session_chunks(lo, hi, project.as_deref(), false, None);
                allow = Some(match allow {
                    None => ids,
                    Some(a) => {
                        let s: HashSet<i64> = a.into_iter().collect();
                        let mut v: Vec<i64> = ids.into_iter().filter(|i| s.contains(i)).collect();
                        v.sort_unstable();
                        v.dedup();
                        v
                    }
                });
                if allow.as_ref().is_some_and(|a| a.is_empty()) {
                    continue;
                }
                if pq.semantic.is_empty() && pq.exact.is_empty() {
                    let newest = self.session_chunks(lo, hi, project.as_deref(), true, allow.as_deref());
                    for (n, cid) in newest.into_iter().take(limit).enumerate() {
                        hits.put((kind.clone(), cid), Score { sem: Some(1.0 - n as f64 / 1000.0), ..Default::default() });
                    }
                    continue;
                }
            }
            let allowed: Option<HashSet<i64>> = if pq.exact.is_empty() { None } else { allow.as_ref().map(|a| a.iter().copied().collect()) };
            if !pq.exact.is_empty() {
                let lim = if allowed.is_none() { 400 } else { 5000 };
                let rows = self.db.read(|c| words::contains(c, kind, &pq.exact, lim, roots.as_deref(), true)).unwrap_or_default();
                for (cid, bm) in rows {
                    if allowed.as_ref().is_none_or(|a| a.contains(&cid)) {
                        hits.put((kind.clone(), cid), Score { bm25: bm, ..Default::default() });
                    }
                }
                if kind == "file" {
                    let where_ = pq.exact.iter().map(|_| "instr(lower(f.name), ?)>0").collect::<Vec<_>>().join(" AND ");
                    let sql = format!("SELECT MIN(c.id) FROM files f JOIN file_chunks c ON c.path=f.path WHERE {where_} \
                                       GROUP BY f.path LIMIT 200");
                    let args: Vec<String> = pq.exact.iter().map(|p| p.to_lowercase()).collect();
                    let named: Vec<i64> = self.db.read(|c| {
                        c.prepare(&sql)?.query_map(rusqlite::params_from_iter(args.iter()), |r| r.get(0))?.collect()
                    }).unwrap_or_default();
                    for cid in named {
                        if allowed.as_ref().is_none_or(|a| a.contains(&cid)) {
                            let k = (kind.clone(), cid);
                            let mut h = hits.get(&k).unwrap_or_default();
                            h.bm25 += 50.0;
                            h.name = true;
                            hits.put(k, h);
                        }
                    }
                }
            } else if let (Some(v), "code") = (&qv, kind.as_str()) {
                if let Some(b) = &by {
                    allow = Some(self.by_allow(b, allow.as_deref()));
                    if allow.as_ref().is_some_and(|a| a.is_empty()) {
                        continue;
                    }
                }
                let k = if lo.is_some() || hi.is_some() { 2000 } else { 400 };
                let mut ranked: Vec<(i64, f64)> = self.code_search(&pq.semantic, allow.as_deref(), k, roots.as_deref(), v)
                    .into_iter().collect();
                ranked.sort_by_key(|x| x.0);
                for (cid, sc) in ranked {
                    hits.put((kind.clone(), cid), Score { sem: Some(sc), ..Default::default() });
                    code_ranked.insert((kind.clone(), cid), sc);
                }
            } else if let Some(v) = &qv {
                let idx = self.vecs_of(kind);
                let has: bool = self.db.read(|c| c.query_row(&format!("SELECT 1 FROM {table} LIMIT 1"), [], |_| Ok(true)))
                    .unwrap_or(false);
                if has {
                    let mut k = 60usize.max(limit * 4);
                    if (lo.is_some() || hi.is_some()) && kind != "session" {
                        k = k.max(2000);
                    }
                    let found = match &allow {
                        Some(a) => idx.search(v, k.min(a.len()), Some(a)),
                        None => idx.search(v, k, None),
                    };
                    for (cid, s) in found {
                        hits.put((kind.clone(), cid), Score { sem: Some(s as f64), ..Default::default() });
                    }
                }
            }
        }
        lap.lap("corpora");
        let mut rows = self.rows(&hits.v.iter().map(|h| h.0.clone()).collect::<Vec<_>>());
        lap.lap("rows");
        let mut hit_of: HashMap<(String, i64), Score> = hits.v.into_iter().collect();
        if let (Some(v), false) = (&qv, pq.exact.is_empty()) {
            for (key, r) in &rows {
                if let Some(h) = hit_of.get_mut(key) {
                    h.sem = Some(from_blob(&r.vec).iter().zip(v).map(|(a, b)| a * b).sum::<f32>() as f64);
                }
            }
        }
        let words: Vec<String> = pq.semantic.split_whitespace().filter(|w| w.chars().count() >= 3).map(|w| w.to_lowercase()).collect();
        let matched = if !pq.exact.is_empty() && qv.is_some() { "both" } else if !pq.exact.is_empty() { "exact" } else { "semantic" };
        let mut out: Vec<Hit> = Vec::new();
        let mut keys: Vec<&(String, i64)> = rows.keys().collect();
        keys.sort_by(|a, b| {
            let ka = kinds.iter().position(|k| *k == a.0).unwrap_or(9);
            let kb = kinds.iter().position(|k| *k == b.0).unwrap_or(9);
            (ka, a.1).cmp(&(kb, b.1))
        });
        for key in keys {
            let r = &rows[key];
            let Some(h) = hit_of.get(key) else { continue };
            let low = r.text.to_lowercase();
            if exact_short.iter().any(|p| !low.contains(&p.to_lowercase()) && !h.name) {
                continue;
            }
            let where_ = format!("{} {} {}", r.title, r.subtitle, r.path.as_deref().unwrap_or("")).to_lowercase();
            if pq.exclude.iter().any(|x| {
                let x = x.to_lowercase();
                low.contains(&x) || where_.contains(&x)
            }) {
                continue;
            }
            if let Some(e) = &ext {
                if !r.path.as_deref().unwrap_or("").to_lowercase().ends_with(&format!(".{e}")) {
                    continue;
                }
            }
            if let Some(w) = &within {
                if !format!("{} {}", r.path.as_deref().unwrap_or(""), r.subtitle).to_lowercase().contains(w.as_str()) {
                    continue;
                }
            }
            if let Some(b) = &by {
                if !self.by_ok(b, &key.0, r, key.1) {
                    continue;
                }
            }
            let score = if let Some(s) = code_ranked.get(key) {
                *s
            } else if let Some(s) = h.sem {
                s + if words.iter().any(|w| r.title.to_lowercase().contains(w.as_str())) { 0.05 } else { 0.0 }
            } else {
                h.bm25
            };
            out.push(Hit {
                id: r.id.clone(), kind: key.0.clone(), title: r.title.clone(), subtitle: r.subtitle.clone(),
                path: r.path.clone(), line: r.line, row: Some(key.clone()), score: round4(score), ext: r.ext.clone(),
                matched: matched.into(), mtime: r.mtime.clone(), key: r.key.clone(), is_dir: r.is_dir,
                author: r.author.clone(), ..Default::default()
            });
        }
        if docs_too {
            out.extend(self.doc_hits(&pq, qv.as_deref(), scope, limit, &mut rows, &words));
            lap.lap("docs");
        }
        if lo.is_some() || hi.is_some() {
            out.retain(|x| lo.is_none_or(|l| ts(&x.mtime) >= l) && hi.is_none_or(|h| ts(&x.mtime) < h));
        }
        out.sort_by(|a, b| b.score.total_cmp(&a.score).then_with(|| ts(&b.mtime).total_cmp(&ts(&a.mtime))));
        let mut best: Vec<Hit> = Vec::new();
        let mut seen = HashSet::new();
        for mut x in out {
            let k = x.key.take().unwrap_or_else(|| x.id.clone());
            if seen.insert(k) {
                best.push(x);
            }
        }
        lap.lap("rank");
        let mut extra: Vec<Hit> = Vec::new();
        let (bys, exts, ins) = (by.as_deref(), ext.as_deref(), within.as_deref());
        if pq.semantic.is_empty() && kf == Some("repo") {
            extra.extend(self.folders("", scope, true, false, limit, bys, exts, ins, None));
        } else if pq.semantic.is_empty() && pq.exact.is_empty() && by.is_some() && matches!(kf, None | Some("folder")) {
            extra.extend(self.folders("", scope, false, false, limit, bys, exts, ins, None));
        }
        if !pq.semantic.is_empty() && matches!(kf, None | Some("folder") | Some("repo")) {
            extra.extend(self.folders(&pq.semantic, scope, kf == Some("repo"), kf.is_none(), limit, bys, exts, ins, qv.as_deref()));
        }
        if !pq.semantic.is_empty() && matches!(kf, None | Some("person")) && by.is_none() {
            extra.extend(self.people_hits(&pq.semantic, scope, kf.is_none(), 8, qv.as_deref()));
        }
        if matches!(kf, Some("folder") | Some("repo") | Some("person")) {
            best = extra;
        } else if !extra.is_empty() {
            let ids: HashSet<String> = extra.iter().map(|e| e.id.clone()).collect();
            best = extra.into_iter().chain(best.into_iter().filter(|x| !ids.contains(&x.id))).collect();
        }
        lap.lap("folders+people");
        let (mut results, roots_stale, paths_stale) = on_disk(best, &rows, limit);
        let terms: Vec<String> = if pq.exact.is_empty() { words.clone() } else { pq.exact.clone() };
        for x in results.iter_mut() {
            if let Some(row) = x.row.take() {
                if let Some(r) = rows.get(&row) {
                    let (s, h) = lines::snippet(&r.text, &terms, 220);
                    x.snippet = Some(s);
                    x.highlights = Some(h);
                }
            }
        }
        lap.lap("on-disk");
        if retry && (!roots_stale.is_empty() || !paths_stale.is_empty()) {
            let r: Vec<String> = roots_stale.into_iter().collect();
            let p: Vec<String> = paths_stale.into_iter().collect();
            if self.catch_up(&r, &p) > 0 {
                return self.find_once(q, limit, kinds_in, scope, false);
            }
        }
        let mut shown = 0;
        for x in results.iter_mut() {
            if x.kind != "code" {
                continue;
            }
            let Some(line) = x.line else { continue };
            let cid: i64 = x.id.split_once(':').and_then(|p| p.1.parse().ok()).unwrap_or(-1);
            let Some(r) = rows.get(&("code".to_string(), cid)) else { continue };
            let (off, def, focus) = lines::focus(&r.text, &pq.semantic, &pq.exact);
            x.definition = Some(def.map(Value::String).unwrap_or(Value::Null));
            x.focus = Some(focus);
            x.focus_line = Some(line + off as i64);
            x.chunk_line = Some(line);
            x.line = x.focus_line;
            if shown < 3 {
                shown += 1;
                x.excerpt = Some(lines::excerpt(&r.text, line, &pq.semantic, &pq.exact));
            }
        }
        lap.lap("lines");
        lap.ms.insert("busy".into(), json!(self.busy()));
        Ok(json!({"query": pq.to_json(), "results": results, "scope": scopes::describe(scope), "timing": lap.ms}))
    }

    /// What is indexing now, for the timing of a search.
    fn busy(&self) -> Vec<&'static str> {
        let mut out = Vec::new();
        if self.repos_progress().is_some() {
            out.push("repos");
        }
        if self.files.progress.lock().map(|p| p.is_some()).unwrap_or(false) {
            out.push("files");
        }
        out
    }

    pub(crate) fn vecs_of(&self, kind: &str) -> &crate::VecIndex {
        match kind {
            "file" => &self.file_vecs,
            "code" => &self.code_vecs,
            "doc" => &self.doc_vecs,
            _ => &self.session_vecs,
        }
    }

    /// The chunk ids of the sessions with their last activity in [lo, hi) whose project has `project` in its name
    /// or folder. `newest`: the first chunk of each session, the newest session first, within `allow`.
    fn session_chunks(&self, lo: Option<f64>, hi: Option<f64>, project: Option<&str>, newest: bool, allow: Option<&[i64]>)
                      -> Vec<i64> {
        let mut wh = vec!["1".to_string()];
        let mut args: Vec<String> = Vec::new();
        if let Some(l) = lo {
            wh.push("s.updated >= ?".into());
            args.push(iso_utc(l));
        }
        if let Some(h) = hi {
            wh.push("s.updated < ?".into());
            args.push(iso_utc(h));
        }
        if let Some(p) = project {
            wh.push("(instr(lower(s.project_name), ?) > 0 OR instr(lower(s.project), ?) > 0)".into());
            args.push(p.into());
            args.push(p.into());
        }
        let sql = format!("SELECT {} FROM chunks c JOIN sessions s ON s.id = c.session_id WHERE {}{}",
                          if newest { "MIN(c.id)" } else { "c.id" }, wh.join(" AND "),
                          if newest { " GROUP BY s.id ORDER BY MAX(s.updated) DESC" } else { "" });
        let ids: Vec<i64> = self.db.read(|c| {
            c.prepare(&sql)?.query_map(rusqlite::params_from_iter(args.iter()), |r| r.get(0))?.collect()
        }).unwrap_or_default();
        if newest {
            return match allow {
                Some(a) => {
                    let s: HashSet<i64> = a.iter().copied().collect();
                    ids.into_iter().filter(|i| s.contains(i)).collect()
                }
                None => ids,
            };
        }
        ids.into_iter().filter(|i| self.session_vecs.contains(*i)).collect()
    }

    /// Rows of chunks by (kind, id), as results show them.
    pub fn rows(&self, keys: &[(String, i64)]) -> HashMap<(String, i64), Row> {
        let home = self.config().home.to_string_lossy().into_owned();
        let mut by: Vec<(String, Vec<i64>)> = Vec::new();
        for (k, id) in keys {
            match by.iter_mut().find(|x| &x.0 == k) {
                Some(x) => x.1.push(*id),
                None => by.push((k.clone(), vec![*id])),
            }
        }
        let mut out = HashMap::new();
        let _ = self.db.read(|c| {
            for (kind, ids) in &by {
                for part in ids.chunks(900) {
                    rows_of(c, kind, part, &home, &mut out)?;
                }
            }
            Ok(())
        });
        out
    }

    /// Hits in the collected documents, as results with the id doc:<id>. Their rows go into `rows`.
    fn doc_hits(&self, pq: &Query, qv: Option<&[f32]>, scope: Option<&Scope>, limit: usize,
                rows: &mut HashMap<(String, i64), Row>, words: &[String]) -> Vec<Hit> {
        let allow: Option<Vec<i64>> = scope.and_then(|s| s.docs.as_ref())
            .map(|srcs| self.docs_allow(&srcs.iter().cloned().collect::<Vec<_>>()));
        if allow.as_ref().is_some_and(|a| a.is_empty()) {
            return Vec::new();
        }
        let exact_short: Vec<String> = pq.exact.iter().filter(|p| p.chars().count() < 3).cloned().collect();
        let exact_fts: Vec<String> = pq.exact.iter().filter(|p| p.chars().count() >= 3).cloned().collect();
        let mut hits: Vec<(i64, Score)> = Vec::new();
        if !pq.exact.is_empty() {
            let found: Vec<(i64, f64)> = if !exact_fts.is_empty() {
                self.docs_match(&words::fts_query(&exact_fts), 400)
            } else {
                self.docs_contains(&exact_short, 400).into_iter().map(|c| (c, 0.0)).collect()
            };
            let ok: Option<HashSet<i64>> = allow.as_ref().map(|a| a.iter().copied().collect());
            hits = found.into_iter().filter(|(c, _)| ok.as_ref().is_none_or(|o| o.contains(c)))
                .map(|(c, bm)| (c, Score { bm25: -bm, ..Default::default() })).collect();
        } else if let Some(v) = qv {
            let found = self.docs_search(v, 60usize.max(limit * 4), allow.as_deref());
            hits = found.into_iter().map(|(c, s)| (c, Score { sem: Some(s as f64), ..Default::default() })).collect();
        }
        let got = self.doc_rows(&hits.iter().map(|h| h.0).collect::<Vec<_>>());
        let mut out = Vec::new();
        let mut ordered: Vec<&(i64, Score)> = hits.iter().collect();
        ordered.sort_by_key(|h| h.0);
        for (cid, h) in ordered {
            let Some(r) = got.get(cid) else { continue };
            let low = r.text.to_lowercase();
            if exact_short.iter().any(|p| !low.contains(&p.to_lowercase())) {
                continue;
            }
            let where_ = format!("{} {}", r.title, r.subtitle).to_lowercase();
            if pq.exclude.iter().any(|x| low.contains(&x.to_lowercase()) || where_.contains(&x.to_lowercase())) {
                continue;
            }
            if pq.get("in").is_some_and(|w| !r.subtitle.to_lowercase().contains(w)) {
                continue;
            }
            let mut sem = h.sem;
            if let (Some(v), false) = (qv, pq.exact.is_empty()) {
                sem = Some(from_blob(&r.vec).iter().zip(v).map(|(a, b)| a * b).sum::<f32>() as f64);
            }
            let score = match sem {
                Some(s) => s + if words.iter().any(|w| r.title.to_lowercase().contains(w.as_str())) { 0.05 } else { 0.0 },
                None => h.bm25,
            };
            let matched = if !pq.exact.is_empty() && qv.is_some() { "both" } else if !pq.exact.is_empty() { "exact" } else { "semantic" };
            rows.insert(("doc".into(), *cid), r.clone());
            out.push(Hit {
                id: r.id.clone(), kind: "doc".into(), title: r.title.clone(), subtitle: r.subtitle.clone(),
                row: Some(("doc".into(), *cid)), score: round4(score), url: Some(r.url.clone().unwrap_or_default()),
                private: r.private, matched: matched.into(), mtime: r.mtime.clone(), key: r.key.clone(), ..Default::default()
            });
        }
        out
    }

    /// Document chunks with their document, as search shows a hit.
    pub fn doc_rows(&self, ids: &[i64]) -> HashMap<i64, Row> {
        self.docs_rows(ids).into_iter().map(|(cid, d)| (cid, Row {
            id: d.id, text: d.text, vec: crate::vectors::to_blob(&d.vec), title: d.title, subtitle: d.subtitle,
            mtime: d.mtime.map(|t| json!(t)).unwrap_or(Value::Null), key: Some(d.key), url: d.url,
            private: Some(d.private), ..Default::default()
        })).collect()
    }
}

fn rows_of(c: &Connection, kind: &str, ids: &[i64], home: &str, out: &mut HashMap<(String, i64), Row>) -> rusqlite::Result<()> {
    let ph = marks(ids.len());
    let p = rusqlite::params_from_iter(ids_param(ids));
    match kind {
        "file" => {
            let sql = format!("SELECT c.id, coalesce(c.text, ''), c.vec, c.start, f.path, coalesce(f.name, ''), coalesce(f.ext, ''), \
                               f.mtime, coalesce(f.kind, ''), f.author FROM file_chunks c JOIN files f ON f.path=c.path WHERE c.id IN ({ph})");
            let mut st = c.prepare(&sql)?;
            let it = st.query_map(p, |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, Option<Vec<u8>>>(2)?,
                r.get::<_, Option<i64>>(3)?, r.get::<_, String>(4)?, r.get::<_, String>(5)?, r.get::<_, String>(6)?,
                r.get::<_, Option<f64>>(7)?, r.get::<_, String>(8)?, r.get::<_, Option<String>>(9)?)))?;
            for x in it {
                let (cid, text, vec, start, path, name, ext, mtime, k, author) = x?;
                let parent = Path::new(&path).parent().map(|p| p.to_string_lossy().into_owned()).unwrap_or_default().replacen(home, "~", 1);
                out.insert(("file".into(), cid), Row {
                    id: format!("file:{path}"), text, vec: vec.unwrap_or_default(), title: name, ext,
                    subtitle: if k == "folder" { format!("Folder in {parent}") } else { parent },
                    mtime: mtime.map(|m| json!(m)).unwrap_or(Value::Null), is_dir: k == "folder", author,
                    line: start.filter(|s| *s != 0 && ["code", "doc", "data"].contains(&k.as_str())),
                    path: Some(path), ..Default::default()
                });
            }
        }
        "code" => {
            let sql = format!("SELECT c.id, coalesce(c.text, ''), c.vec, c.start, coalesce(c.path, ''), r.root, coalesce(r.name, ''), \
                               c.last_ts, coalesce(c.wt, ''), w.branch FROM code_chunks c JOIN repos r ON r.root=c.repo \
                               LEFT JOIN worktrees w ON w.path=c.wt WHERE c.id IN ({ph})");
            let mut st = c.prepare(&sql)?;
            let it = st.query_map(p, |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, Option<Vec<u8>>>(2)?,
                r.get::<_, Option<i64>>(3)?, r.get::<_, String>(4)?, r.get::<_, String>(5)?, r.get::<_, String>(6)?,
                r.get::<_, Option<i64>>(7)?, r.get::<_, String>(8)?, r.get::<_, Option<String>>(9)?)))?;
            for x in it {
                let (cid, text, vec, start, path, root, rname, ts, wt, branch) = x?;
                let parent = Path::new(&path).parent().map(|p| p.to_string_lossy().into_owned()).unwrap_or_default();
                let where_ = if !parent.is_empty() && parent != "." {
                    format!("{rname}/{parent}").trim_end_matches(['/', '.']).to_string()
                } else {
                    rname
                };
                let at = if wt.is_empty() { String::new() } else {
                    format!(" @ {}", branch.filter(|b| !b.is_empty()).unwrap_or_else(|| name_of(&wt)))
                };
                let base = if wt.is_empty() { root.clone() } else { wt.clone() };
                let ext = crate::extract::suffix(Path::new(&path)).trim_start_matches('.').to_string();
                out.insert(("code".into(), cid), Row {
                    id: format!("code:{cid}"), text, vec: vec.unwrap_or_default(), title: name_of(&path), ext,
                    path: Some(Path::new(&base).join(&path).to_string_lossy().into_owned()), line: start,
                    mtime: ts.map(|t| json!(t)).unwrap_or(Value::Null), key: Some(format!("{root}:{path}")),
                    root: Some(root), subtitle: format!("{where_}{at}"), ..Default::default()
                });
            }
        }
        _ => {
            let sql = format!("SELECT c.id, coalesce(c.text, ''), c.vec, s.id, s.title, coalesce(s.source, ''), \
                               coalesce(s.project_name, ''), s.updated FROM chunks c JOIN sessions s ON s.id=c.session_id \
                               WHERE c.id IN ({ph})");
            let mut st = c.prepare(&sql)?;
            let it = st.query_map(p, |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, Option<Vec<u8>>>(2)?,
                r.get::<_, String>(3)?, r.get::<_, Option<String>>(4)?, r.get::<_, String>(5)?, r.get::<_, String>(6)?,
                r.get::<_, Option<String>>(7)?)))?;
            for x in it {
                let (cid, text, vec, sid, title, src, proj, upd) = x?;
                let day: String = upd.as_deref().unwrap_or("").chars().take(10).collect();
                let subtitle = [src, proj, day].into_iter().filter(|x| !x.is_empty()).collect::<Vec<_>>().join(" · ");
                out.insert(("session".into(), cid), Row {
                    id: format!("session:{sid}"), text, vec: vec.unwrap_or_default(),
                    title: title.filter(|t| !t.is_empty()).unwrap_or_else(|| sid.clone()),
                    mtime: upd.map(Value::String).unwrap_or(Value::Null), subtitle, ..Default::default()
                });
            }
        }
    }
    Ok(())
}

/// The first `limit` results that still exist on disk, and the repos and files of results whose file changed since
/// it was indexed. Those are marked stale; a code hit whose text only moved gets its new line.
fn on_disk(best: Vec<Hit>, rows: &HashMap<(String, i64), Row>, limit: usize) -> (Vec<Hit>, HashSet<String>, HashSet<String>) {
    let (mut out, mut roots, mut paths) = (Vec::new(), HashSet::new(), HashSet::new());
    for mut x in best {
        if out.len() == limit {
            break;
        }
        let path = x.path.clone().unwrap_or_default();
        if !(x.kind == "code" || x.kind == "file") || path.is_empty() || x.is_dir {
            out.push(x);
            continue;
        }
        if x.kind == "file" {
            match std::fs::metadata(&path) {
                Ok(m) => {
                    let now = crate::files::mtime_of(&m);
                    if x.mtime.as_f64().is_none_or(|t| (t - now).abs() > 1e-6) {
                        x.stale = Some(true);
                        paths.insert(path);
                    }
                }
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                    paths.insert(path);
                    continue;
                }
                Err(_) => {}
            }
            out.push(x);
            continue;
        }
        let cid: i64 = x.id.split_once(':').and_then(|p| p.1.parse().ok()).unwrap_or(-1);
        let Some(r) = rows.get(&("code".to_string(), cid)) else {
            out.push(x);
            continue;
        };
        match std::fs::read(&path) {
            Ok(b) => {
                let text = String::from_utf8_lossy(&b);
                let lines: Vec<&str> = text.split('\n').collect();
                let n = r.text.matches('\n').count() + 1;
                let from = (x.line.unwrap_or(1) - 1).max(0) as usize;
                let have = lines.get(from..(from + n).min(lines.len())).map(|l| l.join("\n")).unwrap_or_default();
                if have != r.text {
                    x.stale = Some(true);
                    if let Some(root) = &r.root {
                        roots.insert(root.clone());
                    }
                    if let Some(i) = text.find(&r.text) {
                        x.line = Some(text[..i].matches('\n').count() as i64 + 1);
                    }
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                if let Some(root) = &r.root {
                    roots.insert(root.clone());
                }
                continue;
            }
            Err(_) => {}
        }
        out.push(x);
    }
    (out, roots, paths)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing;

    fn seed(ix: &Index, name: &str) {
        let root = std::env::temp_dir().join(format!("index-search-{}-{name}", std::process::id()));
        let root_s = root.to_string_lossy().into_owned();
        let v = |t: &str| crate::vectors::to_blob(&ix.embed_docs(&[t.to_string()], None)[0]);
        let w = ix.db.write();
        w.execute("INSERT INTO repos VALUES (?, 'app', '', '', 0)", [&root_s]).expect("repo");
        let code = [
            ("src/billing/invoice.py", "def send_invoice(order):\n    retry_webhook(order)\n    return True"),
            ("src/billing/tests/test_invoice.py", "def test_send_invoice():\n    assert send_invoice(1)"),
            ("src/hooks.py", "def retry_webhook(event):\n    raise ValueError(\"webhook delivery failed for order\")"),
        ];
        for (i, (p, t)) in code.iter().enumerate() {
            let f = root.join(p);
            std::fs::create_dir_all(f.parent().expect("parent")).expect("dir");
            std::fs::write(&f, t).expect("file");
            w.execute("INSERT INTO code_chunks (id, repo, path, dir, start, end, text, vec, authors, last_ts, wt) \
                       VALUES (?, ?, ?, '', 1, 3, ?, ?, '{\"Sam Lee <sam@x>\": 3}', 100, '')",
                      rusqlite::params![i as i64 + 1, root_s, p, t, v(t)]).expect("chunk");
        }
        w.execute("INSERT INTO sessions (id, source, project, title, path, updated, n_chunks, project_name) \
                   VALUES ('claude:s1', 'claude', '/w/app', 'Fix the invoice webhook', '/x', '2026-10-01T10:00:00', 1, 'app')", [])
            .expect("session");
        w.execute("INSERT INTO chunks (id, session_id, idx, hash, text, vec) VALUES (1, 'claude:s1', 0, 'h', \
                   'User: the invoice webhook fails', ?)", [v("User: the invoice webhook fails")]).expect("chunk");
        drop(w);
        ix.db.read(|c| {
            ix.code_vecs.load(c, "code_chunks")?;
            ix.session_vecs.load(c, "chunks")
        }).expect("load");
    }

    #[test]
    fn finds_code_sessions_and_exact_text() {
        let ix = testing::index();
        seed(&ix, "find");
        let r = ix.find("send invoice retry webhook", 10, None, None).expect("find");
        let ids: Vec<&str> = r["results"].as_array().expect("results").iter().filter_map(|x| x["id"].as_str()).collect();
        assert!(ids.contains(&"code:1") && ids.contains(&"session:claude:s1"));
        let first_code = ids.iter().position(|i| i.starts_with("code:")).unwrap_or(99);
        assert_eq!(ids[first_code], "code:1");
        let r = ix.find("\"delivery failed\"", 10, Some(&["code".to_string()]), None).expect("find");
        let res = r["results"].as_array().expect("results");
        assert_eq!(res.len(), 1);
        assert_eq!(res[0]["id"], "code:3");
        assert_eq!(res[0]["match"], "exact");
        let r = ix.find("webhook -invoice kind:code", 10, None, None).expect("find");
        assert!(r["results"].as_array().expect("results").iter().all(|x| x["id"] == "code:3"));
    }

    /// Query vectors that 0.21's embedder made, by query text.
    struct Stored(HashMap<String, Vec<f32>>);

    impl crate::Embed for Stored {
        fn embed(&self, texts: &[&str], _query: bool) -> Vec<Vec<f32>> {
            texts.iter().map(|t| self.0.get(*t).cloned().unwrap_or_else(|| vec![0.0; 768])).collect()
        }
        fn dim(&self) -> usize {
            768
        }
    }

    /// Parity with 0.21's /api/find: PARITY_DIR holds copy.db (a copy of 0.21's index.db), live.json (its answers)
    /// and qvecs.json (the query vectors); this writes rust.json beside them.
    #[test]
    #[ignore]
    fn find_parity() {
        let dir = std::path::PathBuf::from(std::env::var("PARITY_DIR").expect("PARITY_DIR"));
        let read = |f: &str| serde_json::from_str::<Value>(&std::fs::read_to_string(dir.join(f)).expect("file")).expect("json");
        let qv: HashMap<String, Vec<f32>> = read("qvecs.json").as_object().expect("obj").iter()
            .map(|(k, v)| (k.clone(), v.as_array().expect("arr").iter().map(|x| x.as_f64().unwrap_or(0.0) as f32).collect()))
            .collect();
        let live = read("live.json");
        let cfg = crate::Config::new(json!({}), std::path::PathBuf::from(std::env::var("HOME").unwrap_or_default()));
        let ix = Index::open(&dir.join("copy.db"), Arc::new(Stored(qv)), cfg).expect("open");
        ix.build_folder_index();
        let mut out = serde_json::Map::new();
        let t = Instant::now();
        for q in live.as_object().expect("obj").keys() {
            out.insert(q.clone(), ix.find(q, 10, None, None).unwrap_or_else(|e| json!({"error": e})));
        }
        eprintln!("{} queries in {:?}", out.len(), t.elapsed());
        std::fs::write(dir.join("rust.json"), Value::Object(out).to_string()).expect("write");
        if let Ok(cwds) = std::env::var("PARITY_CWDS") {
            for cwd in cwds.split(':') {
                eprintln!("resolve auto {cwd}: {}", ix.api_scopes_resolve(Some("auto"), Some(cwd)).unwrap_or_else(|e| json!(e)));
            }
        }
    }

    /// Parity with 0.21's `Searcher._code`: PARITY_DIR holds code.db (0.21's index of one repo) and
    /// code_qvecs.json; this writes rust_code.json ({query: {chunk id: score}}) beside them.
    #[test]
    #[ignore]
    fn code_parity() {
        let dir = std::path::PathBuf::from(std::env::var("PARITY_DIR").expect("PARITY_DIR"));
        let qv: HashMap<String, Vec<f32>> = serde_json::from_str::<HashMap<String, Vec<f32>>>(
            &std::fs::read_to_string(dir.join("code_qvecs.json")).expect("file")).expect("json");
        let cfg = crate::Config::new(json!({}), std::path::PathBuf::from(std::env::var("HOME").unwrap_or_default()));
        let ix = Index::open(&dir.join("code.db"), Arc::new(Stored(qv.clone())), cfg).expect("open");
        let t = Instant::now();
        let out: serde_json::Map<String, Value> = qv.iter()
            .map(|(q, v)| (q.clone(), json!(ix.code_search(q, None, 400, None, v)))).collect();
        eprintln!("{} queries in {:?}", out.len(), t.elapsed());
        std::fs::write(dir.join("rust_code.json"), Value::Object(out).to_string()).expect("write");
    }

    #[test]
    fn code_names_rank_their_definition_first() {
        let ix = testing::index();
        seed(&ix, "names");
        let qv = ix.embed_query("retry_webhook");
        let got = ix.code_search("retry_webhook", None, 400, None, &qv);
        let best = got.iter().max_by(|a, b| a.1.total_cmp(b.1)).map(|x| *x.0);
        assert_eq!(best, Some(3));
    }
}
