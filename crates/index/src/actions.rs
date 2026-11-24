//! What the REST API and the MCP server do with a result id: resolve it, read it, open it on this Mac.
//!
//! Ids: `file:/abs/path`, `code:<chunk id>`, `session:<session id>`, `folder:/abs/dir`, `person:<name>`,
//! `doc:<document id>` (a collected ticket, PR, message or page). Errors read "not found: <id>" for an unknown id
//! (0.21's KeyError, a 404) and say what is wrong for a bad argument (0.21's ValueError, a 400).

use std::collections::HashMap;
use std::path::Path;
use std::process::Command;

use serde_json::{Value, json};
use store::rusqlite::{self, OptionalExtension};

use crate::scopes::Scope;
use crate::search::folders::Tally;
use crate::search::lines::result_lines;
use crate::Index;

/// A resolved id: its kind, where it is on disk, and what was looked up to resolve it.
#[derive(Debug, Clone)]
pub struct Item {
    pub kind: String,
    pub path: Option<String>,
    pub line: Option<i64>,
    pub title: String,
    pub data: Value,
}

fn not_found(id: &str) -> String {
    format!("not found: {id}")
}

fn name_of(p: &str) -> String {
    Path::new(p).file_name().map(|x| x.to_string_lossy().into_owned()).unwrap_or_default()
}

/// Percent-encoding as Python's `urllib.parse.quote` (with `/` kept).
pub fn quote(s: &str) -> String {
    s.bytes().map(|b| match b {
        b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' | b'/' => (b as char).to_string(),
        _ => format!("%{b:02X}"),
    }).collect()
}

fn quote_plus(s: &str) -> String {
    quote(s).replace("%20", "+").replace('/', "%2F")
}

fn open_url(url: &str) -> bool {
    Command::new("open").arg(url).output().map(|o| o.status.success()).unwrap_or(false)
}

fn run_open(args: &[&str]) {
    let _ = Command::new("open").args(args).output();
}

fn authors(json_text: &str) -> Vec<(String, i64)> {
    match serde_json::from_str::<Value>(if json_text.is_empty() { "{}" } else { json_text }) {
        Ok(Value::Object(m)) => m.into_iter().map(|(k, v)| (k, v.as_i64().unwrap_or(0))).collect(),
        _ => Vec::new(),
    }
}

impl Index {
    pub fn resolve_item(&self, id: &str) -> Result<Item, String> {
        let (kind, rf) = id.split_once(':').unwrap_or((id, ""));
        let item = |kind: &str, path: Option<String>, line: Option<i64>, title: String, data: Value| {
            Ok(Item { kind: kind.into(), path, line, title, data })
        };
        match kind {
            "doc" => {
                let doc: i64 = rf.parse().map_err(|_| not_found(id))?;
                self.doc_item(doc, id)
            }
            "file" => {
                if rf.contains("/connections/") {
                    let data = self.config().home.join(".pensieve");
                    if let Some(doc) = self.docs_resolve_path(rf, &data) {
                        return self.doc_item(doc, id);
                    }
                }
                if !Path::new(rf).exists() {
                    return Err(not_found(id));
                }
                item("file", Some(rf.into()), None, name_of(rf), Value::Null)
            }
            "code" => {
                let c = rf.parse::<i64>().ok().and_then(|cid| self.api_code(cid)).ok_or_else(|| not_found(id))?;
                let s = |k: &str| c[k].as_str().unwrap_or("").to_string();
                let path = Path::new(&s("checkout")).join(s("path")).to_string_lossy().into_owned();
                let branch = s("branch");
                let title = format!("{}/{}{}", s("repo"), s("path"), if branch.is_empty() { String::new() } else { format!(" @ {branch}") });
                item("code", Some(path), c["start"].as_i64(), title, c)
            }
            "session" => {
                let s = self.session(rf).ok_or_else(|| not_found(id))?;
                let title = s["title"].as_str().unwrap_or("").to_string();
                item("session", None, None, title, s)
            }
            "folder" => {
                if !Path::new(rf).is_dir() {
                    return Err(not_found(id));
                }
                item("folder", Some(rf.into()), None, name_of(rf), Value::Null)
            }
            "person" if !rf.is_empty() => item("person", None, None, rf.into(), Value::Null),
            _ => Err(not_found(id)),
        }
    }

    fn doc_item(&self, doc: i64, id: &str) -> Result<Item, String> {
        type R = (String, String, String, String, Option<String>, Option<String>, i64, Option<f64>);
        let r: Option<R> = self.db.read(|c| c.query_row(
            "SELECT title, source_id, key, kind, url, status, private, time FROM doc_meta WHERE doc_id = ?", [doc],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?))).optional())
            .ok().flatten();
        let (title, source, key, kind, url, status, private, time) = r.ok_or_else(|| not_found(id))?;
        Ok(Item { kind: "doc".into(), path: None, line: None, title: title.clone(), data: json!({
            "id": doc, "title": title, "source_id": source, "key": key, "kind": kind, "url": url, "status": status,
            "private": private != 0, "time": time,
        })})
    }

    fn code_rows(&self, where_: &str, args: &[&str]) -> Vec<(String, String, String, i64)> {
        self.db.read(|c| {
            c.prepare(&format!("SELECT coalesce(repo, ''), coalesce(path, ''), coalesce(authors, ''), coalesce(last_ts, 0) \
                                FROM code_chunks WHERE wt='' AND {where_}"))?
                .query_map(rusqlite::params_from_iter(args.iter()), |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?
                .collect()
        }).unwrap_or_default()
    }

    /// A folder: its files, and who wrote its code (blamed lines) when it is in an indexed repo.
    fn read_folder(&self, path: &str, max_files: usize) -> Value {
        let roots: Vec<String> = self.db.read(|c| c.prepare("SELECT root FROM repos")?.query_map([], |r| r.get(0))?.collect())
            .unwrap_or_default();
        let root = roots.into_iter().filter(|r| path == r || path.starts_with(&format!("{r}/"))).last();
        let rows = match &root {
            Some(r) => {
                let rel = if path == r { String::new() } else { path[r.len() + 1..].to_string() };
                let like = if rel.is_empty() { "%".to_string() } else { format!("{rel}/%") };
                self.code_rows("repo=? AND path LIKE ?", &[r, &like])
            }
            None => Vec::new(),
        };
        let (mut owners, mut files) = (Tally::default(), Tally::default());
        for (_, p, a, _) in &rows {
            files.add(p, 1);
            for (who, n) in authors(a) {
                owners.add(who.split(" <").next().unwrap_or(""), n);
            }
        }
        let mut names: Vec<String> = if !files.is_empty() {
            files.keys().to_vec()
        } else {
            std::fs::read_dir(path).map(|d| d.flatten().filter_map(|e| {
                let n = e.file_name().to_string_lossy().into_owned();
                (!n.starts_with('.')).then(|| if e.path().is_dir() { format!("{n}/") } else { n })
            }).collect()).unwrap_or_default()
        };
        names.sort();
        let tot = owners.keys().iter().map(|k| owners.get(k)).sum::<i64>().max(1);
        json!({"id": format!("folder:{path}"), "kind": "folder", "path": path, "repo": root.as_deref().map(name_of),
               "files": names.iter().take(max_files).collect::<Vec<_>>(), "n_files": names.len(),
               "truncated": names.len() > max_files,
               "owners": owners.most_common(6).into_iter()
                   .map(|(w, n)| json!({"name": w, "share": (n as f64 / tot as f64 * 1000.0).round() / 1000.0})).collect::<Vec<_>>()})
    }

    /// Where a person wrote code (blamed lines per repo and folder) and when they were last active.
    fn read_person(&self, name: &str) -> Value {
        let nl = name.to_lowercase();
        let mut per_repo = Tally::default();
        let mut per_dir: HashMap<String, Tally> = HashMap::new();
        let mut last = 0;
        for (repo, p, a, ts) in self.code_rows("lower(authors) LIKE ?", &[&format!("%{nl}%")]) {
            for (who, n) in authors(&a) {
                if who.split(" <").next().unwrap_or("").to_lowercase() == nl {
                    per_repo.add(&repo, n);
                    let dir = if p.contains('/') { p.split('/').take(2).collect::<Vec<_>>().join("/") } else { ".".into() };
                    per_dir.entry(repo.clone()).or_default().add(&dir, n);
                    last = last.max(ts);
                }
            }
        }
        json!({"id": format!("person:{name}"), "kind": "person", "name": name,
               "last_change": if last == 0 { Value::Null } else { json!(last) },
               "repos": per_repo.most_common(6).into_iter().map(|(r, n)| json!({
                   "repo": name_of(&r), "lines": n,
                   "top_folders": per_dir.get(&r).map(|t| t.most_common(4).into_iter().map(|x| x.0).collect::<Vec<_>>()).unwrap_or_default(),
               })).collect::<Vec<_>>()})
    }

    /// GET /api/item: the text of an item (a file's text, a code chunk with its owners and commits, a transcript, a
    /// collected document), a folder's files and owners, or where a person wrote code.
    pub fn read_item(&self, id: &str, max_chars: usize) -> Result<Value, String> {
        let r = self.resolve_item(id)?;
        let clip = |t: &str| t.chars().take(max_chars).collect::<String>();
        Ok(match r.kind.as_str() {
            "doc" => {
                let d = &r.data;
                let doc = d["id"].as_i64().unwrap_or(0);
                let text = self.doc_markdown(doc).unwrap_or_default();
                json!({"id": format!("doc:{doc}"), "kind": "doc", "title": d["title"], "url": d["url"], "source": d["source_id"],
                       "key": d["key"], "doc_kind": d["kind"], "status": d["status"], "private": d["private"],
                       "updated": d["time"], "text": clip(&text), "truncated": text.chars().count() > max_chars})
            }
            "folder" => self.read_folder(r.path.as_deref().unwrap_or(""), 60),
            "person" => self.read_person(&r.title),
            "file" => {
                let path = r.path.unwrap_or_default();
                let chunks: Vec<String> = self.files_file(&path)
                    .and_then(|f| f["chunks"].as_array().map(|a| a.iter().filter_map(|c| c["text"].as_str().map(String::from)).collect()))
                    .unwrap_or_default();
                let text = if chunks.is_empty() { crate::extract::extract(Path::new(&path), 20_000_000) } else { chunks.join("\n") };
                json!({"id": id, "kind": "file", "path": path, "text": clip(&text), "truncated": text.chars().count() > max_chars})
            }
            "code" => {
                let c = &r.data;
                let mut owners: Vec<(String, i64)> = c["authors"].as_object()
                    .map(|m| m.iter().map(|(k, v)| (k.clone(), v.as_i64().unwrap_or(0))).collect()).unwrap_or_default();
                owners.sort_by_key(|x| std::cmp::Reverse(x.1));
                owners.truncate(5);
                let take = |k: &str, n: usize| c[k].as_array().map(|a| a.iter().take(n).cloned().collect::<Vec<_>>()).unwrap_or_default();
                json!({"id": id, "kind": "code", "path": r.path, "repo": c["repo"], "branch": c["branch"],
                       "lines": [c["start"], c["end"]], "text": clip(c["text"].as_str().unwrap_or("")),
                       "owners": owners.into_iter().map(|(a, n)| json!({"author": a, "lines": n})).collect::<Vec<_>>(),
                       "recent_commits": take("commits", 5),
                       "sessions": take("sessions", 5).into_iter().map(|s| json!({"id": format!("session:{}", s["id"].as_str().unwrap_or("")), "title": s["title"]})).collect::<Vec<_>>()})
            }
            _ => {
                let s = &r.data;
                let (mut n, mut out) = (0usize, Vec::new());
                for t in s["turns"].as_array().into_iter().flatten() {
                    if n > max_chars {
                        break;
                    }
                    let line = format!("{}: {}", t["role"].as_str().unwrap_or(""), t["text"].as_str().unwrap_or(""));
                    n += line.chars().count();
                    out.push(line);
                }
                json!({"id": id, "kind": "session", "title": s["title"], "source": s["source"], "project": s["project"],
                       "started": s["started"], "summary": s["summary"], "transcript": clip(&out.join("\n\n")), "truncated": n > max_chars})
            }
        })
    }

    /// Show the visualizer: in the Mac app when it is installed, else in the browser at `port`.
    pub fn visualize(&self, id: Option<&str>, view: Option<&str>, port: u16) -> Value {
        let q: Vec<String> = [("id", id), ("view", view)].iter().filter_map(|(k, v)| v.map(|v| format!("{k}={}", quote_plus(v)))).collect();
        let app = format!("pensieve://visualize{}", if q.is_empty() { String::new() } else { format!("?{}", q.join("&")) });
        if open_url(&app) {
            return json!({"ok": true, "where": "app"});
        }
        let frag = match (id, view) {
            (Some(i), _) => format!("#open={}", quote(i)),
            (None, Some(v)) => format!("#view={v}"),
            _ => String::new(),
        };
        open_url(&format!("http://127.0.0.1:{port}/{frag}"));
        json!({"ok": true, "where": "browser"})
    }

    /// POST /api/open and MCP open_item: open an item in its app (an editor at the line for code, Finder for a
    /// folder, the visualizer for a session or person). `action`: open, reveal, visualize, editor or terminal.
    /// `port`: the app's port, for the browser when the Mac app is not installed.
    pub fn open_item(&self, id: &str, action: &str, port: u16) -> Result<Value, String> {
        if !["open", "reveal", "visualize", "editor", "terminal"].contains(&action) {
            return Err("action must be open, reveal, visualize, editor or terminal".into());
        }
        let r = self.resolve_item(id)?;
        let cfg = self.config();
        let editor = cfg.get("editor").as_str().unwrap_or("default").to_string();
        let known = ["vscode", "cursor", "zed"].contains(&editor.as_str());
        let path = r.path.clone().unwrap_or_default();
        if r.kind == "doc" {
            let url = r.data["url"].as_str().unwrap_or("").to_string();
            if action == "open" && !url.is_empty() && open_url(&url) {
                return Ok(json!({"ok": true, "url": url}));
            }
            return Ok(self.visualize(Some(&format!("doc:{}", r.data["id"])), None, port));
        }
        if action == "editor" || action == "terminal" {
            if r.kind != "folder" {
                return Err(format!("{action} opens a folder"));
            }
            if action == "editor" && known && open_url(&format!("{editor}://file{}", quote(&path))) {
                return Ok(json!({"ok": true, "path": path, "editor": editor}));
            }
            let app = if action == "terminal" {
                cfg.get("terminal").as_str().filter(|t| !t.is_empty()).unwrap_or("Terminal").to_string()
            } else if editor != "default" && !known {
                editor
            } else {
                "Finder".into()
            };
            run_open(&["-a", &app, &path]);
            return Ok(json!({"ok": true, "path": path, "editor": app}));
        }
        if action == "visualize" || r.kind == "session" || r.kind == "person" {
            return Ok(self.visualize(Some(id), None, port));
        }
        if r.kind == "folder" && action == "open" {
            run_open(&[&path]);
            return Ok(json!({"ok": true, "path": path}));
        }
        if action == "reveal" {
            run_open(&["-R", &path]);
            return Ok(json!({"ok": true, "path": path}));
        }
        if let (Some(line), true) = (r.line.filter(|l| *l != 0), known) {
            if open_url(&format!("{editor}://file{}:{line}", quote(&path))) {
                return Ok(json!({"ok": true, "path": path, "line": line, "editor": editor}));
            }
        }
        if editor != "default" && !known {
            run_open(&["-a", &editor, &path]);
            return Ok(json!({"ok": true, "path": path, "editor": editor}));
        }
        run_open(&[&path]);
        Ok(json!({"ok": true, "path": path}))
    }

    fn check_scope(&self, scope: Option<&Scope>, id: &str) -> Result<(), String> {
        if self.scope_contains(scope, id) {
            return Ok(());
        }
        Err(format!("{id} is outside this connection's scope ({})", scope.map(|s| s.name.as_str()).unwrap_or("all")))
    }

    /// MCP read: one id, or several separated by spaces or commas (at most 8, sharing `max_chars`).
    pub fn mcp_read(&self, id: &str, max_chars: usize, scope: Option<&Scope>) -> Result<Value, String> {
        let ids: Vec<String> = id.replace(',', " ").split_whitespace().map(String::from).collect();
        if ids.len() <= 1 {
            let one = ids.first().cloned().unwrap_or_else(|| id.trim().to_string());
            self.check_scope(scope, &one)?;
            return self.read_item(&one, max_chars);
        }
        let each = 2000.max(max_chars / ids.len());
        let items: Vec<Value> = ids.iter().take(8).map(|i| {
            match self.check_scope(scope, i).and_then(|_| self.read_item(i, each)) {
                Ok(v) => v,
                Err(e) if e.starts_with("not found") => json!({"id": i, "error": "not found"}),
                Err(e) => json!({"id": i, "error": e}),
            }
        }).collect();
        let mut out = json!({"items": items});
        if ids.len() > 8 {
            out["skipped"] = json!(ids[8..]);
        }
        Ok(out)
    }

    /// Items most similar in meaning to a result id, inside the scope.
    pub fn similar_items(&self, id: &str, limit: usize, scope: Option<&Scope>) -> Result<Vec<Value>, String> {
        self.check_scope(scope, id)?;
        let (kind, rf) = id.split_once(':').unwrap_or((id, ""));
        let keep = |v: Vec<Value>| -> Vec<Value> {
            v.into_iter().filter(|x| self.scope_contains(scope, x["id"].as_str().unwrap_or(""))).take(limit).collect()
        };
        if kind == "doc" || (kind == "file" && rf.contains("/connections/")) {
            let doc = if kind == "doc" { rf.parse().ok() } else { self.docs_resolve_path(rf, &self.config().home.join(".pensieve")) };
            let doc = doc.ok_or_else(|| not_found(id))?;
            return Ok(keep(self.docs_similar(doc, limit * 3)));
        }
        match kind {
            "file" => Ok(keep(self.files_similar(rf, limit * 3))),
            "session" => Ok(keep(self.similar_sessions(rf, limit * 3).into_iter().map(|mut s| {
                let sid = format!("session:{}", s["id"].as_str().unwrap_or(""));
                s["id"] = json!(sid);
                s
            }).collect())),
            "code" => {
                let c = rf.parse::<i64>().ok().and_then(|cid| self.api_code(cid));
                let Some(c) = c else { return Ok(Vec::new()) };
                let text: String = c["text"].as_str().unwrap_or("").chars().take(1500).collect();
                let rx = fancy_regex::Regex::new(r#"["']|(?<!\S)-(?=\S)|\b(kind|ext|in):"#).map_err(|e| e.to_string())?;
                let q = rx.replace_all(&text, " ").into_owned();
                let hits = self.find(&q, limit + 1, Some(&["code".to_string()]), scope)?;
                Ok(hits["results"].as_array().into_iter().flatten().filter(|h| h["id"] != id).take(limit).cloned().collect())
            }
            _ => Err("id must start with file:, code:, session: or doc:".into()),
        }
    }

    /// MCP similar: one line per item.
    pub fn mcp_similar(&self, id: &str, limit: usize, scope: Option<&Scope>, cwd: Option<&str>) -> Result<String, String> {
        let items = self.similar_items(id, limit, scope)?;
        let found = result_lines(&items, cwd, &self.config().home.to_string_lossy());
        Ok(if found.is_empty() { "Nothing similar.".into() } else { found })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quotes_as_python() {
        assert_eq!(quote("/a b/ü"), "/a%20b/%C3%BC");
        assert_eq!(quote_plus("code:1 x/y"), "code%3A1+x%2Fy");
    }

    #[test]
    fn unknown_ids_are_not_found() {
        let ix = crate::testing::index();
        assert_eq!(ix.read_item("code:999", 100).err().as_deref(), Some("not found: code:999"));
        assert!(ix.read_item("bogus", 100).is_err());
        assert_eq!(ix.read_item("person:Sam Lee", 100).expect("person")["kind"], "person");
        assert!(ix.open_item("person:x", "launch", 8765).is_err());
    }
}
