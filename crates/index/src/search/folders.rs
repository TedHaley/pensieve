//! Folders, repos and people as search results. The folder index holds every folder of the indexed repos (main
//! checkouts) and of the document folders: the words of its path, its files, and the mean vector of what is in it.
//! It is built in the background (`folder_index_tick`); a search never waits for it, except by:<person>, which
//! needs the authors to filter at all.

use std::collections::{BTreeSet, HashMap};
use std::path::Path;
use std::sync::Arc;
use std::time::Instant;

use serde_json::Value;
use store::rusqlite;

use super::Hit;
use crate::scopes::Scope;
use crate::text::code_words;
use crate::vectors::{dot, unit};
use crate::Index;

/// Seconds between background rebuilds while the index keeps changing.
pub const FOLDER_REBUILD: u64 = 30;

/// Counts that keep the order in which their keys first came, as Python's Counter does for ties.
#[derive(Debug, Clone, Default)]
pub struct Tally {
    order: Vec<String>,
    count: HashMap<String, i64>,
}

impl Tally {
    pub fn add(&mut self, k: &str, n: i64) {
        match self.count.get_mut(k) {
            Some(c) => *c += n,
            None => {
                self.order.push(k.to_string());
                self.count.insert(k.to_string(), n);
            }
        }
    }

    pub fn get(&self, k: &str) -> i64 {
        self.count.get(k).copied().unwrap_or(0)
    }

    pub fn len(&self) -> usize {
        self.order.len()
    }

    pub fn is_empty(&self) -> bool {
        self.order.is_empty()
    }

    pub fn keys(&self) -> &[String] {
        &self.order
    }

    pub fn most_common(&self, n: usize) -> Vec<(String, i64)> {
        let mut v: Vec<(String, i64)> = self.order.iter().map(|k| (k.clone(), self.count[k])).collect();
        v.sort_by_key(|x| std::cmp::Reverse(x.1));
        v.truncate(n);
        v
    }
}

#[derive(Debug, Clone, Default)]
pub struct Dir {
    pub repo: Option<String>,
    pub name: String,
    pub rel: String,
    pub root: bool,
    pub idx: Vec<usize>,
    pub files: Tally,
    pub segs: Vec<BTreeSet<String>>,
}

#[derive(Debug, Default)]
pub struct FolderIndex {
    pub fp: (Option<i64>, Option<i64>, i64),
    pub at: Option<Instant>,
    pub keys: Vec<String>,
    pub dirs: Vec<Dir>,
    pub centroids: Vec<Vec<f32>>,
    /// (kind, chunk id) of each position that `Dir::idx` holds.
    pub ids: Vec<(&'static str, i64)>,
    pub people: Tally,
    pub by_word: HashMap<String, BTreeSet<usize>>,
    pub words: Vec<String>,
    pub authors: HashMap<i64, String>,
    pub doc_author: HashMap<i64, String>,
}

fn path_match(words: &[String], segs: &[BTreeSet<String>]) -> (f64, f64) {
    if words.is_empty() {
        return (0.0, 0.0);
    }
    let (mut last, mut found) = (-1i64, 0usize);
    for w in words {
        let at: Vec<usize> = segs.iter().enumerate()
            .filter(|(_, seg)| seg.iter().any(|x| x == w || (w.len() >= 4 && x.starts_with(w.as_str()))))
            .map(|x| x.0).collect();
        if let Some(&a) = at.last() {
            found += 1;
            last = last.max(a as i64);
        }
    }
    let cover = found as f64 / words.len() as f64;
    if found == 0 {
        return (0.0, 0.0);
    }
    let below = segs.len() as f64 - 1.0 - last as f64;
    (cover, cover + if below == 0.0 { 0.15 } else { 0.0 } - 0.08 * below)
}

fn parent_of(p: &str) -> String {
    Path::new(p).parent().map(|x| x.to_string_lossy().into_owned()).unwrap_or_default()
}

fn name_of(p: &str) -> String {
    Path::new(p).file_name().map(|x| x.to_string_lossy().into_owned()).unwrap_or_default()
}

impl Index {
    fn folder_fp(&self) -> (Option<i64>, Option<i64>, i64) {
        self.db.read(|c| c.query_row("SELECT (SELECT MAX(id) FROM code_chunks), (SELECT MAX(id) FROM file_chunks), \
                                     (SELECT COUNT(*) FROM repos)", [], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))))
            .unwrap_or((None, None, 0))
    }

    /// The folder index as it is now (empty until the first build).
    pub fn folder_index(&self) -> Arc<FolderIndex> {
        self.search.folders.read().unwrap_or_else(|e| e.into_inner()).clone().unwrap_or_default()
    }

    fn folder_index_now(&self) -> Arc<FolderIndex> {
        if let Some(f) = self.search.folders.read().unwrap_or_else(|e| e.into_inner()).clone() {
            return f;
        }
        self.build_folder_index()
    }

    /// Build the folder index when it is missing, or when the index changed, no sweep runs (`sweeping`) and
    /// FOLDER_REBUILD seconds passed. The app calls this on a background thread: at start, and every few seconds.
    pub fn folder_index_tick(&self, sweeping: bool) -> bool {
        let cur = self.search.folders.read().unwrap_or_else(|e| e.into_inner()).clone();
        let due = match &cur {
            None => true,
            Some(f) => f.fp != self.folder_fp() && !sweeping
                && f.at.is_none_or(|t| t.elapsed().as_secs() > FOLDER_REBUILD),
        };
        if due {
            self.build_folder_index();
        }
        due
    }

    pub fn build_folder_index(&self) -> Arc<FolderIndex> {
        let fp = self.folder_fp();
        let home = self.config().home.to_string_lossy().into_owned();
        type CodeRow = (i64, String, String, String);
        type DocRow = (i64, String, String, String);
        let (names, code, docs): (HashMap<String, String>, Vec<CodeRow>, Vec<DocRow>) = self.db.read(|c| {
            let names = c.prepare("SELECT root, coalesce(name, '') FROM repos")?
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<rusqlite::Result<_>>()?;
            let code = c.prepare("SELECT id, coalesce(repo, ''), coalesce(path, ''), coalesce(authors, '') FROM code_chunks WHERE wt=''")?
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?.collect::<rusqlite::Result<_>>()?;
            let docs = c.prepare("SELECT c.id, f.path, coalesce(f.author, ''), coalesce(f.root, '') FROM file_chunks c \
                                  JOIN files f ON f.path=c.path WHERE f.kind!='folder'")?
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?.collect::<rusqlite::Result<_>>()?;
            Ok((names, code, docs))
        }).unwrap_or_default();
        let mut keys: Vec<String> = Vec::new();
        let mut dirs: Vec<Dir> = Vec::new();
        let mut at: HashMap<String, usize> = HashMap::new();
        let mut add = |key: String, d: Dir| -> usize {
            *at.entry(key.clone()).or_insert_with(|| {
                keys.push(key);
                dirs.push(d);
                dirs.len() - 1
            })
        };
        let mut ids: Vec<(&'static str, i64)> = Vec::new();
        let mut by_file: Vec<((String, String), Vec<usize>)> = Vec::new();
        let mut by_file_at: HashMap<(String, String), usize> = HashMap::new();
        for (i, (cid, repo, path, _)) in code.iter().enumerate() {
            ids.push(("code", *cid));
            let k = (repo.clone(), path.clone());
            let j = *by_file_at.entry(k.clone()).or_insert_with(|| {
                by_file.push((k, Vec::new()));
                by_file.len() - 1
            });
            by_file[j].1.push(i);
        }
        let mut pending: Vec<(usize, Vec<usize>, String)> = Vec::new();
        for ((repo, path), idx) in &by_file {
            let parts: Vec<&str> = path.split('/').collect();
            let parts = &parts[..parts.len() - 1];
            for depth in 0..=parts.len() {
                let rel = parts[..depth].join("/");
                let key = format!("{repo}/{rel}").trim_end_matches('/').to_string();
                let name = names.get(repo).cloned().unwrap_or_else(|| name_of(repo));
                let d = add(key, Dir { repo: Some(repo.clone()), name, rel: rel.clone(), root: rel.is_empty(), ..Default::default() });
                pending.push((d, idx.clone(), path.rsplit('/').next().unwrap_or("").to_string()));
            }
        }
        for (j, (cid, path, _, root)) in docs.iter().enumerate() {
            ids.push(("file", *cid));
            let parent = parent_of(path);
            let base = if root.is_empty() { home.clone() } else { self.config().expand(root).to_string_lossy().into_owned() };
            if !parent.starts_with(&base) {
                continue;
            }
            let parts: Vec<&str> = if parent != base { parent[base.len()..].trim_matches('/').split('/').collect() } else { vec![] };
            for depth in 1..=parts.len() {
                let rel = parts[..depth].join("/");
                let d = add(format!("{base}/{rel}"), Dir { repo: None, name: name_of(&base), rel, root: false, ..Default::default() });
                pending.push((d, vec![code.len() + j], name_of(path)));
            }
        }
        for (d, idx, file) in pending {
            dirs[d].files.add(&file, idx.len() as i64);
            dirs[d].idx.extend(idx);
        }
        let centroids = self.folder_vectors(&dirs, &code, &docs);
        let mut by_word: HashMap<String, BTreeSet<usize>> = HashMap::new();
        for (j, d) in dirs.iter_mut().enumerate() {
            let mut segs = vec![d.name.clone()];
            if !d.rel.is_empty() {
                segs.extend(d.rel.split('/').map(String::from));
            }
            d.segs = segs.iter().map(|x| code_words(x).split_whitespace().map(String::from).collect()).collect();
            for seg in &d.segs {
                for w in seg {
                    by_word.entry(w.clone()).or_default().insert(j);
                }
            }
        }
        let mut people = Tally::default();
        for (_, _, _, a) in &code {
            if let Ok(Value::Object(m)) = serde_json::from_str::<Value>(if a.is_empty() { "{}" } else { a }) {
                for (who, n) in m {
                    people.add(who.split(" <").next().unwrap_or(&who), n.as_i64().unwrap_or(0));
                }
            }
        }
        let mut words: Vec<String> = by_word.keys().cloned().collect();
        words.sort();
        let fi = Arc::new(FolderIndex {
            fp,
            at: Some(Instant::now()),
            keys,
            dirs,
            centroids,
            ids,
            people,
            by_word,
            words,
            authors: code.iter().map(|c| (c.0, c.3.to_lowercase())).collect(),
            doc_author: docs.iter().map(|c| (c.0, c.2.to_lowercase())).collect(),
        });
        *self.search.folders.write().unwrap_or_else(|e| e.into_inner()) = Some(fi.clone());
        fi
    }

    fn folder_vectors(&self, dirs: &[Dir], code: &[(i64, String, String, String)], docs: &[(i64, String, String, String)])
                      -> Vec<Vec<f32>> {
        let dim = self.code_vecs.dim;
        let mut v: Vec<Option<Vec<f32>>> = self.code_vecs.take(&code.iter().map(|c| c.0).collect::<Vec<_>>());
        v.extend(self.file_vecs.take(&docs.iter().map(|c| c.0).collect::<Vec<_>>()));
        dirs.iter().map(|d| {
            let mut s = vec![0f32; dim];
            for &i in &d.idx {
                if let Some(Some(x)) = v.get(i) {
                    for (a, b) in s.iter_mut().zip(x) {
                        *a += b;
                    }
                }
            }
            unit(&s)
        }).collect()
    }

    /// Folder (and repo) results: path words and what the folder holds both count. `strong_only`: only folders
    /// whose path names every word of the query (shown above files in a plain search).
    #[allow(clippy::too_many_arguments)]
    pub fn folders(&self, q: &str, scope: Option<&Scope>, repos_only: bool, strong_only: bool, limit: usize,
                   by: Option<&str>, ext: Option<&str>, within: Option<&str>, code_qv: Option<&[f32]>) -> Vec<Hit> {
        let ix = self.folder_index();
        if ix.keys.is_empty() {
            return Vec::new();
        }
        let words: Vec<String> = code_words(q).split_whitespace().map(String::from).collect::<BTreeSet<_>>().into_iter().collect();
        let blank = q.trim().is_empty();
        let mut qv: Option<Vec<f32>> = if blank { Some(vec![0.0; ix.centroids.first().map(|c| c.len()).unwrap_or(1)]) } else { None };
        let home = self.config().home.to_string_lossy().into_owned();
        let by_ok: Option<std::collections::HashSet<usize>> = by.map(|b| {
            ix.ids.iter().enumerate().filter(|(_, (k, id))| {
                let a = if *k == "code" { ix.authors.get(id) } else { ix.doc_author.get(id) };
                a.is_some_and(|a| a.contains(b))
            }).map(|x| x.0).collect()
        });
        let candidates: Vec<usize> = if strong_only && !words.is_empty() {
            let mut cand: Option<BTreeSet<usize>> = None;
            for w in &words {
                let mut hit: BTreeSet<usize> = ix.by_word.get(w).cloned().unwrap_or_default();
                if w.len() >= 4 {
                    let i = ix.words.partition_point(|x| x.as_str() < w.as_str());
                    for x in ix.words[i..].iter().take_while(|x| x.starts_with(w.as_str())) {
                        hit.extend(&ix.by_word[x]);
                    }
                }
                cand = Some(match cand {
                    None => hit,
                    Some(c) => c.intersection(&hit).copied().collect(),
                });
                if cand.as_ref().is_some_and(|c| c.is_empty()) {
                    return Vec::new();
                }
            }
            cand.unwrap_or_default().into_iter().collect()
        } else {
            (0..ix.keys.len()).collect()
        };
        let mut out: Vec<(f64, usize, f64)> = Vec::new();
        for j in candidates {
            let key = &ix.keys[j];
            let d = &ix.dirs[j];
            if repos_only && !d.root {
                continue;
            }
            if let Some(s) = scope {
                if let (Some(r), Some(sr)) = (&d.repo, &s.repos) {
                    if !sr.contains(r) {
                        continue;
                    }
                }
                if d.repo.is_none() {
                    if let Some(f) = &s.folders {
                        if !f.iter().any(|x| key.starts_with(self.config().expand(x).to_string_lossy().as_ref())) {
                            continue;
                        }
                    }
                }
            }
            if within.is_some_and(|w| !key.to_lowercase().contains(w)) {
                continue;
            }
            if let Some(e) = ext {
                if !d.files.keys().iter().any(|n| n.to_lowercase().ends_with(&format!(".{e}"))) {
                    continue;
                }
            }
            if let Some(ok) = &by_ok {
                if !d.idx.iter().any(|i| ok.contains(i)) {
                    continue;
                }
            }
            let (cover, path) = path_match(&words, &d.segs);
            if strong_only && (cover < 1.0 || path < 1.0) {
                continue;
            }
            if qv.is_none() {
                qv = Some(match code_qv {
                    Some(v) => v.to_vec(),
                    None => self.embed_query(q),
                });
            }
            let sim = qv.as_ref().map(|v| dot(&ix.centroids[j], v) as f64).unwrap_or(0.0);
            let score = path + 0.8 * sim + if blank { d.files.len() as f64 / 1e6 } else { 0.0 };
            out.push((score, j, cover));
        }
        out.sort_by(|a, b| b.0.total_cmp(&a.0));
        out.truncate(if strong_only { 2 } else { limit });
        out.into_iter().map(|(score, j, _)| {
            let key = &ix.keys[j];
            let d = &ix.dirs[j];
            let n = d.files.len();
            let where_ = if d.repo.is_some() {
                format!("{}{}", d.name, d.rel.rsplit_once('/').map(|x| format!("/{}", x.0)).unwrap_or_default())
            } else {
                parent_of(key).replacen(&home, "~", 1)
            };
            let title = if d.root { d.name.clone() } else { d.rel.rsplit('/').next().unwrap_or("").to_string() };
            let subtitle = format!("{}{} file{}", if d.root { "Repository · ".to_string() } else { format!("Folder in {where_} · ") },
                                   thousands(n as i64), if n == 1 { "" } else { "s" });
            let snippet = format!("Mostly {}", d.files.most_common(4).into_iter().map(|x| x.0).collect::<Vec<_>>().join(", "));
            Hit {
                id: format!("folder:{key}"),
                kind: if d.root { "repo" } else { "folder" }.into(),
                title,
                subtitle,
                path: Some(key.clone()),
                is_dir: true,
                score: super::round4(if strong_only { 1.0 + 0.1 * score } else { score }),
                matched: "folder".into(),
                snippet: Some(snippet),
                highlights: Some(vec![]),
                ..Default::default()
            }
        }).collect()
    }

    /// Code chunk ids that `by` (any part of a name, lowercase) has blamed lines in, within `allow`.
    pub fn by_allow(&self, by: &str, allow: Option<&[i64]>) -> Vec<i64> {
        let ids: Vec<i64> = self.db.read(|c| {
            c.prepare("SELECT id FROM code_chunks WHERE lower(authors) LIKE ?")?
                .query_map([format!("%{by}%")], |r| r.get(0))?.collect()
        }).unwrap_or_default();
        match allow {
            None => ids,
            Some(a) => {
                let a: std::collections::HashSet<i64> = a.iter().copied().collect();
                let mut v: Vec<i64> = ids.into_iter().filter(|i| a.contains(i)).collect();
                v.sort_unstable();
                v.dedup();
                v
            }
        }
    }

    pub(crate) fn by_ok(&self, by: &str, kind: &str, row: &super::Row, cid: i64) -> bool {
        match kind {
            "session" => row.subtitle.to_lowercase().contains(by),
            "file" => row.author.as_deref().unwrap_or("").to_lowercase().contains(by),
            _ => self.folder_index_now().authors.get(&cid).is_some_and(|a| a.contains(by)),
        }
    }

    /// People for a query: someone it names outright, or (kind:person) whoever wrote most of the matching code.
    pub fn people_hits(&self, q: &str, scope: Option<&Scope>, named_only: bool, limit: usize, code_qv: Option<&[f32]>)
                       -> Vec<Hit> {
        let ix = self.folder_index();
        let names = &ix.people;
        let ql = q.trim().to_lowercase();
        let mut named: Vec<&String> = names.keys().iter().filter(|w| {
            let wl = w.to_lowercase();
            ql.chars().count() >= 3 && (wl == ql || wl.starts_with(&format!("{ql} ")) || wl.split_whitespace().any(|x| x == ql))
        }).collect();
        if !named.is_empty() {
            named.sort_by_key(|w| std::cmp::Reverse(names.get(w)));
            return named.into_iter().take(3).map(|w| Hit {
                id: format!("person:{w}"), kind: "person".into(), title: w.clone(),
                subtitle: format!("Person · {} lines written", thousands(names.get(w))),
                author: Some(w.clone()), highlights: Some(vec![]), snippet: Some("Open to see what they know about".into()),
                score: 1.5, matched: "person".into(), ..Default::default()
            }).collect();
        }
        if named_only {
            return Vec::new();
        }
        let allow = self.allowlist(scope, "code");
        let qv = match code_qv {
            Some(v) => v.to_vec(),
            None => self.embed_query(q),
        };
        let mut top: Vec<(i64, f64)> = self.code_search(q, allow.as_deref(), 400, None, &qv).into_iter().collect();
        top.sort_by(|a, b| b.1.total_cmp(&a.1));
        top.truncate(40);
        let hits: Vec<(i64, f32)> = top.into_iter().map(|(c, s)| (c, s as f32)).collect();
        self.experts_from(&hits).into_iter().take(limit).map(|e| {
            let w = e["author"].as_str().unwrap_or("").split(" <").next().unwrap_or("").to_string();
            let share = e["share"].as_f64().unwrap_or(0.0);
            let files: Vec<String> = e["files"].as_array().map(|a| a.iter().filter_map(|x| x.as_str()).take(3).map(name_of).collect()).unwrap_or_default();
            Hit {
                id: format!("person:{w}"), kind: "person".into(), title: w.clone(),
                subtitle: format!("Person · wrote {}% of the code about this", (share * 100.0).round() as i64),
                mtime: e.get("last_active").cloned().unwrap_or(Value::Null), author: Some(w),
                highlights: Some(vec![]), snippet: Some(format!("Main files: {}", files.join(", "))),
                score: super::round4(share), matched: "person".into(), ..Default::default()
            }
        }).collect()
    }
}

/// 12345 as "12,345".
pub fn thousands(n: i64) -> String {
    let s = n.abs().to_string();
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    if n < 0 { format!("-{out}") } else { out }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn path_words_name_folders() {
        let segs = |v: &[&str]| v.iter().map(|s| code_words(s).split_whitespace().map(String::from).collect()).collect::<Vec<BTreeSet<String>>>();
        let (cover, path) = path_match(&["billing".into()], &segs(&["app", "src", "billing"]));
        assert_eq!(cover, 1.0);
        assert!((path - 1.15).abs() < 1e-9);
        let (_, deeper) = path_match(&["pipeline".into()], &segs(&["app", "pipelines", "jobs"]));
        assert!((deeper - 0.92).abs() < 1e-9);
        assert_eq!(thousands(1234567), "1,234,567");
        let mut t = Tally::default();
        t.add("b", 1);
        t.add("a", 2);
        t.add("c", 1);
        assert_eq!(t.most_common(3).iter().map(|x| x.0.as_str()).collect::<Vec<_>>(), vec!["a", "b", "c"]);
    }
}
