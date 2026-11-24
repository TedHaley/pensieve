//! The parts that every crate of Pensieve shares: the data folder, the time, and SQLite files in WAL mode. Each crate
//! owns its own tables: it passes its schema to `Db::open` with a version, and the schema runs again when the version
//! goes up. A schema must be idempotent (`CREATE TABLE IF NOT EXISTS`).

use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub use rusqlite;
use rusqlite::{Connection, OpenFlags};

const BUSY: Duration = Duration::from_secs(10);

/// `~/.pensieve`, or `$PENSIEVE_DATA` when it is set (tests and benchmarks use a copy).
pub fn data_dir() -> PathBuf {
    let d = std::env::var_os("PENSIEVE_DATA").map(PathBuf::from)
        .unwrap_or_else(|| home().join(".pensieve"));
    let _ = std::fs::create_dir_all(&d);
    d
}

pub fn home() -> PathBuf {
    PathBuf::from(std::env::var("HOME").unwrap_or_default())
}

/// Seconds since 1970, UTC.
pub fn now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// One SQLite file: one connection that writes, behind a lock, and a new read-only connection for each reader.
/// In WAL mode readers never wait for the writer.
pub struct Db {
    path: PathBuf,
    writer: Mutex<Connection>,
}

fn setup(c: &Connection) -> rusqlite::Result<()> {
    c.busy_timeout(BUSY)?;
    c.pragma_update(None, "foreign_keys", "ON")?;
    c.pragma_update(None, "synchronous", "NORMAL")
}

impl Db {
    /// Open (or make) `path`, and run `schema` when the file's `user_version` is less than `version`.
    pub fn open(path: &Path, schema: &str, version: i64) -> rusqlite::Result<Db> {
        if let Some(dir) = path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let c = Connection::open(path)?;
        setup(&c)?;
        let mode: String = c.query_row("PRAGMA journal_mode = WAL", [], |r| r.get(0))?;
        debug_assert_eq!(mode.to_lowercase(), "wal");
        let have: i64 = c.query_row("PRAGMA user_version", [], |r| r.get(0))?;
        if have < version {
            c.execute_batch(&format!("BEGIN; {schema}; PRAGMA user_version = {version}; COMMIT;"))?;
        }
        Ok(Db { path: path.to_path_buf(), writer: Mutex::new(c) })
    }

    /// An in-memory file, for tests.
    pub fn memory(schema: &str) -> rusqlite::Result<Db> {
        let c = Connection::open_in_memory()?;
        c.execute_batch(schema)?;
        Ok(Db { path: PathBuf::new(), writer: Mutex::new(c) })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// The writer's connection. Hold it only for one block of statements.
    pub fn write(&self) -> MutexGuard<'_, Connection> {
        self.writer.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Run `f` in one transaction on the writer: all of its changes commit, or none do.
    pub fn tx<T>(&self, f: impl FnOnce(&rusqlite::Transaction) -> rusqlite::Result<T>) -> rusqlite::Result<T> {
        let mut c = self.write();
        let t = c.transaction()?;
        let out = f(&t)?;
        t.commit()?;
        Ok(out)
    }

    /// A read-only connection of its own. An in-memory Db reads through the writer instead.
    pub fn read<T>(&self, f: impl FnOnce(&Connection) -> rusqlite::Result<T>) -> rusqlite::Result<T> {
        if self.path.as_os_str().is_empty() {
            return f(&self.write());
        }
        let c = Connection::open_with_flags(&self.path, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX)?;
        setup(&c)?;
        f(&c)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn runs_the_schema_once_per_version() {
        let dir = std::env::temp_dir().join(format!("store-test-{}", std::process::id()));
        let p = dir.join("t.db");
        let schema = "CREATE TABLE IF NOT EXISTS t (k TEXT PRIMARY KEY, v INTEGER NOT NULL) STRICT";
        let db = Db::open(&p, schema, 1).unwrap();
        db.tx(|t| t.execute("INSERT INTO t VALUES ('a', 1)", [])).unwrap();
        drop(db);
        let db = Db::open(&p, schema, 1).unwrap();
        assert_eq!(db.read(|c| c.query_row("SELECT v FROM t", [], |r| r.get::<_, i64>(0))).unwrap(), 1);
        let _ = std::fs::remove_dir_all(dir);
    }
}
