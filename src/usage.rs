//! A local log of every MCP tool call and hook decision, as in 0.21: which agent made it, from where, with what
//! arguments, how long it took, and what it returned. It lives in the `usage_calls` table of store.db. Logging never
//! fails a call: an error to log is dropped.

use std::sync::OnceLock;

use serde_json::Value;
use store::Db;

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS usage_calls (
  id         INTEGER PRIMARY KEY,
  at         INTEGER NOT NULL,
  source     TEXT,
  tool       TEXT NOT NULL,
  client     TEXT,
  conn       TEXT,
  cwd        TEXT,
  scope      TEXT,
  args       TEXT CHECK (args IS NULL OR json_valid(args)),
  ms         REAL,
  ok         INTEGER CHECK (ok IN (0, 1)),
  error      TEXT,
  n_results  INTEGER,
  results    TEXT CHECK (results IS NULL OR json_valid(results)),
  laps       TEXT CHECK (laps IS NULL OR json_valid(laps)),
  out_chars  INTEGER
) STRICT;
CREATE INDEX IF NOT EXISTS usage_calls_at ON usage_calls (at);
CREATE INDEX IF NOT EXISTS usage_calls_tool ON usage_calls (tool, at);";

fn db() -> Option<&'static Db> {
    static DB: OnceLock<Option<Db>> = OnceLock::new();
    DB.get_or_init(|| Db::open(&store::data_dir().join("store.db"), SCHEMA, 1).map_err(|e| eprintln!("usage log: {e}")).ok()).as_ref()
}

#[derive(Default)]
pub struct Call {
    pub source: &'static str,
    pub tool: String,
    pub client: String,
    pub conn: String,
    pub cwd: String,
    pub scope: String,
    pub args: Value,
    pub ms: f64,
    pub ok: bool,
    pub error: Option<String>,
    pub n_results: Option<i64>,
    pub results: Option<Value>,
    pub out_chars: usize,
}

thread_local! {
    static NOTE: std::cell::RefCell<Option<(i64, Value)>> = const { std::cell::RefCell::new(None) };
}

/// What a tool found, for its log row (0.21's `usage.note`): the count and [[id, path], …]. A tool call runs on one
/// thread, so the row that the call logs next takes it.
pub fn note(n_results: i64, results: Value) {
    NOTE.with(|n| *n.borrow_mut() = Some((n_results, results)));
}

pub fn take_note() -> Option<(i64, Value)> {
    NOTE.with(|n| n.borrow_mut().take())
}

fn text(s: &str) -> Option<&str> {
    (!s.is_empty()).then_some(s)
}

pub fn record(c: Call) {
    let Some(db) = db() else { return };
    let _ = db.tx(|t| t.execute(
        "INSERT INTO usage_calls (at, source, tool, client, conn, cwd, scope, args, ms, ok, error, n_results, results, out_chars) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)",
        store::rusqlite::params![store::now() as i64, c.source, c.tool, text(&c.client), text(&c.conn), text(&c.cwd), text(&c.scope),
            (!c.args.is_null()).then(|| c.args.to_string()), c.ms, c.ok as i64, c.error, c.n_results,
            c.results.map(|r| r.to_string()), c.out_chars as i64]));
}
