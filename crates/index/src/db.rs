//! index.db: the tables of sessions, files, code and collected documents, their word indexes, and the derived
//! tables beside them. The definitions are 0.21's, column for column, so a copy of 0.21's index.db opens as is.

use store::rusqlite::{self, Connection, OptionalExtension, functions::FunctionFlags};

use crate::text::{code_tokens, repo_token};

pub const VERSION: i64 = 6;

/// Meta values that say which word index definitions the file has (0.21's `search.FTS_VERSION`, `WORDS_VERSION`).
pub const FTS_VERSION: &str = "words-v1";
pub const WORDS_VERSION: &str = "words-v4";

pub const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS doc_chunks (
  id      INTEGER PRIMARY KEY,
  doc_id  INTEGER NOT NULL,
  idx     INTEGER NOT NULL,
  text    TEXT NOT NULL,
  vec     BLOB NOT NULL,
  hash    TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS doc_chunks_doc ON doc_chunks (doc_id, idx);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_docs USING fts5 (text, content='doc_chunks', content_rowid='id',
                                                       tokenize='unicode61');
CREATE TABLE IF NOT EXISTS doc_index_state (
  doc_id  INTEGER PRIMARY KEY,
  hash    TEXT NOT NULL,
  at      INTEGER NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value BLOB NOT NULL, expires_at INTEGER NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS session_usage (
  session_id      TEXT NOT NULL,
  hour            INTEGER NOT NULL,
  model           TEXT NOT NULL,
  calls           INTEGER NOT NULL CHECK (calls >= 0),
  input           INTEGER NOT NULL CHECK (input >= 0),
  output          INTEGER NOT NULL CHECK (output >= 0),
  cache_write     INTEGER NOT NULL CHECK (cache_write >= 0),
  cache_read      INTEGER NOT NULL CHECK (cache_read >= 0),
  cache_write_1h  INTEGER NOT NULL CHECK (cache_write_1h >= 0),
  PRIMARY KEY (session_id, hour, model)
) STRICT;
CREATE INDEX IF NOT EXISTS session_usage_hour ON session_usage (hour);

CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, source TEXT, project TEXT, title TEXT, path TEXT,
  mtime REAL, size INTEGER, started TEXT, updated TEXT, n_chunks INTEGER,
  x REAL, y REAL, z REAL, cluster INTEGER, project_name TEXT, summary TEXT, tags TEXT);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, idx INTEGER,
  hash TEXT, text TEXT, vec BLOB, x REAL, y REAL, z REAL, summary TEXT);
CREATE INDEX IF NOT EXISTS chunks_sess ON chunks(session_id, idx);
CREATE TABLE IF NOT EXISTS topics(id INTEGER PRIMARY KEY, name TEXT, description TEXT, keywords TEXT, members TEXT);
CREATE TABLE IF NOT EXISTS session_files(session_id TEXT, path TEXT, edited INTEGER, PRIMARY KEY(session_id, path));
-- Thoughts (crate::thoughts): the exchanges of each session, the parts they form, and the thoughts that join parts.
CREATE TABLE IF NOT EXISTS exchanges(
  session_id TEXT, k INTEGER, ts TEXT, hash TEXT, prompt TEXT, reply TEXT, files TEXT, vec BLOB,
  -- The git branch of the working folder. NULL: the transcript was not read for it yet. '': it names none.
  branch TEXT,
  PRIMARY KEY(session_id, k));
CREATE TABLE IF NOT EXISTS thoughts(
  id TEXT PRIMARY KEY, name TEXT, auto_name TEXT, started TEXT, updated TEXT,
  n_sessions INTEGER, n_exchanges INTEGER);
CREATE TABLE IF NOT EXISTS thought_parts(
  thought_id TEXT, session_id TEXT, a INTEGER, z INTEGER, started TEXT, updated TEXT,
  PRIMARY KEY(session_id, a));
CREATE INDEX IF NOT EXISTS thought_parts_thought ON thought_parts(thought_id);
-- A name that the user gave. It follows the exchange (session_id, k) that its thought started at.
CREATE TABLE IF NOT EXISTS thought_names(session_id TEXT, k INTEGER, name TEXT, PRIMARY KEY(session_id, k));

CREATE TABLE IF NOT EXISTS repos(root TEXT PRIMARY KEY, name TEXT, remote TEXT, head TEXT, indexed REAL);
CREATE TABLE IF NOT EXISTS code_files(repo TEXT, path TEXT, blob TEXT, PRIMARY KEY(repo, path));
CREATE TABLE IF NOT EXISTS code_chunks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT, path TEXT, dir TEXT, start INTEGER, end INTEGER,
  text TEXT, vec BLOB, authors TEXT, last_ts INTEGER, x REAL, y REAL, z REAL, gx REAL, gy REAL, gz REAL, wt TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS cc_repo ON code_chunks(repo, path);
CREATE TABLE IF NOT EXISTS commits(repo TEXT, sha TEXT, ts INTEGER, author TEXT, subject TEXT, PRIMARY KEY(repo, sha));
CREATE TABLE IF NOT EXISTS checkout_files(repo TEXT, wt TEXT, path TEXT, blob TEXT, PRIMARY KEY(repo, wt, path));
CREATE TABLE IF NOT EXISTS worktrees(path TEXT PRIMARY KEY, repo TEXT, branch TEXT);
CREATE INDEX IF NOT EXISTS cc_files ON code_chunks(wt, repo, path, authors, last_ts);

CREATE TABLE IF NOT EXISTS files(
  path TEXT PRIMARY KEY, root TEXT, name TEXT, ext TEXT, kind TEXT, mtime REAL, size INTEGER,
  n_chunks INTEGER, x REAL, y REAL, z REAL, cluster INTEGER, author TEXT, author_source TEXT, chars INTEGER, mode TEXT);
CREATE TABLE IF NOT EXISTS file_use(
  path TEXT PRIMARY KEY, opens INTEGER NOT NULL DEFAULT 0, last_open REAL, edits INTEGER NOT NULL DEFAULT 0, last_edit REAL);
CREATE TABLE IF NOT EXISTS file_chunks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, idx INTEGER, start INTEGER, end INTEGER, text TEXT, vec BLOB);
CREATE INDEX IF NOT EXISTS fc_path ON file_chunks(path);
CREATE TABLE IF NOT EXISTS data_points(id TEXT PRIMARY KEY, x REAL, y REAL, z REAL, cluster INTEGER);
CREATE TABLE IF NOT EXISTS data_topics(id INTEGER PRIMARY KEY, sig TEXT, name TEXT, description TEXT, keywords TEXT, reps TEXT);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_files USING fts5(text, content='file_chunks', content_rowid='id',
  tokenize='unicode61 remove_diacritics 0');
CREATE TRIGGER IF NOT EXISTS fts_files_ai AFTER INSERT ON file_chunks BEGIN
  INSERT INTO fts_files(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER IF NOT EXISTS fts_files_ad AFTER DELETE ON file_chunks BEGIN
  INSERT INTO fts_files(fts_files, rowid, text) VALUES('delete', old.id, old.text); END;
CREATE TRIGGER IF NOT EXISTS fts_files_au AFTER UPDATE OF text ON file_chunks BEGIN
  INSERT INTO fts_files(fts_files, rowid, text) VALUES('delete', old.id, old.text);
  INSERT INTO fts_files(rowid, text) VALUES (new.id, new.text); END;
CREATE VIRTUAL TABLE IF NOT EXISTS fts_sessions USING fts5(text, content='chunks', content_rowid='id',
  tokenize='unicode61 remove_diacritics 0');
CREATE TRIGGER IF NOT EXISTS fts_sessions_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO fts_sessions(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER IF NOT EXISTS fts_sessions_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO fts_sessions(fts_sessions, rowid, text) VALUES('delete', old.id, old.text); END;
CREATE TRIGGER IF NOT EXISTS fts_sessions_au AFTER UPDATE OF text ON chunks BEGIN
  INSERT INTO fts_sessions(fts_sessions, rowid, text) VALUES('delete', old.id, old.text);
  INSERT INTO fts_sessions(rowid, text) VALUES (new.id, new.text); END;
CREATE VIRTUAL TABLE IF NOT EXISTS fts_code_words USING fts5(words, repo, content='', contentless_delete=1);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_code_words_vocab USING fts5vocab(fts_code_words, 'row');
CREATE TRIGGER IF NOT EXISTS fts_code_words_ai AFTER INSERT ON code_chunks BEGIN
  INSERT INTO fts_code_words(rowid, words, repo) VALUES (new.id, code_tokens(new.path || ' ' || new.text), repo_token(new.repo)); END;
CREATE TRIGGER IF NOT EXISTS fts_code_words_ad AFTER DELETE ON code_chunks BEGIN
  DELETE FROM fts_code_words WHERE rowid=old.id; END;
CREATE TRIGGER IF NOT EXISTS fts_code_words_au AFTER UPDATE OF text ON code_chunks BEGIN
  DELETE FROM fts_code_words WHERE rowid=old.id;
  INSERT INTO fts_code_words(rowid, words, repo) VALUES (new.id, code_tokens(new.path || ' ' || new.text), repo_token(new.repo)); END;
INSERT OR IGNORE INTO meta VALUES ('fts', 'words-v1');
INSERT OR IGNORE INTO meta VALUES ('fts_words', 'words-v4')
"#;

/// The SQL functions that the code word index's triggers call. Every connection that writes code chunks needs them.
pub fn functions(c: &Connection) -> rusqlite::Result<()> {
    let f = FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC;
    c.create_scalar_function("code_tokens", 1, f, |ctx| {
        Ok(code_tokens(&ctx.get::<Option<String>>(0)?.unwrap_or_default()))
    })?;
    c.create_scalar_function("repo_token", 1, f, |ctx| {
        Ok(repo_token(&ctx.get::<Option<String>>(0)?.unwrap_or_default()))
    })
}

pub fn meta(c: &Connection, key: &str) -> Option<String> {
    c.query_row("SELECT value FROM meta WHERE key = ?", [key], |r| r.get(0)).optional().ok().flatten()
}

pub fn set_meta(c: &Connection, key: &str, value: &str) -> rusqlite::Result<()> {
    c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", [key, value]).map(|_| ())
}

/// `?, ?, ...` for an IN list of `n` values (`NULL` for none, so that the list matches nothing).
pub fn marks(n: usize) -> String {
    if n == 0 { "NULL".into() } else { vec!["?"; n].join(",") }
}
