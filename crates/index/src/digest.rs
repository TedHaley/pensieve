//! Session digests: the part of a session that a thread can use without its transcript (its id, times, links and
//! components). A digest holds no transcript text. Schema: 0.21's `session_digest.schema.json`.

use std::collections::BTreeMap;
use std::sync::LazyLock;

use fancy_regex::Regex as Fancy;
use regex::Regex;
use serde_json::{Value, json};

use crate::parsers::Session;
use crate::segment;

pub const SCHEMA: &str = "session_digest/0.2";

static PR_URL: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"https?://(?:www\.)?github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)").expect("regex"));
static PR_URL_AT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^https?://(?:www\.)?github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)").expect("regex"));
static PR_REF: LazyLock<Fancy> = LazyLock::new(|| Fancy::new(r"(?<![\w/.@-])([\w.-]+/[\w.-]+)#(\d+)\b").expect("regex"));
static TICKET: LazyLock<Fancy> = LazyLock::new(|| Fancy::new(r"(?<![\w-])([A-Z][A-Z0-9]{1,9})-(\d{1,6})\b").expect("regex"));
static URL: LazyLock<Regex> = LazyLock::new(|| Regex::new(r#"https?://[^\s`'"<>()\[\]{}]+"#).expect("regex"));
static BRANCH: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\b(?:checkout -b|switch -c|switch -C|checkout -B|push -u origin|push origin) ([\w./-]+)").expect("regex")
});
static HTML_END: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?:&(?:gt|lt|quot|amp|#\d+);?)+$").expect("regex"));

/// Words that look like ticket keys but are not: standards, formats and tools.
const NOT_TICKETS: [&str; 46] = [
    "UTF", "ISO", "SHA", "HTTP", "HTTPS", "TLS", "SSL", "RFC", "ES", "MD", "MP", "PDF", "CSV", "JSON", "API", "UTC",
    "GMT", "ASCII", "ARM", "X", "IPV", "IPV6", "AES", "RSA", "SHA256", "OAUTH", "CVE", "COVID", "H", "ISO8601", "PEP",
    "CPU", "GPU", "RAM", "SSH", "DNS", "TCP", "UDP", "XML", "HTML", "CSS", "", "", "", "", "",
];

const TRAILING: &[char] = &['.', ',', ';', ':', '!', '?'];

/// A URL without trailing punctuation, HTML entities, fragment or query: the same page gives the same key.
pub fn canonical_url(url: &str) -> String {
    let u = url.trim_end_matches(TRAILING);
    let u = HTML_END.replace(u, "");
    let u = u.trim_end_matches(TRAILING);
    let (scheme, rest) = match u.split_once("://") {
        Some((s, r)) if !s.is_empty() && !s.contains('/') => (s.to_lowercase(), r),
        _ => return u.split(['?', '#']).next().unwrap_or("").trim_end_matches('/').to_string(),
    };
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let netloc = rest[..end].to_lowercase();
    let path = rest[end..].split(['?', '#']).next().unwrap_or("").trim_end_matches('/');
    format!("{scheme}://{netloc}{path}")
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Link {
    pub kind: String,
    pub key: String,
    #[serde(rename = "ref")]
    pub reference: String,
    pub count: usize,
}

/// Every link in `text` (pull requests, tickets, branches, URLs), once per key, with how often it appears.
pub fn extract_links(text: &str) -> Vec<Link> {
    let mut found: BTreeMap<(String, String), Link> = BTreeMap::new();
    let mut add = |kind: &str, key: String, r: String| {
        found.entry((kind.into(), key.clone()))
            .or_insert(Link { kind: kind.into(), key, reference: r, count: 0 })
            .count += 1;
    };
    let mut spans = Vec::new();
    for c in PR_URL.captures_iter(text) {
        let m = c.get(0).map(|m| (m.start(), m.end(), m.as_str())).unwrap_or((0, 0, ""));
        spans.push((m.0, m.1));
        add("pr", format!("{}#{}", &c[1], &c[2]), m.2.trim_end_matches(TRAILING).to_string());
    }
    for c in PR_REF.captures_iter(text).flatten() {
        let start = c.get(0).map(|m| m.start()).unwrap_or(0);
        if spans.iter().any(|(a, b)| *a <= start && start < *b) {
            continue;
        }
        let key = format!("{}#{}", &c[1], &c[2]);
        add("pr", key.clone(), key);
    }
    for c in TICKET.captures_iter(text).flatten() {
        if NOT_TICKETS.contains(&&c[1]) {
            continue;
        }
        let key = format!("{}-{}", &c[1], &c[2]);
        add("ticket", key.clone(), key);
    }
    for c in BRANCH.captures_iter(text) {
        let name = c[1].trim_end_matches(TRAILING).to_string();
        if !name.is_empty() {
            add("branch", name.clone(), name);
        }
    }
    for m in URL.find_iter(text) {
        if PR_URL_AT.is_match(m.as_str()) {
            continue;
        }
        let url = canonical_url(m.as_str());
        if !url.is_empty() {
            add("url", url.clone(), url);
        }
    }
    let mut out: Vec<Link> = found.into_values().collect();
    out.sort_by(|a, b| (&a.kind, std::cmp::Reverse(a.count), &a.key).cmp(&(&b.kind, std::cmp::Reverse(b.count), &b.key)));
    out
}

/// The `n` most used content words of a text. Words, not sentences: a digest holds no transcript text.
pub fn top_words(text: &str, n: usize) -> Vec<String> {
    let mut w: Vec<(String, usize)> = segment::words(text).into_iter().collect();
    w.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    w.into_iter().take(n).map(|x| x.0).collect()
}

/// The components of a session, each with its id, turns, words and links.
pub fn build_components(s: &Session, shift: Option<f64>) -> Vec<Value> {
    segment::components(&s.turns, shift.unwrap_or(segment::SHIFT), segment::MIN_EXCHANGES).into_iter().map(|c| {
        let (lo, hi) = c.turns;
        let part = s.turns[lo..hi].iter().map(|t| t.1.as_str()).filter(|t| !t.is_empty()).collect::<Vec<_>>().join("\n");
        json!({
            "id": format!("{}#{}", s.id, c.index), "index": c.index, "turns": [lo, hi], "exchanges": c.exchanges,
            "words": top_words(&part, 4), "links": extract_links(&part),
        })
    }).collect()
}

/// A digest of a parsed session. `summary` and the steps are empty here; the summarizer fills them.
pub fn build(s: &Session) -> Value {
    let text = s.turns.iter().map(|t| t.1.as_str()).filter(|t| !t.is_empty()).collect::<Vec<_>>().join("\n");
    json!({
        "schema": SCHEMA, "session_id": s.id, "source": s.source, "started_at": s.started, "ended_at": s.updated,
        "summary": "", "steps_done": [], "steps_left": [], "closing_signal": null,
        "links": extract_links(&text), "components": build_components(s, None),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_links_once_per_key() {
        let t = "See https://github.com/acme/app/pull/12. and acme/app#12, ABC-42 twice ABC-42, UTF-8, \
                 git checkout -b fix/login then https://Example.com/a/?q=1#x";
        let l = extract_links(t);
        let keys: Vec<(&str, &str, usize)> = l.iter().map(|x| (x.kind.as_str(), x.key.as_str(), x.count)).collect();
        assert_eq!(keys, vec![("branch", "fix/login", 1), ("pr", "acme/app#12", 2), ("ticket", "ABC-42", 2),
                              ("url", "https://example.com/a", 1)]);
    }
}
