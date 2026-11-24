//! Token use of agent sessions, from the usage each agent writes into its transcripts, and the `session_usage`
//! table of index.db (sums per session, hour and model).
//!
//! - Claude Code: `message.usage` on assistant lines. A streamed reply is several lines with one message id: the
//!   last line of an id wins. Subagents write their own transcripts in `<session>/subagents/`; they count too.
//! - Codex: `token_count` events. `last_token_usage` is the turn's use; an event that repeats the total is skipped.
//!   The model is in the `turn_context` line before it.
//! - Qwen Code: `usageMetadata` on assistant lines, one per line id.
//!
//! Costs are estimates from Anthropic API list prices (`PRICES`). Cache writes count 1.25 times the input price
//! (5-minute cache) or 2 times (1-hour cache).

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Seek, SeekFrom};
use std::path::Path;

use serde_json::Value;
use store::rusqlite::{self, Connection, params};

use crate::Index;

/// (model id prefix, input, output, cache read) in US dollars per million tokens, list prices of 2026-10. The
/// longest prefix that matches wins.
pub const PRICES: [(&str, f64, f64, f64); 15] = [
    ("claude-fable-5-1", 10.0, 50.0, 0.25),
    ("claude-mythos-5-1", 10.0, 50.0, 0.25),
    ("claude-fable-5", 10.0, 50.0, 1.0),
    ("claude-mythos-5", 10.0, 50.0, 1.0),
    ("claude-opus-5-5", 4.0, 20.0, 0.20),
    ("claude-opus-5", 5.0, 25.0, 0.50),
    ("claude-opus-4-1", 15.0, 75.0, 1.50),
    ("claude-opus-4-2", 15.0, 75.0, 1.50),
    ("claude-opus-4", 5.0, 25.0, 0.50),
    ("claude-sonnet-5-5", 2.0, 10.0, 0.20),
    ("claude-sonnet-5", 2.0, 10.0, 0.20),
    ("claude-sonnet-4", 3.0, 15.0, 0.30),
    ("claude-haiku-5-5", 0.10, 0.50, 0.01),
    ("claude-haiku-4", 1.0, 5.0, 0.10),
    ("claude-3-5-haiku", 0.80, 4.0, 0.08),
];
pub const PRICES_AS_OF: &str = "2026-10";

/// (input, output, cache read) per million tokens of a model, or None when it has no price here.
pub fn price(model: &str) -> Option<(f64, f64, f64)> {
    let low = model.to_lowercase();
    let m = low.strip_prefix("anthropic.").unwrap_or(&low);
    let m = m.strip_prefix("us.anthropic.").unwrap_or(m);
    let mut by_len = PRICES.to_vec();
    by_len.sort_by_key(|p| std::cmp::Reverse(p.0.len()));
    by_len.into_iter().find(|p| m.starts_with(p.0)).map(|p| (p.1, p.2, p.3))
}

/// The estimated cost in US dollars, or None for a model with no price.
pub fn cost(model: &str, input: i64, output: i64, cache_write: i64, cache_read: i64, cache_write_1h: i64) -> Option<f64> {
    let (i, o, r) = price(model)?;
    let w5 = (cache_write - cache_write_1h).max(0) as f64;
    Some((input as f64 * i + output as f64 * o + w5 * i * 1.25 + cache_write_1h as f64 * i * 2.0 + cache_read as f64 * r) / 1e6)
}

/// One model call: (hour, model, input, output, cache write, cache read, cache write 1h).
pub type Call = (i64, String, i64, i64, i64, i64, i64);

/// The usage state of one session: each model call by its id, the read offsets of subagent transcripts, and the
/// Codex state (the current model and the last total).
#[derive(Debug, Clone, Default)]
pub struct Usage {
    pub calls: HashMap<String, Call>,
    sub: HashMap<String, u64>,
    model: String,
    total: Option<Value>,
}

/// The start of the hour of an ISO time, as Unix seconds; 0 when the time is missing or not a time.
pub fn hour(ts: &str) -> i64 {
    iso_seconds(ts).map(|t| (t.floor() as i64).div_euclid(3600) * 3600).unwrap_or(0)
}

/// Seconds since 1970 of an ISO time. A time without a zone is local time, as in Python.
pub fn iso_seconds(ts: &str) -> Option<f64> {
    if ts.is_empty() {
        return None;
    }
    if let Ok(t) = chrono::DateTime::parse_from_rfc3339(ts) {
        return Some(t.timestamp_micros() as f64 / 1e6);
    }
    for f in ["%Y-%m-%dT%H:%M:%S%.f%:z", "%Y-%m-%d %H:%M:%S%.f%:z", "%Y-%m-%dT%H:%M%:z"] {
        if let Ok(t) = chrono::DateTime::parse_from_str(ts, f) {
            return Some(t.timestamp_micros() as f64 / 1e6);
        }
    }
    use chrono::TimeZone;
    let naive = ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%dT%H:%M"].iter()
        .find_map(|f| chrono::NaiveDateTime::parse_from_str(ts, f).ok())
        .or_else(|| chrono::NaiveDate::parse_from_str(ts, "%Y-%m-%d").ok().and_then(|d| d.and_hms_opt(0, 0, 0)))?;
    chrono::Local.from_local_datetime(&naive).earliest().map(|t| t.timestamp_micros() as f64 / 1e6)
}

fn int(v: Option<&Value>) -> i64 {
    match v {
        Some(Value::Number(n)) => n.as_i64().filter(|x| *x > 0).unwrap_or(0),
        Some(Value::Bool(true)) => 1,
        _ => 0,
    }
}

fn s<'a>(v: &'a Value, key: &str) -> &'a str {
    v.get(key).and_then(Value::as_str).unwrap_or("")
}

pub fn claude_line(d: &Value, acc: &mut Usage) {
    if s(d, "type") != "assistant" {
        return;
    }
    let Some(m) = d.get("message").filter(|m| m.is_object()) else { return };
    let (Some(u), model) = (m.get("usage").filter(|u| u.is_object()), s(m, "model")) else { return };
    if model.is_empty() || model.starts_with('<') {
        return;
    }
    let key = [s(m, "id"), s(d, "requestId"), s(d, "uuid")].into_iter().find(|k| !k.is_empty());
    let Some(key) = key else { return };
    let cc = u.get("cache_creation").filter(|c| c.is_object());
    acc.calls.insert(key.to_string(), (
        hour(s(d, "timestamp")), model.to_string(), int(u.get("input_tokens")), int(u.get("output_tokens")),
        int(u.get("cache_creation_input_tokens")), int(u.get("cache_read_input_tokens")),
        int(cc.and_then(|c| c.get("ephemeral_1h_input_tokens"))),
    ));
}

pub fn qwen_line(d: &Value, acc: &mut Usage) {
    let Some(u) = d.get("usageMetadata").filter(|u| u.is_object()) else { return };
    let uuid = s(d, "uuid");
    if s(d, "type") != "assistant" || uuid.is_empty() {
        return;
    }
    let cached = int(u.get("cachedContentTokenCount"));
    let model = Some(s(d, "model")).filter(|m| !m.is_empty()).unwrap_or("qwen");
    acc.calls.insert(uuid.to_string(), (
        hour(s(d, "timestamp")), model.to_string(), (int(u.get("promptTokenCount")) - cached).max(0),
        int(u.get("candidatesTokenCount")) + int(u.get("thoughtsTokenCount")), 0, cached, 0,
    ));
}

pub fn codex_line(d: &Value, acc: &mut Usage) {
    let Some(p) = d.get("payload").filter(|p| p.is_object()) else { return };
    if s(d, "type") == "turn_context" && !s(p, "model").is_empty() {
        acc.model = s(p, "model").to_string();
        return;
    }
    let Some(info) = p.get("info").filter(|i| i.is_object()) else { return };
    if s(p, "type") != "token_count" {
        return;
    }
    let total = info.get("total_token_usage").and_then(|t| t.get("total_tokens")).filter(|t| !t.is_null());
    let last = info.get("last_token_usage").filter(|l| l.is_object());
    let (Some(total), Some(last)) = (total, last) else { return };
    if acc.total.as_ref() == Some(total) {
        return;
    }
    acc.total = Some(total.clone());
    let cached = int(last.get("cached_input_tokens"));
    let model = if acc.model.is_empty() { "codex".to_string() } else { acc.model.clone() };
    acc.calls.insert(format!("t{total}"), (
        hour(s(d, "timestamp")), model, (int(last.get("input_tokens")) - cached).max(0), int(last.get("output_tokens")),
        0, cached, 0,
    ));
}

/// Read whole lines from `offset`, passing each that holds one of `marks` to `f`; returns the offset after the
/// last whole line.
fn read(path: &Path, offset: u64, f: fn(&Value, &mut Usage), marks: &[&str], acc: &mut Usage) -> u64 {
    let Ok(mut file) = std::fs::File::open(path) else { return offset };
    if file.seek(SeekFrom::Start(offset)).is_err() {
        return offset;
    }
    let mut r = BufReader::new(file);
    let (mut off, mut raw) = (offset, Vec::new());
    loop {
        raw.clear();
        match r.read_until(b'\n', &mut raw) {
            Ok(n) if n > 0 && raw.last() == Some(&b'\n') => {}
            _ => break,
        }
        off += raw.len() as u64;
        if !marks.iter().any(|m| raw.windows(m.len()).any(|w| w == m.as_bytes())) {
            continue;
        }
        if let Ok(d @ Value::Object(_)) = serde_json::from_slice::<Value>(&raw) {
            f(&d, acc);
        }
    }
    off
}

const CLAUDE_MARKS: [&str; 1] = ["\"usage\""];

/// The usage of a Claude Code session's subagents (`<session>/subagents/*.jsonl`), from where the last read stopped.
pub fn subagents(path: &Path, acc: &mut Usage) {
    let folder = path.with_extension("").join("subagents");
    let Ok(rd) = std::fs::read_dir(&folder) else { return };
    let mut files: Vec<_> = rd.flatten().map(|e| e.path()).filter(|p| p.extension().is_some_and(|x| x == "jsonl")).collect();
    files.sort();
    for p in files {
        let k = p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        let Ok(size) = p.metadata().map(|m| m.len()) else { continue };
        let mut off = acc.sub.get(&k).copied().unwrap_or(0);
        if size < off {
            let prefix = format!("{k}:");
            acc.calls.retain(|c, _| !c.starts_with(&prefix));
            off = 0;
        }
        if size == off {
            continue;
        }
        let mut sub = Usage::default();
        let end = read(&p, off, claude_line, &CLAUDE_MARKS, &mut sub);
        acc.sub.insert(k.clone(), end);
        for (c, v) in sub.calls {
            acc.calls.insert(format!("{k}:{c}"), v);
        }
    }
}

/// The usage of a whole transcript (and its subagents), without the rest of the parse.
pub fn scan(source: &str, path: &Path) -> Usage {
    let mut acc = Usage::default();
    if path.extension().is_none_or(|x| x != "jsonl") {
        return acc;
    }
    match source {
        "claude" => {
            read(path, 0, claude_line, &CLAUDE_MARKS, &mut acc);
            subagents(path, &mut acc);
        }
        "qwen" => {
            read(path, 0, qwen_line, &["\"usageMetadata\""], &mut acc);
        }
        "codex" => {
            read(path, 0, codex_line, &["\"token_count\"", "\"turn_context\""], &mut acc);
        }
        _ => {}
    }
    acc
}

/// One row of `session_usage`: sums of one hour and model.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Row {
    pub hour: i64,
    pub model: String,
    pub calls: i64,
    pub input: i64,
    pub output: i64,
    pub cache_write: i64,
    pub cache_read: i64,
    pub cache_write_1h: i64,
}

/// A session's usage as rows: sums per hour and model, in (hour, model) order.
pub fn rows(acc: &Usage) -> Vec<Row> {
    let mut out: std::collections::BTreeMap<(i64, String), [i64; 6]> = Default::default();
    for (h, m, i, o, cw, cr, cw1) in acc.calls.values() {
        let r = out.entry((*h, m.clone())).or_default();
        for (x, v) in r.iter_mut().zip([1, *i, *o, *cw, *cr, *cw1]) {
            *x += v;
        }
    }
    out.into_iter().map(|((hour, model), v)| Row {
        hour, model, calls: v[0], input: v[1], output: v[2], cache_write: v[3], cache_read: v[4], cache_write_1h: v[5],
    }).collect()
}

/// Write a session's rows in place of the ones it had (on a writer connection).
pub fn replace_session(c: &Connection, session_id: &str, usage: &[Row]) -> rusqlite::Result<()> {
    c.execute("DELETE FROM session_usage WHERE session_id = ?", [session_id])?;
    let mut st = c.prepare("INSERT INTO session_usage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")?;
    for r in usage {
        st.execute(params![session_id, r.hour, r.model, r.calls, r.input, r.output, r.cache_write, r.cache_read, r.cache_write_1h])?;
    }
    Ok(())
}

pub fn delete_sessions(c: &Connection, session_ids: &[String]) -> rusqlite::Result<()> {
    for part in session_ids.chunks(500) {
        c.execute(&format!("DELETE FROM session_usage WHERE session_id IN ({})", crate::db::marks(part.len())),
                  rusqlite::params_from_iter(part))?;
    }
    Ok(())
}

impl Index {
    /// Replace the usage rows of several sessions in one transaction.
    pub fn write_usage(&self, items: &[(String, Vec<Row>)]) -> rusqlite::Result<()> {
        if items.is_empty() {
            return Ok(());
        }
        self.db.tx(|t| {
            for (sid, rows) in items {
                replace_session(t, sid, rows)?;
            }
            Ok(())
        })
    }

    pub fn sessions_with_usage(&self) -> HashMap<String, ()> {
        self.db.read(|c| {
            let mut st = c.prepare("SELECT DISTINCT session_id FROM session_usage")?;
            let r = st.query_map([], |r| r.get::<_, String>(0))?.flatten().map(|s| (s, ())).collect();
            Ok(r)
        }).unwrap_or_default()
    }

    /// (session id, row) from hour `since` on: for sums over some sessions only.
    pub fn usage_since(&self, since: i64) -> Vec<(String, Row)> {
        self.db.read(|c| {
            let mut st = c.prepare("SELECT session_id, hour, model, calls, input, output, cache_write, cache_read, \
                                    cache_write_1h FROM session_usage WHERE hour >= ?")?;
            let r = st.query_map([since], |r| Ok((r.get(0)?, Row {
                hour: r.get(1)?, model: r.get(2)?, calls: r.get(3)?, input: r.get(4)?, output: r.get(5)?,
                cache_write: r.get(6)?, cache_read: r.get(7)?, cache_write_1h: r.get(8)?,
            })))?.flatten().collect();
            Ok(r)
        }).unwrap_or_default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prices_by_longest_prefix() {
        assert_eq!(price("claude-opus-5-5-20261001"), Some((4.0, 20.0, 0.20)));
        assert_eq!(price("us.anthropic.claude-opus-5"), Some((5.0, 25.0, 0.50)));
        assert_eq!(price("gpt-5"), None);
        let c = cost("claude-opus-5", 1_000_000, 0, 1_000_000, 0, 1_000_000).unwrap_or(0.0);
        assert!((c - 15.0).abs() < 1e-9);
    }

    #[test]
    fn hours_of_iso_times() {
        assert_eq!(hour("2026-01-01T01:30:00.500Z"), 1_767_229_200);
        assert_eq!(hour(""), 0);
        assert_eq!(hour("soon"), 0);
    }

    #[test]
    fn codex_skips_repeated_totals() {
        let mut acc = Usage::default();
        let ctx = serde_json::json!({"type": "turn_context", "payload": {"model": "gpt-x"}});
        let tc = serde_json::json!({"type": "event_msg", "timestamp": "2026-01-01T00:00:00Z", "payload": {"type": "token_count",
            "info": {"total_token_usage": {"total_tokens": 30}, "last_token_usage": {"input_tokens": 20, "cached_input_tokens": 5, "output_tokens": 10}}}});
        codex_line(&ctx, &mut acc);
        codex_line(&tc, &mut acc);
        codex_line(&tc, &mut acc);
        let r = rows(&acc);
        assert_eq!(r.len(), 1);
        assert_eq!((r[0].model.as_str(), r[0].calls, r[0].input, r[0].cache_read), ("gpt-x", 1, 15, 5));
    }
}
