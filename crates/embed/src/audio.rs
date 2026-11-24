//! The sound encoder, as 0.21's `gemma_mlx._audio` builds it: log-mel features (transformers'
//! Gemma4AudioFeatureExtractor), two strided convolutions, then a conformer whose attention sees the 12 frames up
//! to each frame, and the projection of its frames into the text model's input (embed_audio). Each output frame
//! covers 40 ms.

use std::path::Path;

use anyhow::bail;
use candle_core::{D, DType, Device, Module, Result, Tensor};
use candle_nn::{Linear, VarBuilder};
use serde_json::Value;

use crate::nn::{EPS, Rms, apply, linear};

/// Samples per second, and the most seconds the model reads.
pub const RATE: usize = 16000;
pub const MAX_SECONDS: usize = 30;

const FRAME: usize = 320;
const HOP: usize = 160;
const FFT: usize = 512;
const MELS: usize = 128;
const MEL_FLOOR: f64 = 1e-3;

/// The log-mel features of a sound and which frames are sound (not padding), as the feature extractor makes them
/// for a batch of one: padded to a multiple of 128 samples, half a frame of silence in front.
pub fn log_mel(samples: &[f32]) -> (Vec<[f32; MELS]>, Vec<bool>) {
    let s = &samples[..samples.len().min(RATE * MAX_SECONDS)];
    let padded = s.len().next_multiple_of(128).max(128);
    let pad_left = FRAME / 2;
    let total = pad_left + padded;
    let wave = |i: usize| if i >= pad_left && i - pad_left < s.len() { s[i - pad_left] } else { 0.0 };
    let real = |i: usize| i >= pad_left && i - pad_left < s.len();
    let frames = if total > FRAME { (total - FRAME - 1) / HOP + 1 } else { 0 };
    let window: Vec<f32> = (0..FRAME).map(|n| (0.5 - 0.5 * (2.0 * std::f64::consts::PI * n as f64 / FRAME as f64).cos()) as f32).collect();
    let filters = mel_filters();
    let mut planner = rustfft::FftPlanner::<f64>::new();
    let fft = planner.plan_fft_forward(FFT);
    let mut out = Vec::with_capacity(frames);
    let mut keep = Vec::with_capacity(frames);
    let mut buf = vec![rustfft::num_complex::Complex::new(0.0, 0.0); FFT];
    for f in 0..frames {
        let start = f * HOP;
        for (n, b) in buf.iter_mut().enumerate() {
            let v = if n < FRAME { (wave(start + n) * window[n]) as f64 } else { 0.0 };
            *b = rustfft::num_complex::Complex::new(v, 0.0);
        }
        fft.process(&mut buf);
        let mag: Vec<f64> = buf[..FFT / 2 + 1].iter().map(|c| c.norm()).collect();
        let ok = real(start + FRAME);
        let mut row = [0f32; MELS];
        for (m, r) in row.iter_mut().enumerate() {
            let e: f64 = mag.iter().enumerate().map(|(k, v)| v * filters[k * MELS + m]).sum();
            *r = if ok { (e + MEL_FLOOR).ln() as f32 } else { 0.0 };
        }
        out.push(row);
        keep.push(ok);
    }
    (out, keep)
}

/// transformers' `mel_filter_bank` with the HTK mel scale and no norm: (257 bins, 128 filters), row-major.
fn mel_filters() -> Vec<f64> {
    let mel = |f: f64| 2595.0 * (1.0 + f / 700.0).log10();
    let hz = |m: f64| 700.0 * (10f64.powf(m / 2595.0) - 1.0);
    let (lo, hi) = (mel(0.0), mel(8000.0));
    let freqs: Vec<f64> = (0..MELS + 2).map(|i| hz(lo + (hi - lo) * i as f64 / (MELS + 1) as f64)).collect();
    let bins = FFT / 2 + 1;
    let mut out = vec![0.0; bins * MELS];
    for k in 0..bins {
        let f = (RATE / 2) as f64 * k as f64 / (bins - 1) as f64;
        for m in 0..MELS {
            let down = -(freqs[m] - f) / (freqs[m + 1] - freqs[m]);
            let up = (freqs[m + 2] - f) / (freqs[m + 2] - freqs[m + 1]);
            out[k * MELS + m] = down.min(up).max(0.0);
        }
    }
    out
}

/// A linear layer whose input and output are clamped to ranges learned in training (ClippableLinear).
struct Clipped {
    l: Linear,
    lim: [f64; 4],
}

impl Clipped {
    fn load(vb: &VarBuilder, a: usize, b: usize, name: &str) -> Result<Clipped> {
        let v = vb.pp(name);
        let s = |n: &str| -> Result<f64> { Ok(v.get((), n)?.to_dtype(DType::F32)?.to_scalar::<f32>()? as f64) };
        Ok(Clipped { l: linear(&v, a, b, "linear")?, lim: [s("input_min")?, s("input_max")?, s("output_min")?, s("output_max")?] })
    }

    fn forward(&self, x: &Tensor) -> Result<Tensor> {
        apply(&self.l, &x.clamp(self.lim[0], self.lim[1])?)?.clamp(self.lim[2], self.lim[3])
    }
}

struct FeedForward {
    l1: Clipped,
    l2: Clipped,
    pre: Rms,
    post: Rms,
}

impl FeedForward {
    fn load(vb: &VarBuilder, h: usize) -> Result<FeedForward> {
        Ok(FeedForward {
            l1: Clipped::load(vb, h, 4 * h, "ffw_layer_1")?,
            l2: Clipped::load(vb, 4 * h, h, "ffw_layer_2")?,
            pre: Rms::load(vb, h, "pre_layer_norm")?,
            post: Rms::load(vb, h, "post_layer_norm")?,
        })
    }

    fn forward(&self, x: &Tensor, residual: f64) -> Result<Tensor> {
        let y = self.l2.forward(&candle_nn::ops::silu(&self.l1.forward(&self.pre.forward(x)?)?)?)?;
        x + (self.post.forward(&y)? * residual)?
    }
}

struct Layer {
    ff1: FeedForward,
    ff2: FeedForward,
    q: Clipped,
    k: Clipped,
    v: Clipped,
    post: Clipped,
    rel_k: Linear,
    per_dim_scale: Tensor,
    conv_start: Clipped,
    conv_end: Clipped,
    conv_w: Tensor,
    conv_pre: Rms,
    conv_norm: Rms,
    norm_pre_attn: Rms,
    norm_post_attn: Rms,
    norm_out: Rms,
}

pub struct Audio {
    conv0: (Tensor, Tensor),
    conv1: (Tensor, Tensor),
    input_proj: Linear,
    layers: Vec<Layer>,
    out_proj: candle_nn::Linear,
    proj: Linear,
    rel: Tensor,
    hidden: usize,
    heads: usize,
    span: usize,
    cap: f64,
    invalid: f64,
    residual: f64,
    kernel: usize,
    dtype: DType,
}

impl Audio {
    pub fn load(cfg: &Value, weights: &Path, dtype: DType, dev: &Device, text_hidden: usize) -> anyhow::Result<Audio> {
        if cfg["attention_context_right"].as_u64().unwrap_or(0) != 0 {
            bail!("the sound encoder sees only past frames; this model also looks ahead");
        }
        let u = |k: &str| cfg[k].as_u64().unwrap_or(0) as usize;
        let (h, heads) = (u("hidden_size"), u("num_attention_heads"));
        let span = u("attention_context_left") - 1;
        let kernel = u("conv_kernel_size");
        let ch: Vec<usize> = cfg["subsampling_conv_channels"].as_array().map(|a| a.iter().filter_map(|v| v.as_u64()).map(|v| v as usize).collect()).unwrap_or_default();
        let (c0, c1) = (ch[0], ch[1]);
        // SAFETY: the weights file is mmapped read-only; it is not changed while the model is loaded
        let vb = unsafe { VarBuilder::from_mmaped_safetensors(&[weights], dtype, dev)? };
        let t = vb.pp("audio_tower");
        let sub = t.pp("subsample_conv_projection");
        let conv = |i: usize, a: usize, b: usize| -> Result<(Tensor, Tensor)> {
            Ok((sub.get((b, a, 3, 3), &format!("layer{i}.conv.weight"))?, sub.get(b, &format!("layer{i}.norm.weight"))?))
        };
        let mut layers = Vec::new();
        for i in 0..u("num_hidden_layers") {
            let l = t.pp(format!("layers.{i}"));
            let (a, c) = (l.pp("self_attn"), l.pp("lconv1d"));
            layers.push(Layer {
                ff1: FeedForward::load(&l.pp("feed_forward1"), h)?,
                ff2: FeedForward::load(&l.pp("feed_forward2"), h)?,
                q: Clipped::load(&a, h, h, "q_proj")?,
                k: Clipped::load(&a, h, h, "k_proj")?,
                v: Clipped::load(&a, h, h, "v_proj")?,
                post: Clipped::load(&a, h, h, "post")?,
                rel_k: linear(&a, h, h, "relative_k_proj")?,
                per_dim_scale: a.get(h / heads, "per_dim_scale")?.to_dtype(DType::F32)?,
                conv_start: Clipped::load(&c, h, 2 * h, "linear_start")?,
                conv_end: Clipped::load(&c, h, h, "linear_end")?,
                conv_w: c.get((h, 1, kernel), "depthwise_conv1d.weight")?.reshape((h, kernel))?.t()?.contiguous()?,
                conv_pre: Rms::load(&c, h, "pre_layer_norm")?,
                conv_norm: Rms::load(&c, h, "conv_norm")?,
                norm_pre_attn: Rms::load(&l, h, "norm_pre_attn")?,
                norm_post_attn: Rms::load(&l, h, "norm_post_attn")?,
                norm_out: Rms::load(&l, h, "norm_out")?,
            });
        }
        let out_dims = u("output_proj_dims");
        let half = h / 2;
        let step = -(10000f64.ln() / (half.max(2) - 1) as f64);
        let mut rel = Vec::with_capacity((span + 1) * h);
        for d in (0..=span).rev() {
            let a: Vec<f32> = (0..half).map(|i| (d as f32) * ((i as f64 * step).exp() as f32)).collect();
            rel.extend(a.iter().map(|v| v.sin()));
            rel.extend(a.iter().map(|v| v.cos()));
        }
        Ok(Audio {
            conv0: conv(0, 1, c0)?,
            conv1: conv(1, c0, c1)?,
            input_proj: linear(&sub, c0 / 4 * c1, h, "input_proj_linear")?,
            layers,
            out_proj: candle_nn::Linear::new(t.get((out_dims, h), "output_proj.weight")?, Some(t.get(out_dims, "output_proj.bias")?)),
            proj: linear(&vb.pp("embed_audio"), out_dims, text_hidden, "embedding_projection")?,
            rel: Tensor::from_vec(rel, (span + 1, h), dev)?.to_dtype(dtype)?,
            hidden: h,
            heads,
            span,
            cap: cfg["attention_logit_cap"].as_f64().unwrap_or(50.0),
            invalid: cfg["attention_invalid_logits_value"].as_f64().unwrap_or(-1e9),
            residual: cfg["residual_weight"].as_f64().unwrap_or(0.5),
            kernel,
            dtype,
        })
    }

    /// The soft tokens of a sound (mono samples at RATE): (tokens, text hidden), only the frames that are sound.
    pub fn soft_tokens(&self, samples: &[f32]) -> anyhow::Result<Tensor> {
        let dev = self.rel.device().clone();
        let (mel, keep) = log_mel(samples);
        let n = mel.len();
        let flat: Vec<f32> = mel.iter().flat_map(|r| r.iter().copied()).collect();
        let x = Tensor::from_vec(flat, (1, 1, n, MELS), &dev)?;
        let (x, keep) = self.conv_layer(&x, &keep, &self.conv0)?;
        let (x, keep) = self.conv_layer(&x, &keep, &self.conv1)?;
        // (1, channels, time, frequency) -> (1, time, frequency × channels)
        let (_, c, t, f) = x.dims4()?;
        let x = x.permute((0, 2, 3, 1))?.reshape((1, t, f * c))?;
        let mut x = apply(&self.input_proj, &x)?;
        let ok = self.valid(&keep, &dev)?;
        for l in &self.layers {
            x = self.layer(l, &x, &ok)?;
        }
        let x = self.out_proj.forward(&x.contiguous()?)?;
        let x = apply(&self.proj, &Rms::plain().forward(&x.to_dtype(DType::F32)?)?.to_dtype(self.dtype)?)?;
        let idx: Vec<u32> = keep.iter().enumerate().filter(|(_, k)| **k).map(|(i, _)| i as u32).collect();
        if idx.is_empty() {
            bail!("no sound");
        }
        Ok(x.squeeze(0)?.index_select(&Tensor::new(idx.as_slice(), &dev)?, 0)?)
    }

    /// A 3×3 convolution with stride 2 over (time, frequency) of the frames that are sound, a LayerNorm over the
    /// channels (no bias), ReLU. Every second frame stays.
    fn conv_layer(&self, x: &Tensor, keep: &[bool], (w, norm): &(Tensor, Tensor)) -> Result<(Tensor, Vec<bool>)> {
        let n = keep.len();
        let k: Vec<f32> = keep.iter().map(|&b| if b { 1.0 } else { 0.0 }).collect();
        let k = Tensor::from_vec(k, (1, 1, n, 1), x.device())?;
        let x = x.to_dtype(DType::F32)?.broadcast_mul(&k)?.to_dtype(w.dtype())?;
        let y = x.conv2d(w, 1, 2, 1, 1)?.permute((0, 2, 3, 1))?.contiguous()?;
        let y = layer_norm(&y, norm)?.relu()?.permute((0, 3, 1, 2))?.contiguous()?;
        Ok((y, keep.iter().step_by(2).copied().collect()))
    }

    /// (n, span) 1 where key j of query i is frame i - j, a frame that is sound, and query i is sound.
    fn valid(&self, keep: &[bool], dev: &Device) -> Result<Tensor> {
        let n = keep.len();
        let mut ok = vec![0u8; n * self.span];
        for i in 0..n {
            for j in 0..self.span.min(i + 1) {
                ok[i * self.span + j] = (keep[i] && keep[i - j]) as u8;
            }
        }
        Tensor::from_vec(ok, (n, self.span, 1), dev)
    }

    fn layer(&self, l: &Layer, x: &Tensor, ok: &Tensor) -> Result<Tensor> {
        let x = l.ff1.forward(x, self.residual)?;
        let a = self.attention(l, &l.norm_pre_attn.forward(&x)?, ok)?;
        let x = (&x + l.norm_post_attn.forward(&a)?)?;
        let x = self.light_conv(l, &x)?;
        l.norm_out.forward(&l.ff2.forward(&x, self.residual)?)
    }

    /// Local attention over the span frames up to each frame, with a learned relative position term and a logit
    /// cap. x is (1, n, hidden).
    fn attention(&self, l: &Layer, x: &Tensor, ok: &Tensor) -> Result<Tensor> {
        let n = x.dim(1)?;
        let (h, d, span) = (self.heads, self.hidden / self.heads, self.span);
        let f32 = |t: Tensor| t.to_dtype(DType::F32)?.reshape((n, h, d));
        let scale = (d as f64).powf(-0.5) / 2f64.ln();
        let softplus = (l.per_dim_scale.exp()? + 1.0)?.log()?;
        let q = f32(l.q.forward(x)?)?.broadcast_mul(&(softplus * scale)?)?;
        let k = (f32(l.k.forward(x)?)? * ((1.0 + std::f64::consts::E).ln() / 2f64.ln()))?;
        let v = f32(l.v.forward(x)?)?;
        // distances 0..span
        let rk = apply(&l.rel_k, &self.rel)?.to_dtype(DType::F32)?.reshape((span + 1, h, d))?;
        let pad = Tensor::zeros((span - 1, h, d), DType::F32, x.device())?;
        let (kp, vp) = (Tensor::cat(&[&pad, &k], 0)?, Tensor::cat(&[&pad, &v], 0)?);
        let mut logits = Vec::with_capacity(span);
        let mut vs = Vec::with_capacity(span);
        for j in 0..span {
            let kj = kp.narrow(0, span - 1 - j, n)?;
            let rj = rk.get(span - j)?.unsqueeze(0)?;
            logits.push(q.broadcast_mul(&kj.broadcast_add(&rj)?)?.sum(D::Minus1)?);
            vs.push(vp.narrow(0, span - 1 - j, n)?);
        }
        let logits = ((Tensor::stack(&logits, 1)? / self.cap)?.tanh()? * self.cap)?;
        let invalid = Tensor::full(self.invalid as f32, logits.shape(), x.device())?;
        let logits = ok.broadcast_as(logits.shape())?.where_cond(&logits, &invalid)?;
        let w = candle_nn::ops::softmax(&logits, 1)?;
        let vs = Tensor::stack(&vs, 1)?;
        let out = vs.broadcast_mul(&w.unsqueeze(3)?)?.sum(1)?.reshape((1, n, self.hidden))?;
        l.post.forward(&out.to_dtype(x.dtype())?)
    }

    /// GLU, a causal depthwise convolution over time, norm, SiLU, back to hidden; added to x.
    fn light_conv(&self, l: &Layer, x: &Tensor) -> Result<Tensor> {
        let hd = self.hidden;
        let y = l.conv_start.forward(&l.conv_pre.forward(x)?)?;
        let y = (y.narrow(2, 0, hd)? * candle_nn::ops::sigmoid(&y.narrow(2, hd, hd)?)?)?.squeeze(0)?;
        let n = y.dim(0)?;
        let y = Tensor::cat(&[&Tensor::zeros((self.kernel - 1, hd), y.dtype(), y.device())?, &y], 0)?;
        let mut acc = y.narrow(0, 0, n)?.broadcast_mul(&l.conv_w.get(0)?)?;
        for k in 1..self.kernel {
            acc = (acc + y.narrow(0, k, n)?.broadcast_mul(&l.conv_w.get(k)?)?)?;
        }
        let y = l.conv_end.forward(&candle_nn::ops::silu(&l.conv_norm.forward(&acc.unsqueeze(0)?)?)?)?;
        x + y
    }
}

/// LayerNorm over the last axis with a weight and no bias, in float32.
fn layer_norm(x: &Tensor, w: &Tensor) -> Result<Tensor> {
    let dt = x.dtype();
    let x = x.to_dtype(DType::F32)?;
    let mean = x.mean_keepdim(D::Minus1)?;
    let c = x.broadcast_sub(&mean)?;
    let var = c.sqr()?.mean_keepdim(D::Minus1)?;
    c.broadcast_div(&(var + EPS)?.sqrt()?)?.broadcast_mul(&w.to_dtype(DType::F32)?)?.to_dtype(dt)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frames_past_the_sound_are_padding() {
        let s: Vec<f32> = (0..15000).map(|i| (i as f32 * 0.05).sin() * 0.1).collect();
        let (mel, keep) = log_mel(&s);
        // 15000 samples pad to 15104 (a multiple of 128), plus 160 in front
        assert_eq!(mel.len(), (15264 - 321) / 160 + 1);
        assert!(keep[0] && !keep[mel.len() - 1]);
        assert!(mel[10].iter().all(|v| v.is_finite()));
    }

    #[test]
    fn mel_filters_are_triangles_up_to_one() {
        let f = mel_filters();
        let bins = FFT / 2 + 1;
        // the lowest filters are narrower than a bin, and some see no bin at all, as in transformers
        let peaks: Vec<f64> = (0..MELS).map(|m| (0..bins).map(|k| f[k * MELS + m]).fold(0.0, f64::max)).collect();
        assert!(peaks.iter().all(|p| *p <= 1.0));
        assert!(peaks[MELS / 4..].iter().all(|p| *p > 0.0));
    }
}
