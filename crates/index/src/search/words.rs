//! Text lookups in the word indexes, as grep finds text.
//!
//! Each word index holds words, not trigrams (a twelfth of the size, and faster at the median):
//! `fts_code_words` holds the code tokens of a chunk's path and text and its repo token, `fts_files` and
//! `fts_sessions` the unicode61 words of the chunk text. A lookup asks the index for the chunks with the text's
//! words in order, then checks each chunk's text.

use std::collections::BTreeSet;

use store::rusqlite::{self, Connection, types::Value as Sql};

use crate::text::{code_tokens, repo_token};

/// (chunk table, word index) of a kind of chunk.
pub fn corpus(kind: &str) -> (&'static str, &'static str) {
    match kind {
        "file" => ("file_chunks", "fts_files"),
        "code" => ("code_chunks", "fts_code_words"),
        _ => ("chunks", "fts_sessions"),
    }
}

/// The words that the word index of `kind` holds for a text, in order.
pub fn words(kind: &str, text: &str) -> Vec<String> {
    if kind == "code" {
        return code_tokens(text).split_whitespace().map(String::from).collect();
    }
    let low = text.to_lowercase();
    let mut out = Vec::new();
    let mut cur = String::new();
    for c in low.chars() {
        if c.is_alphanumeric() {
            cur.push(c);
        } else if !cur.is_empty() {
            out.push(std::mem::take(&mut cur));
        }
    }
    if !cur.is_empty() {
        out.push(cur);
    }
    out
}

fn is_cased_one_way(w: &str) -> bool {
    let cased: Vec<char> = w.chars().filter(|c| c.is_alphabetic()).collect();
    !cased.is_empty() && (cased.iter().all(|c| c.is_lowercase()) || cased.iter().all(|c| c.is_uppercase()))
}

/// The ways to read one name typed without its capitals or underscores as known code words, at most 8:
/// "textarea" is also "text area", as in TextArea. With `prefix`, the last part may be the start of a word.
pub fn joined(c: &Connection, s: &str, prefix: bool) -> BTreeSet<Vec<String>> {
    let runs: Vec<&str> = {
        let b = s.as_bytes();
        let (mut out, mut i) = (Vec::new(), 0);
        while i < b.len() {
            if b[i].is_ascii_alphabetic() {
                let mut j = i + 1;
                while j < b.len() && b[j].is_ascii_alphanumeric() {
                    j += 1;
                }
                out.push(&s[i..j]);
                i = j;
            } else {
                i += 1;
            }
        }
        out
    };
    if runs.len() != 1 || !is_cased_one_way(runs[0]) || !(4..=32).contains(&runs[0].len()) {
        return BTreeSet::new();
    }
    let w = runs[0].to_lowercase();
    let mut subs = BTreeSet::new();
    for i in 0..w.len() {
        for j in i + 2..=w.len() {
            subs.insert(w[i..j].to_string());
        }
    }
    let subs: Vec<String> = subs.into_iter().collect();
    let sql = format!("SELECT term FROM fts_code_words_vocab WHERE term IN ({})", crate::db::marks(subs.len()));
    let known: std::collections::HashSet<String> = match c.prepare(&sql) {
        Ok(mut st) => match st.query_map(rusqlite::params_from_iter(subs.iter()), |r| r.get::<_, String>(0)) {
            Ok(rows) => rows.filter_map(Result::ok).collect(),
            Err(_) => return BTreeSet::new(),
        },
        Err(_) => return BTreeSet::new(),
    };
    let mut out: Vec<Vec<String>> = Vec::new();
    fn walk(w: &str, i: usize, parts: &mut Vec<String>, known: &std::collections::HashSet<String>, prefix: bool,
            out: &mut Vec<Vec<String>>) {
        if out.len() >= 8 {
            return;
        }
        let rest = &w[i..];
        if !parts.is_empty() && (known.contains(rest) || (prefix && rest.len() >= 2)) {
            let mut p = parts.clone();
            p.push(rest.to_string());
            out.push(p);
        }
        for j in i + 2..w.len().saturating_sub(1) {
            if known.contains(&w[i..j]) {
                parts.push(w[i..j].to_string());
                walk(w, j, parts, known, prefix, out);
                parts.pop();
            }
        }
    }
    walk(&w, 0, &mut Vec::new(), &known, prefix, &mut out);
    out.into_iter().collect()
}

/// The FTS5 phrase for the words of a text. It only has to find more chunks than the text is in, because the
/// chunk's text is checked after. The costly words are left out: a last word of 1 or 2 letters as a prefix, a
/// first word of one letter, and in a long text all but the 8 longest words in a row.
pub fn phrase(words: &[String], prefix: bool) -> String {
    let mut w: Vec<String> = words.to_vec();
    let mut prefix = prefix;
    if prefix && w.len() > 1 && w[w.len() - 1].chars().count() < 3 {
        w.pop();
        prefix = false;
    }
    if w.len() > 1 && w[0].chars().count() < 2 {
        w.remove(0);
    }
    if w.len() > 8 {
        let sum = |i: usize| w[i..i + 8].iter().map(|x| x.chars().count()).sum::<usize>();
        let mut best = 0;
        for i in 0..w.len() - 7 {
            if sum(i) > sum(best) {
                best = i;
            }
        }
        prefix = prefix && best + 8 == w.len();
        w = w[best..best + 8].to_vec();
    }
    format!("\"{}\"{}", w.join(" "), if prefix { " *" } else { "" })
}

/// An FTS5 query that needs every phrase (the collected documents' word index).
pub fn fts_query(phrases: &[String]) -> String {
    phrases.iter().map(|p| format!("\"{}\"", p.replace('"', "\"\""))).collect::<Vec<_>>().join(" AND ")
}

fn like_arg(s: &str) -> String {
    let mut out = String::from("%");
    for c in s.chars() {
        if matches!(c, '%' | '_' | '\\') {
            out.push('\\');
        }
        out.push(c);
    }
    out.push('%');
    out
}

/// (chunk id, bm25) of the `kind` chunks that contain every one of `texts`, ignoring case: at most `limit`, in id
/// order, or best first with `rank` (else the score is 0; a higher score is better). A text that ends inside a
/// word matches that word as a prefix; one that starts inside a word is found only where it starts a word. Code
/// words also come from the text in lowercase and split into known words (`joined`). A text of punctuation only
/// reads every chunk's text. `roots`: code only, the repos to look in.
pub fn contains(c: &Connection, kind: &str, texts: &[String], limit: usize, roots: Option<&[String]>, rank: bool)
                -> rusqlite::Result<Vec<(i64, f64)>> {
    let (table, fts) = corpus(kind);
    let mut check = texts.iter().map(|_| "t.text LIKE ? ESCAPE '\\'").collect::<Vec<_>>().join(" AND ");
    let mut args: Vec<Sql> = texts.iter().map(|s| Sql::Text(like_arg(s))).collect();
    let roots = roots.filter(|r| kind == "code" && !r.is_empty());
    if let Some(r) = roots {
        check += &format!(" AND t.repo IN ({})", crate::db::marks(r.len()));
        args.extend(r.iter().map(|x| Sql::Text(x.clone())));
    }
    let mut parts = Vec::new();
    for s in texts {
        let mut forms: BTreeSet<Vec<String>> = [words(kind, s), words(kind, &s.to_lowercase())].into_iter()
            .filter(|f| !f.is_empty()).collect();
        if forms.is_empty() {
            let sql = format!("SELECT t.id FROM {table} t WHERE {check} LIMIT ?");
            let mut a = args.clone();
            a.push(Sql::Integer(limit as i64));
            let mut st = c.prepare(&sql)?;
            return st.query_map(rusqlite::params_from_iter(a), |r| Ok((r.get::<_, i64>(0)?, 0.0)))?.collect();
        }
        let star = s.chars().last().is_some_and(|ch| ch.is_alphanumeric());
        if kind == "code" {
            forms.extend(joined(c, s, star));
        }
        parts.push(format!("({})", forms.iter().map(|f| phrase(f, star)).collect::<Vec<_>>().join(" OR ")));
    }
    let mut m = parts.join(" AND ");
    if kind == "code" {
        m = format!("words : ({m})");
        if let Some(r) = roots {
            m += &format!(" AND repo : ({})", r.iter().map(|x| repo_token(x)).collect::<Vec<_>>().join(" OR "));
        }
    }
    let score = if !rank { "0".to_string() } else if kind == "code" { format!("bm25({fts}, 1.0, 0.0)") } else { format!("bm25({fts})") };
    let sql = format!("SELECT t.id, {score} FROM {fts} JOIN {table} t ON t.id={fts}.rowid WHERE {fts} MATCH ? AND {check}{} LIMIT ?",
                      if rank { " ORDER BY 2" } else { "" });
    let mut a = vec![Sql::Text(m)];
    a.extend(args);
    a.push(Sql::Integer(limit as i64));
    let mut st = c.prepare(&sql)?;
    let rows = st.query_map(rusqlite::params_from_iter(a), |r| Ok((r.get::<_, i64>(0)?, -r.get::<_, f64>(1)?)))?;
    rows.collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn phrases_drop_costly_words() {
        let w = |s: &str| s.split(' ').map(String::from).collect::<Vec<_>>();
        assert_eq!(phrase(&w("fetch us"), true), "\"fetch\"");
        assert_eq!(phrase(&w("a fetch user"), true), "\"fetch user\" *");
        assert_eq!(fts_query(&["say \"hi\"".into()]), "\"say \"\"hi\"\"\"");
        assert_eq!(words("file", "Hello_wörld 42"), vec!["hello", "wörld", "42"]);
    }
}
