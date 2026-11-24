//! Who knows the code about a topic, one chunk with its sessions and commits, the code near a session, folders
//! that overlap in meaning, area health, deployed lines, the code side of the Data map, and a repo's overview.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, LazyLock};

use regex::Regex;
use serde_json::{Value, json};
use store::rusqlite::{self, OptionalExtension};

use super::git::{self, GENERATED_DIRS, git, is_bot};
use super::query::{Cached, Tally, authors};
use super::{ALL, lock};
use crate::db::marks;
use crate::{Index, vectors};

fn round(x: f64, d: i32) -> f64 {
    let p = 10f64.powi(d);
    (x * p).round() / p
}

fn name_of(p: &str) -> String {
    git::file_name(p).to_string()
}

/// `1234567` as `1,234,567`.
fn thousands(n: usize) -> String {
    let s = n.to_string();
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    out
}

/// Package names from a repo's manifests (pyproject.toml, package.json, go.mod, Cargo.toml, Gemfile).
pub fn dependencies(manifests: &HashMap<String, String>) -> Vec<String> {
    static PY: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?ms)^dependencies\s*=\s*\[(.*?)^\]").expect("regex"));
    static PY_NAME: LazyLock<Regex> = LazyLock::new(|| Regex::new(r#"(?m)^\s*['"]([A-Za-z0-9][A-Za-z0-9_.\-]*)"#).expect("regex"));
    static JS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r#"(?s)"dependencies"\s*:\s*\{(.*?)\}"#).expect("regex"));
    static JS_NAME: LazyLock<Regex> = LazyLock::new(|| Regex::new(r#""([^"]+)"\s*:"#).expect("regex"));
    static GO: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?m)^\s*(?:require\s+)?([\w.\-]+/[\w.\-/]+)\s+v[\d.]").expect("regex"));
    static RS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?ms)^\[dependencies\](.*?)(?:^\[|\z)").expect("regex"));
    static RS_NAME: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?m)^([\w\-]+)\s*=").expect("regex"));
    static GEM: LazyLock<Regex> = LazyLock::new(|| Regex::new(r#"(?m)^\s*gem\s+['"]([^'"]+)"#).expect("regex"));
    let get = |k: &str| manifests.get(k).map(String::as_str).unwrap_or("");
    let mut out: Vec<String> = Vec::new();
    if let Some(m) = PY.captures(get("pyproject.toml")) {
        out.extend(PY_NAME.captures_iter(&m[1]).map(|c| c[1].to_string()));
    }
    if let Some(m) = JS.captures(get("package.json")) {
        out.extend(JS_NAME.captures_iter(&m[1]).map(|c| c[1].to_string()));
    }
    out.extend(GO.captures_iter(get("go.mod")).map(|c| c[1].to_string()));
    if let Some(m) = RS.captures(get("Cargo.toml")) {
        out.extend(RS_NAME.captures_iter(&m[1]).map(|c| c[1].to_string()));
    }
    out.extend(GEM.captures_iter(get("Gemfile")).map(|c| c[1].to_string()));
    let mut seen = HashSet::new();
    out.retain(|x| !x.is_empty() && seen.insert(x.clone()));
    out
}

impl Index {
    /// For Insights' manager view: each sizeable area of a repo with its age, the share of its lines by authors who
    /// stopped committing, and the active people who know it best. Only stale or mostly-departed areas are listed.
    pub fn area_health(&self, name: &str, stale_days: i64, inactive_days: i64, min_lines: f64) -> Option<Value> {
        let root = self.repo_root(name).filter(|r| r != ALL)?;
        let cached = self.blame_areas_pub(&root);
        let Cached::Blame(b) = &*cached else { return None };
        let (last, gone) = b.departed_pub(inactive_days);
        let reference = b.reference();
        let entry = |area: &str, c: &Tally, newest: i64| -> Option<Value> {
            let total = c.total();
            if total < min_lines || area == "." || GENERATED_DIRS.contains(&area.split('/').next().unwrap_or("")) {
                return None;
            }
            let gone_lines: f64 = c.iter().filter(|(nm, _)| gone.contains(*nm)).map(|x| x.1).sum();
            let all = c.most_common(usize::MAX);
            let active: Vec<&(String, f64)> = all.iter().filter(|(nm, _)| !gone.contains(nm)).collect();
            let age = (newest != 0).then(|| ((reference - newest) / 86400).max(0));
            let la = |nm: &str| last.get(nm).copied().filter(|t| *t != 0);
            Some(json!({"area": area, "lines": total as i64, "age_days": age, "stale": age.is_some_and(|a| a >= stale_days),
                "departed_share": round(gone_lines / total, 3),
                "bus_factor": active.iter().filter(|(_, n)| n / total >= 0.10).count(),
                "departed": all.iter().filter(|(nm, _)| gone.contains(nm)).take(3)
                    .map(|(nm, n)| json!({"name": nm, "share": round(n / total, 3), "last_active": la(nm)})).collect::<Vec<_>>(),
                "knows": active.iter().take(3).map(|(nm, n)| json!({"name": nm, "share": round(n / total, 3), "last_active": la(nm)})).collect::<Vec<_>>()}))
        };
        let mut out = Vec::new();
        let mut listed = HashSet::new();
        for (area, c) in b.area_lines() {
            if let Some(e) = entry(area, c, b.area_last(area)) {
                let stale = e["stale"].as_bool().unwrap_or(false);
                if stale || e["departed_share"].as_f64().unwrap_or(0.0) >= 0.5 || e["bus_factor"].as_u64() == Some(0) {
                    if stale {
                        listed.insert(area.clone());
                    }
                    out.push(e);
                }
            }
        }
        let areas: HashSet<&String> = b.area_lines().iter().map(|x| &x.0).collect();
        for (deep, c) in b.deep_lines() {
            let top2 = deep.split('/').take(2).collect::<Vec<_>>().join("/");
            if areas.contains(deep) || listed.contains(&top2) {
                continue;
            }
            if let Some(e) = entry(deep, c, b.deep_last(deep)) {
                if e["stale"].as_bool().unwrap_or(false) {
                    out.push(e);
                }
            }
        }
        Some(json!({"repo": name, "as_of": reference, "stale_days": stale_days, "inactive_days": inactive_days, "areas": out}))
    }

    /// (time, author email, lines added) of each commit of the last `days` that reached the default branch. Kept in
    /// index.db's cache until the branch moves, or the next day.
    pub fn deployed(&self, name: &str, days: i64) -> Vec<(i64, String, i64)> {
        let Some(root) = self.repo_root(name).filter(|r| r != ALL) else { return Vec::new() };
        let Some(r) = git::deploy_ref(&root) else { return Vec::new() };
        let sha = git(&root, &["rev-parse", "--verify", "--quiet", &r]).trim().to_string();
        if sha.is_empty() {
            return Vec::new();
        }
        let key = format!("deployed:{root}:{days}");
        let today = chrono::Local::now().format("%Y-%m-%d").to_string();
        let hit: Option<Vec<u8>> = self.db.read(|c| c.query_row("SELECT value FROM cache WHERE key=?", [&key], |r| r.get(0)).optional())
            .ok().flatten();
        if let Some(v) = hit.and_then(|b| serde_json::from_slice::<Value>(&b).ok()) {
            if v["sha"] == sha.as_str() && v["day"] == today.as_str() {
                return v["rows"].as_array().into_iter().flatten().filter_map(|r| {
                    Some((r.get(0)?.as_i64()?, r.get(1)?.as_str()?.to_string(), r.get(2)?.as_i64()?))
                }).collect();
            }
        }
        let rows = git::deployed_commits(&root, &r, days);
        let v = json!({"sha": sha, "day": today, "rows": rows});
        let exp = store::now() as i64 + 7 * 86400;
        let _ = self.db.write().execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
            rusqlite::params![key, v.to_string().into_bytes(), exp]);
        rows
    }

    fn session_titles(&self, ids: &[String]) -> HashMap<String, (String, String)> {
        if ids.is_empty() {
            return HashMap::new();
        }
        self.db.read(|c| {
            let mut st = c.prepare(&format!("SELECT id, coalesce(title, ''), coalesce(source, '') FROM sessions WHERE id IN ({})", marks(ids.len())))?;
            st.query_map(rusqlite::params_from_iter(ids), |r| Ok((r.get(0)?, (r.get(1)?, r.get(2)?))))?.collect()
        }).unwrap_or_default()
    }

    /// GET /api/code/{cid}: one chunk with its blame, the sessions that touched its file and the file's last
    /// commits. None: no such chunk.
    pub fn api_code(&self, cid: i64) -> Option<Value> {
        type R = (String, String, i64, i64, String, String, Option<i64>, String);
        let r: R = self.db.read(|c| c.query_row(
            "SELECT repo, path, start, end, text, coalesce(authors, ''), last_ts, coalesce(wt, '') FROM code_chunks WHERE id=?",
            [cid], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?))).optional())
            .ok().flatten()?;
        let links = self.session_links_pub(&r.0).remove(&r.1).unwrap_or_default();
        let titles = self.session_titles(&links.iter().map(|x| x.0.clone()).collect::<Vec<_>>());
        let sess: Vec<Value> = links.iter().map(|(s, e)| {
            let (t, src) = titles.get(s).cloned().unwrap_or_default();
            json!({"id": s, "edited": e, "title": t, "source": src})
        }).collect();
        let log = git(&r.0, &["log", "--format=%H%x1f%at%x1f%ae%x1f%s", "-n", "8", "--", &r.1]);
        let commits: Vec<Value> = log.trim().split('\n').filter(|l| l.matches('\x1f').count() == 3).map(|l| {
            let c: Vec<&str> = l.split('\x1f').collect();
            json!({"sha": &c[0][..c[0].len().min(8)], "ts": c[1], "author": c[2], "subject": c[3]})
        }).collect();
        let branch: Option<String> = if r.7.is_empty() { None } else {
            self.db.read(|c| c.query_row("SELECT branch FROM worktrees WHERE path=?", [&r.7], |x| x.get(0)).optional()).ok().flatten().flatten()
        };
        let au: serde_json::Map<String, Value> = authors(&r.5).into_iter().map(|(k, v)| (k, json!(v))).collect();
        Some(json!({"id": cid, "repo": name_of(&r.0), "path": r.1, "start": r.2, "end": r.3, "text": r.4,
            "authors": au, "last_ts": r.6, "sessions": sess, "commits": commits,
            "worktree": (!r.7.is_empty()).then(|| r.7.clone()), "branch": branch,
            "checkout": if r.7.is_empty() { r.0.clone() } else { r.7.clone() }}))
    }

    /// People ranked by the blamed lines of these chunks, each weighted by its similarity (bots left out): who to ask.
    pub fn experts_from(&self, hits: &[(i64, f32)]) -> Vec<Value> {
        let ids: Vec<i64> = hits.iter().map(|h| h.0).collect();
        let mut rows: HashMap<i64, (String, String, i64)> = HashMap::new();
        for part in ids.chunks(900) {
            let got: Vec<(i64, (String, String, i64))> = self.db.read(|c| {
                let mut st = c.prepare(&format!("SELECT id, path, coalesce(authors, ''), coalesce(last_ts, 0) FROM code_chunks WHERE id IN ({})", marks(part.len())))?;
                st.query_map(rusqlite::params_from_iter(part), |r| Ok((r.get(0)?, (r.get(1)?, r.get(2)?, r.get(3)?))))?.collect()
            }).unwrap_or_default();
            rows.extend(got);
        }
        let mut score = Tally::default();
        let mut files: HashMap<String, Tally> = HashMap::new();
        let mut last: HashMap<String, i64> = HashMap::new();
        for (cid, sim) in hits {
            let Some((path, au, ts)) = rows.get(cid) else { continue };
            for (a, n) in authors(au) {
                if is_bot(&a) {
                    continue;
                }
                score.add(&a, (*sim as f64).max(0.0) * n as f64);
                files.entry(a.clone()).or_default().add(path, n as f64);
                let l = last.entry(a).or_default();
                *l = (*l).max(*ts);
            }
        }
        let tot = score.total();
        let tot = if tot == 0.0 { 1.0 } else { tot };
        score.most_common(8).into_iter().map(|(a, s)| json!({"author": a, "share": round(s / tot, 3), "last_active": last.get(&a),
            "files": files.get(&a).map(|f| f.most_common(4).into_iter().map(|x| x.0).collect::<Vec<_>>()).unwrap_or_default()})).collect()
    }

    /// experts() for the files and folders that a topic names by path, or None when it names none.
    fn experts_by_path(&self, q: &str, repo: Option<&str>, allow: Option<&HashSet<i64>>) -> Option<Value> {
        static PATHS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[\w./-]*/[\w./-]+|[\w-]+\.[a-z]{1,5}\b").expect("regex"));
        let paths: Vec<String> = PATHS.find_iter(q)
            .map(|m| m.as_str().trim_matches(|c| ".\u{2f}`'\"".contains(c)).trim_end_matches('/').to_string())
            .filter(|p| p.chars().count() > 3).collect();
        if paths.is_empty() {
            return None;
        }
        let root = repo.filter(|r| *r != ALL).and_then(|r| self.repo_root(r));
        let mut rows: Vec<(i64, String, String, i64, i64)> = Vec::new();
        for p in paths.iter().take(4) {
            let like = if p.contains('/') { p.clone() } else { format!("%/{p}") };
            let sql = format!("SELECT id, repo, path, start, end FROM code_chunks WHERE (path = ? OR path LIKE ? OR path LIKE ?){} LIMIT 3000",
                if root.is_some() { " AND repo = ?" } else { "" });
            let mut args = vec![p.clone(), like, format!("{p}/%")];
            args.extend(root.clone());
            let got: Vec<(i64, String, String, i64, i64)> = self.db.read(|c| {
                c.prepare(&sql)?.query_map(rusqlite::params_from_iter(&args), |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)))?.collect()
            }).unwrap_or_default();
            rows.extend(got);
        }
        rows.retain(|r| allow.is_none_or(|a| a.contains(&r.0)));
        if rows.is_empty() {
            return None;
        }
        let mut seen = HashSet::new();
        let mut chunks = Vec::new();
        for (cid, rp, path, start, end) in &rows {
            if !seen.contains(path) && chunks.len() < 10 {
                seen.insert(path.clone());
                chunks.push(json!({"id": cid, "repo": name_of(rp), "path": path, "start": start, "end": end, "score": 1.0}));
            }
        }
        let hits: Vec<(i64, f32)> = rows.iter().map(|r| (r.0, 1.0)).collect();
        Some(json!({"chunks": chunks, "experts": self.experts_from(&hits), "hit_ids": rows.iter().map(|r| r.0).collect::<Vec<_>>()}))
    }

    /// Who wrote the code most related to a topic (or to `vec`): {chunks (the 10 best), experts, hit_ids}. `allow`:
    /// a scope's chunk ids; `repo`: one repo. A topic that names a file or folder counts every chunk under it.
    pub fn experts(&self, q: Option<&str>, vec: Option<&[f32]>, repo: Option<&str>, k: usize, allow: Option<&[i64]>) -> Value {
        let allow_set: Option<HashSet<i64>> = allow.map(|a| a.iter().copied().collect());
        if let Some(named) = q.and_then(|q| self.experts_by_path(q, repo, allow_set.as_ref())) {
            return named;
        }
        let empty = || json!({"chunks": [], "experts": [], "hit_ids": []});
        let qv = match vec {
            Some(v) => v.to_vec(),
            None => self.embed_query(q.unwrap_or("")),
        };
        if allow.is_some_and(|a| a.is_empty()) {
            return empty();
        }
        let mut allow: Option<Vec<i64>> = allow.map(|a| a.to_vec());
        if let Some(r) = repo.filter(|r| *r != ALL) {
            let root = self.repo_root(r).unwrap_or_default();
            let ids: Vec<i64> = self.db.read(|c| c.prepare("SELECT id FROM code_chunks WHERE repo=?")?.query_map([&root], |r| r.get(0))?.collect())
                .unwrap_or_default();
            let ids: Vec<i64> = ids.into_iter().filter(|i| self.code_vecs.contains(*i)).collect();
            if ids.is_empty() {
                return empty();
            }
            allow = Some(ids);
        }
        let hits = self.code_vecs.search(&qv, k, allow.as_deref());
        let mut chunks = Vec::new();
        for (cid, s) in hits.iter().take(10) {
            let r: Option<(String, String, i64, i64)> = self.db.read(|c| c.query_row(
                "SELECT repo, path, start, end FROM code_chunks WHERE id=?", [cid],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))).optional()).ok().flatten();
            if let Some(r) = r {
                chunks.push(json!({"id": cid, "repo": name_of(&r.0), "path": r.1, "start": r.2, "end": r.3, "score": s}));
            }
        }
        json!({"chunks": chunks, "experts": self.experts_from(&hits), "hit_ids": hits.iter().map(|h| h.0).collect::<Vec<_>>()})
    }

    /// GET /api/experts?q=&repo=.
    pub fn api_experts(&self, q: &str, repo: Option<&str>) -> Value {
        self.experts(Some(q), None, repo, 40, None)
    }

    /// MCP who_knows (and people(topic=)): {people, code}. `allow`: the scope's code chunk ids. The app marks the
    /// user's own entries (`you`) and checks the repo against the scope first.
    pub fn mcp_who_knows(&self, topic: &str, repo: Option<&str>, allow: Option<&[i64]>) -> Result<Value, String> {
        if let Some(r) = repo {
            self.repo_root(r).ok_or_else(|| format!("unknown repo '{r}'"))?;
        }
        let r = self.experts(Some(topic), None, repo, 40, allow);
        let code: Vec<Value> = r["chunks"].as_array().into_iter().flatten().map(|c| json!({
            "id": format!("code:{}", c["id"]), "repo": c["repo"], "path": c["path"], "line": c["start"],
            "score": round(c["score"].as_f64().unwrap_or(0.0), 3)})).collect();
        Ok(json!({"people": r["experts"], "code": code}))
    }

    /// GET /api/sessioncode/{sid}: the files a session touched, the user's commits in its time window, and the code
    /// nearest to what it was about, with who wrote that code. None: no such session.
    pub fn api_sessioncode(&self, sid: &str) -> Option<Value> {
        let s: (Option<String>, Option<String>, Option<String>) = self.db.read(|c| c.query_row(
            "SELECT project, started, updated FROM sessions WHERE id=?", [sid], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).optional())
            .ok().flatten()?;
        let canon = self.canon_map();
        let root = git::project_root(s.0.as_deref().unwrap_or(""), &self.config().home).and_then(|r| canon.get(&r).cloned());
        let files: Vec<Value> = self.db.read(|c| {
            c.prepare("SELECT path, coalesce(edited, 0) FROM session_files WHERE session_id=? AND path!=''")?
                .query_map([sid], |r| Ok(json!({"path": r.get::<_, String>(0)?, "edited": r.get::<_, i64>(1)? != 0})))?.collect()
        }).unwrap_or_default();
        let mut commits = Vec::new();
        if let (Some(root), Some(a), Some(b)) = (&root, s.1.as_deref(), s.2.as_deref()) {
            if let (Some(lo), Some(hi)) = (crate::tokens::iso_seconds(a), crate::tokens::iso_seconds(b)) {
                let me = git(root, &["config", "user.email"]).trim().to_lowercase();
                commits = self.db.read(|c| {
                    c.prepare("SELECT sha, ts, author, subject FROM commits WHERE repo=? AND ts BETWEEN ? AND ? AND lower(author)=? ORDER BY ts")?
                        .query_map(rusqlite::params![root, lo - 300.0, hi + 1800.0, me], |r| {
                            let sha: String = r.get(0)?;
                            Ok(json!({"sha": &sha[..sha.len().min(8)], "ts": r.get::<_, i64>(1)?, "author": r.get::<_, String>(2)?, "subject": r.get::<_, String>(3)?}))
                        })?.collect()
                }).unwrap_or_default();
            }
        }
        let ids: Vec<i64> = self.db.read(|c| c.prepare("SELECT id FROM chunks WHERE session_id=?")?.query_map([sid], |r| r.get(0))?.collect())
            .unwrap_or_default();
        let vs: Vec<Vec<f32>> = self.session_vecs.take(&ids).into_iter().flatten().collect();
        let has_code = self.db.read(|c| c.query_row("SELECT 1 FROM code_chunks LIMIT 1", [], |_| Ok(())).optional()).ok().flatten().is_some();
        let mut near = json!({"chunks": [], "experts": []});
        if !vs.is_empty() && has_code {
            let dim = vs[0].len();
            let mean: Vec<f32> = (0..dim).map(|d| vs.iter().map(|v| v[d]).sum::<f32>() / vs.len() as f32).collect();
            let c = vectors::unit(&mean);
            let rn = root.as_deref().map(name_of);
            near = self.experts(None, Some(&c), rn.as_deref(), 40, None);
        }
        Some(json!({"repo": root.as_deref().map(name_of), "files": files, "commits": commits,
            "nearest": near["chunks"], "experts": near["experts"]}))
    }

    /// GET /api/repo/{name}/overlaps: folder pairs (2 levels deep; across repos for `__all__`) whose meaning is
    /// closest.
    pub fn api_repo_overlaps(&self, name: &str, top: usize) -> Value {
        let cross = name == ALL;
        let rows: Vec<(String, i64, String)> = self.sel_pub(name, "path, id, authors", |p, r| {
            Ok((p, r.get::<_, i64>(2)?, r.get::<_, Option<String>>(3)?.unwrap_or_default()))
        });
        let depth = if cross { 3 } else { 2 };
        let mut groups: Vec<(String, Vec<i64>)> = Vec::new();
        let mut pos: HashMap<String, usize> = HashMap::new();
        let mut owners: HashMap<String, Tally> = HashMap::new();
        for (path, cid, au) in rows {
            let parts: Vec<&str> = path.split('/').collect();
            let key = if parts.len() > depth { parts[..depth].join("/") } else {
                let k = parts[..parts.len() - 1].join("/");
                if k.is_empty() { ".".into() } else { k }
            };
            let i = *pos.entry(key.clone()).or_insert_with(|| { groups.push((key.clone(), Vec::new())); groups.len() - 1 });
            groups[i].1.push(cid);
            for (a, n) in authors(&au) {
                if !is_bot(&a) {
                    owners.entry(key.clone()).or_default().add(&a, n as f64);
                }
            }
        }
        groups.retain(|(k, v)| v.len() >= 3 && (!cross || k.contains('/')));
        let (keys, c) = vectors::centroids(&groups, &self.code_vecs);
        if keys.len() < 2 {
            return json!([]);
        }
        let mut pairs: Vec<(f32, &String, &String)> = Vec::new();
        for i in 0..keys.len() {
            for j in i + 1..keys.len() {
                let (a, b) = (&keys[i], &keys[j]);
                if a.starts_with(&format!("{b}/")) || b.starts_with(&format!("{a}/")) {
                    continue;
                }
                if cross && a.split('/').next() == b.split('/').next() {
                    continue;
                }
                pairs.push((vectors::dot(&c[i], &c[j]), a, b));
            }
        }
        pairs.sort_by(|x, y| y.0.total_cmp(&x.0).then(y.1.cmp(x.1)).then(y.2.cmp(x.2)));
        let top_owner = |k: &str| owners.get(k).map(|t| t.most_common(2).into_iter().map(|x| x.0).collect::<Vec<_>>()).unwrap_or_default();
        Value::Array(pairs.into_iter().take(top).map(|(s, a, b)| json!({"sim": round(s as f64, 3), "a": a, "b": b,
            "owners_a": top_owner(a), "owners_b": top_owner(b)})).collect())
    }

    /// path -> time of the last commit that touched it, from one `git log`, kept until HEAD moves (then only the
    /// new commits are read).
    fn commit_times(&self, root: &str) -> Arc<HashMap<String, f64>> {
        let head = git(root, &["rev-parse", "HEAD"]).trim().to_string();
        if head.is_empty() {
            return Arc::new(HashMap::new());
        }
        let old = lock(&self.repos.ctimes).get(root).cloned();
        if let Some((h, v)) = &old {
            if *h == head {
                return v.clone();
            }
        }
        let newer = old.as_ref().is_some_and(|(h, _)| std::process::Command::new("git")
            .args(["-C", root, "merge-base", "--is-ancestor", h, &head]).output().map(|o| o.status.success()).unwrap_or(false));
        let range = old.as_ref().map(|(h, _)| format!("{h}..{head}")).unwrap_or_default();
        let mut args = vec!["log", "--format=%x1e%ct", "--name-only", "--no-renames"];
        if newer {
            args.push(&range);
        }
        let log = git(root, &args);
        let mut out: HashMap<String, f64> = HashMap::new();
        for rec in log.split('\x1e') {
            let lines: Vec<&str> = rec.trim().split('\n').collect();
            let Some(t) = lines.first().and_then(|l| l.parse::<i64>().ok()) else { continue };
            for f in &lines[1..] {
                if !f.is_empty() && !out.contains_key(*f) {
                    out.insert(f.to_string(), t as f64);
                }
            }
        }
        if newer {
            if let Some((_, o)) = &old {
                let mut all = (**o).clone();
                all.extend(out);
                out = all;
            }
        }
        let out = Arc::new(out);
        lock(&self.repos.ctimes).insert(root.to_string(), (head, out.clone()));
        out
    }

    /// The code points of the Data map (GET /api/data/points): one per main-checkout file that the map placed
    /// (`data_points` holds `code:<repo>:<path>`), in 0.21's shape. The app adds documents and sessions.
    pub fn data_points_code(&self) -> Vec<Value> {
        let home = self.config().home.to_string_lossy().into_owned();
        let (pos, rows) = self.db.read(|c| {
            let pos: HashMap<String, (Option<f64>, Option<f64>, Option<f64>, Option<i64>)> = c.prepare("SELECT id, x, y, z, cluster FROM data_points WHERE id LIKE 'code:%'")?
                .query_map([], |r| Ok((r.get(0)?, (r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))))?.collect::<Result<_, _>>()?;
            let rows: Vec<(String, String, i64, Option<i64>, Option<String>)> = c.prepare(
                "SELECT repo, path, id, last_ts, authors FROM code_chunks WHERE wt='' ORDER BY repo, path, id")?
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)))?.collect::<Result<_, _>>()?;
            Ok((pos, rows))
        }).unwrap_or_default();
        let names = self.repo_names_pub();
        struct F { first: i64, ts: Option<i64>, n: usize, blame: Tally }
        let mut files: Vec<((String, String), F)> = Vec::new();
        for (repo, path, id, ts, au) in rows {
            let key = (repo, path);
            if files.last().is_none_or(|l| l.0 != key) {
                files.push((key.clone(), F { first: id, ts: None, n: 0, blame: Tally::default() }));
            }
            let Some(f) = files.last_mut().map(|l| &mut l.1) else { continue };
            f.first = f.first.min(id);
            f.ts = match (f.ts, ts) { (Some(a), Some(b)) => Some(a.max(b)), (a, b) => a.or(b) };
            f.n += 1;
            for (who, lines) in authors(au.as_deref().unwrap_or("")) {
                f.blame.add(who.split(" <").next().unwrap_or(&who), lines as f64);
            }
        }
        let mut ctimes: HashMap<String, Arc<HashMap<String, f64>>> = HashMap::new();
        let mut out = Vec::new();
        for ((repo, path), f) in files {
            let k = format!("code:{repo}:{path}");
            let Some(p) = pos.get(&k) else { continue };
            let ct = ctimes.entry(repo.clone()).or_insert_with(|| self.commit_times(&repo)).clone();
            let when: Option<f64> = ct.get(&path).copied().or_else(|| {
                std::fs::metadata(format!("{repo}/{path}")).ok().and_then(|m| m.modified().ok())
                    .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_secs_f64())
                    .or(f.ts.map(|t| t as f64))
            });
            let full = format!("{repo}/{path}");
            let display = crate::text::tilde(&full, &home);
            out.push(json!({"id": k, "type": "code", "open": format!("code:{}", f.first), "title": name_of(&path), "path": full,
                "display": display, "repo": names.get(&repo).cloned().unwrap_or_else(|| name_of(&repo)), "repo_root": repo,
                "ext": git::suffix(&path).trim_start_matches('.').to_string(), "mtime": when, "committed": ct.contains_key(&path),
                "chunks": f.n, "author": f.blame.most_common(1).into_iter().next().map(|x| x.0), "author_source": "git",
                "p": [p.0, p.1, p.2], "cluster": p.3}));
        }
        out
    }

    /// The points of a repo that its overview counts: its code files on the Data map and the sessions that ran in it.
    pub fn overview_points(&self, root: &str) -> Vec<Value> {
        let mut pts: Vec<Value> = self.data_points_code().into_iter().filter(|p| p["repo_root"] == root).collect();
        let placed: HashSet<String> = self.db.read(|c| {
            c.prepare("SELECT id FROM data_points WHERE id LIKE 'session:%'")?.query_map([], |r| r.get(0))?.collect()
        }).unwrap_or_default();
        let scope = self.session_scope();
        let rows: Vec<(String, Option<String>)> = self.db.read(|c| {
            c.prepare("SELECT id, title FROM sessions WHERE n_chunks>0")?.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect()
        }).unwrap_or_default();
        for (sid, title) in rows {
            let k = format!("session:{sid}");
            if placed.contains(&k) && scope.get(&sid).and_then(|s| s.root.as_deref()) == Some(root) {
                pts.push(json!({"id": k, "type": "session", "title": title.unwrap_or_else(|| sid.clone()), "repo_root": root}));
            }
        }
        pts
    }

    /// How many topics the overview asks the Data map for: max(4, min(10, round(sqrt(points) / 2))).
    pub fn overview_topics_k(&self, root: &str) -> usize {
        let n = self.overview_points(root).len() as f64;
        let x = n.sqrt() / 2.0;
        let r = if (x - x.floor() - 0.5).abs() < 1e-12 { 2.0 * (x / 2.0).round() } else { x.round() };
        (r as usize).clamp(4, 10)
    }

    /// MCP overview / repo(part=overview): where things are in a repo. `topics`: the Data map's topics of the
    /// repo's folder ({topics: [{id, name, description, keywords, reps}], of: {item id: topic id}}), with
    /// `overview_topics_k` topics; without it the topic part is left out.
    pub fn mcp_overview(&self, root: &str, topics: Option<&Value>) -> String {
        let pts = self.overview_points(root);
        let by_id: HashMap<&str, &Value> = pts.iter().map(|p| (p["id"].as_str().unwrap_or(""), p)).collect();
        let code: Vec<&Value> = pts.iter().filter(|p| p["type"] == "code").collect();
        let rel = |p: &Value| -> String {
            let path = p["path"].as_str().unwrap_or("");
            path.strip_prefix(&format!("{root}/")).unwrap_or(path).to_string()
        };
        let mut langs = Tally::default();
        let mut tops = Tally::default();
        let mut authors_t = Tally::default();
        for p in &code {
            let e = p["ext"].as_str().filter(|e| !e.is_empty()).unwrap_or("(none)");
            langs.add(e, 1.0);
            let r = rel(p);
            tops.add(&format!("{}{}", r.split('/').next().unwrap_or(""), if r.contains('/') { "/" } else { "" }), 1.0);
            if let Some(a) = p["author"].as_str().filter(|a| !a.is_empty()) {
                authors_t.add(a, 1.0);
            }
        }
        let mut readmes: Vec<String> = code.iter().filter(|p| p["title"].as_str().unwrap_or("").to_lowercase().starts_with("readme")).map(|p| rel(p)).collect();
        readmes.sort_by(|a, b| (a.matches('/').count(), a).cmp(&(b.matches('/').count(), b)));
        readmes.truncate(5);
        let sessions = pts.iter().filter(|p| p["type"] == "session").count();
        let n = |x: f64| x as i64;
        let tops_list = tops.most_common(8);
        let mut out = vec![
            format!("{}: {} files, {sessions} agent sessions", name_of(root), thousands(code.len())),
            format!("languages: {}", langs.most_common(6).iter().map(|(e, c)| format!("{e} {}", n(*c))).collect::<Vec<_>>().join(", ")),
            format!("top folders: {}", tops_list.iter().map(|(d, c)| format!("{d} {}", n(*c))).collect::<Vec<_>>().join(", ")),
            format!("most lines by: {}", authors_t.most_common(5).iter().map(|(a, c)| format!("{a} ({} files)", n(*c))).collect::<Vec<_>>().join(", ")),
        ];
        if !readmes.is_empty() {
            out.push(format!("READMEs: {}", readmes.join(", ")));
        }
        let mut big: Vec<&&Value> = code.iter().collect();
        big.sort_by_key(|p| std::cmp::Reverse(p["chunks"].as_i64().unwrap_or(0)));
        out.push(format!("largest files: {}", big.iter().take(6).map(|p| format!("{} ({} chunks)", rel(p), p["chunks"])).collect::<Vec<_>>().join(", ")));
        let firsts: Vec<(String, String)> = self.db.read(|c| {
            c.prepare("SELECT path, text FROM code_chunks WHERE repo=? AND wt='' AND path IN (?,?,?,?,?,?) ORDER BY path, start")?
                .query_map([root, "README.md", "pyproject.toml", "package.json", "go.mod", "Cargo.toml", "Gemfile"], |r| Ok((r.get(0)?, r.get(1)?)))?.collect()
        }).unwrap_or_default();
        let mut first: HashMap<String, String> = HashMap::new();
        for (p, t) in firsts {
            let e = first.entry(p).or_default();
            e.push_str(&t);
            e.push('\n');
        }
        if let Some(r) = first.get("README.md").filter(|r| !r.is_empty()) {
            let flat = r.split_whitespace().collect::<Vec<_>>().join(" ");
            out.push(format!("README starts: {}", crate::text::head(&flat, 600)));
        }
        let deps = dependencies(&first);
        if !deps.is_empty() {
            out.push(format!("dependencies: {}", deps.iter().take(25).cloned().collect::<Vec<_>>().join(", ")));
        }
        let holds = |d: &str| -> Tally {
            let mut t = Tally::default();
            for p in &code {
                let r = rel(p);
                if let Some(rest) = r.strip_prefix(d) {
                    if rest.contains('/') {
                        t.add(&format!("{}/", rest.split('/').next().unwrap_or("")), 1.0);
                    }
                }
            }
            t
        };
        for (d, _) in tops_list.iter().take(4) {
            if !d.ends_with('/') {
                continue;
            }
            let subs = holds(d);
            if subs.is_empty() {
                continue;
            }
            out.push(format!("{d} holds: {}", subs.most_common(10).iter().map(|(s, c)| format!("{s} {}", n(*c))).collect::<Vec<_>>().join(", ")));
            for (s, _) in subs.most_common(4) {
                let inner = holds(&format!("{d}{s}"));
                if !inner.is_empty() {
                    out.push(format!("  {d}{s} holds: {}", inner.most_common(10).iter().map(|(x, c)| format!("{x} {}", n(*c))).collect::<Vec<_>>().join(", ")));
                }
            }
        }
        let Some(res) = topics else { return out.join("\n") };
        let of: HashMap<String, i64> = res["of"].as_object().into_iter().flatten().filter_map(|(k, v)| Some((k.clone(), v.as_i64()?))).collect();
        let mut members: HashMap<i64, usize> = HashMap::new();
        for v in of.values() {
            *members.entry(*v).or_default() += 1;
        }
        let list: Vec<&Value> = res["topics"].as_array().map(|a| a.iter().collect()).unwrap_or_default();
        out.push(format!("\n{} topics, biggest first. A topic groups files that are about the same thing; its name comes from keywords. Read its main files to learn what it does.", list.len()));
        let mut sorted = list.clone();
        sorted.sort_by_key(|t| std::cmp::Reverse(members.get(&t["id"].as_i64().unwrap_or(-1)).copied().unwrap_or(0)));
        let small = code.len() <= 150;
        for t in sorted {
            let tid = t["id"].as_i64().unwrap_or(-1);
            let reps: Vec<&str> = t["reps"].as_array().into_iter().flatten().filter_map(Value::as_str).collect();
            let ids: Vec<&str> = of.iter().filter(|(i, c)| **c == tid && by_id.contains_key(i.as_str())).map(|(i, _)| i.as_str()).collect();
            let files: Vec<String> = if small {
                let rank: HashMap<&str, usize> = reps.iter().enumerate().map(|(k, i)| (*i, k)).collect();
                let mut c: Vec<&str> = ids.iter().filter(|i| by_id[*i]["type"] == "code").copied().collect();
                c.sort_by_key(|i| (rank.get(i).copied().unwrap_or(usize::MAX), *i));
                c.into_iter().map(|i| rel(by_id[i])).collect()
            } else {
                reps.iter().filter(|i| by_id.get(*i).is_some_and(|p| p["type"] == "code")).take(8).map(|i| rel(by_id[*i])).collect()
            };
            let sess: Vec<String> = reps.iter().filter(|i| by_id.get(*i).is_some_and(|p| p["type"] == "session")).take(2)
                .map(|i| crate::text::head(by_id[*i]["title"].as_str().unwrap_or(""), 60).to_string()).collect();
            let mut who = Tally::default();
            for i in &ids {
                let p = by_id[*i];
                if let Some(a) = p["author"].as_str().filter(|a| !a.is_empty() && p["type"] == "code") {
                    who.add(a, 1.0);
                }
            }
            let desc = t["description"].as_str().filter(|d| !d.is_empty()).map(|d| format!(": {d}")).unwrap_or_default();
            out.push(format!("- {} ({} items){desc}", t["name"].as_str().unwrap_or(""), ids.len()));
            out.push(format!("  keywords: {}", t["keywords"].as_str().unwrap_or("")));
            if !files.is_empty() {
                out.push(format!("{}{}", if small { "  files, main first: " } else { "  main files: " }, files.join(", ")));
            }
            if !sess.is_empty() {
                out.push(format!("  sessions: {}", sess.join("; ")));
            }
            if !who.is_empty() {
                out.push(format!("  authors: {}", who.most_common(3).iter().map(|(a, c)| format!("{a} ({})", n(*c))).collect::<Vec<_>>().join(", ")));
            }
        }
        out.join("\n")
    }

    /// MCP reindex: sweep folders and repos on the next pass of the app's indexing loops.
    pub fn mcp_reindex(&self) -> Value {
        self.watch.force_full();
        json!({"ok": true})
    }

    /// Sessions indexed before touched files were recorded: read their transcripts once and record the files
    /// (a '' row marks "read, touched nothing"). Runs at the start of each full repo sweep.
    pub fn backfill_session_files(&self) -> usize {
        let todo: Vec<(String, String, String)> = self.db.read(|c| {
            c.prepare("SELECT id, coalesce(source, ''), coalesce(path, '') FROM sessions \
                       WHERE id NOT IN (SELECT DISTINCT session_id FROM session_files)")?
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?.collect()
        }).unwrap_or_default();
        let mut n = 0;
        for (sid, src, path) in todo {
            let Ok(s) = crate::parsers::parse(&src, std::path::Path::new(&path)) else { continue };
            let ok = self.db.tx(|t| {
                t.execute("DELETE FROM session_files WHERE session_id=?", [&sid])?;
                if s.touched.is_empty() {
                    t.execute("INSERT OR REPLACE INTO session_files VALUES(?, '', 0)", [&sid])?;
                }
                for (p, e) in &s.touched {
                    t.execute("INSERT OR REPLACE INTO session_files VALUES(?,?,?)", rusqlite::params![sid, p, *e as i64])?;
                }
                Ok(())
            });
            n += ok.is_ok() as usize;
        }
        n
    }

    /// MCP repo(repo, part, inactive_days): overview (give `topics` as for `mcp_overview`), gaps or unexplored.
    pub fn mcp_repo(&self, repo: &str, part: &str, inactive_days: i64, topics: Option<&Value>) -> Result<Value, String> {
        match part {
            "overview" => {
                let root = self.repo_root(repo).ok_or_else(|| format!("unknown repo '{repo}'"))?;
                Ok(Value::String(self.mcp_overview(&root, topics)))
            }
            _ if repo.is_empty() => Err(format!("name the repo for part '{part}'")),
            "gaps" => self.knowledge_gaps(repo, inactive_days, 150.0).ok_or_else(|| format!("unknown repo '{repo}'")),
            "unexplored" => self.mcp_unexplored(repo),
            _ => Err("part is overview, gaps or unexplored".into()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_dependencies_of_each_manifest() {
        let m: HashMap<String, String> = [
            ("pyproject.toml", "[project]\ndependencies = [\n  \"numpy>=1\",\n  'mlx-lm; sys_platform == \"darwin\"',\n]\n"),
            ("package.json", "{\"dependencies\": {\"react\": \"18\", \"zod\": \"3\"}}"),
            ("Cargo.toml", "[package]\nname = \"x\"\n[dependencies]\nserde = \"1\"\nregex = \"1\"\n"),
        ].into_iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
        assert_eq!(dependencies(&m), vec!["numpy", "mlx-lm", "react", "zod", "serde", "regex"]);
        assert_eq!(thousands(1234567), "1,234,567");
    }
}
