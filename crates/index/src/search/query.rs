//! The search box's language, Google-style:
//!
//! ```text
//! pricing model                 -> by meaning
//! "rate limit"                  -> must contain those exact words (case-insensitive)
//! "rate limit" retries          -> contains the phrase, ranked by meaning of 'retries'
//! -draft  -"old notes"          -> exclude
//! kind:code  ext:pdf  in:my-app -> filters (kind is file, doc, code, session, folder, repo or person)
//! person:sam                    -> the same as kind:person sam
//! since:3d  until:2026-10-01    -> by time
//! ```

use std::collections::BTreeMap;

use chrono::{Duration, Local, NaiveDate, TimeZone};
use regex::Regex;
use serde_json::{Value, json};

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Query {
    pub semantic: String,
    pub exact: Vec<String>,
    pub exclude: Vec<String>,
    /// kind, ext, in, by, project: lowercase text. since, until: in `since` / `until`.
    pub filters: BTreeMap<String, String>,
    pub since: Option<f64>,
    pub until: Option<f64>,
}

impl Query {
    pub fn get(&self, key: &str) -> Option<&str> {
        self.filters.get(key).map(|s| s.as_str())
    }

    pub fn to_json(&self) -> Value {
        let mut f = serde_json::Map::new();
        for (k, v) in &self.filters {
            f.insert(k.clone(), json!(v));
        }
        if let Some(s) = self.since {
            f.insert("since".into(), json!(s));
        }
        if let Some(u) = self.until {
            f.insert("until".into(), json!(u));
        }
        json!({"semantic": self.semantic, "exact": self.exact, "exclude": self.exclude, "filters": f})
    }
}

/// `kind:` words and what they mean.
pub fn kind_of(v: &str) -> String {
    match v {
        "file" | "files" => "file",
        "doc" | "docs" | "document" | "documents" => "doc",
        "code" => "code",
        "session" | "sessions" | "chat" | "agent" => "session",
        "folder" | "folders" | "dir" | "dirs" | "directory" => "folder",
        "repo" | "repos" | "repository" => "repo",
        "person" | "people" | "who" | "author" => "person",
        other => other,
    }
    .to_string()
}

/// since:/until: as epoch seconds: a date (with `end`, the end of that day), a time back (3d, 2w, 1m, 12h, 1y),
/// today or yesterday.
pub fn when(val: &str, end: bool) -> Result<f64, String> {
    let v = val.trim().to_lowercase();
    let unit = |c: char| match c {
        'h' => Some(3600i64),
        'd' => Some(86400),
        'w' => Some(7 * 86400),
        'm' => Some(30 * 86400),
        'y' => Some(365 * 86400),
        _ => None,
    };
    if let Some(last) = v.chars().last() {
        let num = &v[..v.len() - last.len_utf8()];
        if !num.is_empty() && num.chars().all(|c| c.is_ascii_digit()) {
            if let (Some(u), Ok(n)) = (unit(last), num.parse::<i64>()) {
                return Ok(store::now() - (n * u) as f64);
            }
        }
    }
    let today = Local::now().date_naive();
    let d = match v.as_str() {
        "today" => Some(today),
        "yesterday" => today.pred_opt(),
        _ => NaiveDate::parse_from_str(&v, "%Y-%m-%d").ok(),
    };
    let Some(d) = d else {
        return Err(format!("'{val}': since: and until: take a date (2026-10-01), a time back (3d, 2w, 1m, 12h), \
                            today or yesterday"));
    };
    let d = if end { d + Duration::days(1) } else { d };
    let t = d.and_hms_opt(0, 0, 0).and_then(|t| Local.from_local_datetime(&t).earliest());
    t.map(|t| t.timestamp() as f64).ok_or_else(|| format!("'{val}': not a date"))
}

fn is_word_char(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// The query's parts. Errors only on a since:/until: value that is no time.
pub fn parse(q: &str) -> Result<Query, String> {
    let token = Regex::new(r#"(-?)"([^"]*)"?|(\S+)"#).map_err(|e| e.to_string())?;
    let filter = Regex::new(r"(?is)^(kind|ext|in|by|person|since|until|project):(.+)$").map_err(|e| e.to_string())?;
    let mut out = Query::default();
    let mut words = Vec::new();
    for c in token.captures_iter(q) {
        if let Some(p) = c.get(2) {
            let neg = c.get(1).is_some_and(|m| !m.as_str().is_empty());
            let p = p.as_str().trim();
            if !p.is_empty() {
                if neg { out.exclude.push(p.into()) } else { out.exact.push(p.into()) }
            }
            continue;
        }
        let word = c.get(3).map(|m| m.as_str()).unwrap_or("");
        if let Some(m) = filter.captures(word) {
            let key = m[1].to_lowercase();
            let raw = m[2].to_string();
            let val = raw.to_lowercase().trim_start_matches('.').to_string();
            match key.as_str() {
                "since" => out.since = Some(when(&val, false)?),
                "until" => out.until = Some(when(&val, true)?),
                "person" => {
                    out.filters.insert("kind".into(), "person".into());
                    words.push(raw);
                }
                "kind" => {
                    out.filters.insert(key, kind_of(&val));
                }
                _ => {
                    out.filters.insert(key, val);
                }
            }
            continue;
        }
        let mut ch = word.chars();
        let excl = ch.next() == Some('-')
            && ch.next().is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
            && ch.next().is_some_and(is_word_char)
            && ch.next().is_some_and(is_word_char);
        if excl {
            out.exclude.push(word[1..].into());
        } else if !word.is_empty() {
            words.push(word.into());
        }
    }
    out.semantic = words.join(" ");
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_phrases_filters_and_exclusions() {
        let q = parse(r#""rate limit" retries -draft -"old notes" kind:files ext:.PDF in:app -p --x"#).expect("q");
        assert_eq!(q.exact, vec!["rate limit"]);
        assert_eq!(q.exclude, vec!["draft", "old notes"]);
        assert_eq!(q.semantic, "retries -p --x");
        assert_eq!(q.get("kind"), Some("file"));
        assert_eq!(q.get("ext"), Some("pdf"));
        let p = parse("person:Sam").expect("q");
        assert_eq!((p.get("kind"), p.semantic.as_str()), (Some("person"), "Sam"));
        assert!(parse("since:3d").expect("q").since.is_some());
        assert!(parse("since:soon").is_err());
    }
}
