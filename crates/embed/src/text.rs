//! EmbeddingGemma 2's text model (transformers' `EmbeddingGemma2Model` text part), as 0.21's `gemma_mlx._model`
//! builds it: Gemma 4 layers with per-layer inputs (PLE), bidirectional attention, and a projection to 768 numbers.
//! It reads token ids, or input embeddings where some positions are an image's or a sound's soft tokens.

use candle_core::{D, DType, Device, IndexOp, Result, Tensor};
use candle_nn::{Linear, VarBuilder};
use serde_json::Value;

use crate::nn::{Rms, apply, heads_first, linear};

pub(crate) mod fast;

/// The parts of `text_config` that the model needs.
#[derive(Clone, Debug)]
pub struct Config {
    pub hidden: usize,
    pub per_layer: usize,
    pub layers: usize,
    pub heads: usize,
    pub intermediate: usize,
    pub vocab: usize,
    pub embedding_dim: usize,
    pub sliding_window: usize,
    /// Per layer: head size, key/value heads, whether it is a sliding layer, and its rope theta.
    pub layer: Vec<(usize, usize, bool, f64)>,
}

impl Config {
    pub fn from_json(c: &Value) -> anyhow::Result<Config> {
        let u = |k: &str| c[k].as_u64().map(|v| v as usize).ok_or_else(|| anyhow::anyhow!("text_config.{k} missing"));
        let layers = u("num_hidden_layers")?;
        let mut layer = Vec::new();
        for i in 0..layers {
            let over = &c["per_layer_config"][format!("{i:02}")];
            let hd = over["head_dim"].as_u64().map(|v| v as usize).unwrap_or(u("head_dim")?);
            let kv = over["num_key_value_heads"].as_u64().map(|v| v as usize).unwrap_or(u("num_key_value_heads")?);
            let kind = c["layer_types"][i].as_str().unwrap_or("full_attention");
            let theta = c["rope_parameters"][kind]["rope_theta"].as_f64().unwrap_or(10000.0);
            layer.push((hd, kv, kind == "sliding_attention", theta));
        }
        Ok(Config {
            hidden: u("hidden_size")?,
            per_layer: u("hidden_size_per_layer_input")?,
            layers,
            heads: u("num_attention_heads")?,
            intermediate: u("intermediate_size")?,
            vocab: u("vocab_size")?,
            embedding_dim: u("embedding_dim")?,
            sliding_window: u("sliding_window")?,
            layer,
        })
    }
}

struct Attention {
    q: Linear,
    k: Linear,
    v: Linear,
    o: Linear,
    q_norm: Rms,
    k_norm: Rms,
    head_dim: usize,
    kv_heads: usize,
    sliding: bool,
    inv_freq: Vec<f32>,
}

struct Layer {
    attn: Attention,
    gate: Linear,
    up: Linear,
    down: Linear,
    ple_gate: Linear,
    ple_proj: Linear,
    ple_norm: Rms,
    input_norm: Rms,
    post_attn_norm: Rms,
    pre_ff_norm: Rms,
    post_ff_norm: Rms,
    scalar: Tensor,
}

pub struct TextModel {
    pub cfg: Config,
    embed: Tensor,
    layers: Vec<Layer>,
    norm: Rms,
    ple_proj: Linear,
    ple_norm: Rms,
    proj: Linear,
    scale: f64,
    dtype: DType,
    device: Device,
}

impl TextModel {
    /// The model from `vb`, whose prefix is the text model's (`language_model`).
    pub fn load(cfg: Config, vb: VarBuilder) -> Result<TextModel> {
        let h = cfg.hidden;
        let mut layers = Vec::new();
        for i in 0..cfg.layers {
            let (hd, kv, sliding, theta) = cfg.layer[i];
            let l = vb.pp(format!("layers.{i}"));
            let a = l.pp("self_attn");
            let inv_freq = (0..hd / 2).map(|j| (1.0 / theta.powf(2.0 * j as f64 / hd as f64)) as f32).collect();
            layers.push(Layer {
                attn: Attention {
                    q: linear(&a, h, cfg.heads * hd, "q_proj")?,
                    k: linear(&a, h, kv * hd, "k_proj")?,
                    v: linear(&a, h, kv * hd, "v_proj")?,
                    o: linear(&a, cfg.heads * hd, h, "o_proj")?,
                    q_norm: Rms::load(&a, hd, "q_norm")?,
                    k_norm: Rms::load(&a, hd, "k_norm")?,
                    head_dim: hd,
                    kv_heads: kv,
                    sliding,
                    inv_freq,
                },
                gate: linear(&l.pp("mlp"), h, cfg.intermediate, "gate_proj")?,
                up: linear(&l.pp("mlp"), h, cfg.intermediate, "up_proj")?,
                down: linear(&l.pp("mlp"), cfg.intermediate, h, "down_proj")?,
                ple_gate: linear(&l.pp("ple_block"), h, cfg.per_layer, "per_layer_input_gate")?,
                ple_proj: linear(&l.pp("ple_block"), cfg.per_layer, h, "per_layer_projection")?,
                ple_norm: Rms::load(&l.pp("ple_block"), h, "post_per_layer_input_norm")?,
                input_norm: Rms::load(&l, h, "input_layernorm")?,
                post_attn_norm: Rms::load(&l, h, "post_attention_layernorm")?,
                pre_ff_norm: Rms::load(&l, h, "pre_feedforward_layernorm")?,
                post_ff_norm: Rms::load(&l, h, "post_feedforward_layernorm")?,
                scalar: l.get(1, "layer_scalar")?,
            });
        }
        // the embedding scale rounds like the reference's bfloat16 weights: sqrt(512) -> 22.625
        let scale = half::bf16::from_f64((h as f64).sqrt()).to_f64();
        Ok(TextModel {
            embed: vb.get((cfg.vocab, h), "embed_tokens.weight")?,
            norm: Rms::load(&vb, h, "norm")?,
            ple_proj: linear(&vb.pp("ple"), h, cfg.layers * cfg.per_layer, "per_layer_model_projection")?,
            ple_norm: Rms::load(&vb.pp("ple"), cfg.per_layer, "per_layer_projection_norm")?,
            proj: linear(&vb, h, cfg.embedding_dim, "embedding_projection")?,
            layers,
            scale,
            dtype: vb.dtype(),
            device: vb.device().clone(),
            cfg,
        })
    }

    pub fn dtype(&self) -> DType {
        self.dtype
    }

    pub fn device(&self) -> &Device {
        &self.device
    }

    /// Token embeddings, scaled: ids (b, n) -> (b, n, hidden).
    pub fn embed(&self, ids: &Tensor) -> Result<Tensor> {
        let (b, n) = ids.dims2()?;
        let e = self.embed.index_select(&ids.flatten_all()?, 0)?.reshape((b, n, self.cfg.hidden))?;
        e * self.scale
    }

    /// The last hidden states projected to the embedding size: x (b, n, hidden) input embeddings, keep (b, n) 1 for
    /// a real position and 0 for padding. Returns (b, n, embedding_dim) in float32.
    pub fn forward(&self, x: &Tensor, keep: &Tensor) -> Result<Tensor> {
        let (b, n, _) = x.dims3()?;
        let c = &self.cfg;
        let li = (apply(&self.ple_proj, x)? * (c.hidden as f64).powf(-0.5))?.reshape((b, n, c.layers, c.per_layer))?;
        let li = self.ple_norm.forward(&li)?;
        let masks = self.masks(keep, n)?;
        let mut x = x.clone();
        for (i, l) in self.layers.iter().enumerate() {
            let input = li.i((.., .., i, ..))?;
            let mask = if l.attn.sliding { &masks.1 } else { &masks.0 };
            x = l.forward(&x, &input, mask, c.heads)?;
        }
        apply(&self.proj, &self.norm.forward(&x)?)?.to_dtype(DType::F32)
    }

    /// Additive float32 masks, full and sliding: (b, 1, 1 or n, n). Padding is never attended to; a sliding layer
    /// sees the positions at most sliding_window away.
    fn masks(&self, keep: &Tensor, n: usize) -> Result<(Tensor, Tensor)> {
        let keep = keep.to_dtype(DType::F32)?;
        let full = ((&keep - 1.0)? * 1e30)?.unsqueeze(1)?.unsqueeze(1)?;
        if n <= self.cfg.sliding_window + 1 {
            return Ok((full.clone(), full));
        }
        let w = self.cfg.sliding_window as i64;
        let near: Vec<f32> = (0..n as i64)
            .flat_map(|i| (0..n as i64).map(move |j| if (i - j).abs() <= w { 0.0 } else { -1e30 }))
            .collect();
        let near = Tensor::from_vec(near, (1, 1, n, n), keep.device())?;
        Ok((full.clone(), full.broadcast_add(&near)?))
    }
}

impl Layer {
    fn forward(&self, x: &Tensor, input: &Tensor, mask: &Tensor, heads: usize) -> Result<Tensor> {
        let a = self.attn.forward(&self.input_norm.forward(x)?, mask, heads)?;
        let x = (x + self.post_attn_norm.forward(&a)?)?;
        let h = self.pre_ff_norm.forward(&x)?;
        let m = apply(&self.down, &(apply(&self.gate, &h)?.gelu()? * apply(&self.up, &h)?)?)?;
        let x = (&x + self.post_ff_norm.forward(&m)?)?;
        let y = apply(&self.ple_proj, &(apply(&self.ple_gate, &x)?.gelu()? * input)?)?;
        (x + self.ple_norm.forward(&y)?)?.broadcast_mul(&self.scalar)
    }
}

impl Attention {
    fn forward(&self, x: &Tensor, mask: &Tensor, heads: usize) -> Result<Tensor> {
        let (b, n, _) = x.dims3()?;
        let hd = self.head_dim;
        let (cos, sin) = rope_tables(&self.inv_freq, n, x.device(), x.dtype())?;
        let split = |t: Tensor, h: usize| t.reshape((b, n, h, hd));
        let q = heads_first(&self.q_norm.forward(&split(apply(&self.q, x)?, heads)?)?)?;
        let k = heads_first(&self.k_norm.forward(&split(apply(&self.k, x)?, self.kv_heads)?)?)?;
        let v = heads_first(&Rms::plain().forward(&split(apply(&self.v, x)?, self.kv_heads)?)?)?;
        let q = candle_nn::rotary_emb::rope(&q, &cos, &sin)?;
        let k = candle_nn::rotary_emb::rope(&k, &cos, &sin)?;
        let out = attend(&q, &k, &v, mask, heads / self.kv_heads)?;
        apply(&self.o, &out.transpose(1, 2)?.reshape((b, n, heads * hd))?)
    }
}

/// cos and sin of each position times each frequency: (n, freqs).
pub fn rope_tables(inv_freq: &[f32], n: usize, dev: &Device, dt: DType) -> Result<(Tensor, Tensor)> {
    let f = Tensor::from_slice(inv_freq, (1, inv_freq.len()), dev)?;
    let pos = Tensor::arange(0u32, n as u32, dev)?.to_dtype(DType::F32)?.reshape((n, 1))?;
    let a = pos.broadcast_mul(&f)?;
    Ok((a.cos()?.to_dtype(dt)?, a.sin()?.to_dtype(dt)?))
}

/// softmax(q kᵀ + mask) v with scale 1, the softmax in float32. k and v have `rep` times fewer heads than q.
pub fn attend(q: &Tensor, k: &Tensor, v: &Tensor, mask: &Tensor, rep: usize) -> Result<Tensor> {
    let (b, h, n, d) = q.dims4()?;
    // Metal's fused kernel, where it has one for the head size in float32 (the image encoder's)
    if q.device().is_metal() && d == 64 && std::env::var_os("EMBED_NO_SDPA").is_none() {
        let m = mask.to_dtype(q.dtype())?.broadcast_as((b, h, n, k.dim(2)?))?;
        return candle_nn::ops::sdpa(q, k, v, Some(&m), false, 1.0, 1.0);
    }
    let kv = h / rep;
    let m = k.dim(2)?;
    // grouped heads: the rep query heads of one key head become rows of one matmul
    let q = q.reshape((b, kv, rep * n, d))?;
    let s = q.matmul(&k.t()?)?.to_dtype(DType::F32)?.reshape((b, h, n, m))?;
    let p = candle_nn::ops::softmax_last_dim(&s.broadcast_add(mask)?)?.to_dtype(v.dtype())?;
    p.reshape((b, kv, rep * n, m))?.matmul(&v.contiguous()?)?.reshape((b, h, n, v.dim(D::Minus1)?))
}
