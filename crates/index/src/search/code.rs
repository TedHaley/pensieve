//! Code search, tuned on bench/ for agent-style queries: descriptions, names, error strings and mixes of these.
//!
//! Candidates are the best chunks by meaning and by keywords (identifier-aware), and the chunks that contain the
//! whole query or a code name from it. Each file is scored by its best chunk:
//!
//! ```text
//! z(meaning) + 0.2 z(keywords) - 0.5 [test/spec/schema file]
//! + 3 [contains the whole query] + log(1 + times) + [same case]        (not for a bare name)
//! + 2 [contains a code name from the query] / log2(1 + files that do)
//! + 2 [defines that name, and at most 3 files do]
//! ```
//!
//! and put back on the cosine scale (best chunk similarity + sigma * the rest) so it merges with other corpora.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::LazyLock;
use std::time::Instant;

use regex::Regex;
use store::rusqlite::{self, Connection, types::Value as Sql};

use super::words::contains;
use crate::Index;
use crate::db::{WORDS_VERSION, marks};
use crate::text::{code_words, repo_token};
use crate::vectors::from_blob;

/// Keyword ranking skips query words in more than this share of code chunks: BM25 weighs them little, yet SQLite
/// scores every chunk that holds them.
pub const COMMON_WORD: f64 = 0.05;

pub static TESTY: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(^|/)(tests?|__tests__|spec|specs|__mocks__|mocks?|fixtures?|stories|e2e)/|([._-](test|spec|stories|mock)s?\.[a-z]+$)|(^|/)test_[^/]+$|/docs?/schema/")
        .expect("regex")
});
pub static SYMBOL: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?mi)^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function|module|interface|type|struct|fn|func|const|let|CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|FUNCTION))\s+([A-Za-z_][\w.]*)")
        .expect("regex")
});
static NAME: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[A-Za-z_][A-Za-z0-9_]*").expect("regex"));
static NAMEY: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"[a-z][A-Z]|[A-Z]{2,}[a-z]|[a-z]\d|^[A-Z][a-z]+[A-Z]").expect("regex"));

/// Words of a query that look like code names (snake_case, camelCase, CONSTANT_CASE, name2), or the whole query
/// when it is one word.
pub fn code_names(q: &str) -> Vec<String> {
    let ws: Vec<&str> = NAME.find_iter(q).map(|m| m.as_str()).collect();
    if ws.len() == 1 && ws[0] == q.trim() {
        return vec![ws[0].to_string()];
    }
    ws.into_iter().filter(|w| w.trim_matches('_').contains('_') || NAMEY.is_match(w)).map(String::from).collect()
}

fn word_char(c: Option<char>) -> bool {
    c.is_some_and(|c| c.is_alphanumeric() || c == '_')
}

/// Whether `name` is in `text` with no word character right before or after it.
pub fn has_name(text: &str, name: &str) -> bool {
    if name.is_empty() {
        return false;
    }
    let mut from = 0;
    while let Some(i) = text[from..].find(name) {
        let at = from + i;
        let end = at + name.len();
        if !word_char(text[..at].chars().last()) && !word_char(text[end..].chars().next()) {
            return true;
        }
        from = at + text[at..].chars().next().map(char::len_utf8).unwrap_or(1);
    }
    false
}

fn count(hay: &str, needle: &str) -> f64 {
    if needle.is_empty() { (hay.chars().count() + 1) as f64 } else { hay.matches(needle).count() as f64 }
}

fn mean(x: &[f64]) -> f64 {
    if x.is_empty() { 0.0 } else { x.iter().sum::<f64>() / x.len() as f64 }
}

fn std(x: &[f64]) -> f64 {
    let m = mean(x);
    (x.iter().map(|v| (v - m) * (v - m)).sum::<f64>() / x.len().max(1) as f64).sqrt()
}

fn z(x: &[f64]) -> Vec<f64> {
    let (m, s) = (mean(x), std(x));
    x.iter().map(|v| (v - m) / (s + 1e-9)).collect()
}

/// How many code chunks hold each word, kept five minutes.
#[derive(Default)]
pub struct Df {
    pub at: Option<Instant>,
    pub n_chunks: i64,
    pub df: HashMap<String, i64>,
}

struct Row {
    text: String,
    vec: Vec<u8>,
    fkey: String,
    path: String,
    wt: bool,
}

impl Index {
    fn df_of(&self, t: &str) -> i64 {
        self.search.df.lock().unwrap_or_else(|e| e.into_inner()).df.get(t).copied().unwrap_or(0)
    }

    fn df_or(&self, t: &str, default: i64) -> i64 {
        self.search.df.lock().unwrap_or_else(|e| e.into_inner()).df.get(t).copied().unwrap_or(default)
    }

    /// The query words worth keyword-ranking on: the ones in at most COMMON_WORD of the code chunks, or the two
    /// rarest when every word is that common.
    pub fn rare_terms(&self, c: &Connection, terms: &[String]) -> Vec<String> {
        let mut g = self.search.df.lock().unwrap_or_else(|e| e.into_inner());
        if g.at.is_none_or(|t| t.elapsed().as_secs() > 300) {
            g.df.clear();
            g.at = Some(Instant::now());
            g.n_chunks = c.query_row("SELECT COUNT(*) FROM code_chunks", [], |r| r.get::<_, i64>(0)).unwrap_or(0).max(1);
        }
        let Ok(mut st) = c.prepare_cached("SELECT doc FROM fts_code_words_vocab WHERE term=?") else { return terms.to_vec() };
        for t in terms {
            if !g.df.contains_key(t) {
                let n: i64 = st.query_row([t], |r| r.get(0)).unwrap_or(0);
                g.df.insert(t.clone(), n);
            }
        }
        let n = g.n_chunks as f64;
        let keep: Vec<String> = terms.iter().filter(|t| (g.df[*t] as f64) <= COMMON_WORD * n).cloned().collect();
        if !keep.is_empty() {
            return keep;
        }
        let mut by = terms.to_vec();
        by.sort_by_key(|t| g.df[t]);
        by.truncate(2);
        by
    }

    /// The fixed text of a message as it shows at runtime ("order_id[20879] not found in cache"): the query split
    /// at numbers and at words no code holds (the values of a format string), fragments of 8+ characters, and
    /// whether there was anything to split at. Call after `rare_terms` looked up the query's words.
    pub fn fragments(&self, q: &str) -> (Vec<String>, bool) {
        let digits = Regex::new(r"\d+").expect("regex");
        let toks = Regex::new(r"\s+|\S+").expect("regex");
        let mut out: Vec<String> = Vec::new();
        for part in digits.split(q) {
            let mut cur = String::new();
            for tok in toks.find_iter(part).map(|m| m.as_str()) {
                let cw = code_words(tok);
                let terms: Vec<&str> = cw.split_whitespace().collect();
                if !terms.is_empty() && terms.iter().all(|t| self.df_or(t, 1) == 0) {
                    out.push(std::mem::take(&mut cur));
                } else {
                    cur.push_str(tok);
                }
            }
            out.push(cur);
        }
        let holes = out.len() > 1;
        let out: Vec<String> = out.iter().map(|f| f.trim().to_string()).filter(|f| f.chars().count() >= 8).collect();
        let mut more: Vec<(i64, i64, String)> = Vec::new();
        for f in &out {
            let ts: Vec<&str> = f.split_whitespace().collect();
            for (i, tok) in ts.iter().enumerate() {
                let df = code_words(tok).split_whitespace().map(|t| self.df_of(t)).min().unwrap_or(0);
                for p in [ts[..i].join(" "), ts[i + 1..].join(" ")] {
                    more.push((df, -(p.chars().count() as i64), p));
                }
            }
        }
        more.sort();
        let mut by_len = out.clone();
        by_len.sort_by_key(|f| std::cmp::Reverse(f.chars().count()));
        let mut seen = HashSet::new();
        let mut res = Vec::new();
        for f in by_len.into_iter().chain(more.into_iter().map(|m| m.2)) {
            if f.chars().count() >= 8 && f != q && seen.insert(f.clone()) {
                res.push(f);
            }
        }
        (res, holes)
    }

    /// Code chunks to rank, best chunk of each file: {chunk id: score}. `qv`: the query's vector. `allow`: only
    /// these chunk ids. `roots`: only these repos (the word index filters by them).
    pub fn code_search(&self, q: &str, allow: Option<&[i64]>, k: usize, roots: Option<&[String]>, qv: &[f32])
                       -> HashMap<i64, f64> {
        self.db.read(|c| Ok(self.code_in(c, q, allow, k, roots, qv))).unwrap_or_default()
    }

    fn code_in(&self, c: &Connection, q: &str, allow: Option<&[i64]>, k: usize, roots: Option<&[String]>, qv: &[f32])
               -> HashMap<i64, f64> {
        let allowed: Option<HashSet<i64>> = allow.map(|a| a.iter().copied().collect());
        let ok = |id: i64| allowed.as_ref().is_none_or(|a| a.contains(&id));
        let has_any: bool = c.query_row("SELECT 1 FROM code_chunks LIMIT 1", [], |_| Ok(true)).unwrap_or(false);
        if !has_any {
            return HashMap::new();
        }
        let mut cand: HashSet<i64> = HashSet::new();
        let kk = match allow {
            Some(a) => k.min(a.len()),
            None => k,
        };
        cand.extend(self.code_vecs.search(qv, kk, allow).into_iter().map(|x| x.0));
        let mut kw: HashMap<i64, f64> = HashMap::new();
        let words: BTreeSet<String> = code_words(q).split_whitespace().map(String::from).collect();
        let terms = self.rare_terms(c, &words.into_iter().collect::<Vec<_>>());
        let roots = roots.filter(|r| !r.is_empty());
        if !terms.is_empty() {
            let mut fq = terms.iter().map(|t| format!("\"{t}\"")).collect::<Vec<_>>().join(" OR ");
            let scoped = roots.is_some() && crate::db::meta(c, "fts_words").as_deref() == Some(WORDS_VERSION);
            if let (true, Some(r)) = (scoped, roots) {
                fq = format!("words : ({fq}) AND repo : ({})", r.iter().map(|x| repo_token(x)).collect::<Vec<_>>().join(" OR "));
            }
            let lim = if allowed.is_none() || scoped { k } else { 20000 };
            if let Ok(mut st) = c.prepare("SELECT rowid, bm25(fts_code_words, 1.0, 0.0) FROM fts_code_words WHERE fts_code_words MATCH ? ORDER BY 2 LIMIT ?") {
                if let Ok(rows) = st.query_map(rusqlite::params![fq, lim as i64], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, f64>(1)?))) {
                    for (cid, bm) in rows.flatten() {
                        if ok(cid) && kw.len() < k {
                            kw.insert(cid, -bm);
                        }
                    }
                }
            }
        }
        cand.extend(kw.keys().copied());
        let ql = q.trim();
        let ql_n = ql.chars().count();
        let has = |s: &str| -> HashSet<i64> {
            contains(c, "code", &[s.to_string()], if allowed.is_none() { 500 } else { 5000 }, roots, false)
                .unwrap_or_default().into_iter().map(|x| x.0).filter(|id| ok(*id)).collect()
        };
        let phrase = if ql_n >= 8 { has(ql) } else { HashSet::new() };
        let names = code_names(ql);
        let bare = names.len() == 1 && names[0] == ql;
        let mut frags: Vec<(String, HashSet<i64>)> = Vec::new();
        let mut pieces = Vec::new();
        if ql_n >= 8 && phrase.is_empty() && !bare {
            let (p, holes) = self.fragments(ql);
            pieces = p;
            if holes {
                for f in pieces.iter().take(8) {
                    if frags.iter().any(|(g, _)| g.contains(f.as_str())) {
                        continue;
                    }
                    let hits = has(f);
                    if !hits.is_empty() {
                        cand.extend(&hits);
                        frags.push((f.clone(), hits));
                        if frags.len() == 3 {
                            break;
                        }
                    }
                }
            }
        }
        let mut by_name: Vec<(String, HashSet<i64>)> = Vec::new();
        for n in &names {
            if n.chars().count() < 3 {
                continue;
            }
            let h = has(n);
            cand.extend(&h);
            match by_name.iter_mut().find(|x| &x.0 == n) {
                Some(x) => x.1 = h,
                None => by_name.push((n.clone(), h)),
            }
        }
        cand.extend(&phrase);
        if cand.is_empty() {
            return HashMap::new();
        }
        let mut rows: HashMap<i64, Row> = HashMap::new();
        let cl: Vec<i64> = cand.iter().copied().collect();
        for part in cl.chunks(900) {
            let sql = format!("SELECT id, coalesce(text, ''), vec, coalesce(path, ''), coalesce(repo, ''), coalesce(wt, '') \
                               FROM code_chunks WHERE id IN ({})", marks(part.len()));
            let Ok(mut st) = c.prepare(&sql) else { continue };
            let it = st.query_map(rusqlite::params_from_iter(part.iter().map(|x| Sql::Integer(*x))), |r| {
                Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, Option<Vec<u8>>>(2)?.unwrap_or_default(),
                    r.get::<_, String>(3)?, r.get::<_, String>(4)?, r.get::<_, String>(5)?))
            });
            if let Ok(it) = it {
                for (cid, text, vec, path, repo, wt) in it.flatten() {
                    rows.insert(cid, Row { text, vec, fkey: format!("{repo}:{path}"), path, wt: !wt.is_empty() });
                }
            }
        }
        if !pieces.is_empty() && frags.is_empty() {
            let low: Vec<(i64, String)> = rows.iter().map(|(id, r)| (*id, r.text.to_lowercase())).collect();
            for f in &pieces {
                if frags.iter().any(|(g, _)| g.contains(f.as_str())) {
                    continue;
                }
                let fl = f.to_lowercase();
                let hits: HashSet<i64> = low.iter().filter(|(_, t)| t.contains(&fl)).map(|x| x.0).collect();
                if !hits.is_empty() {
                    frags.push((f.clone(), hits));
                    if frags.len() == 3 {
                        break;
                    }
                }
            }
        }
        let mut order: Vec<i64> = rows.keys().copied().collect();
        order.sort_unstable();
        let mut keys: Vec<String> = Vec::new();
        let mut files: HashMap<String, String> = HashMap::new();
        let mut best: HashMap<String, i64> = HashMap::new();
        let mut sem: HashMap<String, f64> = HashMap::new();
        let mut main: HashMap<String, bool> = HashMap::new();
        let mut kwf: HashMap<String, f64> = HashMap::new();
        for cid in &order {
            let r = &rows[cid];
            let s = from_blob(&r.vec).iter().zip(qv).map(|(a, b)| a * b).sum::<f32>() as f64;
            if !files.contains_key(&r.fkey) {
                files.insert(r.fkey.clone(), r.path.clone());
                keys.push(r.fkey.clone());
            }
            let cur = (main.get(&r.fkey).copied().unwrap_or(false), sem.get(&r.fkey).copied().unwrap_or(-9.0));
            if (!r.wt, s) > cur {
                sem.insert(r.fkey.clone(), s);
                best.insert(r.fkey.clone(), *cid);
                main.insert(r.fkey.clone(), !r.wt);
            }
            let e = kwf.entry(r.fkey.clone()).or_insert(0.0);
            *e = e.max(kw.get(cid).copied().unwrap_or(0.0));
        }
        let n = keys.len();
        let s_: Vec<f64> = keys.iter().map(|f| sem[f]).collect();
        let k_: Vec<f64> = keys.iter().map(|f| kwf[f]).collect();
        let zk = z(&k_);
        let mut extra: Vec<f64> = (0..n).map(|i| 0.2 * zk[i] - if TESTY.is_match(&files[&keys[i]]) { 0.5 } else { 0.0 }).collect();
        let pos: HashMap<&str, usize> = keys.iter().enumerate().map(|(i, f)| (f.as_str(), i)).collect();
        let mut tier: Option<Vec<f64>> = None;
        let mut cnt = vec![0f64; n];
        let qlow = ql.to_lowercase();
        if !phrase.is_empty() {
            let mut hit = vec![0f64; n];
            let mut cased = vec![0f64; n];
            let mut c2 = [vec![0f64; n], vec![0f64; n]];
            for cid in &phrase {
                if let Some(r) = rows.get(cid) {
                    let j = pos[r.fkey.as_str()];
                    hit[j] = 1.0;
                    c2[r.wt as usize][j] += count(&r.text.to_lowercase(), &qlow);
                    cased[j] = cased[j].max(if r.text.contains(ql) { 1.0 } else { 0.0 });
                }
            }
            cnt = (0..n).map(|j| if c2[0][j] > 0.0 { c2[0][j] } else { c2[1][j] }).collect();
            for j in 0..n {
                extra[j] += 3.0 * hit[j] + if bare { 0.0 } else { cnt[j].ln_1p() + cased[j] };
            }
            tier = Some(hit);
        }
        if frags.iter().any(|(_, h)| !h.is_empty()) {
            let qchars: Vec<char> = qlow.chars().collect();
            let mut spans: HashMap<usize, HashSet<i64>> = HashMap::new();
            cnt = vec![0f64; n];
            for (f, cids) in &frags {
                let fl = f.to_lowercase();
                let at: i64 = qlow.find(&fl).map(|b| qlow[..b].chars().count() as i64).unwrap_or(-1);
                let len = f.chars().count() as i64;
                let js: HashSet<usize> = cids.iter().filter_map(|c| rows.get(c)).map(|r| pos[r.fkey.as_str()]).collect();
                for j in js {
                    spans.entry(j).or_default().extend(at..at + len);
                }
                for cid in cids {
                    if let Some(r) = rows.get(cid).filter(|r| !r.wt) {
                        cnt[pos[r.fkey.as_str()]] += count(&r.text.to_lowercase(), &fl);
                    }
                }
            }
            let qn = qchars.len() as f64;
            let mut got = vec![0f64; n];
            for (j, s) in &spans {
                got[*j] = if s.len() as f64 >= qn / 2.0 { s.len() as f64 / qn } else { 0.0 };
            }
            for j in 0..n {
                extra[j] += 3.0 * got[j];
            }
            tier = Some(got);
        }
        for (name, cids) in &by_name {
            let mut has_ = vec![0f64; n];
            let mut defs: BTreeSet<usize> = BTreeSet::new();
            for cid in cids {
                if let Some(r) = rows.get(cid) {
                    if has_name(&r.text, name) {
                        let j = pos[r.fkey.as_str()];
                        has_[j] = 1.0;
                        if SYMBOL.captures_iter(&r.text).any(|m| m[1].rsplit('.').next() == Some(name.as_str())) {
                            defs.insert(j);
                        }
                    }
                }
            }
            let total: f64 = has_.iter().sum();
            if total > 0.0 {
                for j in 0..n {
                    extra[j] += 2.0 * has_[j] / (1.0 + total).log2();
                }
            }
            if !defs.is_empty() && defs.len() <= 3 {
                for j in defs {
                    extra[j] += 2.0;
                }
            }
        }
        let sd = std(&s_);
        let mut total: Vec<f64> = (0..n).map(|i| s_[i] + sd * extra[i]).collect();
        if let (Some(t), false) = (&tier, bare) {
            if t.iter().any(|x| *x != 0.0) {
                let mut ord: Vec<usize> = (0..n).collect();
                ord.sort_by(|&a, &b| (-t[a], -cnt[a], -total[a]).partial_cmp(&(-t[b], -cnt[b], -total[b]))
                    .unwrap_or(std::cmp::Ordering::Equal));
                let mut sorted = total.clone();
                sorted.sort_by(|a, b| b.total_cmp(a));
                let mut nt = vec![0f64; n];
                for (rank, &i) in ord.iter().enumerate() {
                    nt[i] = sorted[rank];
                }
                total = nt;
            }
        }
        keys.iter().enumerate().map(|(i, f)| (best[f], total[i])).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_and_word_bounds() {
        assert_eq!(code_names("retry failed webhook retryWebhook ICON_CANVAS"), vec!["retryWebhook", "ICON_CANVAS"]);
        assert_eq!(code_names("textarea"), vec!["textarea"]);
        assert!(has_name("x = retryWebhook(1)", "retryWebhook"));
        assert!(!has_name("retryWebhooks", "retryWebhook"));
        assert!(TESTY.is_match("src/tests/x.py") && TESTY.is_match("a/b.test.ts") && !TESTY.is_match("src/x.py"));
        assert!(SYMBOL.captures("  export async function fooBar(").is_some_and(|m| &m[1] == "fooBar"));
    }
}
