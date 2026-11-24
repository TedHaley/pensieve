//! The search index of the documents that connections collect: chunks of each document's text in index.db
//! (`doc_chunks`, its word index `fts_docs`) and their vectors. The documents themselves live in the connect crate's
//! store.db: the app passes them in (`DocInput`), and what a search hit shows of a document is kept beside its
//! chunks in `doc_meta`.
//!
//! `docs_sync` compares each document's hash with the hash it had when it was chunked (`doc_index_state`) and chunks
//! again only what changed. A chunk whose text did not change keeps its row and its vector. Search hits are
//! `doc:<id>`.

use std::collections::{HashMap, HashSet};
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};

use serde_json::{Value, json};
use store::rusqlite::{self, Connection, params};

use crate::db::marks;
use crate::files::chunk_text;
use crate::text::sha1_hex;
use crate::{Index, vectors};

const META: &str = "CREATE TABLE IF NOT EXISTS doc_meta (
  doc_id     INTEGER PRIMARY KEY,
  source_id  TEXT NOT NULL,
  source     TEXT NOT NULL,
  key        TEXT NOT NULL,
  kind       TEXT NOT NULL,
  title      TEXT NOT NULL,
  url        TEXT,
  status     TEXT,
  private    INTEGER NOT NULL DEFAULT 0,
  time       REAL
) STRICT;
CREATE INDEX IF NOT EXISTS doc_meta_source ON doc_meta (source_id);";

/// One collected document, as store.db holds it (0.21 `models.Document`).
#[derive(Debug, Clone, Default)]
pub struct DocInput {
    pub id: i64,
    pub source_id: String,
    /// The source's name, as Sources shows it.
    pub source_name: String,
    pub key: String,
    pub kind: String,
    pub title: String,
    pub url: Option<String>,
    pub status: Option<String>,
    pub private: bool,
    /// updated_at, else seen_at.
    pub time: Option<f64>,
    pub text: String,
    pub hash: String,
}

/// What a search hit shows of a document chunk (0.21 `DocIndex.rows`).
#[derive(Debug, Clone)]
pub struct DocRow {
    pub id: String,
    pub doc: i64,
    pub text: String,
    pub vec: Vec<f32>,
    pub title: String,
    pub subtitle: String,
    pub url: Option<String>,
    pub private: bool,
    pub source: String,
    pub mtime: Option<f64>,
    pub key: String,
}

#[derive(Default)]
pub struct State {
    ready: AtomicBool,
    pub progress: Mutex<Option<Value>>,
}

impl Index {
    fn docs_ready(&self) {
        if !self.docs.ready.swap(true, Ordering::SeqCst) {
            let _ = self.db.write().execute_batch(META);
        }
    }

    /// Chunk and embed the documents that changed and drop the ones that left. `want`: (id, hash) of every
    /// document of the sources that are indexed now; `fetch`: those documents in full, by id. With `budget`
    /// (seconds), stops after that long; the next call goes on. Returns the documents touched. Blocking.
    pub fn docs_sync(&self, want: &[(i64, String)], fetch: &dyn Fn(&[i64]) -> Vec<DocInput>, budget: Option<f64>,
                     log: &dyn Fn(&str)) -> usize {
        self.docs_ready();
        let end = budget.map(|b| std::time::Instant::now() + std::time::Duration::from_secs_f64(b));
        let dim = self.embedder.dim() as i64 * 2;
        let wrong: Vec<i64> = self.db.read(|c| {
            let mut st = c.prepare("SELECT DISTINCT doc_id FROM doc_chunks WHERE length(vec) != ?")?;
            st.query_map([dim], |r| r.get(0))?.collect()
        }).unwrap_or_default();
        if !wrong.is_empty() {
            log(&format!("document index: {} documents have vectors of another width; indexing them again", wrong.len()));
            self.docs_forget(&wrong);
        }
        let have: HashMap<i64, String> = self.db.read(|c| {
            let mut st = c.prepare("SELECT doc_id, hash FROM doc_index_state")?;
            st.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect()
        }).unwrap_or_default();
        let wanted: HashMap<i64, &str> = want.iter().map(|(i, h)| (*i, h.as_str())).collect();
        let gone: Vec<i64> = have.keys().filter(|i| !wanted.contains_key(i)).copied().collect();
        let todo: Vec<i64> = want.iter().filter(|(i, h)| have.get(i) != Some(h)).map(|(i, _)| *i).collect();
        let mut touched = 0;
        if !gone.is_empty() {
            self.docs_forget(&gone);
            touched += gone.len();
        }
        let cfg = self.config();
        let (size, cap) = (cfg.int("chunk_chars").max(1) as usize, cfg.int("max_chunks").max(0) as usize);
        for (n, part) in todo.chunks(32).enumerate() {
            if end.is_some_and(|e| std::time::Instant::now() > e) {
                break;
            }
            *self.docs.progress.lock().unwrap_or_else(|e| e.into_inner()) =
                Some(json!({"name": "documents", "root": "", "done": n * 32, "total": todo.len()}));
            for d in fetch(part) {
                let chunks = if d.text.trim().is_empty() { Vec::new() } else { chunk_text(&d.text, size, cap) };
                if let Err(e) = self.index_doc(&d, &chunks) {
                    log(&format!("document {}: {e}", d.id));
                    continue;
                }
                touched += 1;
            }
        }
        *self.docs.progress.lock().unwrap_or_else(|e| e.into_inner()) = None;
        if touched > 0 {
            log(&format!("indexed {touched} documents"));
        }
        touched
    }

    fn index_doc(&self, d: &DocInput, chunks: &[(usize, usize, String)]) -> rusqlite::Result<()> {
        let texts: Vec<String> = chunks.iter().map(|c| c.2.clone()).collect();
        let hashes: Vec<String> = texts.iter().map(|t| sha1_hex(t)).collect();
        let old: HashMap<(i64, String), i64> = self.db.read(|c| {
            let mut st = c.prepare("SELECT id, idx, hash FROM doc_chunks WHERE doc_id = ? ORDER BY idx")?;
            st.query_map([d.id], |r| Ok(((r.get(1)?, r.get(2)?), r.get(0)?)))?.collect()
        })?;
        let keep: HashSet<(i64, String)> = hashes.iter().enumerate().map(|(i, h)| (i as i64, h.clone())).collect();
        let drop: Vec<i64> = old.iter().filter(|(k, _)| !keep.contains(*k)).map(|(_, c)| *c).collect();
        let new: Vec<usize> = (0..hashes.len()).filter(|&i| !old.contains_key(&(i as i64, hashes[i].clone()))).collect();
        let vecs = if new.is_empty() {
            Vec::new()
        } else {
            let t: Vec<String> = new.iter().map(|&i| texts[i].clone()).collect();
            self.embed_docs(&t, Some(&vec![d.title.clone(); t.len()]))
        };
        let ids = self.db.tx(|t| {
            drop_chunks(t, &drop)?;
            let mut ids = Vec::new();
            for (&i, v) in new.iter().zip(&vecs) {
                t.execute("INSERT INTO doc_chunks (doc_id, idx, text, vec, hash) VALUES (?, ?, ?, ?, ?)",
                          params![d.id, i as i64, texts[i], vectors::to_blob(v), hashes[i]])?;
                let cid = t.last_insert_rowid();
                t.execute("INSERT INTO fts_docs (rowid, text) VALUES (?, ?)", params![cid, texts[i]])?;
                ids.push(cid);
            }
            t.execute("INSERT OR REPLACE INTO doc_index_state VALUES (?, ?, ?)", params![d.id, d.hash, store::now() as i64])?;
            t.execute("INSERT OR REPLACE INTO doc_meta VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      params![d.id, d.source_id, d.source_name, d.key, d.kind, d.title, d.url, d.status, d.private as i64, d.time])?;
            Ok(ids)
        })?;
        self.doc_vecs.remove_many(&drop);
        self.doc_vecs.add(&ids.into_iter().zip(vecs).collect::<Vec<_>>());
        Ok(())
    }

    /// Drop every chunk of these documents and their state. Returns the dropped chunk ids.
    pub fn docs_forget(&self, doc_ids: &[i64]) -> Vec<i64> {
        self.docs_ready();
        let out = self.db.tx(|t| {
            let mut out = Vec::new();
            for part in doc_ids.chunks(500) {
                let ids: Vec<i64> = {
                    let mut st = t.prepare(&format!("SELECT id FROM doc_chunks WHERE doc_id IN ({})", marks(part.len())))?;
                    st.query_map(rusqlite::params_from_iter(part), |r| r.get(0))?.collect::<rusqlite::Result<_>>()?
                };
                drop_chunks(t, &ids)?;
                t.execute(&format!("DELETE FROM doc_index_state WHERE doc_id IN ({})", marks(part.len())), rusqlite::params_from_iter(part))?;
                t.execute(&format!("DELETE FROM doc_meta WHERE doc_id IN ({})", marks(part.len())), rusqlite::params_from_iter(part))?;
                out.extend(ids);
            }
            Ok(out)
        }).unwrap_or_default();
        self.doc_vecs.remove_many(&out);
        out
    }

    /// The chunk ids of the documents of these sources, for a search within a scope.
    pub fn docs_allow(&self, sources: &[String]) -> Vec<i64> {
        self.docs_ready();
        self.db.read(|c| {
            let mut st = c.prepare(&format!("SELECT c.id FROM doc_chunks c JOIN doc_meta m ON m.doc_id = c.doc_id \
                                             WHERE m.source_id IN ({})", marks(sources.len())))?;
            st.query_map(rusqlite::params_from_iter(sources), |r| r.get(0))?.collect()
        }).unwrap_or_default()
    }

    /// (chunk id, cosine) of the chunks nearest to `qv`. `allow`: only these chunk ids.
    pub fn docs_search(&self, qv: &[f32], k: usize, allow: Option<&[i64]>) -> Vec<(i64, f32)> {
        if allow.is_some_and(|a| a.is_empty()) {
            return Vec::new();
        }
        let k = allow.map(|a| k.min(a.len())).unwrap_or(k);
        self.doc_vecs.search(qv, k, allow)
    }

    /// (chunk id, bm25) of the chunks that match an FTS5 query, best first (a lower bm25 is better).
    pub fn docs_match(&self, fts_query: &str, limit: usize) -> Vec<(i64, f64)> {
        self.db.read(|c| {
            let mut st = c.prepare("SELECT rowid, bm25(fts_docs) FROM fts_docs WHERE fts_docs MATCH ? ORDER BY 2 LIMIT ?")?;
            st.query_map(params![fts_query, limit as i64], |r| Ok((r.get(0)?, r.get(1)?)))?.collect()
        }).unwrap_or_default()
    }

    /// Chunks whose text holds every one of `words` (short words that FTS cannot match alone).
    pub fn docs_contains(&self, words: &[String], limit: usize) -> Vec<i64> {
        let cond = vec!["instr(lower(text), ?) > 0"; words.len().max(1)].join(" AND ");
        let mut args: Vec<Value> = words.iter().map(|w| json!(w.to_lowercase())).collect();
        if words.is_empty() {
            args.push(json!(""));
        }
        self.db.read(|c| {
            let mut st = c.prepare(&format!("SELECT id FROM doc_chunks WHERE {cond} LIMIT ?"))?;
            let mut p: Vec<Box<dyn rusqlite::ToSql>> = args.iter().map(|a| Box::new(a.as_str().unwrap_or("").to_string()) as Box<dyn rusqlite::ToSql>).collect();
            p.push(Box::new(limit as i64));
            st.query_map(rusqlite::params_from_iter(p.iter()), |r| r.get(0))?.collect()
        }).unwrap_or_default()
    }

    /// For each chunk id: the chunk and its document, as a search hit shows it.
    pub fn docs_rows(&self, ids: &[i64]) -> HashMap<i64, DocRow> {
        self.docs_ready();
        let mut out = HashMap::new();
        let _ = self.db.read(|c| {
            for part in ids.chunks(900) {
                rows_into(c, part, &mut out)?;
            }
            Ok(())
        });
        out
    }

    /// The documents nearest in meaning to one document (the mean of its chunk vectors): {id, title, subtitle, url,
    /// score}.
    pub fn docs_similar(&self, doc_id: i64, k: usize) -> Vec<Value> {
        let vecs: Vec<Vec<f32>> = self.db.read(|c| {
            let mut st = c.prepare("SELECT vec FROM doc_chunks WHERE doc_id = ?")?;
            st.query_map([doc_id], |r| r.get::<_, Vec<u8>>(0))?.map(|b| b.map(|b| vectors::from_blob(&b))).collect()
        }).unwrap_or_default();
        let Some(dim) = vecs.first().map(|v| v.len()) else { return Vec::new() };
        let mut q = vec![0f32; dim];
        for v in &vecs {
            for (a, b) in q.iter_mut().zip(v) {
                *a += b / vecs.len() as f32;
            }
        }
        let q = vectors::unit(&q);
        let hits = self.docs_search(&q, k * 4, None);
        let rows = self.docs_rows(&hits.iter().map(|h| h.0).collect::<Vec<_>>());
        let mut seen = HashSet::new();
        let mut out = Vec::new();
        for (cid, s) in hits {
            let Some(r) = rows.get(&cid) else { continue };
            if r.doc == doc_id || !seen.insert(r.id.clone()) {
                continue;
            }
            out.push(json!({"id": r.id, "title": r.title, "subtitle": r.subtitle, "url": r.url,
                            "score": (s as f64 * 1e4).round() / 1e4}));
        }
        out.truncate(k);
        out
    }

    /// The document of a 0.21 v1 connection file (`<data>/connections/<source>/<name>.md`), for ids that agents
    /// kept from before storage v2. `data_dir`: 0.21's data folder. Matches the key as the file name.
    pub fn docs_resolve_path(&self, path: &str, data_dir: &std::path::Path) -> Option<i64> {
        let p = std::path::Path::new(path);
        if p.extension().and_then(|e| e.to_str()) != Some("md") || p.parent()?.parent()? != data_dir.join("connections") {
            return None;
        }
        let sid = p.parent()?.file_name()?.to_string_lossy().into_owned();
        let stem = p.file_stem()?.to_string_lossy().into_owned();
        self.docs_ready();
        self.db.read(|c| c.query_row("SELECT doc_id FROM doc_meta WHERE source_id = ? AND key = ?", [&sid, &stem], |r| r.get(0)))
            .ok()
    }

    pub fn docs_progress(&self) -> Option<Value> {
        self.docs.progress.lock().unwrap_or_else(|e| e.into_inner()).clone()
    }
}

fn rows_into(c: &Connection, part: &[i64], out: &mut HashMap<i64, DocRow>) -> rusqlite::Result<()> {
    let mut st = c.prepare(&format!(
        "SELECT c.id, c.doc_id, c.text, c.vec, m.title, m.url, m.private, m.source_id, m.source, m.kind, m.status, m.time \
         FROM doc_chunks c JOIN doc_meta m ON m.doc_id = c.doc_id WHERE c.id IN ({})", marks(part.len())))?;
    let rows = st.query_map(rusqlite::params_from_iter(part), |r| {
        let doc: i64 = r.get(1)?;
        let source_id: String = r.get(7)?;
        let name: String = r.get(8)?;
        let kind: String = r.get(9)?;
        let status: Option<String> = r.get(10)?;
        let src = if name.is_empty() { source_id.clone() } else { name };
        let subtitle = [src, kind, status.unwrap_or_default()].into_iter().filter(|x| !x.is_empty()).collect::<Vec<_>>().join(" · ");
        Ok((r.get::<_, i64>(0)?, DocRow {
            id: format!("doc:{doc}"),
            doc,
            text: r.get(2)?,
            vec: vectors::from_blob(&r.get::<_, Vec<u8>>(3)?),
            title: r.get(4)?,
            subtitle,
            url: r.get(5)?,
            private: r.get::<_, i64>(6)? != 0,
            source: source_id,
            mtime: r.get::<_, Option<f64>>(11)?.filter(|t| *t != 0.0),
            key: format!("doc:{doc}"),
        }))
    })?;
    for r in rows {
        let (id, row) = r?;
        out.insert(id, row);
    }
    Ok(())
}

fn drop_chunks(t: &Connection, ids: &[i64]) -> rusqlite::Result<()> {
    for part in ids.chunks(500) {
        let rows: Vec<(i64, String)> = {
            let mut st = t.prepare(&format!("SELECT id, text FROM doc_chunks WHERE id IN ({})", marks(part.len())))?;
            st.query_map(rusqlite::params_from_iter(part), |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<rusqlite::Result<_>>()?
        };
        for (cid, text) in rows {
            t.execute("INSERT INTO fts_docs (fts_docs, rowid, text) VALUES ('delete', ?, ?)", params![cid, text])?;
        }
        t.execute(&format!("DELETE FROM doc_chunks WHERE id IN ({})", marks(part.len())), rusqlite::params_from_iter(part))?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn doc(id: i64, text: &str, hash: &str) -> DocInput {
        DocInput { id, source_id: "connections/wiki".into(), source_name: "Wiki".into(), key: format!("K-{id}"),
                   kind: "page".into(), title: format!("Page {id}"), text: text.into(), hash: hash.into(), ..Default::default() }
    }

    #[test]
    fn chunks_changed_documents_only_and_finds_them() {
        let ix = crate::testing::index();
        let docs = vec![doc(1, "pricing model for enterprise plans", "a"), doc(2, "onboarding checklist for new hires", "b")];
        let want: Vec<(i64, String)> = docs.iter().map(|d| (d.id, d.hash.clone())).collect();
        let fetch = |ids: &[i64]| docs.iter().filter(|d| ids.contains(&d.id)).cloned().collect::<Vec<_>>();
        assert_eq!(ix.docs_sync(&want, &fetch, None, &|_| {}), 2);
        assert_eq!(ix.docs_sync(&want, &fetch, None, &|_| {}), 0);
        let q = ix.embed_query("enterprise pricing");
        let hits = ix.docs_search(&q, 5, None);
        let rows = ix.docs_rows(&[hits[0].0]);
        assert_eq!(rows[&hits[0].0].id, "doc:1");
        assert_eq!(rows[&hits[0].0].subtitle, "Wiki · page");
        assert_eq!(ix.docs_match("\"onboarding\"", 5).len(), 1);
        assert_eq!(ix.docs_contains(&["HIRES".into()], 5).len(), 1);
        assert_eq!(ix.docs_allow(&["connections/wiki".into()]).len(), 2);
        assert_eq!(ix.docs_sync(&want[..1], &fetch, None, &|_| {}), 1);
        assert_eq!(ix.doc_vecs.len(), 1);
        assert_eq!(ix.docs_match("\"onboarding\"", 5).len(), 0);
    }
}
