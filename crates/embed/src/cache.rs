//! A vector for each distinct text that was embedded, so that the same text is never embedded twice (0.21's
//! `embedcache.py`). The key is the sha1 of the exact text that the model reads (after `model_text`: the title and
//! the format are part of it) and the model's name. Vectors are float16, as in 0.21's chunk tables.
//!
//! The table is `embed_cache`, in the file that the app gives (0.21 keeps it in index.db). 0.21's `fill_from_v1`
//! (a migration from v1 databases) is not ported: `import` copies the rows of 0.21's own cache instead.

use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use rusqlite::{OpenFlags, params_from_iter};
use sha1::{Digest, Sha1};
use store::{Db, rusqlite};

use crate::{Embedder, MODEL};

pub const SCHEMA: &str = "CREATE TABLE IF NOT EXISTS embed_cache (
  hash   TEXT NOT NULL,
  model  TEXT NOT NULL,
  vec    BLOB NOT NULL,
  PRIMARY KEY (hash, model)
) STRICT";
/// Keys per SELECT: below SQLite's limit on bound parameters.
const PART: usize = 500;

/// The key of a model text: its sha1, in hex.
pub fn key(text: &str) -> String {
    let d = Sha1::digest(text.as_bytes());
    d.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn to_f16(v: &[f32]) -> Vec<u8> {
    v.iter().flat_map(|x| half::f16::from_f32(*x).to_le_bytes()).collect()
}

pub fn from_f16(b: &[u8]) -> Vec<f32> {
    b.chunks_exact(2).map(|c| half::f16::from_le_bytes([c[0], c[1]]).to_f32()).collect()
}

pub struct EmbedCache {
    db: Arc<Db>,
    /// Texts found and not found by `embed`, since start.
    pub hits: AtomicU64,
    pub misses: AtomicU64,
}

impl EmbedCache {
    /// The cache in `db`; makes its table when it is not there.
    pub fn new(db: Arc<Db>) -> rusqlite::Result<EmbedCache> {
        db.write().execute_batch(SCHEMA)?;
        Ok(EmbedCache { db, hits: AtomicU64::new(0), misses: AtomicU64::new(0) })
    }

    /// The cache in its own file at `path`.
    pub fn open(path: &Path) -> rusqlite::Result<EmbedCache> {
        EmbedCache::new(Arc::new(Db::open(path, SCHEMA, 1)?))
    }

    /// hash -> vector, for the hashes that are in the cache.
    pub fn get(&self, model: &str, hashes: &[String]) -> rusqlite::Result<std::collections::HashMap<String, Vec<f32>>> {
        let mut uniq: Vec<&String> = hashes.iter().collect();
        uniq.sort();
        uniq.dedup();
        let mut out = std::collections::HashMap::new();
        for part in uniq.chunks(PART) {
            let sql = format!("SELECT hash, vec FROM embed_cache WHERE model=? AND hash IN ({})", vec!["?"; part.len()].join(","));
            self.db.read(|c| {
                let mut st = c.prepare(&sql)?;
                let args = std::iter::once(model).chain(part.iter().map(|h| h.as_str()));
                let rows = st.query_map(params_from_iter(args), |r| Ok((r.get::<_, String>(0)?, r.get::<_, Vec<u8>>(1)?)))?;
                for r in rows {
                    let (h, v) = r?;
                    out.insert(h, from_f16(&v));
                }
                Ok(())
            })?;
        }
        Ok(out)
    }

    /// Store (hash, vector) pairs. A hash that is there already keeps its vector.
    pub fn put(&self, model: &str, items: &[(String, Vec<f32>)]) -> rusqlite::Result<()> {
        if items.is_empty() {
            return Ok(());
        }
        self.db.tx(|t| {
            let mut st = t.prepare("INSERT OR IGNORE INTO embed_cache(hash, model, vec) VALUES(?,?,?)")?;
            for (h, v) in items {
                st.execute(rusqlite::params![h, model, to_f16(v)])?;
            }
            Ok(())
        })
    }

    pub fn count(&self, model: Option<&str>) -> rusqlite::Result<i64> {
        self.db.read(|c| match model {
            Some(m) => c.query_row("SELECT COUNT(*) FROM embed_cache WHERE model=?", [m], |r| r.get(0)),
            None => c.query_row("SELECT COUNT(*) FROM embed_cache", [], |r| r.get(0)),
        })
    }

    /// Copy the rows of another file's `embed_cache` (0.21's index.db), opened read-only. Returns the rows read.
    pub fn import(&self, src: &Path) -> rusqlite::Result<usize> {
        let uri = format!("file:{}?mode=ro", src.display());
        let c = rusqlite::Connection::open_with_flags(uri, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI)?;
        let mut st = c.prepare("SELECT hash, model, vec FROM embed_cache")?;
        let mut rows = st.query([])?;
        let mut n = 0;
        let mut batch: Vec<(String, String, Vec<u8>)> = Vec::new();
        let flush = |batch: &mut Vec<(String, String, Vec<u8>)>| {
            self.db.tx(|t| {
                let mut ins = t.prepare("INSERT OR IGNORE INTO embed_cache(hash, model, vec) VALUES(?,?,?)")?;
                for (h, m, v) in batch.iter() {
                    ins.execute(rusqlite::params![h, m, v])?;
                }
                Ok(())
            })?;
            batch.clear();
            Ok::<_, rusqlite::Error>(())
        };
        while let Some(r) = rows.next()? {
            batch.push((r.get(0)?, r.get(1)?, r.get(2)?));
            n += 1;
            if batch.len() >= 5000 {
                flush(&mut batch)?;
            }
        }
        flush(&mut batch)?;
        Ok(n)
    }
}

/// Vectors for model texts (`Embedder::model_text`), each distinct text embedded once: a text in the cache, or
/// twice in this call, is not embedded again (0.21's `indexer.embed` for indexing). Vectors that come from the
/// cache went through float16, as in 0.21. Without a cache every text is embedded.
pub fn embed(e: &Embedder, cache: Option<&EmbedCache>, texts: &[String]) -> anyhow::Result<Vec<Vec<f32>>> {
    let keys: Vec<String> = texts.iter().map(|t| key(t)).collect();
    let have = match cache {
        Some(c) => c.get(MODEL, &keys).unwrap_or_default(),
        None => Default::default(),
    };
    let mut first: std::collections::HashMap<&str, usize> = std::collections::HashMap::new();
    let mut todo = Vec::new();
    for (i, k) in keys.iter().enumerate() {
        if !have.contains_key(k) && !first.contains_key(k.as_str()) {
            first.insert(k, i);
            todo.push(i);
        }
    }
    if let Some(c) = cache {
        c.hits.fetch_add((texts.len() - todo.len()) as u64, Ordering::Relaxed);
        c.misses.fetch_add(todo.len() as u64, Ordering::Relaxed);
    }
    let made = e.embed_model_texts(&todo.iter().map(|&i| texts[i].clone()).collect::<Vec<_>>())?;
    if let Some(c) = cache {
        let items: Vec<(String, Vec<f32>)> = todo.iter().zip(&made).map(|(&i, v)| (keys[i].clone(), v.clone())).collect();
        let _ = c.put(MODEL, &items);
    }
    let fresh: std::collections::HashMap<usize, &Vec<f32>> = todo.iter().copied().zip(&made).collect();
    Ok(keys.iter().map(|k| match have.get(k) {
        Some(v) => v.clone(),
        None => fresh[&first[k.as_str()]].clone(),
    }).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys_are_the_sha1_of_the_text() {
        assert_eq!(key(""), "da39a3ee5e6b4b0d3255bfef95601890afd80709");
        assert_eq!(key("title: none | text: x").len(), 40);
    }

    #[test]
    fn a_vector_round_trips_through_float16_and_keeps_the_first_put() {
        let c = EmbedCache::new(Arc::new(Db::memory(SCHEMA).unwrap())).unwrap();
        c.put("m", &[("a".into(), vec![0.5, -0.25, 1.0 / 3.0])]).unwrap();
        c.put("m", &[("a".into(), vec![9.0, 9.0, 9.0])]).unwrap();
        let got = c.get("m", &["a".into(), "b".into(), "a".into()]).unwrap();
        assert_eq!(got.len(), 1);
        assert_eq!(got["a"][..2], [0.5, -0.25]);
        assert!((got["a"][2] - 1.0 / 3.0).abs() < 1e-3);
        assert_eq!(c.count(Some("m")).unwrap(), 1);
        assert_eq!(c.count(Some("other")).unwrap(), 0);
    }
}
