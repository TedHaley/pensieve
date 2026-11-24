//! The text model's forward pass for texts (at most 512 tokens), encoded by hand into one Metal command buffer: the
//! products on Metal Performance Shaders (about 1.5 times as fast as candle's kernels in float32), the rest in the
//! small kernels below. It reads the weights that candle loaded, in place. Images, video and sound, whose sequences
//! need the sliding mask, stay on candle.
//!
//! The arithmetic is the one `TextModel::forward` does, in float32: norms, rope, a masked softmax with scale 1,
//! GELU (tanh) and the per-layer inputs. Attention runs as batches of densely packed per-head matrices: MPS gave
//! wrong results for batches of strided matrices (a row stride wider than the matrix) after the first.
//!
//! The fused q/k/v and gate/up weights are copies, about 300 MB on top of candle's weights.

use std::collections::HashMap;
use std::ptr::NonNull;
use std::sync::Mutex;

use anyhow::{Context, bail};
use candle_core::{DType, Storage, Tensor};
use objc2::rc::Retained;
use objc2::runtime::ProtocolObject;
use objc2::{AnyThread, Message};
use objc2_foundation::NSString;
use objc2_metal::{MTLBuffer, MTLCommandBuffer, MTLCommandEncoder, MTLCommandQueue, MTLCompileOptions,
                  MTLComputeCommandEncoder, MTLComputePipelineState, MTLDevice, MTLLibrary, MTLMathMode, MTLResource,
                  MTLResourceOptions, MTLSize};
use objc2_metal_performance_shaders::{MPSDataType, MPSMatrix, MPSMatrixDescriptor, MPSMatrixMultiplication};

use super::TextModel;

const SOURCE: &str = r#"
#include <metal_stdlib>
using namespace metal;

static float tg_sum(float v, threadgroup float* sh, uint sl, uint sg, uint tn) {
    v = simd_sum(v);
    if (sl == 0) sh[sg] = v;
    threadgroup_barrier(mem_flags::mem_threadgroup);
    float r = 0;
    for (uint i = 0; i < (tn + 31) / 32; i++) r += sh[i];
    return r;
}

static float tg_max(float v, threadgroup float* sh, uint sl, uint sg, uint tn) {
    v = simd_max(v);
    if (sl == 0) sh[sg] = v;
    threadgroup_barrier(mem_flags::mem_threadgroup);
    float r = -INFINITY;
    for (uint i = 0; i < (tn + 31) / 32; i++) r = max(r, sh[i]);
    return r;
}

static float gelu(float x) {
    return 0.5f * x * (1.0f + precise::tanh(0.7978845608028654f * (x + 0.044715f * x * x * x)));
}

#define ROW uint row [[threadgroup_position_in_grid]], uint t [[thread_position_in_threadgroup]], \
    uint tn [[threads_per_threadgroup]], uint sl [[thread_index_in_simdgroup]], uint sg [[simdgroup_index_in_threadgroup]]

kernel void embed(device const uint* ids [[buffer(0)]], device const float* table [[buffer(1)]],
                  device float* out [[buffer(2)]], constant uint& dim [[buffer(3)]], constant float& scale [[buffer(4)]],
                  uint2 g [[thread_position_in_grid]]) {
    out[ulong(g.y) * dim + g.x] = table[ulong(ids[g.y]) * dim + g.x] * scale;
}

kernel void rms(device const float* x [[buffer(0)]], device const float* w [[buffer(1)]], device float* out [[buffer(2)]],
                constant uint& dim [[buffer(3)]], constant float& eps [[buffer(4)]], constant float& pre [[buffer(5)]],
                constant uint& has_w [[buffer(6)]], ROW) {
    threadgroup float sh[32];
    device const float* r = x + ulong(row) * dim;
    device float* o = out + ulong(row) * dim;
    float s = 0;
    for (uint j = t; j < dim; j += tn) { float v = r[j] * pre; s += v * v; }
    float inv = rsqrt(tg_sum(s, sh, sl, sg, tn) / dim + eps);
    for (uint j = t; j < dim; j += tn) o[j] = r[j] * pre * inv * (has_w ? w[j] : 1.0f);
}

kernel void add_norm(device float* x [[buffer(0)]], device const float* y [[buffer(1)]], device const float* w [[buffer(2)]],
                     constant uint& dim [[buffer(3)]], constant float& eps [[buffer(4)]], constant float& scale [[buffer(5)]], ROW) {
    threadgroup float sh[32];
    device float* r = x + ulong(row) * dim;
    device const float* q = y + ulong(row) * dim;
    float s = 0;
    for (uint j = t; j < dim; j += tn) s += q[j] * q[j];
    float inv = rsqrt(tg_sum(s, sh, sl, sg, tn) / dim + eps);
    for (uint j = t; j < dim; j += tn) r[j] = (r[j] + q[j] * inv * w[j]) * scale;
}

// one threadgroup per (row, head) of the fused q, k, v: q and k get their norm and rope, v its plain norm
kernel void qkv_prep(device float* qkv [[buffer(0)]], device const float* qw [[buffer(1)]], device const float* kw [[buffer(2)]],
                     device const float* inv_freq [[buffer(3)]], constant uint& heads [[buffer(4)]], constant uint& kv [[buffer(5)]],
                     constant uint& hd [[buffer(6)]], constant uint& n [[buffer(7)]], constant float& eps [[buffer(8)]],
                     uint2 tg [[threadgroup_position_in_grid]], uint2 t2 [[thread_position_in_threadgroup]],
                     uint2 tn2 [[threads_per_threadgroup]], uint sl [[thread_index_in_simdgroup]], uint sg [[simdgroup_index_in_threadgroup]]) {
    threadgroup float sh[32];
    uint t = t2.x, tn = tn2.x;
    uint width = (heads + 2 * kv) * hd;
    uint row = tg.x, head = tg.y;
    device float* p = qkv + ulong(row) * width + head * hd;
    float s = 0;
    for (uint j = t; j < hd; j += tn) s += p[j] * p[j];
    float inv = rsqrt(tg_sum(s, sh, sl, sg, tn) / hd + eps);
    if (head >= heads + kv) {
        for (uint j = t; j < hd; j += tn) p[j] *= inv;
        return;
    }
    device const float* w = head < heads ? qw : kw;
    float pos = float(row % n);
    uint half_ = hd / 2;
    for (uint j = t; j < half_; j += tn) {
        float a = p[j] * inv * w[j], b = p[j + half_] * inv * w[j + half_];
        float f = pos * inv_freq[j];
        float c = precise::cos(f), si = precise::sin(f);
        p[j] = a * c - b * si;
        p[j + half_] = b * c + a * si;
    }
}

// q, k and v laid out as (batch, head, position, d) for MPS, which batches only densely packed matrices; k and v
// repeat for the query heads that share them
kernel void split_heads(device const float* qkv [[buffer(0)]], device float* q [[buffer(1)]], device float* k [[buffer(2)]],
                        device float* v [[buffer(3)]], constant uint& heads [[buffer(4)]], constant uint& kv [[buffer(5)]],
                        constant uint& hd [[buffer(6)]], constant uint& n [[buffer(7)]], uint3 g [[thread_position_in_grid]]) {
    uint j = g.x, h = g.y, row = g.z;
    uint width = (heads + 2 * kv) * hd, grp = h / (heads / kv);
    ulong dst = ((ulong(row / n) * heads + h) * n + row % n) * hd + j;
    device const float* r = qkv + ulong(row) * width;
    q[dst] = r[h * hd + j];
    k[dst] = r[(heads + grp) * hd + j];
    v[dst] = r[(heads + kv + grp) * hd + j];
}

kernel void merge_heads(device const float* o [[buffer(0)]], device float* att [[buffer(1)]], constant uint& heads [[buffer(2)]],
                        constant uint& hd [[buffer(3)]], constant uint& n [[buffer(4)]], uint3 g [[thread_position_in_grid]]) {
    uint j = g.x, h = g.y, row = g.z;
    att[ulong(row) * heads * hd + h * hd + j] = o[((ulong(row / n) * heads + h) * n + row % n) * hd + j];
}

kernel void softmax_mask(device float* s [[buffer(0)]], device const float* keep [[buffer(1)]], constant uint& n [[buffer(2)]],
                         constant uint& per_batch [[buffer(3)]], ROW) {
    threadgroup float sh[32];
    threadgroup float sh2[32];
    device float* r = s + ulong(row) * n;
    device const float* k = keep + (row / per_batch) * n;
    float m = -INFINITY;
    for (uint j = t; j < n; j += tn) if (k[j] > 0) m = max(m, r[j]);
    m = tg_max(m, sh, sl, sg, tn);
    float z = 0;
    for (uint j = t; j < n; j += tn) { float e = k[j] > 0 ? precise::exp(r[j] - m) : 0.0f; r[j] = e; z += e; }
    z = tg_sum(z, sh2, sl, sg, tn);
    for (uint j = t; j < n; j += tn) r[j] /= z;
}

kernel void gelu_mul(device const float* gu [[buffer(0)]], device float* out [[buffer(1)]], constant uint& inter [[buffer(2)]],
                     uint2 g [[thread_position_in_grid]]) {
    ulong r = g.y;
    out[r * inter + g.x] = gelu(gu[r * 2 * inter + g.x]) * gu[r * 2 * inter + inter + g.x];
}

kernel void gelu_mul_li(device const float* gt [[buffer(0)]], device const float* li [[buffer(1)]], device float* out [[buffer(2)]],
                        constant uint& dim [[buffer(3)]], constant uint& li_stride [[buffer(4)]], constant uint& li_off [[buffer(5)]],
                        uint2 g [[thread_position_in_grid]]) {
    ulong r = g.y;
    out[r * dim + g.x] = gelu(gt[r * dim + g.x]) * li[r * li_stride + li_off + g.x];
}
"#;

type Obj<T> = Retained<ProtocolObject<T>>;
/// An MPS product's shape: transposed right side, result rows and columns, inner size, batch.
type ProductKey = (bool, usize, usize, usize, usize);

/// A Metal buffer and a byte offset in it.
#[derive(Clone)]
struct Buf(Obj<dyn MTLBuffer>, usize);

struct LayerW {
    qkv: Buf,
    o: Buf,
    gate_up: Buf,
    down: Buf,
    ple_gate: Buf,
    ple_proj: Buf,
    q_norm: Buf,
    k_norm: Buf,
    input_norm: Buf,
    post_attn_norm: Buf,
    pre_ff_norm: Buf,
    post_ff_norm: Buf,
    ple_norm: Buf,
    inv_freq: Buf,
    scalar: f32,
    head_dim: usize,
    kv_heads: usize,
}

/// A float32 matrix in a buffer, as MPS reads it: rows × cols, `mats` of them packed one after the other.
struct Mat<'a> {
    buf: &'a Buf,
    rows: usize,
    cols: usize,
    mats: usize,
}

pub struct Fast {
    device: Obj<dyn MTLDevice>,
    queue: Obj<dyn MTLCommandQueue>,
    pipes: HashMap<&'static str, Obj<dyn MTLComputePipelineState>>,
    layers: Vec<LayerW>,
    embed: Buf,
    ple_proj: Buf,
    ple_norm: Buf,
    norm: Buf,
    proj: Buf,
    products: Mutex<HashMap<ProductKey, Retained<MPSMatrixMultiplication>>>,
    scratch: Mutex<HashMap<&'static str, Buf>>,
    /// The candle tensors whose buffers the weights above point into.
    _hold: Vec<Tensor>,
}

// SAFETY: Metal devices, queues, pipelines and buffers are thread-safe; MPS kernels are only used under the mutex
unsafe impl Send for Fast {}
unsafe impl Sync for Fast {}

fn metal_buf(t: &Tensor) -> anyhow::Result<Buf> {
    let (s, l) = t.storage_and_layout();
    match &*s {
        Storage::Metal(m) => {
            let b: &ProtocolObject<dyn MTLBuffer> = m.buffer().as_ref();
            Ok(Buf(b.retain(), l.start_offset() * t.dtype().size_in_bytes()))
        }
        _ => bail!("not a Metal tensor"),
    }
}

impl Fast {
    /// The fast path for `m`, when it runs on Metal in float32.
    pub fn new(m: &TextModel) -> anyhow::Result<Fast> {
        if !m.device.is_metal() || m.dtype != DType::F32 {
            bail!("the fast path needs float32 weights on Metal");
        }
        let mut hold = Vec::new();
        let mut keep = |t: Tensor| -> anyhow::Result<Buf> {
            let t = t.contiguous()?;
            let b = metal_buf(&t)?;
            hold.push(t);
            Ok(b)
        };
        let dev = m.device.clone();
        let mut layers = Vec::new();
        for l in &m.layers {
            let a = &l.attn;
            let qkv = Tensor::cat(&[a.q.weight(), a.k.weight(), a.v.weight()], 0)?;
            let gu = Tensor::cat(&[l.gate.weight(), l.up.weight()], 0)?;
            let w = |r: &crate::nn::Rms| r.weight().cloned().context("a norm without weight");
            layers.push(LayerW {
                qkv: keep(qkv)?,
                o: keep(a.o.weight().clone())?,
                gate_up: keep(gu)?,
                down: keep(l.down.weight().clone())?,
                ple_gate: keep(l.ple_gate.weight().clone())?,
                ple_proj: keep(l.ple_proj.weight().clone())?,
                q_norm: keep(w(&a.q_norm)?)?,
                k_norm: keep(w(&a.k_norm)?)?,
                input_norm: keep(w(&l.input_norm)?)?,
                post_attn_norm: keep(w(&l.post_attn_norm)?)?,
                pre_ff_norm: keep(w(&l.pre_ff_norm)?)?,
                post_ff_norm: keep(w(&l.post_ff_norm)?)?,
                ple_norm: keep(w(&l.ple_norm)?)?,
                inv_freq: keep(Tensor::new(a.inv_freq.as_slice(), &dev)?)?,
                scalar: l.scalar.to_dtype(DType::F32)?.flatten_all()?.to_vec1::<f32>()?[0],
                head_dim: a.head_dim,
                kv_heads: a.kv_heads,
            });
        }
        let embed = keep(m.embed.clone())?;
        let ple_proj = keep(m.ple_proj.weight().clone())?;
        let ple_norm = keep(m.ple_norm.weight().cloned().context("ple norm")?)?;
        let norm = keep(m.norm.weight().cloned().context("final norm")?)?;
        let proj = keep(m.proj.weight().clone())?;
        // candle's work that made the fused weights finishes before another queue reads them
        if let candle_core::Device::Metal(md) = &dev {
            md.wait_until_completed()?;
        }
        let device = embed.0.device();
        let queue = device.newCommandQueue().context("no Metal command queue")?;
        let opts = MTLCompileOptions::new();
        opts.setMathMode(MTLMathMode::Safe);
        let lib = device.newLibraryWithSource_options_error(&NSString::from_str(SOURCE), Some(&opts))
            .map_err(|e| anyhow::anyhow!("Metal kernels: {e}"))?;
        let mut pipes = HashMap::new();
        for name in ["embed", "rms", "add_norm", "qkv_prep", "split_heads", "merge_heads", "softmax_mask", "gelu_mul",
                     "gelu_mul_li"] {
            let f = lib.newFunctionWithName(&NSString::from_str(name)).context("missing kernel")?;
            let p = device.newComputePipelineStateWithFunction_error(&f).map_err(|e| anyhow::anyhow!("{name}: {e}"))?;
            pipes.insert(name, p);
        }
        Ok(Fast { device, queue, pipes, layers, embed, ple_proj, ple_norm, norm, proj,
                  products: Mutex::new(HashMap::new()), scratch: Mutex::new(HashMap::new()), _hold: hold })
    }

    /// The working buffer `name`, kept from call to call: a new buffer costs a page fault per page of its first use.
    /// It grows when a call needs more.
    fn buffer(&self, name: &'static str, floats: usize) -> anyhow::Result<Buf> {
        let mut s = self.scratch.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(b) = s.get(name).filter(|b| b.0.length() >= floats * 4) {
            return Ok(b.clone());
        }
        let b = self.device.newBufferWithLength_options(floats.max(1) * 4, MTLResourceOptions::StorageModeShared)
            .context("out of GPU memory")?;
        s.insert(name, Buf(b.clone(), 0));
        Ok(Buf(b, 0))
    }

    fn upload<T: Copy>(&self, name: &'static str, data: &[T]) -> anyhow::Result<Buf> {
        let b = self.buffer(name, std::mem::size_of_val(data).div_ceil(4))?;
        // SAFETY: the buffer is shared, at least as long as the data, and not yet used by the GPU
        unsafe { std::ptr::copy_nonoverlapping(data.as_ptr() as *const u8, b.0.contents().as_ptr() as *mut u8, std::mem::size_of_val(data)) };
        Ok(b)
    }

    /// The last hidden states projected to the embedding size, for `b` texts of `n` tokens each (ids and keep row
    /// by row, keep 1 for a token and 0 for padding): b × n rows of `embedding_dim` floats.
    pub fn forward(&self, m: &TextModel, ids: &[u32], keep: &[f32], b: usize, n: usize) -> anyhow::Result<Vec<f32>> {
        // off the main thread nothing drains what Metal and MPS autorelease (command buffers, encoders, matrices)
        objc2::rc::autoreleasepool(|_| self.run(m, ids, keep, b, n))
    }

    fn run(&self, m: &TextModel, ids: &[u32], keep: &[f32], b: usize, n: usize) -> anyhow::Result<Vec<f32>> {
        let c = &m.cfg;
        let (rows, h, heads) = (b * n, c.hidden, c.heads);
        let ids = self.upload("ids", ids)?;
        let keep = self.upload("keep", keep)?;
        let x = self.buffer("x", rows * h)?;
        let y = self.buffer("y", rows * h)?;
        let li = self.buffer("li", rows * c.layers * c.per_layer)?;
        let widest = self.layers.iter().map(|l| (heads + 2 * l.kv_heads) * l.head_dim).max().unwrap_or(0);
        let qkv = self.buffer("qkv", rows * widest)?;
        let per_head = rows * self.layers.iter().map(|l| heads * l.head_dim).max().unwrap_or(0);
        let att = self.buffer("att", per_head)?;
        let scores = self.buffer("scores", b * heads * n * n)?;
        let (qh, kh, vh) = (self.buffer("qh", per_head)?, self.buffer("kh", per_head)?, self.buffer("vh", per_head)?);
        let gu = self.buffer("gu", rows * 2 * c.intermediate)?;
        let mid = self.buffer("mid", rows * c.intermediate)?;
        let out = self.buffer("out", rows * c.embedding_dim)?;
        let cb = self.queue.commandBuffer().context("no command buffer")?;
        let mut run = Run { fast: self, cb: &cb, enc: None };
        let eps = crate::nn::EPS as f32;
        run.kernel("embed", &[&ids, &self.embed, &x], &[u(h), f(m.scale as f32)], Grid::Threads(h, rows))?;
        run.linear(&x, &self.ple_proj, &li, rows, h, c.layers * c.per_layer)?;
        run.kernel("rms", &[&li, &self.ple_norm, &li], &[u(c.per_layer), f(eps), f((h as f32).powf(-0.5)), u(1)],
                   Grid::Rows(rows * c.layers, 1))?;
        for (i, l) in self.layers.iter().enumerate() {
            let (hd, kv) = (l.head_dim, l.kv_heads);
            let width = (heads + 2 * kv) * hd;
            run.kernel("rms", &[&x, &l.input_norm, &y], &[u(h), f(eps), f(1.0), u(1)], Grid::Rows(rows, 1))?;
            run.linear(&y, &l.qkv, &qkv, rows, h, width)?;
            run.kernel("qkv_prep", &[&qkv, &l.q_norm, &l.k_norm, &l.inv_freq],
                       &[u(heads), u(kv), u(hd), u(n), f(eps)], Grid::Rows(rows, heads + 2 * kv))?;
            run.kernel("split_heads", &[&qkv, &qh, &kh, &vh], &[u(heads), u(kv), u(hd), u(n)], Grid::Cube(hd, heads, rows))?;
            let bh = b * heads;
            let per = |buf, cols| Mat { buf, rows: n, cols, mats: bh };
            run.product(&per(&qh, hd), &per(&kh, hd), &per(&scores, n), true, bh)?;
            run.kernel("softmax_mask", &[&scores, &keep], &[u(n), u(heads * n)], Grid::Rows(bh * n, 1))?;
            run.product(&per(&scores, n), &per(&vh, hd), &per(&qh, hd), false, bh)?;
            run.kernel("merge_heads", &[&qh, &att], &[u(heads), u(hd), u(n)], Grid::Cube(hd, heads, rows))?;
            run.linear(&att, &l.o, &y, rows, heads * hd, h)?;
            run.kernel("add_norm", &[&x, &y, &l.post_attn_norm], &[u(h), f(eps), f(1.0)], Grid::Rows(rows, 1))?;
            run.kernel("rms", &[&x, &l.pre_ff_norm, &y], &[u(h), f(eps), f(1.0), u(1)], Grid::Rows(rows, 1))?;
            run.linear(&y, &l.gate_up, &gu, rows, h, 2 * c.intermediate)?;
            run.kernel("gelu_mul", &[&gu, &mid], &[u(c.intermediate)], Grid::Threads(c.intermediate, rows))?;
            run.linear(&mid, &l.down, &y, rows, c.intermediate, h)?;
            run.kernel("add_norm", &[&x, &y, &l.post_ff_norm], &[u(h), f(eps), f(1.0)], Grid::Rows(rows, 1))?;
            run.linear(&x, &l.ple_gate, &y, rows, h, c.per_layer)?;
            run.kernel("gelu_mul_li", &[&y, &li, &mid], &[u(c.per_layer), u(c.layers * c.per_layer), u(i * c.per_layer)],
                       Grid::Threads(c.per_layer, rows))?;
            run.linear(&mid, &l.ple_proj, &y, rows, c.per_layer, h)?;
            run.kernel("add_norm", &[&x, &y, &l.ple_norm], &[u(h), f(eps), f(l.scalar)], Grid::Rows(rows, 1))?;
        }
        run.kernel("rms", &[&x, &self.norm, &y], &[u(h), f(eps), f(1.0), u(1)], Grid::Rows(rows, 1))?;
        run.linear(&y, &self.proj, &out, rows, h, c.embedding_dim)?;
        run.end();
        cb.commit();
        cb.waitUntilCompleted();
        if let Some(e) = cb.error() {
            bail!("the text model on Metal: {e}");
        }
        let mut v = vec![0f32; rows * c.embedding_dim];
        // SAFETY: the command buffer completed; the buffer is shared and holds rows × embedding_dim floats
        unsafe { std::ptr::copy_nonoverlapping(out.0.contents().as_ptr() as *const f32, v.as_mut_ptr(), v.len()) };
        Ok(v)
    }
}

enum Arg {
    U(u32),
    F(f32),
}

fn u(v: usize) -> Arg {
    Arg::U(v as u32)
}

fn f(v: f32) -> Arg {
    Arg::F(v)
}

enum Grid {
    /// One thread per (column, row).
    Threads(usize, usize),
    /// One thread per (column, head, row).
    Cube(usize, usize, usize),
    /// One threadgroup per (row, column) of the grid.
    Rows(usize, usize),
}

struct Run<'a> {
    fast: &'a Fast,
    cb: &'a ProtocolObject<dyn MTLCommandBuffer>,
    enc: Option<Obj<dyn MTLComputeCommandEncoder>>,
}

impl Run<'_> {
    fn end(&mut self) {
        if let Some(e) = self.enc.take() {
            e.endEncoding();
        }
    }

    fn kernel(&mut self, name: &str, bufs: &[&Buf], args: &[Arg], grid: Grid) -> anyhow::Result<()> {
        if self.enc.is_none() {
            self.enc = Some(self.cb.computeCommandEncoder().context("no compute encoder")?);
        }
        let Some(e) = &self.enc else { bail!("no compute encoder") };
        let p = self.fast.pipes.get(name).context("unknown kernel")?;
        e.setComputePipelineState(p);
        // SAFETY: the buffers live until the command buffer completes; each argument is 4 bytes, as the kernels
        // declare them
        unsafe {
            for (i, b) in bufs.iter().enumerate() {
                e.setBuffer_offset_atIndex(Some(&b.0), b.1, i);
            }
            for (j, a) in args.iter().enumerate() {
                let mut v = match a {
                    Arg::U(v) => v.to_ne_bytes(),
                    Arg::F(v) => v.to_ne_bytes(),
                };
                e.setBytes_length_atIndex(NonNull::new_unchecked(v.as_mut_ptr().cast()), 4, bufs.len() + j);
            }
        }
        let size = |w, h| MTLSize { width: w, height: h, depth: 1 };
        match grid {
            Grid::Threads(w, h) => e.dispatchThreads_threadsPerThreadgroup(size(w, h), size(w.min(256), 1)),
            Grid::Cube(w, h, d) => e.dispatchThreads_threadsPerThreadgroup(MTLSize { width: w, height: h, depth: d },
                                                                          size(w.min(256), 1)),
            Grid::Rows(r, c) => e.dispatchThreadgroups_threadsPerThreadgroup(size(r, c), size(256, 1)),
        }
        Ok(())
    }

    /// out (rows × n) = x (rows × k) times wᵀ, w being (n × k).
    fn linear(&mut self, x: &Buf, w: &Buf, out: &Buf, rows: usize, k: usize, n: usize) -> anyhow::Result<()> {
        let mat = |buf, rows, cols| Mat { buf, rows, cols, mats: 1 };
        self.product(&mat(x, rows, k), &mat(w, n, k), &mat(out, rows, n), true, 1)
    }

    /// c = a × b (or a × bᵀ), for `batch` matrices of each.
    fn product(&mut self, a: &Mat, b: &Mat, c: &Mat, trans_b: bool, batch: usize) -> anyhow::Result<()> {
        self.end();
        let k = a.cols;
        let key = (trans_b, c.rows, c.cols, k, batch);
        let mut cache = self.fast.products.lock().unwrap_or_else(|e| e.into_inner());
        let device = &self.fast.device;
        // SAFETY: plain initialisation of an MPS kernel for this device
        let mm = cache.entry(key).or_insert_with(|| unsafe {
            let mm = MPSMatrixMultiplication::initWithDevice_transposeLeft_transposeRight_resultRows_resultColumns_interiorColumns_alpha_beta(
                MPSMatrixMultiplication::alloc(), device, false, trans_b, c.rows, c.cols, k, 1.0, 0.0);
            mm.setBatchSize(batch);
            mm
        });
        // SAFETY: each descriptor covers memory inside its buffer, which lives until the command buffer completes
        unsafe {
            let mat = |m: &Mat| {
                let d = MPSMatrixDescriptor::matrixDescriptorWithRows_columns_matrices_rowBytes_matrixBytes_dataType(
                    m.rows, m.cols, m.mats, m.cols * 4, m.rows * m.cols * 4, MPSDataType::Float32);
                MPSMatrix::initWithBuffer_offset_descriptor(MPSMatrix::alloc(), &m.buf.0, m.buf.1, &d)
            };
            mm.encodeToCommandBuffer_leftMatrix_rightMatrix_resultMatrix(self.cb, &mat(a), &mat(b), &mat(c));
        }
        Ok(())
    }
}
