//! What the code index answers: the repo list and maps, who wrote what (owners, people, experts, gaps, area
//! health, territory), one chunk, a session's code, and a repo's overview. Each REST route and MCP tool that 0.21
//! served from `repos.py` has an `api_*` or `mcp_*` method here with 0.21's JSON shape.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::fmt;
use std::path::Path;
use std::sync::{Arc, LazyLock};
use std::time::Instant;

use regex::Regex;
use serde::de::{Deserializer, MapAccess, Visitor};
use serde_json::{Value, json};
use store::rusqlite::{self, OptionalExtension};

use super::git::{self, area_of, git, is_bot, GENERATED_DIRS};
use super::{lock, ALL};
use crate::{Index, vectors};

/// Counts in first-seen order, like Python's Counter: `most_common` keeps that order among equal counts.
#[derive(Debug, Clone, Default)]
pub struct Tally {
    order: Vec<String>,
    n: HashMap<String, f64>,
}

impl Tally {
    pub fn add(&mut self, k: &str, v: f64) {
        match self.n.get_mut(k) {
            Some(x) => *x += v,
            None => {
                self.order.push(k.to_string());
                self.n.insert(k.to_string(), v);
            }
        }
    }

    pub fn get(&self, k: &str) -> f64 {
        self.n.get(k).copied().unwrap_or(0.0)
    }

    pub fn most_common(&self, k: usize) -> Vec<(String, f64)> {
        let mut v: Vec<(String, f64)> = self.order.iter().map(|x| (x.clone(), self.n[x])).collect();
        v.sort_by(|a, b| b.1.total_cmp(&a.1));
        v.truncate(k);
        v
    }

    pub fn total(&self) -> f64 {
        self.n.values().sum()
    }

    pub fn len(&self) -> usize {
        self.order.len()
    }

    pub fn is_empty(&self) -> bool {
        self.order.is_empty()
    }

    pub fn iter(&self) -> impl Iterator<Item = (&String, f64)> {
        self.order.iter().map(|k| (k, self.n[k]))
    }
}

/// An `authors` JSON object (`{"Name <email>": lines}`) in its own key order.
pub fn authors(s: &str) -> Vec<(String, i64)> {
    struct V;
    impl<'de> Visitor<'de> for V {
        type Value = Vec<(String, i64)>;
        fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
            f.write_str("an object of counts")
        }
        fn visit_map<A: MapAccess<'de>>(self, mut m: A) -> Result<Self::Value, A::Error> {
            let mut out = Vec::new();
            while let Some((k, v)) = m.next_entry::<String, f64>()? {
                out.push((k, v as i64));
            }
            Ok(out)
        }
    }
    if s.is_empty() {
        return Vec::new();
    }
    let mut d = serde_json::Deserializer::from_str(s);
    d.deserialize_map(V).unwrap_or_default()
}

/// A string as Python's json.dumps writes it (ASCII only).
fn py_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let mut buf = [0u16; 2];
                for u in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{u:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// `{"Name <email>": 3, ...}` as Python's json.dumps of a Counter writes it, so the column reads the same as 0.21's.
pub fn py_json_counter(items: &[(String, i64)]) -> String {
    let mut s = String::from("{");
    for (i, (k, v)) in items.iter().enumerate() {
        if i > 0 {
            s.push_str(", ");
        }
        py_str(k, &mut s);
        s.push_str(&format!(": {v}"));
    }
    s.push('}');
    s
}

fn round(x: f64, d: i32) -> f64 {
    let p = 10f64.powi(d);
    (x * p).round() / p
}

fn name_of(p: &str) -> String {
    git::file_name(p).to_string()
}

/// What a query derived from every chunk of a repo, kept until the code changes.
pub enum Cached {
    Areas(HashMap<String, Area>),
    Agg(Vec<Value>, Vec<Value>),
    Blame(Box<BlameAreas>),
}

/// One code area: its unit centroid, chunks, owners by blamed lines, newest line.
pub struct Area {
    pub centroid: Vec<f32>,
    pub chunks: usize,
    pub owners: Tally,
    pub last: i64,
    order: usize,
}

#[derive(Default)]
pub struct BlameAreas {
    area_lines: Vec<(String, Tally)>,
    lines_by: Tally,
    area_last: HashMap<String, i64>,
    reference: i64,
    deep_lines: Vec<(String, Tally)>,
    deep_last: HashMap<String, i64>,
    own_last: HashMap<String, i64>,
    emails_by_name: HashMap<String, HashSet<String>>,
    last_by_email: HashMap<String, i64>,
}

fn tally_of<'a>(v: &'a mut Vec<(String, Tally)>, k: &str) -> &'a mut Tally {
    match v.iter().position(|x| x.0 == k) {
        Some(i) => &mut v[i].1,
        None => {
            v.push((k.to_string(), Tally::default()));
            let n = v.len() - 1;
            &mut v[n].1
        }
    }
}

type Rows<T> = rusqlite::Result<Vec<T>>;

impl Index {
    fn cached(&self, key: &str) -> Option<Arc<Cached>> {
        lock(&self.repos.cache).get(key).cloned()
    }

    fn keep(&self, key: &str, v: Cached) -> Arc<Cached> {
        let v = Arc::new(v);
        lock(&self.repos.cache).insert(key.to_string(), v.clone());
        v
    }

    /// The root of a repo by its name or root; `__all__` stands for itself.
    pub fn repo_root(&self, name: &str) -> Option<String> {
        if name == ALL {
            return Some(ALL.into());
        }
        self.db.read(|c| c.query_row("SELECT root FROM repos WHERE name=? OR root=?", [name, name], |r| r.get(0)).optional())
            .ok().flatten()
    }

    fn repo_names(&self) -> HashMap<String, String> {
        self.db.read(|c| c.prepare("SELECT root, name FROM repos")?.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect())
            .unwrap_or_default()
    }

    /// Rows of `cols` (starting with path) for one repo, or for every repo under ALL with each path prefixed by the
    /// repo's name.
    fn sel<T>(&self, name: &str, cols: &str, extra: &str, f: impl Fn(String, &rusqlite::Row) -> rusqlite::Result<T>) -> Vec<T> {
        if name == ALL {
            let names = self.repo_names();
            return self.db.read(|c| {
                let mut st = c.prepare(&format!("SELECT repo, {cols} FROM code_chunks WHERE 1=1 {extra}"))?;
                let rows: Rows<T> = st.query_map([], |r| {
                    let repo: String = r.get(0)?;
                    let path: String = r.get(1)?;
                    let n = names.get(&repo).cloned().unwrap_or_else(|| name_of(&repo));
                    f(format!("{n}/{path}"), r)
                })?.collect();
                rows
            }).unwrap_or_default();
        }
        let Some(root) = self.repo_root(name) else { return Vec::new() };
        self.db.read(|c| {
            let mut st = c.prepare(&format!("SELECT '', {cols} FROM code_chunks WHERE repo=? {extra}"))?;
            let rows: Rows<T> = st.query_map([&root], |r| f(r.get(1)?, r))?.collect();
            rows
        }).unwrap_or_default()
    }

    /// GET /api/repos and MCP list_repos (filter with `allowed`): every repo with its chunks, files and sessions.
    pub fn api_repos(&self) -> Value {
        let rows: Vec<(String, String, i64, i64)> = self.db.read(|c| {
            c.prepare("SELECT r.root, r.name, COUNT(c.id), COUNT(DISTINCT c.path) FROM repos r \
                       LEFT JOIN code_chunks c ON c.repo=r.root GROUP BY r.root ORDER BY 3 DESC")?
                .query_map([], |r| Ok((r.get(0)?, r.get::<_, Option<String>>(1)?.unwrap_or_default(), r.get(2)?, r.get(3)?)))?.collect()
        }).unwrap_or_default();
        let live: HashSet<String> = self.db.read(|c| {
            c.prepare("SELECT id FROM sessions WHERE n_chunks>0")?.query_map([], |r| r.get(0))?.collect()
        }).unwrap_or_default();
        let mut per: HashMap<String, usize> = HashMap::new();
        for (k, v) in self.session_scope().iter() {
            if live.contains(k) {
                if let Some(r) = &v.root {
                    *per.entry(r.clone()).or_default() += 1;
                }
            }
        }
        Value::Array(rows.into_iter().map(|(root, name, chunks, files)| {
            let s = per.get(&root).copied().unwrap_or(0);
            json!({"root": root, "name": name, "chunks": chunks, "files": files, "sessions": s})
        }).collect())
    }

    /// MCP list_repos: the repos in the connection's scope (`allowed(root)`).
    pub fn mcp_list_repos(&self, allowed: &dyn Fn(&str) -> bool) -> Value {
        let all = self.api_repos();
        Value::Array(all.as_array().into_iter().flatten().filter(|r| allowed(r["root"].as_str().unwrap_or(""))).cloned().collect())
    }

    /// GET /api/repos/upstream: root -> {branch, behind, current}, from the last `check_upstream`.
    pub fn api_repos_upstream(&self) -> Value {
        json!(*lock(&self.repos.upstream))
    }

    /// POST /api/repos/pull {root}: fast-forward the local default branch. Err(404) for a root that is not indexed.
    pub fn api_repos_pull(&self, root: &str) -> Result<Value, u16> {
        if !self.checkouts().values().any(|r| r == root) {
            return Err(404);
        }
        let (ok, message) = git::pull_default(root);
        let u = git::upstream(root);
        if let Some(u) = &u {
            lock(&self.repos.upstream).insert(root.to_string(), u.clone());
        }
        let behind = u.as_ref().and_then(|u| u["behind"].as_i64()).unwrap_or(0);
        Ok(json!({"ok": ok, "message": message, "behind": behind}))
    }

    /// GET /api/repo-check?cwd=: what Pensieve knows of the repo at a folder: state none | indexed | pending |
    /// declined | unknown. A clone of an indexed repo (same remote) counts as that repo.
    pub fn api_repo_check(&self, cwd: &str) -> Value {
        let indexed: Vec<(String, String)> = self.db.read(|c| {
            c.prepare("SELECT root, coalesce(remote, '') FROM repos")?.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect()
        }).unwrap_or_default();
        let cfg = self.config();
        let Some(root) = (!cwd.is_empty()).then(|| git::project_root(cwd, &cfg.home)).flatten() else {
            return json!({"state": "none"});
        };
        let mut canon = indexed.iter().find(|(r, _)| *r == root).map(|(r, _)| r.clone());
        if canon.is_none() {
            let remote = git::norm_remote(&git(&root, &["config", "--get", "remote.origin.url"]));
            canon = indexed.iter().find(|(_, rem)| !remote.is_empty() && *rem == remote).map(|(r, _)| r.clone());
        }
        let root = canon.clone().unwrap_or(root);
        let base = |state: &str| json!({"root": root, "name": name_of(&root), "state": state});
        if !cfg.enabled("repos") || !cfg.enabled(&format!("repos/{root}")) {
            return base("declined");
        }
        if canon.is_some() {
            return base("indexed");
        }
        if cfg.strs("repos").iter().any(|p| cfg.expand(p) == Path::new(&root)) {
            return base("pending");
        }
        let mut v = base("unknown");
        v["files"] = json!(git(&root, &["ls-files"]).matches('\n').count());
        v
    }

    /// repo-relative path -> [(session id, edited)], from the absolute paths that sessions touched.
    fn session_links(&self, root: &str) -> HashMap<String, Vec<(String, bool)>> {
        let mut aliases: Vec<String> = self.canon_map().into_iter().filter(|(_, v)| v == root).map(|(k, _)| k).collect();
        if aliases.is_empty() {
            aliases.push(root.to_string());
        }
        let rows: Vec<(String, String, bool)> = self.db.read(|c| {
            c.prepare("SELECT session_id, path, coalesce(edited, 0) FROM session_files WHERE path!=''")?
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get::<_, i64>(2)? != 0)))?.collect()
        }).unwrap_or_default();
        let mut links: HashMap<String, Vec<(String, bool)>> = HashMap::new();
        for (sid, p, e) in rows {
            for a in &aliases {
                if let Some(rel) = p.strip_prefix(&format!("{a}/")) {
                    links.entry(rel.to_string()).or_default().push((sid.clone(), e));
                    break;
                }
            }
        }
        links
    }

    /// GET /api/repo/{name}/points: the chunks of a repo's code map (`__all__`: the joint map). None: unknown repo.
    pub fn api_repo_points(&self, name: &str) -> Option<Value> {
        let root = self.repo_root(name)?;
        let mut links = HashMap::new();
        let (cols, extra) = if name == ALL {
            for (r, n) in self.repo_names() {
                for (k, v) in self.session_links(&r) {
                    links.insert(format!("{n}/{k}"), v);
                }
            }
            ("path, id, dir, start, end, authors, last_ts, gx, gy, gz", "AND gx IS NOT NULL")
        } else {
            links = self.session_links(&root);
            ("path, id, dir, start, end, authors, last_ts, x, y, z", "AND x IS NOT NULL")
        };
        let pts = self.sel(name, cols, extra, |path, r| {
            let au = authors(&r.get::<_, Option<String>>(6)?.unwrap_or_default());
            let top = au.iter().fold(None::<&(String, i64)>, |b, x| if b.is_none_or(|b| x.1 > b.1) { Some(x) } else { b })
                .map(|x| x.0.clone()).unwrap_or_default();
            let sessions: BTreeSet<String> = links.get(&path).into_iter().flatten().map(|x| x.0.clone()).collect();
            Ok(json!({"id": r.get::<_, i64>(2)?, "path": path, "dir": r.get::<_, Option<String>>(3)?,
                "start": r.get::<_, Option<i64>>(4)?, "end": r.get::<_, Option<i64>>(5)?, "author": top,
                "ts": r.get::<_, Option<i64>>(7)?,
                "p": [r.get::<_, Option<f64>>(8)?, r.get::<_, Option<f64>>(9)?, r.get::<_, Option<f64>>(10)?],
                "sessions": sessions}))
        });
        Some(json!({"repo": name, "points": pts}))
    }

    /// Per-person commit activity of a repo over `days`: volume, weekly rhythm, areas, recent subjects. Cached for
    /// five minutes. None: unknown repo.
    pub fn repo_team(&self, name: &str, days: i64) -> Option<Value> {
        let root = self.repo_root(name)?;
        if let Some((at, v)) = lock(&self.repos.team).get(&(root.clone(), days)) {
            if at.elapsed().as_secs() < 300 {
                return Some(v.clone());
            }
        }
        static NUM: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(\d+|-)\t(\d+|-)\t(.+)").expect("regex"));
        static RENAME: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\{[^}]* => ([^}]*)\}").expect("regex"));
        let out = git(&root, &["log", &format!("--since={days}.days"), "--no-merges", "--numstat",
            "--format=%x1e%H%x1f%at%x1f%an%x1f%ae%x1f%s"]);
        let me = git(&root, &["config", "user.email"]).trim().to_lowercase();
        let now = store::now();
        let nw = (days / 7).max(1) as usize;
        struct P {
            name: String,
            email: String,
            commits: i64,
            added: i64,
            deleted: i64,
            weeks: Vec<i64>,
            areas: Tally,
            files: HashSet<String>,
            subjects: Vec<Value>,
            last: i64,
        }
        let mut people: Vec<P> = Vec::new();
        let mut head: Option<String> = None;
        let mut area_activity: Vec<(String, Tally)> = Vec::new();
        let mut file_activity: Vec<(String, Tally)> = Vec::new();
        for rec in out.split('\x1e').skip(1) {
            let lines: Vec<&str> = rec.trim_matches('\n').split('\n').collect();
            let bits: Vec<&str> = lines[0].splitn(5, '\x1f').collect();
            if bits.len() < 5 {
                continue;
            }
            let (sha, ts, an, ae, subj) = (bits[0], bits[1].parse::<i64>().unwrap_or(0), bits[2], bits[3].to_lowercase(), bits[4]);
            if head.is_none() {
                head = Some(sha.to_string());
            }
            if is_bot(an) || is_bot(&ae) {
                continue;
            }
            let i = match people.iter().position(|p| p.email == ae) {
                Some(i) => i,
                None => {
                    people.push(P { name: an.into(), email: ae.clone(), commits: 0, added: 0, deleted: 0, weeks: vec![0; nw],
                        areas: Tally::default(), files: HashSet::new(), subjects: Vec::new(), last: 0 });
                    people.len() - 1
                }
            };
            let p = &mut people[i];
            p.commits += 1;
            p.last = p.last.max(ts);
            let age = (((now - ts as f64) / (7.0 * 86400.0)) as i64).clamp(0, nw as i64 - 1) as usize;
            p.weeks[nw - 1 - age] += 1;
            if p.subjects.len() < 15 {
                p.subjects.push(json!({"sha": &sha[..sha.len().min(8)], "ts": ts, "subject": subj}));
            }
            let mut touched: Vec<String> = Vec::new();
            for ln in &lines[1..] {
                let Some(m) = NUM.captures(ln) else { continue };
                let path = RENAME.replace_all(&m[3], "$1").replace("//", "/");
                let path = path.rsplit(" => ").next().unwrap_or(&path).to_string();
                p.added += m[1].parse::<i64>().unwrap_or(0);
                p.deleted += m[2].parse::<i64>().unwrap_or(0);
                p.files.insert(path.clone());
                tally_of(&mut file_activity, &path).add(&ae, 1.0);
                let a = area_of(&path);
                if !touched.contains(&a) {
                    touched.push(a);
                }
            }
            for a in touched {
                p.areas.add(&a, 1.0);
                tally_of(&mut area_activity, &a).add(&ae, 1.0);
            }
        }
        let mut idx: Vec<usize> = (0..people.len()).collect();
        idx.sort_by_key(|&i| std::cmp::Reverse(people[i].commits));
        let ppl: Vec<Value> = idx.iter().map(|&i| {
            let p = &people[i];
            json!({"name": p.name, "email": p.email, "me": p.email == me, "commits": p.commits, "added": p.added,
                "deleted": p.deleted, "weeks": p.weeks,
                "areas": p.areas.most_common(5).into_iter().map(|(a, n)| json!({"area": a, "commits": n as i64})).collect::<Vec<_>>(),
                "files": p.files.len(), "subjects": p.subjects, "last": p.last, "author": format!("{} <{}>", p.name, p.email)})
        }).collect();
        let tal = |v: &[(String, Tally)]| -> Value {
            Value::Object(v.iter().map(|(k, t)| (k.clone(), Value::Object(t.iter().map(|(e, n)| (e.clone(), json!(n as i64))).collect()))).collect())
        };
        let res = json!({"repo": name, "root": root, "days": days, "head": head, "people": ppl, "me": me,
            "area_activity": tal(&area_activity), "file_activity": tal(&file_activity)});
        lock(&self.repos.team).insert((root, days), (Instant::now(), res.clone()));
        Some(res)
    }

    /// GET /api/team/{name}: the team without its area activity. The app adds `summaries` and `summarizing`.
    pub fn api_team(&self, name: &str, days: i64) -> Option<Value> {
        let mut t = self.repo_team(name, days)?;
        if let Some(o) = t.as_object_mut() {
            o.remove("area_activity");
        }
        Some(t)
    }

    /// MCP team (and people(repo=)): commit counts, areas and recent subjects of the 15 most active people.
    pub fn mcp_team(&self, repo: &str, days: i64) -> Result<Value, String> {
        let t = self.repo_team(repo, days).ok_or_else(|| format!("unknown repo '{repo}'"))?;
        let people: Vec<Value> = t["people"].as_array().into_iter().flatten().take(15).map(|p| json!({
            "name": p["name"], "email": p["email"], "me": p["me"], "commits": p["commits"], "areas": p["areas"],
            "recent": p["subjects"].as_array().into_iter().flatten().take(5).map(|s| s["subject"].clone()).collect::<Vec<_>>(),
        })).collect();
        Ok(json!({"repo": repo, "days": days, "people": people}))
    }

    /// Code areas: top-level folders, split into subfolders while an area holds more than ~6% of the repo (down to
    /// 4 levels), each with its centroid, chunks, owners and newest line.
    fn area_vectors(&self, root: &str) -> Arc<Cached> {
        let key = format!("areas\x1f{root}");
        if let Some(c) = self.cached(&key) {
            return c;
        }
        let rows: Vec<(String, i64, String, i64)> = self.db.read(|c| {
            c.prepare("SELECT path, id, coalesce(authors, ''), coalesce(last_ts, 0) FROM code_chunks WHERE repo=?")?
                .query_map([root], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?.collect()
        }).unwrap_or_default();
        let vecs = self.code_vecs.take(&rows.iter().map(|r| r.1).collect::<Vec<_>>());
        let rows: Vec<((String, i64, String, i64), Vec<f32>)> = rows.into_iter().zip(vecs).filter_map(|(r, v)| v.map(|v| (r, v))).collect();
        let dirs: Vec<String> = rows.iter().map(|(r, _)| r.0.rsplit_once('/').map(|x| x.0.to_string()).unwrap_or_else(|| ".".into())).collect();
        let limit = (120f64).max(0.06 * rows.len() as f64);
        let mut areas: HashSet<String> = HashSet::from([".".to_string()]);
        fn split(prefix: &str, depth: usize, dirs: &[String], limit: f64, areas: &mut HashSet<String>) {
            let mut kids = Tally::default();
            for d in dirs {
                let parts: Vec<&str> = d.split('/').collect();
                if d != "." && (prefix.is_empty() || d.starts_with(&format!("{prefix}/"))) && parts.len() > depth {
                    kids.add(parts[depth], 1.0);
                }
            }
            for (k, n) in kids.iter() {
                let a = if prefix.is_empty() { k.clone() } else { format!("{prefix}/{k}") };
                areas.insert(a.clone());
                if n > limit && depth < 3 {
                    split(&a, depth + 1, dirs, limit, areas);
                }
            }
        }
        split("", 0, &dirs, limit, &mut areas);
        let mut acc: HashMap<String, Area> = HashMap::new();
        for ((r, v), d) in rows.iter().zip(&dirs) {
            let a = area_for(d, &areas);
            let n = acc.len();
            let e = acc.entry(a).or_insert_with(|| Area { centroid: vec![0.0; v.len()], chunks: 0, owners: Tally::default(), last: 0, order: n });
            for (s, x) in e.centroid.iter_mut().zip(v) {
                *s += x;
            }
            e.chunks += 1;
            e.last = e.last.max(r.3);
            for (k, n) in authors(&r.2) {
                if !is_bot(&k) {
                    e.owners.add(&k, n as f64);
                }
            }
        }
        for e in acc.values_mut() {
            e.centroid = vectors::unit(&e.centroid);
        }
        self.keep(&key, Cached::Areas(acc))
    }

    /// Explored and unexplored code areas of a repo: untouched areas ranked by how near they sit to the areas the
    /// user's sessions worked in and to what those sessions talked about. `sids`: only these sessions.
    pub fn territory(&self, name: &str, sids: Option<&HashSet<String>>, top: usize) -> Option<Value> {
        let root = self.repo_root(name)?;
        let cached = self.area_vectors(&root);
        let Cached::Areas(areas) = &*cached else { return None };
        let mut names: HashSet<String> = areas.keys().cloned().collect();
        names.extend(areas.keys().filter_map(|a| a.rsplit_once('/').map(|x| x.0.to_string())));
        let scope = self.session_scope();
        let mine: Vec<String> = scope.iter().filter(|(s, v)| sids.is_none_or(|x| x.contains(*s))
            && (v.project == name || v.dirs.iter().any(|d| d.starts_with(&format!("{name}/"))))).map(|(s, _)| s.clone()).collect();
        let mut touched: HashMap<String, usize> = HashMap::new();
        for s in &mine {
            let mut hit: HashSet<String> = HashSet::new();
            for d in &scope[s].dirs {
                if let Some(rest) = d.strip_prefix(&format!("{name}/")) {
                    let a = area_for(rest, &names);
                    if areas.contains_key(&a) {
                        hit.insert(a);
                    } else {
                        hit.extend(areas.keys().filter(|x| x.starts_with(&format!("{a}/")) && a != ".").cloned());
                    }
                }
            }
            for a in hit {
                *touched.entry(a).or_default() += 1;
            }
        }
        let mut keys: Vec<&String> = areas.keys().filter(|a| areas[*a].chunks >= 3).collect();
        keys.sort_by_key(|a| areas[*a].order);
        if keys.is_empty() {
            return Some(json!({"repo": name, "footprint": [], "suggestions": [], "coverage": 0, "sessions": mine.len()}));
        }
        let t: Vec<usize> = (0..keys.len()).filter(|&i| touched.contains_key(keys[i])).collect();
        let adj: Vec<Vec<f32>> = keys.iter().map(|a| t.iter().map(|&j| vectors::dot(&areas[*a].centroid, &areas[keys[j]].centroid)).collect()).collect();
        let adj_max: Vec<f64> = adj.iter().map(|r| r.iter().fold(None::<f32>, |m, x| Some(m.map_or(*x, |m| m.max(*x)))).unwrap_or(0.0) as f64).collect();
        let mut interest = vec![0f64; keys.len()];
        let want: HashSet<&String> = mine.iter().collect();
        let groups: Vec<(String, Vec<i64>)> = self.db.read(|c| {
            let mut st = c.prepare("SELECT id, session_id FROM chunks")?;
            let mut by: HashMap<String, Vec<i64>> = HashMap::new();
            for r in st.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<String>>(1)?)))? {
                let (id, s) = r?;
                if let Some(s) = s.filter(|s| want.contains(s)) {
                    by.entry(s).or_default().push(id);
                }
            }
            Ok(by.into_iter().collect())
        }).unwrap_or_default();
        let (sel, cents) = vectors::centroids(&groups, &self.session_vecs);
        if !sel.is_empty() {
            let dim = cents[0].len();
            let mean: Vec<f32> = (0..dim).map(|d| cents.iter().map(|c| c[d]).sum::<f32>() / cents.len() as f32).collect();
            let v = vectors::unit(&mean);
            interest = keys.iter().map(|a| vectors::dot(&areas[*a].centroid, &v) as f64).collect();
        }
        let z = |x: &[f64]| -> Vec<f64> {
            let m = x.iter().sum::<f64>() / x.len() as f64;
            let sd = (x.iter().map(|v| (v - m).powi(2)).sum::<f64>() / x.len() as f64).sqrt();
            x.iter().map(|v| (v - m) / (sd + 1e-9)).collect()
        };
        let (za, zi) = (z(&adj_max), z(&interest));
        let score: Vec<f64> = (0..keys.len()).map(|i| 0.55 * za[i] + 0.45 * zi[i]).collect();
        let team = lock(&self.repos.team).get(&(root.clone(), 90)).map(|x| x.1.clone()).unwrap_or(json!({}));
        let me = team["me"].as_str().map(String::from).unwrap_or_else(|| git(&root, &["config", "user.email"]).trim().to_string()).to_lowercase();
        let not_me = |a: &str| !a.to_lowercase().contains(&format!("<{me}>"));
        let area_set: HashSet<String> = areas.keys().cloned().collect();
        let mut recent: HashMap<String, Tally> = HashMap::new();
        if let Some(fa) = team["file_activity"].as_object() {
            for (f, c) in fa {
                let a = area_for(f.rsplit_once('/').map(|x| x.0).unwrap_or("."), &area_set);
                for (e, n) in c.as_object().into_iter().flatten() {
                    if *e != me {
                        recent.entry(a.clone()).or_default().add(e, n.as_f64().unwrap_or(0.0));
                    }
                }
            }
        }
        let mut order: Vec<usize> = (0..keys.len()).collect();
        order.sort_by(|&a, &b| score[b].total_cmp(&score[a]));
        let empty = Tally::default();
        let mut sugg = Vec::new();
        for i in order {
            let a = keys[i];
            if touched.contains_key(a) || a == "." || GENERATED_DIRS.contains(&a.split('/').next().unwrap_or("")) {
                continue;
            }
            let near = (!t.is_empty()).then(|| {
                let j = adj[i].iter().enumerate().fold((0, f32::MIN), |b, (j, x)| if *x > b.1 { (j, *x) } else { b }).0;
                keys[t[j]].clone()
            });
            let act = recent.get(a).unwrap_or(&empty);
            let ar = &areas[a];
            sugg.push(json!({"area": a, "chunks": ar.chunks, "score": round(score[i], 3), "near": near,
                "near_sim": (!t.is_empty()).then(|| round(adj_max[i], 3)),
                "owners": ar.owners.most_common(4).into_iter().map(|x| x.0).filter(|k| not_me(k)).take(3).collect::<Vec<_>>(),
                "last_ts": ar.last, "recent_commits": act.total() as i64,
                "recent_people": act.most_common(3).into_iter().map(|x| x.0).collect::<Vec<_>>()}));
            if sugg.len() >= top {
                break;
            }
        }
        let total = keys.iter().map(|a| areas[*a].chunks).sum::<usize>().max(1);
        let mut foot: Vec<(usize, Value)> = keys.iter().map(|a| {
            let ar = &areas[*a];
            (ar.chunks, json!({"area": a, "chunks": ar.chunks, "sessions": touched.get(*a).copied().unwrap_or(0),
                "recent_commits": recent.get(*a).map(|t| t.total() as i64).unwrap_or(0),
                "owners": ar.owners.most_common(3).into_iter().map(|x| x.0).filter(|k| not_me(k)).take(2).collect::<Vec<_>>()}))
        }).collect();
        foot.sort_by_key(|x| std::cmp::Reverse(x.0));
        let covered: usize = touched.keys().filter_map(|a| areas.get(a)).map(|a| a.chunks).sum();
        Some(json!({"repo": name, "footprint": foot.into_iter().map(|x| x.1).collect::<Vec<_>>(), "suggestions": sugg,
            "sessions": mine.len(), "coverage": round(covered as f64 / total as f64, 4)}))
    }

    /// POST /api/territory/{name}: warms the 90-day team first (its recent activity per area), as 0.21 did.
    pub fn api_territory(&self, name: &str, sids: Option<&HashSet<String>>) -> Option<Value> {
        self.repo_team(name, 90);
        self.territory(name, sids, 8)
    }

    /// MCP repo(part=unexplored).
    pub fn mcp_unexplored(&self, repo: &str) -> Result<Value, String> {
        let r = self.territory(repo, None, 8).ok_or_else(|| format!("unknown repo '{repo}'"))?;
        Ok(json!({"coverage": r["coverage"], "suggestions": r["suggestions"]}))
    }

    /// Blame aggregates of a repo: owners per folder and people by lines.
    fn agg(&self, root: &str) -> Arc<Cached> {
        let key = format!("agg\x1f{root}");
        if let Some(c) = self.cached(&key) {
            return c;
        }
        let rows: Vec<(String, String, i64)> = if root == ALL {
            self.sel(ALL, "path, authors, last_ts", "", |p, r| Ok((p, r.get::<_, Option<String>>(2)?.unwrap_or_default(), r.get::<_, Option<i64>>(3)?.unwrap_or(0))))
        } else {
            self.db.read(|c| {
                c.prepare("SELECT path, coalesce(authors, ''), coalesce(last_ts, 0) FROM code_chunks WHERE repo=?")?
                    .query_map([root], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?.collect()
            }).unwrap_or_default()
        };
        let depth = if root == ALL { 3 } else { 2 };
        let mut dirs: Vec<(String, Tally)> = Vec::new();
        let mut dir_chunks: HashMap<String, usize> = HashMap::new();
        struct P { lines: f64, files: HashSet<String>, dirs: Tally, last: i64, order: usize }
        let mut people: HashMap<String, P> = HashMap::new();
        for (path, au, ts) in &rows {
            let parts: Vec<&str> = path.split('/').collect();
            let key = if parts.len() > depth { parts[..depth].join("/") } else {
                let k = parts[..parts.len() - 1].join("/");
                if k.is_empty() { ".".into() } else { k }
            };
            *dir_chunks.entry(key.clone()).or_default() += 1;
            for (a, n) in authors(au) {
                if is_bot(&a) {
                    continue;
                }
                tally_of(&mut dirs, &key).add(&a, n as f64);
                let o = people.len();
                let p = people.entry(a).or_insert_with(|| P { lines: 0.0, files: HashSet::new(), dirs: Tally::default(), last: 0, order: o });
                p.lines += n as f64;
                p.files.insert(path.clone());
                p.dirs.add(&key, n as f64);
                p.last = p.last.max(*ts);
            }
        }
        let total = people.values().map(|p| p.lines).sum::<f64>();
        let total = if total == 0.0 { 1.0 } else { total };
        let mut d_sorted: Vec<&(String, Tally)> = dirs.iter().collect();
        d_sorted.sort_by_key(|(d, _)| std::cmp::Reverse(dir_chunks[d]));
        let owners: Vec<Value> = d_sorted.into_iter().map(|(d, c)| json!({"dir": d, "chunks": dir_chunks[d], "lines": c.total() as i64,
            "owners": c.most_common(4).into_iter().map(|(a, n)| json!({"author": a, "lines": n as i64})).collect::<Vec<_>>()})).collect();
        let mut ps: Vec<(&String, &P)> = people.iter().collect();
        ps.sort_by(|a, b| b.1.lines.total_cmp(&a.1.lines).then(a.1.order.cmp(&b.1.order)));
        let ppl: Vec<Value> = ps.into_iter().map(|(a, p)| json!({"author": a, "lines": p.lines as i64, "share": round(p.lines / total, 4),
            "files": p.files.len(), "last_active": p.last,
            "dirs": p.dirs.most_common(4).into_iter().map(|x| x.0).collect::<Vec<_>>()})).collect();
        self.keep(&key, Cached::Agg(owners, ppl))
    }

    /// GET /api/repo/{name}/owners: the 40 biggest folders with their top blame owners.
    pub fn api_repo_owners(&self, name: &str) -> Value {
        let Some(root) = self.repo_root(name) else { return json!([]) };
        match &*self.agg(&root) {
            Cached::Agg(o, _) => Value::Array(o.iter().take(40).cloned().collect()),
            _ => json!([]),
        }
    }

    /// GET /api/repo/{name}/people: the 60 people with the most blamed lines.
    pub fn api_repo_people(&self, name: &str) -> Value {
        let Some(root) = self.repo_root(name) else { return json!([]) };
        match &*self.agg(&root) {
            Cached::Agg(_, p) => Value::Array(p.iter().take(60).cloned().collect()),
            _ => json!([]),
        }
    }

    /// Blame lines per area and author name, the newest line of each area, each name's emails, and the newest
    /// commit (main checkout only).
    fn blame_areas(&self, root: &str) -> Arc<Cached> {
        let key = format!("blame_areas\x1f{root}");
        if let Some(c) = self.cached(&key) {
            return c;
        }
        let (rows, mut last_by_email, reference, people_git) = self.db.read(|c| {
            let rows: Vec<(String, String, i64)> = c.prepare("SELECT path, coalesce(authors, ''), coalesce(last_ts, 0) FROM code_chunks WHERE repo=? AND wt=''")?
                .query_map([root], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?.collect::<Result<_, _>>()?;
            let last: HashMap<String, i64> = c.prepare("SELECT lower(author), MAX(ts) FROM commits WHERE repo=? GROUP BY lower(author)")?
                .query_map([root], |r| Ok((r.get::<_, Option<String>>(0)?.unwrap_or_default(), r.get::<_, Option<i64>>(1)?.unwrap_or(0))))?
                .collect::<Result<_, _>>()?;
            let reference: i64 = c.query_row("SELECT coalesce(MAX(ts), 0) FROM commits WHERE repo=?", [root], |r| r.get(0))?;
            let pg: Option<Vec<u8>> = c.query_row("SELECT value FROM cache WHERE key=?", [format!("people:git:{root}")], |r| r.get(0)).optional()?;
            Ok((rows, last, reference, pg))
        }).unwrap_or_default();
        if let Some(v) = people_git.and_then(|b| serde_json::from_slice::<Value>(&b).ok()) {
            for (e, a) in v["authors"].as_object().into_iter().flatten() {
                if let Some(t) = a["last"].as_f64().filter(|t| *t > 0.0) {
                    let e = e.to_lowercase();
                    let cur = last_by_email.get(&e).copied().unwrap_or(0);
                    last_by_email.insert(e, cur.max(t as i64));
                }
            }
        }
        static WHO: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(.*?)\s*<([^>]*)>").expect("regex"));
        let who = |a: &str| -> (String, String) {
            match WHO.captures(a) {
                Some(m) => (m[1].trim().to_string(), m[2].trim().to_lowercase()),
                None => (a.trim().to_string(), String::new()),
            }
        };
        let mut b = BlameAreas { reference, last_by_email, ..Default::default() };
        for (path, au, ts) in &rows {
            let area = area_of(path);
            let parts: Vec<&str> = path.split('/').collect();
            let deep = if parts.len() > 3 { parts[..3].join("/") } else { area.clone() };
            let e = b.area_last.entry(area.clone()).or_default();
            *e = (*e).max(*ts);
            let e = b.deep_last.entry(deep.clone()).or_default();
            *e = (*e).max(*ts);
            let mut names: Vec<String> = Vec::new();
            for (a, n) in authors(au) {
                if is_bot(&a) {
                    continue;
                }
                let (nm, em) = who(&a);
                if !names.contains(&nm) {
                    names.push(nm.clone());
                }
                b.emails_by_name.entry(nm.to_lowercase()).or_default().insert(em);
                tally_of(&mut b.area_lines, &area).add(&nm, n as f64);
                tally_of(&mut b.deep_lines, &deep).add(&nm, n as f64);
                b.lines_by.add(&nm, n as f64);
            }
            if names.len() == 1 {
                let e = b.own_last.entry(names[0].clone()).or_default();
                *e = (*e).max(*ts);
            }
        }
        self.keep(&key, Cached::Blame(Box::new(b)))
    }

    /// GET /api/repo/{name}/gaps and MCP repo(part=gaps): areas whose knowledge left with their authors. None for an
    /// unknown repo or `__all__`.
    pub fn knowledge_gaps(&self, name: &str, inactive_days: i64, min_lines: f64) -> Option<Value> {
        let root = self.repo_root(name).filter(|r| r != ALL)?;
        let cached = self.blame_areas(&root);
        let Cached::Blame(b) = &*cached else { return None };
        let (last, gone) = departed(b, inactive_days);
        let people: Vec<Value> = b.lines_by.most_common(40).into_iter().map(|(nm, n)| json!({"name": nm, "lines": n as i64,
            "last_active": last.get(&nm).filter(|t| **t != 0), "departed": gone.contains(&nm)})).collect();
        let mut gaps: Vec<(f64, Value)> = Vec::new();
        for (area, c) in &b.area_lines {
            let total = c.total();
            if total < min_lines || area == "." || GENERATED_DIRS.contains(&area.split('/').next().unwrap_or("")) {
                continue;
            }
            let gone_lines: f64 = c.iter().filter(|(nm, _)| gone.contains(*nm)).map(|x| x.1).sum();
            let share = gone_lines / total;
            let active: Vec<(String, f64)> = c.most_common(usize::MAX).into_iter().filter(|(nm, _)| !gone.contains(nm)).collect();
            let bus = active.iter().filter(|(_, n)| n / total >= 0.10).count();
            if share < 0.5 && bus >= 1 {
                continue;
            }
            let parent = area.split('/').next().unwrap_or("");
            let mut near = Tally::default();
            for (a2, c2) in &b.area_lines {
                if a2 != area && a2.split('/').next() == Some(parent) {
                    for (nm, n) in c2.iter() {
                        if !gone.contains(nm) {
                            near.add(nm, n);
                        }
                    }
                }
            }
            let ask = active.first().map(|x| x.0.clone()).or_else(|| near.most_common(1).into_iter().next().map(|x| x.0));
            let reason = if !active.is_empty() { Some("knows the most of what's left".to_string()) }
                else if !near.is_empty() { Some(format!("most active nearby in {parent}/")) } else { None };
            gaps.push((total * round(share, 3), json!({"area": area, "lines": total as i64, "departed_share": round(share, 3),
                "bus_factor": bus,
                "departed": c.most_common(usize::MAX).into_iter().filter(|(nm, _)| gone.contains(nm)).take(4)
                    .map(|(nm, n)| json!({"name": nm, "share": round(n / total, 3), "last_active": last.get(&nm).filter(|t| **t != 0)})).collect::<Vec<_>>(),
                "active": active.iter().take(4).map(|(nm, n)| json!({"name": nm, "share": round(n / total, 3)})).collect::<Vec<_>>(),
                "ask": ask, "ask_reason": reason})));
        }
        gaps.sort_by(|a, b| b.0.total_cmp(&a.0));
        let mut dep: Vec<(String, f64)> = gone.iter().map(|nm| (nm.clone(), b.lines_by.get(nm))).collect();
        dep.sort_by(|x, y| y.1.total_cmp(&x.1));
        Some(json!({"repo": name, "as_of": b.reference, "inactive_days": inactive_days, "people": people,
            "gaps": gaps.into_iter().take(40).map(|g| g.1).collect::<Vec<_>>(),
            "departed": dep.into_iter().take(20).map(|x| x.0).collect::<Vec<_>>()}))
    }
}

impl BlameAreas {
    pub(crate) fn departed_pub(&self, inactive_days: i64) -> (HashMap<String, i64>, HashSet<String>) {
        departed(self, inactive_days)
    }

    pub(crate) fn reference(&self) -> i64 {
        self.reference
    }

    pub(crate) fn area_lines(&self) -> &[(String, Tally)] {
        &self.area_lines
    }

    pub(crate) fn deep_lines(&self) -> &[(String, Tally)] {
        &self.deep_lines
    }

    pub(crate) fn area_last(&self, a: &str) -> i64 {
        self.area_last.get(a).copied().unwrap_or(0)
    }

    pub(crate) fn deep_last(&self, a: &str) -> i64 {
        self.deep_last.get(a).copied().unwrap_or(0)
    }
}

impl Index {
    pub(crate) fn blame_areas_pub(&self, root: &str) -> Arc<Cached> {
        self.blame_areas(root)
    }

    pub(crate) fn session_links_pub(&self, root: &str) -> HashMap<String, Vec<(String, bool)>> {
        self.session_links(root)
    }

    pub(crate) fn repo_names_pub(&self) -> HashMap<String, String> {
        self.repo_names()
    }

    pub(crate) fn sel_pub<T>(&self, name: &str, cols: &str, f: impl Fn(String, &rusqlite::Row) -> rusqlite::Result<T>) -> Vec<T> {
        self.sel(name, cols, "", f)
    }
}

/// The deepest area that holds folder `d`.
pub fn area_for(d: &str, areas: &HashSet<String>) -> String {
    let mut d = d.to_string();
    while !areas.contains(&d) && d.contains('/') {
        d = d.rsplit_once('/').map(|x| x.0.to_string()).unwrap_or_default();
    }
    if areas.contains(&d) { d } else { ".".into() }
}

/// Each author's last activity, and the authors with none in `inactive_days` before the repo's newest commit.
fn departed(b: &BlameAreas, inactive_days: i64) -> (HashMap<String, i64>, HashSet<String>) {
    let cutoff = b.reference - inactive_days * 86400;
    let mut last = HashMap::new();
    for (nm, _) in b.lines_by.iter() {
        let by_mail = b.emails_by_name.get(&nm.to_lowercase()).into_iter().flatten()
            .map(|e| b.last_by_email.get(e).copied().unwrap_or(0)).max().unwrap_or(0);
        last.insert(nm.clone(), by_mail.max(b.own_last.get(nm).copied().unwrap_or(0)));
    }
    let gone = last.iter().filter(|(_, t)| **t == 0 || **t < cutoff).map(|(n, _)| n.clone()).collect();
    (last, gone)
}
