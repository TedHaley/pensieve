//! The vectors of one kind of chunk (sessions, files, code, documents), in memory: a float16 copy of each vector
//! and a 4-bit code of it. A search scans the 4-bit codes with a table per query, then scores the best candidates
//! again on the float16 copies, so its order is that of the exact dot product. 0.21 kept the same two forms (a
//! turbovec index and float16 arrays); here the chunk table is the only file, and `load` reads it at start.
//!
//! Reads take a read lock and never wait for each other. An add or a remove takes the write lock for the time it
//! takes to copy its vectors in.

use std::collections::{HashMap, HashSet};
use std::sync::RwLock;

use half::f16;
use rayon::prelude::*;
use store::rusqlite::{self, Connection};

/// Candidates per result that the 4-bit scan passes on to the exact score.
const SHORTLIST: usize = 4;
const MIN_SHORTLIST: usize = 200;
/// Below this many allowed vectors, the search scores the allowed ones exactly and skips the scan.
const EXACT_BELOW: usize = 20_000;

#[derive(Default)]
struct Inner {
    ids: Vec<i64>,
    vecs: Vec<f16>,
    /// Two 4-bit codes per byte, `dim / 2` bytes per vector (`dim` is even).
    codes: Vec<u8>,
    /// The step of each vector's codes: a code `c` stands for `(c - 7.5) * step`.
    steps: Vec<f32>,
    pos: HashMap<i64, usize>,
    version: u64,
}

pub struct VecIndex {
    pub dim: usize,
    inner: RwLock<Inner>,
}

/// A float16 BLOB, as the chunk tables store vectors.
pub fn to_blob(v: &[f32]) -> Vec<u8> {
    v.iter().flat_map(|x| f16::from_f32(*x).to_le_bytes()).collect()
}

pub fn from_blob(b: &[u8]) -> Vec<f32> {
    b.chunks_exact(2).map(|p| f16::from_le_bytes([p[0], p[1]]).to_f32()).collect()
}

pub fn dot(a: &[f32], b: &[f32]) -> f32 {
    a.iter().zip(b).map(|(x, y)| x * y).sum()
}

fn dot16(a: &[f16], q: &[f32]) -> f32 {
    a.iter().zip(q).map(|(x, y)| x.to_f32() * y).sum()
}

/// `v` scaled to length 1 (a zero vector stays zero).
pub fn unit(v: &[f32]) -> Vec<f32> {
    let n = dot(v, v).sqrt() + 1e-9;
    v.iter().map(|x| x / n).collect()
}

fn quantize(v: &[f32], out: &mut Vec<u8>) -> f32 {
    let top = v.iter().fold(0f32, |m, x| m.max(x.abs()));
    let step = if top > 0.0 { top / 7.5 } else { 1.0 };
    for p in v.chunks(2) {
        let c = |x: f32| ((x / step + 7.5).round().clamp(0.0, 15.0)) as u8;
        let lo = c(p[0]);
        let hi = p.get(1).map(|x| c(*x)).unwrap_or(8);
        out.push(lo | (hi << 4));
    }
    step
}

impl VecIndex {
    pub fn new(dim: usize) -> VecIndex {
        VecIndex { dim, inner: RwLock::new(Inner::default()) }
    }

    fn r(&self) -> std::sync::RwLockReadGuard<'_, Inner> {
        self.inner.read().unwrap_or_else(|e| e.into_inner())
    }

    fn w(&self) -> std::sync::RwLockWriteGuard<'_, Inner> {
        self.inner.write().unwrap_or_else(|e| e.into_inner())
    }

    /// Read every vector of `table` (columns `id`, `vec`) in place of what the index holds. A row whose BLOB is not
    /// `dim` float16 values is left out. Returns how many vectors it holds.
    pub fn load(&self, c: &Connection, table: &str) -> rusqlite::Result<usize> {
        let mut st = c.prepare(&format!("SELECT id, vec FROM {table}"))?;
        let mut rows = st.query([])?;
        let mut fresh = Inner::default();
        let want = 2 * self.dim;
        while let Some(r) = rows.next()? {
            let id: i64 = r.get(0)?;
            let Ok(blob) = r.get_ref(1)?.as_blob() else { continue };
            if blob.len() != want {
                continue;
            }
            let v = from_blob(blob);
            push(&mut fresh, id, &v);
        }
        let n = fresh.ids.len();
        let mut g = self.w();
        fresh.version = g.version + 1;
        *g = fresh;
        Ok(n)
    }

    /// Add vectors under their chunk ids. An id the index has already gets the new vector.
    pub fn add(&self, items: &[(i64, Vec<f32>)]) {
        let mut g = self.w();
        for (id, v) in items {
            if v.len() != self.dim {
                continue;
            }
            if let Some(&j) = g.pos.get(id) {
                remove_at(&mut g, j, self.dim);
            }
            push(&mut g, *id, v);
        }
        g.version += 1;
    }

    pub fn remove(&self, id: i64) -> bool {
        let mut g = self.w();
        let Some(&j) = g.pos.get(&id) else { return false };
        remove_at(&mut g, j, self.dim);
        g.version += 1;
        true
    }

    pub fn remove_many(&self, ids: &[i64]) -> usize {
        let mut g = self.w();
        let mut n = 0;
        for id in ids {
            if let Some(&j) = g.pos.get(id) {
                remove_at(&mut g, j, self.dim);
                n += 1;
            }
        }
        if n > 0 {
            g.version += 1;
        }
        n
    }

    pub fn contains(&self, id: i64) -> bool {
        self.r().pos.contains_key(&id)
    }

    pub fn len(&self) -> usize {
        self.r().ids.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Changes with every add and remove: a consumer that caches something built from the vectors rebuilds it when
    /// this moved.
    pub fn version(&self) -> u64 {
        self.r().version
    }

    /// The `k` vectors most similar to `q` (dot product), best first, as (chunk id, score). `allow`: only these ids.
    pub fn search(&self, q: &[f32], k: usize, allow: Option<&[i64]>) -> Vec<(i64, f32)> {
        if q.len() != self.dim || k == 0 {
            return Vec::new();
        }
        let g = self.r();
        let dim = self.dim;
        let exact = |rows: &mut dyn Iterator<Item = usize>| -> Vec<(i64, f32)> {
            let rows: Vec<usize> = rows.collect();
            let mut out: Vec<(i64, f32)> =
                rows.par_iter().map(|&j| (g.ids[j], dot16(&g.vecs[j * dim..(j + 1) * dim], q))).collect();
            top(&mut out, k);
            out
        };
        let allowed: Option<HashSet<usize>> = allow.map(|a| a.iter().filter_map(|i| g.pos.get(i).copied()).collect());
        let n_scan = allowed.as_ref().map(|a| a.len()).unwrap_or(g.ids.len());
        if n_scan <= EXACT_BELOW.max(k * SHORTLIST) {
            return match &allowed {
                Some(a) => exact(&mut a.iter().copied()),
                None => exact(&mut (0..g.ids.len())),
            };
        }
        // a table per pair of dimensions: the partial dot product of q with each of the 256 byte values
        let half = dim.div_ceil(2);
        let mut lut = vec![0f32; half * 256];
        for p in 0..half {
            let (a, b) = (q[2 * p], q.get(2 * p + 1).copied().unwrap_or(0.0));
            for byte in 0..256usize {
                let lo = (byte & 15) as f32 - 7.5;
                let hi = (byte >> 4) as f32 - 7.5;
                lut[p * 256 + byte] = a * lo + b * hi;
            }
        }
        let approx = |j: usize| -> f32 {
            let code = &g.codes[j * half..(j + 1) * half];
            let mut s = 0f32;
            for (p, &byte) in code.iter().enumerate() {
                s += lut[p * 256 + byte as usize];
            }
            s * g.steps[j]
        };
        let mut cand: Vec<(usize, f32)> = match &allowed {
            Some(a) => a.par_iter().map(|&j| (j, approx(j))).collect(),
            None => (0..g.ids.len()).into_par_iter().map(|j| (j, approx(j))).collect(),
        };
        let keep = (k * SHORTLIST).max(MIN_SHORTLIST).min(cand.len());
        if keep < cand.len() {
            cand.select_nth_unstable_by(keep, |a, b| b.1.total_cmp(&a.1));
            cand.truncate(keep);
        }
        exact(&mut cand.into_iter().map(|(j, _)| j))
    }

    /// The vector of each id, or None for an id the index does not hold.
    pub fn take(&self, ids: &[i64]) -> Vec<Option<Vec<f32>>> {
        let g = self.r();
        ids.iter()
            .map(|i| g.pos.get(i).map(|&j| g.vecs[j * self.dim..(j + 1) * self.dim].iter().map(|x| x.to_f32()).collect()))
            .collect()
    }

    /// The dot product of `q` with the vector of each id (None for an id the index does not hold).
    pub fn scores(&self, ids: &[i64], q: &[f32]) -> Vec<Option<f32>> {
        let g = self.r();
        ids.iter().map(|i| g.pos.get(i).map(|&j| dot16(&g.vecs[j * self.dim..(j + 1) * self.dim], q))).collect()
    }

    /// Every id and its vector: copies, so that a caller can work on them without the lock.
    pub fn matrix(&self) -> (Vec<i64>, Vec<Vec<f32>>) {
        let g = self.r();
        let v = g.vecs.chunks(self.dim.max(1)).map(|r| r.iter().map(|x| x.to_f32()).collect()).collect();
        (g.ids.clone(), v)
    }

    pub fn ids(&self) -> Vec<i64> {
        self.r().ids.clone()
    }
}

fn push(g: &mut Inner, id: i64, v: &[f32]) {
    g.pos.insert(id, g.ids.len());
    g.ids.push(id);
    g.vecs.extend(v.iter().map(|x| f16::from_f32(*x)));
    let step = quantize(v, &mut g.codes);
    g.steps.push(step);
}

/// Drop row `j`: the last row moves into its place.
fn remove_at(g: &mut Inner, j: usize, dim: usize) {
    let last = g.ids.len() - 1;
    let half = dim.div_ceil(2);
    let id = g.ids[j];
    g.pos.remove(&id);
    if j != last {
        g.ids.swap(j, last);
        g.steps.swap(j, last);
        let (a, b) = g.vecs.split_at_mut(last * dim);
        a[j * dim..(j + 1) * dim].copy_from_slice(&b[..dim]);
        let (a, b) = g.codes.split_at_mut(last * half);
        a[j * half..(j + 1) * half].copy_from_slice(&b[..half]);
        let moved = g.ids[j];
        g.pos.insert(moved, j);
    }
    g.ids.pop();
    g.steps.pop();
    g.vecs.truncate(last * dim);
    g.codes.truncate(last * half);
}

/// Keep the `k` best by score, best first.
pub fn top<T>(v: &mut Vec<(T, f32)>, k: usize) {
    if v.len() > k {
        v.select_nth_unstable_by(k, |a, b| b.1.total_cmp(&a.1));
        v.truncate(k);
    }
    v.sort_by(|a, b| b.1.total_cmp(&a.1));
}

/// The unit mean vector of each key's chunk ids. A key whose chunks have no vector is left out.
pub fn centroids(groups: &[(String, Vec<i64>)], index: &VecIndex) -> (Vec<String>, Vec<Vec<f32>>) {
    let g = index.r();
    let dim = index.dim;
    let mut keys = Vec::new();
    let mut out = Vec::new();
    for (key, ids) in groups {
        let mut sum = vec![0f32; dim];
        let mut n = 0;
        for i in ids {
            if let Some(&j) = g.pos.get(i) {
                for (s, x) in sum.iter_mut().zip(&g.vecs[j * dim..(j + 1) * dim]) {
                    *s += x.to_f32();
                }
                n += 1;
            }
        }
        if n > 0 {
            keys.push(key.clone());
            out.push(unit(&sum));
        }
    }
    (keys, out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn vecs(n: usize, dim: usize) -> Vec<(i64, Vec<f32>)> {
        let mut s = 7u64;
        (0..n)
            .map(|i| {
                let v: Vec<f32> = (0..dim)
                    .map(|_| {
                        s = s.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
                        ((s >> 33) as f32 / (1u64 << 31) as f32) - 0.5
                    })
                    .collect();
                (i as i64 + 1, unit(&v))
            })
            .collect()
    }

    #[test]
    fn finds_the_exact_best_after_the_coarse_scan() {
        let dim = 64;
        let items = vecs(30_000, dim);
        let ix = VecIndex::new(dim);
        ix.add(&items);
        let q = items[1234].1.clone();
        let got = ix.search(&q, 5, None);
        assert_eq!(got[0].0, 1235);
        let mut all: Vec<(i64, f32)> = items.iter().map(|(i, v)| (*i, dot(v, &q))).collect();
        top(&mut all, 5);
        assert_eq!(got.iter().map(|x| x.0).collect::<Vec<_>>(), all.iter().map(|x| x.0).collect::<Vec<_>>());
    }

    /// On a copy of a real index.db (`INDEX_COPY`): the search order is the exact order of the float16 vectors.
    #[test]
    #[ignore]
    fn matches_exact_order_on_real_vectors() {
        let Ok(path) = std::env::var("INDEX_COPY") else { return };
        let c = Connection::open(path).expect("open copy");
        let ix = VecIndex::new(768);
        let n = ix.load(&c, "chunks").expect("load");
        let (ids0, m0) = ix.matrix();
        for copy in 1..4i64 {
            let more: Vec<(i64, Vec<f32>)> = ids0.iter().zip(&m0)
                .map(|(i, v)| (i + copy * 10_000_000, unit(&v.iter().enumerate().map(|(d, x)| x + ((d as i64 * 7 + copy) % 5) as f32 * 0.003).collect::<Vec<_>>())))
                .collect();
            ix.add(&more);
        }
        let (ids, m) = ix.matrix();
        let mut same = 0;
        for t in 0..50 {
            let q = unit(&m[(t * 97) % n].iter().enumerate().map(|(d, x)| x + ((d * 31 + t) % 7) as f32 * 0.002).collect::<Vec<_>>());
            let got: Vec<f32> = ix.search(&q, 10, None).iter().map(|x| x.1).collect();
            let mut all: Vec<(i64, f32)> = ids.iter().zip(&m).map(|(i, v)| (*i, dot(v, &q))).collect();
            top(&mut all, 10);
            same += got.iter().zip(&all).all(|(a, b)| (a - b.1).abs() < 1e-5) as usize;
        }
        assert_eq!(same, 50);
    }

    #[test]
    fn removes_and_filters() {
        let dim = 8;
        let items = vecs(10, dim);
        let ix = VecIndex::new(dim);
        ix.add(&items);
        assert!(ix.remove(3));
        assert!(!ix.contains(3));
        assert_eq!(ix.len(), 9);
        let got = ix.search(&items[4].1, 3, Some(&[5, 6]));
        assert_eq!(got[0].0, 5);
        assert_eq!(got.len(), 2);
        assert!(ix.take(&[10])[0].is_some());
    }
}
