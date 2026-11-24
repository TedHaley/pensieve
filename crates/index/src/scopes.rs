//! Scopes: named slices of the index that an agent (or search in the app) works within.
//!
//! A scope lists source ids, the same ids as Sources: a category ("agents", "files", "repos", "connections") or an
//! item ("repos/<repo root>", "files/~/Documents/specs", "agents/claude", "connections/<server>"). Everything stays
//! in one index; a scope filters every query. Agent sessions come along with the repos they ran in. 'auto' picks a
//! scope from the agent's working directory: the first named scope that includes that repo, else that repo alone.
//! Named scopes live in the settings (`scopes`): the functions that change them return the new value for the app
//! to save.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Instant;

use serde_json::{Map, Value, json};
use store::rusqlite::{self, Connection};

use crate::Index;
use crate::db::marks;

/// What one scope lets through. `None` in a field means everything of that kind.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Scope {
    pub name: String,
    pub sources: Vec<String>,
    pub description: String,
    pub repos: Option<BTreeSet<String>>,
    pub folders: Option<Vec<String>>,
    pub agents: Option<BTreeSet<String>>,
    pub docs: Option<BTreeSet<String>>,
    /// 'auto' in a git repo that Pensieve does not index.
    pub unindexed: bool,
}

fn resolved(p: &Path) -> String {
    std::fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf()).to_string_lossy().into_owned()
}

/// A scope from its source ids. Errors on a source id of no known category.
pub fn build(name: &str, sources: &[String], description: &str, home: &Path) -> Result<Scope, String> {
    let mut s = Scope {
        name: name.into(),
        sources: sources.to_vec(),
        description: description.into(),
        repos: Some(BTreeSet::new()),
        folders: Some(Vec::new()),
        agents: Some(BTreeSet::new()),
        docs: Some(BTreeSet::new()),
        unindexed: false,
    };
    for sid in sources {
        let (cat, item) = sid.split_once('/').unwrap_or((sid.as_str(), ""));
        match cat {
            "repos" => match (&mut s.repos, item) {
                (r, "") => *r = None,
                (Some(r), _) => {
                    r.insert(item.trim_end_matches('/').to_string());
                }
                _ => {}
            },
            "files" => match (&mut s.folders, item) {
                (f, "") => *f = None,
                (Some(f), _) => {
                    let p = match item.strip_prefix('~') {
                        Some(rest) => home.join(rest.trim_start_matches('/')),
                        None => PathBuf::from(item),
                    };
                    f.push(resolved(&p));
                }
                _ => {}
            },
            "apps" => {}
            "connections" => match (&mut s.docs, item) {
                (d, "") => *d = None,
                (Some(d), _) => {
                    d.insert(item.to_string());
                }
                _ => {}
            },
            "agents" => match (&mut s.agents, item) {
                (a, "") => *a = None,
                (Some(a), _) => {
                    a.insert(item.to_string());
                }
                _ => {}
            },
            _ => return Err(format!("unknown source '{sid}' in scope '{name}'")),
        }
    }
    Ok(s)
}

/// The repo root of a folder: linked git worktrees and `.claude/worktrees` checkouts fold into their main repo.
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
                if let Some(gd) = t.split_once("gitdir:").map(|x| PathBuf::from(x.1.trim())) {
                    let gp: Vec<_> = gd.components().collect();
                    if let Some(w) = gp.iter().position(|c| c.as_os_str() == "worktrees") {
                        let main: PathBuf = gp[..w].iter().collect();
                        return main.parent().map(|m| m.to_string_lossy().into_owned());
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

pub fn norm_remote(url: &str) -> String {
    let u = url.trim();
    let u = ["git@", "https://", "http://", "ssh://git@"].iter().find_map(|p| u.strip_prefix(p)).unwrap_or(u);
    let u = u.replace(':', "/");
    u.strip_suffix(".git").unwrap_or(&u).to_lowercase()
}

fn git_remote(root: &str) -> String {
    Command::new("git").args(["-C", root, "config", "--get", "remote.origin.url"]).output().ok()
        .filter(|o| o.status.success())
        .map(|o| norm_remote(&String::from_utf8_lossy(&o.stdout)))
        .unwrap_or_default()
}

/// Folders that stand for an indexed repo (its root, its linked worktrees, clones with its remote), longest first.
pub struct Aliases {
    pub list: Vec<(String, String)>,
    pub roots: Vec<String>,
}

impl Aliases {
    /// The canonical repo root and the repo-relative path of an absolute path, or None outside every indexed repo.
    /// Sibling folders named `<repo>-<feature>` count as the repo.
    pub fn locate(&self, path: &str) -> Option<(String, String)> {
        let p = strip_claude_worktree(path.trim_end_matches('/'));
        for (a, c) in &self.list {
            if p == *a {
                return Some((c.clone(), String::new()));
            }
            if let Some(rest) = p.strip_prefix(&format!("{a}/")) {
                return Some((c.clone(), rest.to_string()));
            }
        }
        for c in &self.roots {
            if let Some(rest) = p.strip_prefix(&format!("{c}-")) {
                if !rest.is_empty() && !rest.starts_with('/') {
                    let rel = rest.split_once('/').map(|x| x.1.to_string()).unwrap_or_default();
                    return Some((c.clone(), rel));
                }
            }
        }
        None
    }
}

fn strip_claude_worktree(p: &str) -> String {
    match p.find("/.claude/worktrees/") {
        Some(i) => {
            let rest = &p[i + "/.claude/worktrees/".len()..];
            match rest.find('/') {
                Some(j) => format!("{}{}", &p[..i], &rest[j..]),
                None => p[..i].to_string(),
            }
        }
        None => p.to_string(),
    }
}

/// Session id -> the canonical repo it belongs to: the repo of its folder, else the repo that most of its touched
/// files are in (at least two, and half of them).
pub fn session_roots(c: &Connection, al: &Aliases) -> rusqlite::Result<HashMap<String, Option<String>>> {
    let mut files: HashMap<String, Vec<String>> = HashMap::new();
    let mut st = c.prepare("SELECT session_id, path FROM session_files WHERE path!=''")?;
    for r in st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))? {
        let (sid, p) = r?;
        files.entry(sid).or_default().push(p);
    }
    let mut out = HashMap::new();
    let mut st = c.prepare("SELECT id, project FROM sessions")?;
    for r in st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?)))? {
        let (sid, cwd) = r?;
        let mut root = cwd.as_deref().filter(|c| !c.is_empty()).and_then(|c| al.locate(c)).map(|x| x.0);
        if root.is_none() {
            let mut per: Vec<(String, usize)> = Vec::new();
            for f in files.get(&sid).map(|v| v.as_slice()).unwrap_or(&[]) {
                if let Some((r, rel)) = al.locate(f) {
                    if rel.is_empty() {
                        continue;
                    }
                    match per.iter_mut().find(|x| x.0 == r) {
                        Some(x) => x.1 += 1,
                        None => per.push((r, 1)),
                    }
                }
            }
            let total: usize = per.iter().map(|x| x.1).sum();
            if let Some((r, n)) = per.iter().max_by_key(|x| x.1) {
                if *n >= 2 && *n as f64 >= 0.5 * total as f64 {
                    root = Some(r.clone());
                }
            }
        }
        out.insert(sid, root);
    }
    Ok(out)
}

fn under(path: &str, folders: &[String]) -> bool {
    folders.iter().any(|f| path == f || path.starts_with(&format!("{f}/")))
}

pub fn repo_allowed(scope: Option<&Scope>, root: &str) -> bool {
    scope.is_none_or(|s| s.repos.as_ref().is_none_or(|r| r.contains(root)))
}

pub fn describe(scope: Option<&Scope>) -> Value {
    match scope {
        None => json!({"name": "all", "description": "Everything Pensieve indexes", "sources": []}),
        Some(s) => json!({"name": s.name, "description": s.description, "sources": s.sources}),
    }
}

impl Index {
    pub fn named_scopes(&self) -> Map<String, Value> {
        self.config().get("scopes").as_object().cloned().unwrap_or_default()
    }

    /// The repo aliases, made again at most once a minute (they need a `git config` per session folder).
    pub fn aliases(&self) -> std::sync::Arc<Aliases> {
        let mut g = self.search.aliases.lock().unwrap_or_else(|e| e.into_inner());
        if let Some((at, a)) = g.as_ref() {
            if at.elapsed().as_secs() < 60 {
                return a.clone();
            }
        }
        let home = self.config().home.clone();
        let (repos, wts, projects) = self.db.read(|c| {
            let repos: Vec<(String, String)> = c.prepare("SELECT root, coalesce(remote, '') FROM repos")?
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<Result<_, _>>()?;
            let wts: Vec<(String, String)> = c.prepare("SELECT path, repo FROM worktrees")?
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<Result<_, _>>()?;
            let projects: Vec<String> = c.prepare("SELECT DISTINCT coalesce(project, '') FROM sessions")?
                .query_map([], |r| r.get(0))?.collect::<Result<_, _>>()?;
            Ok((repos, wts, projects))
        }).unwrap_or_default();
        let mut list: Vec<(String, String)> = repos.iter().map(|(r, _)| (r.clone(), r.clone())).collect();
        list.extend(wts.into_iter().filter(|(_, r)| repos.iter().any(|x| &x.0 == r)));
        let by_remote: HashMap<String, String> =
            repos.iter().filter(|(_, m)| !m.is_empty()).map(|(r, m)| (norm_remote(m), r.clone())).collect();
        let mut seen = HashSet::new();
        for p in projects {
            if let Some(r) = project_root(&p, &home) {
                if seen.insert(r.clone()) && !list.iter().any(|x| x.0 == r) {
                    if let Some(c) = by_remote.get(&git_remote(&r)) {
                        list.push((r, c.clone()));
                    }
                }
            }
        }
        list.sort_by_key(|x| std::cmp::Reverse(x.0.len()));
        let a = std::sync::Arc::new(Aliases { list, roots: repos.into_iter().map(|x| x.0).collect() });
        *g = Some((Instant::now(), a.clone()));
        a
    }

    /// A scope by name: '' or 'all' is everything (None), 'auto' comes from `cwd`, `repos/<root>` and
    /// `files/<folder>` are one-source scopes. Errors with the name for an unknown scope.
    pub fn resolve_scope(&self, name: Option<&str>, cwd: Option<&str>) -> Result<Option<Scope>, String> {
        let cfg = self.config();
        let home = cfg.home.clone();
        let name = name.unwrap_or("");
        if name.is_empty() || name == "all" {
            return Ok(None);
        }
        let named = self.named_scopes();
        let of = |n: &str, v: &Value| -> Result<Scope, String> {
            let src: Vec<String> = v.get("sources").and_then(|s| s.as_array())
                .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect()).unwrap_or_default();
            build(n, &src, v.get("description").and_then(|d| d.as_str()).unwrap_or(""), &home)
        };
        if name == "auto" {
            let Some(cwd) = cwd.filter(|c| !c.is_empty()) else { return Ok(None) };
            let al = self.aliases();
            let pr = project_root(cwd, &home);
            let mut root = al.locate(cwd).map(|x| x.0)
                .or_else(|| pr.as_deref().and_then(|p| al.locate(p)).map(|x| x.0));
            if root.is_none() {
                let Some(pr) = pr else { return Ok(None) };
                let remote = git_remote(&pr);
                let canon = al.roots.iter().find(|r| **r == pr).cloned().or_else(|| {
                    self.db.read(|c| {
                        let rows: Vec<(String, String)> = c.prepare("SELECT root, coalesce(remote, '') FROM repos")?
                            .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<Result<_, _>>()?;
                        Ok(rows.into_iter().find(|(_, m)| !remote.is_empty() && norm_remote(m) == remote).map(|x| x.0))
                    }).ok().flatten()
                });
                let r = canon.clone().unwrap_or(pr);
                let on = cfg.enabled("repos") && cfg.enabled(&format!("repos/{r}"));
                if canon.is_none() || !on {
                    let n = Path::new(&r).file_name().map(|x| x.to_string_lossy().into_owned()).unwrap_or_default();
                    let mut s = build(&format!("{n} (not indexed)"), &[format!("repos/{r}")],
                                      &format!("Pensieve doesn't index {n}; use grep for its code"), &home)?;
                    s.unindexed = true;
                    return Ok(Some(s));
                }
                root = Some(r);
            }
            let Some(root) = root else { return Ok(None) };
            let want = format!("repos/{root}");
            for (n, sc) in &named {
                if sc.get("sources").and_then(|s| s.as_array()).is_some_and(|a| a.iter().any(|x| x.as_str() == Some(&want))) {
                    let mut s = of(n, sc)?;
                    s.name = format!("{n} (auto)");
                    return Ok(Some(s));
                }
            }
            let n = Path::new(&root).file_name().map(|x| x.to_string_lossy().into_owned()).unwrap_or_default();
            return build(&format!("{n} (auto)"), &[want], &format!("The repo at {root}"), &home).map(Some);
        }
        if name.starts_with("repos/") || name.starts_with("files/") {
            return build(name, &[name.to_string()], "", &home).map(Some);
        }
        match named.get(name) {
            Some(sc) => of(name, sc).map(Some),
            None => Err(name.to_string()),
        }
    }

    pub fn scope_session_ids(&self, scope: &Scope) -> HashSet<String> {
        let rows: Vec<(String, String)> = self.db.read(|c| {
            c.prepare("SELECT id, coalesce(source, '') FROM sessions WHERE n_chunks>0")?
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect()
        }).unwrap_or_default();
        let Some(agents) = &scope.agents else { return rows.into_iter().map(|x| x.0).collect() };
        let roots = &scope.repos;
        let sc = if roots.as_ref().is_none_or(|r| !r.is_empty()) {
            let al = self.aliases();
            self.db.read(|c| session_roots(c, &al)).unwrap_or_default()
        } else {
            HashMap::new()
        };
        rows.into_iter().filter(|(sid, src)| {
            let root = sc.get(sid).cloned().flatten();
            agents.contains(src)
                || match roots {
                    None => root.is_some(),
                    Some(r) => root.is_some_and(|x| r.contains(&x)),
                }
        }).map(|x| x.0).collect()
    }

    /// Chunk ids of `kind` ("file", "code", "session") inside the scope, for the vector search's allowlist. None
    /// lets everything through. Kept for two minutes, or until the table gets a new chunk.
    pub fn allowlist(&self, scope: Option<&Scope>, kind: &str) -> Option<Vec<i64>> {
        let scope = scope?;
        let (table, index) = match kind {
            "file" => ("file_chunks", &self.file_vecs),
            "code" => ("code_chunks", &self.code_vecs),
            _ => ("chunks", &self.session_vecs),
        };
        let fp: Option<i64> = self.db.read(|c| c.query_row(&format!("SELECT MAX(id) FROM {table}"), [], |r| r.get(0)))
            .ok().flatten();
        let key = format!("{}\u{1}{}\u{1}{kind}\u{1}{fp:?}", scope.name, scope.sources.join("\u{2}"));
        {
            let g = self.search.allow_cache.lock().unwrap_or_else(|e| e.into_inner());
            if let Some((at, ids)) = g.get(&key) {
                if at.elapsed().as_secs() < 120 {
                    return ids.clone();
                }
            }
        }
        let ids: Option<Vec<i64>> = match kind {
            "code" => scope.repos.as_ref().map(|r| {
                let r: Vec<&String> = r.iter().collect();
                if r.is_empty() {
                    return Vec::new();
                }
                self.db.read(|c| {
                    c.prepare(&format!("SELECT id FROM code_chunks WHERE repo IN ({})", marks(r.len())))?
                        .query_map(rusqlite::params_from_iter(r.iter()), |x| x.get(0))?.collect()
                }).unwrap_or_default()
            }),
            "file" => scope.folders.as_ref().map(|f| {
                if f.is_empty() {
                    return Vec::new();
                }
                let rows: Vec<(i64, String)> = self.db.read(|c| {
                    c.prepare("SELECT id, coalesce(path, '') FROM file_chunks")?
                        .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect()
                }).unwrap_or_default();
                rows.into_iter().filter(|(_, p)| under(p, f)).map(|x| x.0).collect()
            }),
            _ => scope.agents.as_ref().map(|_| {
                let sids = self.scope_session_ids(scope);
                let rows: Vec<(i64, String)> = self.db.read(|c| {
                    c.prepare("SELECT id, coalesce(session_id, '') FROM chunks")?
                        .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect()
                }).unwrap_or_default();
                rows.into_iter().filter(|(_, s)| sids.contains(s)).map(|x| x.0).collect()
            }),
        };
        let ids = ids.map(|v| v.into_iter().filter(|i| index.contains(*i)).collect::<Vec<_>>());
        let mut g = self.search.allow_cache.lock().unwrap_or_else(|e| e.into_inner());
        if g.len() > 64 {
            g.clear();
        }
        g.insert(key, (Instant::now(), ids.clone()));
        ids
    }

    /// Whether a result id (`file:`, `code:`, `session:`, `folder:`, `person:`, `doc:`) is inside the scope.
    pub fn scope_contains(&self, scope: Option<&Scope>, item_id: &str) -> bool {
        let Some(scope) = scope else { return true };
        let (kind, rf) = item_id.split_once(':').unwrap_or((item_id, ""));
        match kind {
            "file" => scope.folders.as_ref().is_none_or(|f| under(&resolved(Path::new(rf)), f) || under(rf, f)),
            "code" => match &scope.repos {
                None => true,
                Some(r) => {
                    let id: i64 = rf.parse().unwrap_or(-1);
                    let repo: Option<String> = self.db.read(|c| {
                        c.query_row("SELECT repo FROM code_chunks WHERE id=?", [id], |x| x.get(0)).or(Ok(None))
                    }).ok().flatten();
                    repo.is_some_and(|x| r.contains(&x))
                }
            },
            "session" => self.scope_session_ids(scope).contains(rf),
            "folder" => {
                scope.repos.as_ref().is_none_or(|r| r.iter().any(|x| rf == x || rf.starts_with(&format!("{x}/"))))
                    || scope.folders.as_ref().is_none_or(|f| under(rf, f))
            }
            "person" => true,
            "doc" => match &scope.docs {
                None => true,
                Some(d) => rf.parse::<i64>().ok()
                    .and_then(|id| self.doc_source(id))
                    .is_some_and(|s| d.contains(&s)),
            },
            _ => false,
        }
    }

    fn scope_info(&self, name: &str, sc: &Value) -> Value {
        let s = self.resolve_scope(Some(name), None).ok().flatten();
        let n = |k: &str| self.allowlist(s.as_ref(), k).map(|a| json!(a.len())).unwrap_or(Value::Null);
        let sessions = s.as_ref().map(|s| self.scope_session_ids(s).len()).unwrap_or(0);
        json!({"name": name, "description": sc.get("description").and_then(|d| d.as_str()).unwrap_or(""),
               "sources": sc.get("sources").cloned().unwrap_or(json!([])),
               "chunks": {"file": n("file"), "code": n("code"), "session": n("session")}, "sessions": sessions})
    }

    /// GET /api/scopes (also the answer of PUT and DELETE /api/scopes/{name} once the app saved the settings).
    pub fn api_scopes(&self) -> Value {
        let scopes: Vec<Value> = self.named_scopes().iter().map(|(n, sc)| self.scope_info(n, sc)).collect();
        json!({"scopes": scopes, "default_scope": self.config().get("default_scope")})
    }

    /// PUT /api/scopes/{name} and MCP save_scope: the new `scopes` setting, for the app to save.
    pub fn save_scope(&self, name: &str, sources: &[String], description: &str) -> Result<Value, String> {
        build(name, sources, description, &self.config().home)?;
        let mut cur = self.named_scopes();
        cur.insert(name.into(), json!({"sources": sources, "description": description}));
        Ok(Value::Object(cur))
    }

    /// DELETE /api/scopes/{name} and MCP delete_scope: the settings to change (`scopes`, and `default_scope` when
    /// it named this scope). `must_exist`: MCP's delete_scope errors on an unknown name; the route does not.
    pub fn delete_scope(&self, name: &str, must_exist: bool) -> Result<Value, String> {
        let mut cur = self.named_scopes();
        if cur.remove(name).is_none() && must_exist {
            return Err(format!("not found: {name}"));
        }
        let mut changes = json!({"scopes": cur});
        if self.config().get("default_scope").as_str() == Some(name) {
            changes["default_scope"] = json!("");
        }
        Ok(changes)
    }

    /// GET /api/scopes/resolve: what an agent would see. `scope` None means the default_scope setting.
    pub fn api_scopes_resolve(&self, scope: Option<&str>, cwd: Option<&str>) -> Result<Value, String> {
        let s = self.scope_param(scope, cwd)?;
        Ok(describe(s.as_ref()))
    }

    /// `?scope=` of a request: absent means the default_scope setting. Errors with "unknown scope 'x'".
    pub fn scope_param(&self, scope: Option<&str>, cwd: Option<&str>) -> Result<Option<Scope>, String> {
        let def = self.config().get("default_scope").as_str().unwrap_or("").to_string();
        let name = scope.map(String::from).unwrap_or(def);
        self.resolve_scope(Some(&name), cwd).map_err(|n| format!("unknown scope '{n}'"))
    }

    pub fn mcp_current_scope(&self, scope: Option<&Scope>) -> Value {
        describe(scope)
    }

    pub fn mcp_list_scopes(&self, scope: Option<&Scope>) -> Value {
        json!({"scopes": self.named_scopes(), "current": describe(scope)["name"]})
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builds_from_sources() {
        let home = Path::new("/home/test");
        let s = build("pay", &["repos/work/app/".into(), "agents/claude".into(), "connections".into()], "", home)
            .expect("scope");
        assert_eq!(s.repos, Some(BTreeSet::from(["work/app".to_string()])));
        assert_eq!(s.docs, None);
        assert!(s.agents.as_ref().is_some_and(|a| a.contains("claude")));
        assert!(build("x", &["bogus/1".into()], "", home).is_err());
    }

    #[test]
    fn locates_worktrees_and_siblings() {
        let al = Aliases { list: vec![("/w/app".into(), "/w/app".into())], roots: vec!["/w/app".into()] };
        assert_eq!(al.locate("/w/app/.claude/worktrees/a1/src/x.rs"), Some(("/w/app".into(), "src/x.rs".into())));
        assert_eq!(al.locate("/w/app-feature/src"), Some(("/w/app".into(), "src".into())));
        assert_eq!(al.locate("/w/other"), None);
        assert_eq!(norm_remote("git@github.com:Org/App.git"), "github.com/org/app");
    }
}
