//! What a result shows: a snippet with highlights, the line of a code chunk that matched and the definition it sits
//! in, numbered excerpt lines, and the MCP search tool's one line per hit (with its ordering and notes: worktree
//! copies last, the agent's own live session left out, near-copies flagged).

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::LazyLock;

use serde_json::{Value, json};

use super::code::{SYMBOL, code_names, has_name};
use crate::scopes::Scope;
use crate::text::code_words;
use crate::Index;

/// A session in the agent's folder written to this recently is the agent's own conversation.
pub const LIVE_SECONDS: f64 = 300.0;
/// Matched code this alike in two files: the query cannot tell them apart, so the agent reads both.
pub const NEAR_COPY: f64 = 0.9;

static STOP: LazyLock<HashSet<&'static str>> = LazyLock::new(|| {
    "the and for with from that this into when where what how all any each its are was has have not but you your can \
     will use used uses using code file files function method class which then than them they"
        .split_whitespace().collect()
});

fn stems(text: &str) -> HashSet<String> {
    code_words(text).split_whitespace().filter(|w| w.len() >= 3 && !STOP.contains(w)).map(|w| w.chars().take(6).collect()).collect()
}

/// How well each line matches: shared words (identifiers split), and much more for an exact phrase or a code
/// name from the query used on that line.
fn line_scores(lines: &[&str], query: &str, exact: &[String]) -> Vec<f64> {
    let want = stems(query);
    let phrases: Vec<String> = exact.iter().map(|p| p.to_lowercase()).collect();
    let names: Vec<String> = code_names(query).into_iter().filter(|n| n.chars().count() >= 3).collect();
    lines.iter().map(|ln| {
        if ln.trim().is_empty() {
            return 0.0;
        }
        let low = ln.to_lowercase();
        want.intersection(&stems(ln)).count() as f64
            + 3.0 * phrases.iter().filter(|p| low.contains(p.as_str())).count() as f64
            + 4.0 * names.iter().filter(|n| has_name(ln, n)).count() as f64
    }).collect()
}

fn first_text(lines: &[&str]) -> usize {
    lines.iter().position(|l| !l.trim().is_empty()).unwrap_or(0)
}

/// The line of a code chunk that best matches the query, and the definition it sits in: (line offset in the
/// chunk, definition line or None, best line).
pub fn focus(text: &str, query: &str, exact: &[String]) -> (usize, Option<String>, String) {
    let lines: Vec<&str> = text.split('\n').collect();
    let sc = line_scores(&lines, query, exact);
    let mut best = 0;
    for i in 0..lines.len() {
        if sc[i] > sc[best] {
            best = i;
        }
    }
    if sc.get(best).is_none_or(|s| *s == 0.0) {
        best = first_text(&lines);
    }
    let sym = (0..=best).rev().find(|&j| SYMBOL.is_match(lines[j])).map(|j| lines[j])
        .or_else(|| lines.iter().find(|l| SYMBOL.is_match(l)).copied());
    let here = lines.get(best).map(|l| l.trim()).unwrap_or("");
    let def = sym.map(|s| s.trim()).filter(|s| *s != here).map(String::from);
    (best, def, here.to_string())
}

/// A few numbered lines that show why a chunk matched: its definition line and its two best-matching lines.
pub fn excerpt(text: &str, start: i64, query: &str, exact: &[String]) -> Vec<(i64, String)> {
    let lines: Vec<&str> = text.split('\n').collect();
    let sc = line_scores(&lines, query, exact);
    let mut top: Vec<usize> = (0..lines.len()).filter(|&i| sc[i] > 0.0).collect();
    top.sort_by(|&a, &b| sc[b].total_cmp(&sc[a]).then(a.cmp(&b)));
    top.truncate(2);
    if top.is_empty() {
        top.push(first_text(&lines));
    }
    let lo = top.iter().copied().min().unwrap_or(0);
    let sym = (0..=lo.min(lines.len().saturating_sub(1))).rev().find(|&j| SYMBOL.is_match(lines[j]));
    let mut keep: Vec<usize> = top.into_iter().chain(sym).collect();
    keep.sort_unstable();
    keep.dedup();
    keep.into_iter().map(|j| (start + j as i64, lines.get(j).map(|l| l.chars().take(120).collect()).unwrap_or_default())).collect()
}

fn lower_char(c: char) -> char {
    c.to_lowercase().next().unwrap_or(c)
}

fn find_from(hay: &[char], needle: &[char], from: usize) -> Option<usize> {
    if needle.is_empty() {
        return Some(from.min(hay.len()));
    }
    (from..=hay.len().checked_sub(needle.len())?).find(|&i| hay[i..i + needle.len()] == *needle)
}

/// A window of the text around the first term, with the spans of every term in it (character offsets).
pub fn snippet(text: &str, terms: &[String], width: usize) -> (String, Vec<[usize; 2]>) {
    let flat: Vec<char> = text.split_whitespace().collect::<Vec<_>>().join(" ").chars().collect();
    let low: Vec<char> = flat.iter().map(|c| lower_char(*c)).collect();
    let tl: Vec<Vec<char>> = terms.iter().map(|t| t.chars().map(lower_char).collect()).collect();
    let pos = tl.iter().filter_map(|t| find_from(&low, t, 0)).min().unwrap_or(0);
    let mut start = pos.saturating_sub(60);
    if start > 0 {
        if let Some(sp) = find_from(&flat, &[' '], start) {
            if sp < pos {
                start = sp + 1;
            }
        }
    }
    let end = (start + width).min(flat.len());
    let mut snip: String = flat[start.min(flat.len())..end].iter().collect();
    if start > 0 {
        snip = format!("…{snip}");
    }
    if start + width < flat.len() {
        snip.push('…');
    }
    let sl: Vec<char> = snip.chars().map(lower_char).collect();
    let mut hl = Vec::new();
    for t in &tl {
        if t.is_empty() {
            continue;
        }
        let mut i = find_from(&sl, t, 0);
        while let Some(at) = i {
            hl.push([at, at + t.len()]);
            i = find_from(&sl, t, at + t.len());
        }
    }
    hl.sort();
    (snip, hl)
}

/// difflib's SequenceMatcher(None, a, b).ratio(), with its automatic junk rule.
pub fn ratio(a: &str, b: &str) -> f64 {
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    if a.is_empty() && b.is_empty() {
        return 1.0;
    }
    let mut b2j: HashMap<char, Vec<usize>> = HashMap::new();
    for (j, c) in b.iter().enumerate() {
        b2j.entry(*c).or_default().push(j);
    }
    if b.len() >= 200 {
        let ntest = b.len() / 100 + 1;
        b2j.retain(|_, v| v.len() <= ntest);
    }
    let longest = |alo: usize, ahi: usize, blo: usize, bhi: usize| -> (usize, usize, usize) {
        let (mut bi, mut bj, mut bs) = (alo, blo, 0usize);
        let mut j2len: HashMap<usize, usize> = HashMap::new();
        for i in alo..ahi {
            let mut next: HashMap<usize, usize> = HashMap::new();
            if let Some(js) = b2j.get(&a[i]) {
                for &j in js {
                    if j < blo {
                        continue;
                    }
                    if j >= bhi {
                        break;
                    }
                    let k = j.checked_sub(1).and_then(|p| j2len.get(&p)).copied().unwrap_or(0) + 1;
                    next.insert(j, k);
                    if k > bs {
                        bi = i + 1 - k;
                        bj = j + 1 - k;
                        bs = k;
                    }
                }
            }
            j2len = next;
        }
        while bi > alo && bj > blo && a[bi - 1] == b[bj - 1] {
            bi -= 1;
            bj -= 1;
            bs += 1;
        }
        while bi + bs < ahi && bj + bs < bhi && a[bi + bs] == b[bj + bs] {
            bs += 1;
        }
        (bi, bj, bs)
    };
    let mut queue = vec![(0, a.len(), 0, b.len())];
    let mut matched = 0;
    while let Some((alo, ahi, blo, bhi)) = queue.pop() {
        let (i, j, k) = longest(alo, ahi, blo, bhi);
        if k > 0 {
            matched += k;
            if alo < i && blo < j {
                queue.push((alo, i, blo, j));
            }
            if i + k < ahi && j + k < bhi {
                queue.push((i + k, ahi, j + k, bhi));
            }
        }
    }
    2.0 * matched as f64 / (a.len() + b.len()) as f64
}

fn strip_ellipsis(s: &str) -> &str {
    s.trim_matches(|c| c == ' ' || c == '…')
}

fn cut(s: &str, n: usize) -> String {
    if s.chars().count() > n { format!("{}…", s.chars().take(n).collect::<String>()) } else { s.to_string() }
}

/// Grep-sized result lines: what an agent re-reads on every later turn, so only what it needs to act on.
pub fn result_lines(results: &[Value], cwd: Option<&str>, home: &str) -> String {
    let cwd = cwd.map(|c| c.trim_end_matches('/')).filter(|c| !c.is_empty());
    let s = |x: &Value, k: &str| x.get(k).and_then(Value::as_str).unwrap_or("").to_string();
    let mut out = Vec::new();
    for x in results {
        let mut path = s(x, "path");
        if let Some(c) = cwd.filter(|c| path.starts_with(&format!("{c}/"))) {
            path = path[c.len() + 1..].to_string();
        } else if path.starts_with(&format!("{home}/")) {
            path = format!("~{}", &path[home.len()..]);
        }
        let flat = s(x, "snippet").split_whitespace().collect::<Vec<_>>().join(" ");
        let mut snip = cut(strip_ellipsis(&flat), 140);
        let id = x.get("id").map(|v| v.as_str().map(String::from).unwrap_or_else(|| v.to_string())).unwrap_or_default();
        let kind = Some(s(x, "kind")).filter(|k| !k.is_empty()).unwrap_or_else(|| id.split(':').next().unwrap_or("").to_string());
        let where_ = if kind == "session" {
            let sub = Some(s(x, "subtitle")).filter(|v| !v.is_empty()).unwrap_or_else(|| s(x, "updated"));
            format!("session \"{}\" ({sub})", s(x, "title"))
        } else if kind == "doc" {
            let url = s(x, "url");
            format!("\"{}\" ({}){}", s(x, "title"), s(x, "subtitle"), if url.is_empty() { String::new() } else { format!(" {url}") })
        } else {
            let line = x.get("focus_line").filter(|v| !v.is_null() && v.as_i64() != Some(0))
                .or_else(|| x.get("line").filter(|v| !v.is_null() && v.as_i64() != Some(0)));
            let mut w = if path.is_empty() {
                s(x, "title")
            } else {
                format!("{path}{}", line.map(|l| format!(":{l}")).unwrap_or_default())
            };
            if x.get("stale").and_then(Value::as_bool) == Some(true) {
                w += " (edited since indexed)";
            }
            if x.get("own_differs").and_then(Value::as_bool) == Some(true) {
                w += " (main checkout; differs in your worktree)";
            }
            let other = s(x, "near_copy");
            if !other.is_empty() {
                let o = match cwd.filter(|c| other.starts_with(&format!("{c}/"))) {
                    Some(c) => other[c.len() + 1..].to_string(),
                    None => other,
                };
                w += &format!(" (near-copy of {o}: read both to tell them apart)");
            }
            let focus = s(x, "focus");
            if !focus.is_empty() {
                let def = s(x, "definition");
                snip = format!("{}{}", if def.is_empty() { String::new() } else { format!("{}  ▸ ", cut(&def, 90)) }, cut(&focus, 140));
            }
            w
        };
        out.push(format!("{where_}  {snip}  [{id}]").replace("    ", "  "));
        if let Some(ex) = x.get("excerpt").and_then(Value::as_array) {
            for e in ex {
                let n = e.get(0).map(|v| v.to_string()).unwrap_or_default();
                let t = e.get(1).and_then(Value::as_str).unwrap_or("");
                out.push(format!("    {n}│ {}", t.trim_end()));
            }
        }
    }
    out.join("\n")
}

impl Index {
    fn worktree_paths(&self) -> Vec<(String, String)> {
        self.db.read(|c| c.prepare("SELECT path, coalesce(repo, '') FROM worktrees")?
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect()).unwrap_or_default()
    }

    /// Sessions still being written in the agent's folder are left out: the calling agent's own conversation
    /// matches its own question best, but holds nothing it does not already know.
    fn without_live_sessions(&self, results: Vec<Value>, cwd: &str) -> Vec<Value> {
        let sids: Vec<String> = results.iter().filter_map(|x| x["id"].as_str()?.strip_prefix("session:").map(String::from)).collect();
        if cwd.is_empty() || sids.is_empty() {
            return results;
        }
        let rows: Vec<(String, String, String)> = self.db.read(|c| {
            c.prepare(&format!("SELECT id, coalesce(project, ''), coalesce(path, '') FROM sessions WHERE id IN ({})",
                               crate::db::marks(sids.len())))?
                .query_map(store::rusqlite::params_from_iter(sids.iter()), |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?.collect()
        }).unwrap_or_default();
        let now = store::now();
        let live: HashSet<String> = rows.into_iter().filter(|(_, project, path)| {
            let p = project.trim_end_matches('/');
            !p.is_empty() && (cwd == p || cwd.starts_with(&format!("{p}/")) || p.starts_with(&format!("{cwd}/")))
                && std::fs::metadata(path).ok().map(|m| crate::files::mtime_of(&m)).is_some_and(|t| now - t < LIVE_SECONDS)
        }).map(|x| x.0).collect();
        results.into_iter().filter(|x| !x["id"].as_str().and_then(|i| i.strip_prefix("session:")).is_some_and(|s| live.contains(s))).collect()
    }

    /// An agent working in a linked worktree sees a main-checkout hit at its own copy of the file, when that copy is
    /// the same (the index holds a worktree's files only where they differ).
    fn to_own_checkout(&self, results: &mut [Value], cwd: &str) {
        if cwd.is_empty() {
            return;
        }
        let Some((wt, root)) = self.worktree_paths().into_iter().find(|(w, _)| cwd == w || cwd.starts_with(&format!("{w}/"))) else { return };
        let changed: HashSet<String> = self.db.read(|c| {
            c.prepare("SELECT path FROM checkout_files WHERE repo=? AND wt=?")?
                .query_map([&root, &wt], |r| r.get(0))?.collect()
        }).unwrap_or_default();
        for x in results.iter_mut() {
            let path = x["path"].as_str().unwrap_or("").to_string();
            if !x["id"].as_str().unwrap_or("").starts_with("code:") {
                continue;
            }
            let Some(rel) = path.strip_prefix(&format!("{root}/")) else { continue };
            if changed.contains(rel) {
                x["own_differs"] = json!(true);
            } else if Path::new(&wt).join(rel).is_file() {
                x["path"] = json!(Path::new(&wt).join(rel).to_string_lossy());
            }
        }
    }

    /// A code hit whose matched code is a near-copy of a higher hit's in another file is noted, so the agent
    /// compares them instead of taking the first.
    fn mark_near_copies(&self, results: &mut [Value]) {
        let code: Vec<usize> = (0..results.len())
            .filter(|&i| results[i]["id"].as_str().unwrap_or("").starts_with("code:") && results[i]["path"].as_str().is_some())
            .collect();
        if code.len() < 2 {
            return;
        }
        let ids: Vec<i64> = code.iter().map(|&i| results[i]["id"].as_str().unwrap_or("")[5..].parse().unwrap_or(-1)).collect();
        let text: HashMap<i64, String> = self.db.read(|c| {
            c.prepare(&format!("SELECT id, coalesce(text, '') FROM code_chunks WHERE id IN ({})", crate::db::marks(ids.len())))?
                .query_map(store::rusqlite::params_from_iter(ids.iter()), |r| Ok((r.get(0)?, r.get(1)?)))?.collect()
        }).unwrap_or_default();
        for j in 0..code.len() {
            let tb = text.get(&ids[j]).cloned().unwrap_or_default();
            for i in 0..j {
                let ta = text.get(&ids[i]).cloned().unwrap_or_default();
                let (pa, pb) = (results[code[i]]["path"].clone(), results[code[j]]["path"].clone());
                if pa != pb && !ta.is_empty() && !tb.is_empty() && ratio(&ta, &tb) >= NEAR_COPY {
                    results[code[j]]["near_copy"] = pa;
                    break;
                }
            }
        }
    }

    /// MCP search: one line per hit, best first. `cwd`: the agent's folder; `scope`: the connection's scope.
    pub fn mcp_search(&self, query: &str, limit: usize, kind: Option<&str>, scope: Option<&Scope>, cwd: Option<&str>)
                      -> Result<String, String> {
        let q = match kind.filter(|k| !k.is_empty()) {
            Some(k) => format!("{query} kind:{k}"),
            None => query.to_string(),
        };
        let r = self.find(&q, limit, None, scope)?;
        let cwd = cwd.unwrap_or("").trim_end_matches('/').to_string();
        let wts: Vec<String> = self.worktree_paths().into_iter().map(|x| x.0).collect();
        let elsewhere = |x: &Value| -> bool {
            if !x["subtitle"].as_str().unwrap_or("").contains(" @ ") {
                return false;
            }
            let path = x["path"].as_str().unwrap_or("");
            let wt = wts.iter().find(|w| path.starts_with(&format!("{w}/")));
            !(!cwd.is_empty() && wt.is_some_and(|w| cwd == *w || cwd.starts_with(&format!("{w}/"))))
        };
        let all: Vec<Value> = r["results"].as_array().cloned().unwrap_or_default();
        let (here, there): (Vec<Value>, Vec<Value>) = all.into_iter().partition(|x| !elsewhere(x));
        let res: Vec<Value> = here.into_iter().chain(there).collect();
        let mut res = self.without_live_sessions(res, &cwd);
        self.mark_near_copies(&mut res);
        self.to_own_checkout(&mut res, &cwd);
        let home = self.config().home.to_string_lossy().into_owned();
        let found = result_lines(&res, Some(&cwd), &home);
        if let Some(s) = scope.filter(|s| s.unindexed) {
            return Ok(format!("{}. `pensieve repo index` indexes it.{}", s.description,
                              if found.is_empty() { String::new() } else { format!("\n{found}") }));
        }
        Ok(if found.is_empty() { "No matches.".into() } else { found })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn focus_finds_the_line_and_its_definition() {
        let text = "import os\n\ndef retry_webhook(event):\n    delay = 2\n    return send(event, backoff=delay)";
        let (off, def, line) = focus(text, "send backoff", &[]);
        assert_eq!(off, 4);
        assert_eq!(def.as_deref(), Some("def retry_webhook(event):"));
        assert_eq!(line, "return send(event, backoff=delay)");
        assert_eq!(excerpt(text, 10, "send backoff", &[]), vec![(12, "def retry_webhook(event):".into()),
                                                                   (14, "    return send(event, backoff=delay)".into())]);
    }

    #[test]
    fn snippets_and_ratios() {
        let (s, h) = snippet("a  b\nrate limit here", &["rate".into()], 220);
        assert_eq!(s, "a b rate limit here");
        assert_eq!(h, vec![[4, 8]]);
        assert!((ratio("abcd", "bcde") - 0.75).abs() < 1e-9);
        assert_eq!(ratio("", ""), 1.0);
    }
}
