//! 3D layout and clustering of the session, file and code maps.
//!
//! The layout is UMAP's (a fuzzy graph of each vector's nearest neighbours by cosine, then a force layout of that
//! graph in 3D), started from the vectors' first three principal axes and run with a fixed seed, so the same vectors
//! give the same map. A full fit reshuffles the map, so callers fit again only when the data grew a lot, and place
//! new points next to their nearest neighbours in between.

use rayon::prelude::*;

use crate::vectors::dot;

pub type P3 = [f32; 3];

/// A small fixed-seed generator: maps and clusters come out the same each run.
pub struct Rng(u64);

impl Rng {
    pub fn new(seed: u64) -> Rng {
        Rng(seed ^ 0x9E37_79B9_7F4A_7C15)
    }

    pub fn next_u64(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    /// Uniform in [0, 1).
    pub fn f32(&mut self) -> f32 {
        (self.next_u64() >> 40) as f32 / (1u64 << 24) as f32
    }

    pub fn below(&mut self, n: usize) -> usize {
        (self.next_u64() % n.max(1) as u64) as usize
    }

    pub fn normal(&mut self) -> f32 {
        let (u, v) = (self.f32().max(1e-7), self.f32());
        (-2.0 * u.ln()).sqrt() * (std::f32::consts::TAU * v).cos()
    }
}

pub fn unit(x: &[Vec<f32>]) -> Vec<Vec<f32>> {
    x.iter().map(|v| crate::vectors::unit(v)).collect()
}

fn median(mut v: Vec<f32>) -> f32 {
    if v.is_empty() {
        return 0.0;
    }
    v.sort_by(|a, b| a.total_cmp(b));
    let n = v.len();
    if n % 2 == 1 { v[n / 2] } else { (v[n / 2 - 1] + v[n / 2]) / 2.0 }
}

/// numpy's percentile with linear interpolation.
fn percentile(mut v: Vec<f32>, q: f32) -> f32 {
    if v.is_empty() {
        return 0.0;
    }
    v.sort_by(|a, b| a.total_cmp(b));
    let at = q / 100.0 * (v.len() - 1) as f32;
    let (lo, hi) = (at.floor() as usize, at.ceil() as usize);
    v[lo] + (v[hi] - v[lo]) * (at - lo as f32)
}

/// Points centred on their median and scaled so that 95% lie within 1, clipped to ±1.6.
pub fn normalize(p: Vec<P3>) -> Vec<P3> {
    if p.is_empty() {
        return p;
    }
    let m: Vec<f32> = (0..3).map(|d| median(p.iter().map(|x| x[d]).collect())).collect();
    let c: Vec<P3> = p.iter().map(|x| [x[0] - m[0], x[1] - m[1], x[2] - m[2]]).collect();
    let r = percentile(c.iter().map(|x| (x[0] * x[0] + x[1] * x[1] + x[2] * x[2]).sqrt()).collect(), 95.0) + 1e-9;
    c.iter().map(|x| [0, 1, 2].map(|d| (x[d] / r).clamp(-1.6, 1.6))).collect()
}

/// The first `k` principal axes' coordinates of each row (block power iteration on the centred rows).
fn pca(x: &[Vec<f32>], k: usize) -> Vec<Vec<f32>> {
    let (n, dim) = (x.len(), x.first().map(|v| v.len()).unwrap_or(0));
    if n == 0 || dim == 0 {
        return vec![vec![0.0; k]; n];
    }
    let mean: Vec<f32> = (0..dim).map(|d| x.iter().map(|v| v[d]).sum::<f32>() / n as f32).collect();
    let xc: Vec<Vec<f32>> = x.iter().map(|v| v.iter().zip(&mean).map(|(a, b)| a - b).collect()).collect();
    let mut rng = Rng::new(0);
    let mut axes: Vec<Vec<f32>> = (0..k).map(|_| (0..dim).map(|_| rng.normal()).collect()).collect();
    for _ in 0..60 {
        let proj: Vec<Vec<f32>> = xc.par_iter().map(|v| axes.iter().map(|a| dot(v, a)).collect()).collect();
        let mut next = vec![vec![0f32; dim]; k];
        for (v, p) in xc.iter().zip(&proj) {
            for (j, a) in next.iter_mut().enumerate() {
                for (s, x) in a.iter_mut().zip(v) {
                    *s += p[j] * x;
                }
            }
        }
        for j in 0..k {
            for i in 0..j {
                let d = dot(&next[j], &next[i]);
                let prev = next[i].clone();
                for (s, x) in next[j].iter_mut().zip(&prev) {
                    *s -= d * x;
                }
            }
            let nn = dot(&next[j], &next[j]).sqrt() + 1e-12;
            next[j].iter_mut().for_each(|s| *s /= nn);
        }
        axes = next;
    }
    xc.iter().map(|v| axes.iter().map(|a| dot(v, a)).collect()).collect()
}

/// `a`, `b` of UMAP's curve 1 / (1 + a d^2b) that fits its min_dist (spread 1).
fn curve(min_dist: f32) -> (f32, f32) {
    let xs: Vec<f32> = (0..300).map(|i| 3.0 * i as f32 / 299.0).collect();
    let ys: Vec<f32> = xs.iter().map(|&x| if x < min_dist { 1.0 } else { (-(x - min_dist)).exp() }).collect();
    let err = |a: f32, b: f32| -> f32 {
        xs.iter().zip(&ys).map(|(&x, &y)| {
            let f = 1.0 / (1.0 + a * x.powf(2.0 * b));
            (f - y) * (f - y)
        }).sum()
    };
    let (mut best, mut ab) = (f32::MAX, (1.58, 0.9));
    for bi in 0..=150 {
        let b = 0.5 + bi as f32 * 0.01;
        for ai in 0..=200 {
            let a = (ai as f32 * 0.02 - 1.0).exp() * 0.5;
            let e = err(a, b);
            if e < best {
                best = e;
                ab = (a, b);
            }
        }
    }
    ab
}

/// The `k` most similar other rows of each row (rows are unit vectors), with their cosine distances.
fn knn(x: &[Vec<f32>], k: usize) -> Vec<Vec<(usize, f32)>> {
    (0..x.len()).into_par_iter().map(|i| {
        let mut s: Vec<(usize, f32)> = (0..x.len()).filter(|&j| j != i).map(|j| (j, dot(&x[i], &x[j]))).collect();
        crate::vectors::top(&mut s, k);
        s.into_iter().map(|(j, c)| (j, (1.0 - c).max(0.0))).collect()
    }).collect()
}

/// UMAP's fuzzy graph: each neighbour's membership from its distance, made symmetric (a + b - ab).
fn fuzzy_graph(nn: &[Vec<(usize, f32)>], k: usize) -> Vec<(usize, usize, f32)> {
    let target = (k as f32).log2();
    let mut w: std::collections::HashMap<(usize, usize), f32> = Default::default();
    for (i, row) in nn.iter().enumerate() {
        let rho = row.iter().map(|x| x.1).filter(|d| *d > 0.0).fold(f32::MAX, f32::min);
        let rho = if rho == f32::MAX { 0.0 } else { rho };
        let (mut lo, mut hi, mut sigma) = (0f32, f32::MAX, 1f32);
        for _ in 0..64 {
            let s: f32 = row.iter().map(|x| (-((x.1 - rho).max(0.0)) / sigma).exp()).sum();
            if (s - target).abs() < 1e-5 {
                break;
            }
            if s > target {
                hi = sigma;
                sigma = (lo + hi) / 2.0;
            } else {
                lo = sigma;
                sigma = if hi == f32::MAX { sigma * 2.0 } else { (lo + hi) / 2.0 };
            }
        }
        for &(j, d) in row {
            let m = (-((d - rho).max(0.0)) / sigma.max(1e-3 * 1e-3)).exp();
            *w.entry((i, j)).or_default() = m;
        }
    }
    let mut out = Vec::new();
    for (&(i, j), &a) in &w {
        let b = w.get(&(j, i)).copied().unwrap_or(0.0);
        if i < j || b == 0.0 {
            let v = a + b - a * b;
            let (p, q) = if i < j { (i, j) } else { (j, i) };
            out.push((p, q, v));
        }
    }
    out.sort_by(|a, b| (a.0, a.1).cmp(&(b.0, b.1)));
    out.dedup_by(|a, b| a.0 == b.0 && a.1 == b.1);
    out
}

/// A 3D map of the rows where similar rows are near points (UMAP, cosine), normalized.
pub fn fit3d(x: &[Vec<f32>], n_neighbors: usize, min_dist: f32) -> Vec<P3> {
    let n = x.len();
    if n < 8 {
        if n < 3 {
            return vec![[0.0; 3]; n];
        }
        return normalize(pca(x, 3).into_iter().map(|v| [v[0], v[1], v[2]]).collect());
    }
    let xu = unit(x);
    let k = n_neighbors.min(n - 1).max(2);
    let edges = fuzzy_graph(&knn(&xu, k), k);
    let (a, b) = curve(min_dist);
    let init = pca(&xu, 3);
    let top = init.iter().flat_map(|v| v.iter().map(|x| x.abs())).fold(0f32, f32::max).max(1e-9);
    let mut rng = Rng::new(42);
    let mut y: Vec<P3> = init.iter().map(|v| [0, 1, 2].map(|d| v[d] / top * 10.0 + rng.normal() * 1e-4)).collect();
    let epochs = if n > 10_000 { 200 } else { 500 };
    let wmax = edges.iter().map(|e| e.2).fold(0f32, f32::max).max(1e-9);
    let edges: Vec<(usize, usize, f32)> = edges.into_iter().filter(|e| e.2 >= wmax / epochs as f32).collect();
    let neg = 5;
    for epoch in 0..epochs {
        let alpha = 1.0 - epoch as f32 / epochs as f32;
        for &(i, j, w) in &edges {
            if rng.f32() > w / wmax {
                continue;
            }
            let d2: f32 = (0..3).map(|d| (y[i][d] - y[j][d]).powi(2)).sum();
            if d2 > 0.0 {
                let c = -2.0 * a * b * d2.powf(b - 1.0) / (a * d2.powf(b) + 1.0);
                for d in 0..3 {
                    let g = (c * (y[i][d] - y[j][d])).clamp(-4.0, 4.0) * alpha;
                    y[i][d] += g;
                    y[j][d] -= g;
                }
            }
            for _ in 0..neg {
                let o = rng.below(n);
                if o == i {
                    continue;
                }
                let d2: f32 = (0..3).map(|d| (y[i][d] - y[o][d]).powi(2)).sum();
                let c = if d2 > 0.0 { 2.0 * b / ((0.001 + d2) * (a * d2.powf(b) + 1.0)) } else { 0.0 };
                for d in 0..3 {
                    let g = if c > 0.0 { (c * (y[i][d] - y[o][d])).clamp(-4.0, 4.0) } else { 4.0 };
                    y[i][d] += g * alpha;
                }
            }
        }
    }
    normalize(y)
}

/// Positions for new rows: the similarity-weighted mean of the positions of their `k` most similar known rows,
/// plus normal jitter (seed 0).
pub fn place_new(x_known: &[Vec<f32>], p_known: &[P3], x_new: &[Vec<f32>], k: usize, jitter: f32) -> Vec<P3> {
    if x_known.is_empty() {
        return vec![[0.0; 3]; x_new.len()];
    }
    let k = k.min(x_known.len());
    let mut out: Vec<P3> = x_new.par_iter().map(|v| {
        let mut s: Vec<(usize, f32)> = x_known.iter().enumerate().map(|(j, u)| (j, dot(v, u))).collect();
        crate::vectors::top(&mut s, k);
        let wsum: f32 = s.iter().map(|x| x.1.max(1e-3)).sum();
        let mut p = [0f32; 3];
        for &(j, w) in &s {
            for d in 0..3 {
                p[d] += p_known[j][d] * w.max(1e-3);
            }
        }
        p.map(|x| x / wsum)
    }).collect();
    let mut rng = Rng::new(0);
    for p in &mut out {
        for x in p.iter_mut() {
            *x += rng.normal() * jitter;
        }
    }
    out
}

/// The position in `x_known` of each new row's most similar known row.
pub fn nearest(x_new: &[Vec<f32>], x_known: &[Vec<f32>]) -> Vec<usize> {
    x_new.par_iter().map(|v| {
        x_known.iter().enumerate().map(|(j, u)| (j, dot(v, u)))
            .fold((0, f32::MIN), |b, x| if x.1 > b.1 { x } else { b }).0
    }).collect()
}

fn dist2(a: &[f32], b: &[f32]) -> f32 {
    a.iter().zip(b).map(|(x, y)| (x - y) * (x - y)).sum()
}

/// KMeans as scikit-learn runs it: greedy k-means++ starts (2 + ln k tries per centre, the one that lowers the
/// total distance most), the best of 8 runs by inertia, seed 0.
pub fn kmeans(x: &[Vec<f32>], k: usize) -> Vec<usize> {
    let n = x.len();
    let dim = x.first().map(|v| v.len()).unwrap_or(0);
    let mut rng = Rng::new(0);
    let tries = 2 + (k as f32).ln() as usize;
    let mut best: (f32, Vec<usize>) = (f32::MAX, vec![0; n]);
    for _ in 0..8 {
        let mut cent: Vec<Vec<f32>> = vec![x[rng.below(n)].clone()];
        let mut d: Vec<f32> = x.iter().map(|v| dist2(v, &cent[0])).collect();
        while cent.len() < k {
            let total: f32 = d.iter().sum();
            let mut pick: Option<(f32, usize, Vec<f32>)> = None;
            for _ in 0..tries {
                let mut r = rng.f32() * total;
                let mut c = n - 1;
                for (i, di) in d.iter().enumerate() {
                    if r < *di {
                        c = i;
                        break;
                    }
                    r -= di;
                }
                let nd: Vec<f32> = x.par_iter().zip(&d).map(|(v, &o)| o.min(dist2(v, &x[c]))).collect();
                let pot: f32 = nd.iter().sum();
                if pick.as_ref().is_none_or(|p| pot < p.0) {
                    pick = Some((pot, c, nd));
                }
            }
            let Some((_, c, nd)) = pick else { break };
            cent.push(x[c].clone());
            d = nd;
        }
        let mut lab = vec![0usize; n];
        let mut inertia = f32::MAX;
        for _ in 0..300 {
            let assign: Vec<(usize, f32)> = x.par_iter().map(|v| {
                cent.iter().enumerate().map(|(c, m)| (c, dist2(v, m))).fold((0, f32::MAX), |b, x| if x.1 < b.1 { x } else { b })
            }).collect();
            let new_inertia: f32 = assign.iter().map(|a| a.1).sum();
            lab = assign.iter().map(|a| a.0).collect();
            let mut sums = vec![vec![0f32; dim]; k];
            let mut counts = vec![0usize; k];
            for (v, &l) in x.iter().zip(&lab) {
                counts[l] += 1;
                for (s, a) in sums[l].iter_mut().zip(v) {
                    *s += a;
                }
            }
            for c in 0..k {
                if counts[c] > 0 {
                    cent[c] = sums[c].iter().map(|s| s / counts[c] as f32).collect();
                }
            }
            let done = (inertia - new_inertia).abs() <= 1e-4 * new_inertia.max(1e-9);
            inertia = new_inertia;
            if done {
                break;
            }
        }
        if inertia < best.0 {
            best = (inertia, lab);
        }
    }
    best.1
}

/// The mean silhouette by cosine distance (on at most 3000 rows, a fixed sample, for big inputs).
fn silhouette(x: &[Vec<f32>], lab: &[usize], k: usize) -> f32 {
    let mut idx: Vec<usize> = (0..x.len()).collect();
    if idx.len() > 3000 {
        let mut rng = Rng::new(1);
        for i in (1..idx.len()).rev() {
            idx.swap(i, rng.below(i + 1));
        }
        idx.truncate(3000);
    }
    let xu = unit(x);
    let s: Vec<f32> = idx.par_iter().map(|&i| {
        let mut sum = vec![0f32; k];
        let mut cnt = vec![0usize; k];
        for &j in &idx {
            if j != i {
                sum[lab[j]] += 1.0 - dot(&xu[i], &xu[j]);
                cnt[lab[j]] += 1;
            }
        }
        let own = lab[i];
        if cnt[own] == 0 {
            return 0.0;
        }
        let a = sum[own] / cnt[own] as f32;
        let b = (0..k).filter(|&c| c != own && cnt[c] > 0).map(|c| sum[c] / cnt[c] as f32).fold(f32::MAX, f32::min);
        if b == f32::MAX { 0.0 } else { (b - a) / a.max(b).max(1e-12) }
    }).collect();
    s.iter().sum::<f32>() / s.len().max(1) as f32
}

/// Labels renumbered by cluster size, so that cluster 0 is the largest (a stable palette order).
fn by_size(lab: Vec<usize>) -> Vec<usize> {
    let k = lab.iter().max().map(|m| m + 1).unwrap_or(0);
    let mut count = vec![0usize; k];
    for &l in &lab {
        count[l] += 1;
    }
    let mut order: Vec<usize> = (0..k).collect();
    order.sort_by_key(|&c| (std::cmp::Reverse(count[c]), c));
    let mut remap = vec![0; k];
    for (new, &old) in order.iter().enumerate() {
        remap[old] = new;
    }
    lab.into_iter().map(|l| remap[l]).collect()
}

/// KMeans with k from kmin to kmax (at most a third of the rows) chosen by silhouette; cluster 0 is the largest.
pub fn cluster(x: &[Vec<f32>], kmin: usize, kmax: usize) -> Vec<usize> {
    let n = x.len();
    if n < kmin * 2 {
        return vec![0; n];
    }
    if kmin == kmax {
        return by_size(kmeans(x, kmin));
    }
    let mut best: Option<(f32, Vec<usize>)> = None;
    for k in kmin..=kmax.min(n / 3) {
        let lab = kmeans(x, k);
        let s = silhouette(x, &lab, k);
        if best.as_ref().is_none_or(|b| s > b.0) {
            best = Some((s, lab));
        }
    }
    best.map(|b| by_size(b.1)).unwrap_or_else(|| vec![0; n])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn blobs() -> Vec<Vec<f32>> {
        let mut rng = Rng::new(3);
        (0..90).map(|i| {
            let c = i % 3;
            (0..16).map(|d| if d % 3 == c { 1.0 } else { 0.0 } + rng.normal() * 0.05).collect()
        }).collect()
    }

    /// `ROWS`: raw little-endian f32 rows of 768, such as the mean vector of each session.
    #[test]
    #[ignore]
    fn silhouettes_on_real_rows() {
        let Ok(p) = std::env::var("ROWS") else { return };
        let b = std::fs::read(p).unwrap_or_default();
        let f: Vec<f32> = b.chunks_exact(4).map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect();
        let x: Vec<Vec<f32>> = f.chunks(768).map(|r| r.to_vec()).collect();
        for k in 4..=8 {
            let lab = kmeans(&x, k);
            let mut n = vec![0; k];
            lab.iter().for_each(|&l| n[l] += 1);
            println!("{k} {} {n:?}", silhouette(&x, &lab, k));
        }
    }

    #[test]
    fn clusters_three_blobs() {
        let x = blobs();
        let lab = cluster(&x, 2, 6);
        assert_eq!(*lab.iter().max().unwrap_or(&0), 2);
        assert!((0..90).all(|i| lab[i] == lab[i % 3]));
    }

    #[test]
    fn layout_keeps_blobs_apart_and_is_stable() {
        let x = blobs();
        let p = fit3d(&x, 15, 0.1);
        assert_eq!(p, fit3d(&x, 15, 0.1));
        let d = |a: P3, b: P3| ((a[0] - b[0]).powi(2) + (a[1] - b[1]).powi(2) + (a[2] - b[2]).powi(2)).sqrt();
        let same: f32 = (0..30).map(|i| d(p[3 * i], p[0])).sum::<f32>() / 30.0;
        let other: f32 = (0..30).map(|i| d(p[3 * i + 1], p[0])).sum::<f32>() / 30.0;
        assert!(same < other);
    }
}
