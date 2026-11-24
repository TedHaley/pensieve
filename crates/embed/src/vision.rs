//! The image encoder (transformers' Gemma4VisionModel, without clipped linears or standardization) and the
//! projection of its pooled patches into the text model's input (embed_vision), as 0.21's `gemma_mlx._vision`.
//! Each patch's position rotates its attention heads on two axes: the first half of a head by x, the second by y.

use std::path::Path;

use anyhow::bail;
use candle_core::{DType, Device, IndexOp, Result, Tensor};
use candle_nn::{Linear, VarBuilder};
use serde_json::Value;

use crate::image::Patches;
use crate::nn::{Rms, apply, heads_first, linear};
use crate::text::{attend, rope_tables};

struct Layer {
    q: Linear,
    k: Linear,
    v: Linear,
    o: Linear,
    q_norm: Rms,
    k_norm: Rms,
    gate: Linear,
    up: Linear,
    down: Linear,
    input_norm: Rms,
    post_attn_norm: Rms,
    pre_ff_norm: Rms,
    post_ff_norm: Rms,
}

pub struct Vision {
    input_proj: Linear,
    table: Tensor,
    layers: Vec<Layer>,
    proj: Linear,
    hidden: usize,
    heads: usize,
    head_dim: usize,
    patch: usize,
    pool: usize,
    image_tokens: usize,
    inv_freq: Vec<f32>,
    dtype: DType,
}

impl Vision {
    pub fn load(cfg: &Value, weights: &Path, dtype: DType, dev: &Device, text_hidden: usize, image_tokens: usize)
        -> anyhow::Result<Vision> {
        if cfg["use_clipped_linears"].as_bool() == Some(true) || cfg["standardize"].as_bool() == Some(true) {
            bail!("this image encoder has clipped linears or standardization, which the port lacks");
        }
        let u = |k: &str| cfg[k].as_u64().unwrap_or(0) as usize;
        let (hidden, heads, head_dim, inter) = (u("hidden_size"), u("num_attention_heads"), u("head_dim"), u("intermediate_size"));
        let patch = u("patch_size");
        // SAFETY: the weights file is mmapped read-only; it is not changed while the model is loaded
        let vb = unsafe { VarBuilder::from_mmaped_safetensors(&[weights], dtype, dev)? };
        let t = vb.pp("vision_tower");
        let mut layers = Vec::new();
        for i in 0..u("num_hidden_layers") {
            let l = t.pp(format!("encoder.layers.{i}"));
            let (a, m) = (l.pp("self_attn"), l.pp("mlp"));
            let lin = |vb: &VarBuilder, a, b, n: &str| linear(vb, a, b, &format!("{n}.linear"));
            layers.push(Layer {
                q: lin(&a, hidden, heads * head_dim, "q_proj")?,
                k: lin(&a, hidden, heads * head_dim, "k_proj")?,
                v: lin(&a, hidden, heads * head_dim, "v_proj")?,
                o: lin(&a, heads * head_dim, hidden, "o_proj")?,
                q_norm: Rms::load(&a, head_dim, "q_norm")?,
                k_norm: Rms::load(&a, head_dim, "k_norm")?,
                gate: lin(&m, hidden, inter, "gate_proj")?,
                up: lin(&m, hidden, inter, "up_proj")?,
                down: lin(&m, inter, hidden, "down_proj")?,
                input_norm: Rms::load(&l, hidden, "input_layernorm")?,
                post_attn_norm: Rms::load(&l, hidden, "post_attention_layernorm")?,
                pre_ff_norm: Rms::load(&l, hidden, "pre_feedforward_layernorm")?,
                post_ff_norm: Rms::load(&l, hidden, "post_feedforward_layernorm")?,
            });
        }
        let half = head_dim / 2;
        let theta = cfg["rope_parameters"]["rope_theta"].as_f64().unwrap_or(100.0);
        let inv_freq = (0..half / 2).map(|j| (1.0 / theta.powf(2.0 * j as f64 / half as f64)) as f32).collect();
        Ok(Vision {
            input_proj: linear(&t.pp("patch_embedder"), 3 * patch * patch, hidden, "input_proj")?,
            table: t.get((2, u("position_embedding_size"), hidden), "patch_embedder.position_embedding_table")?,
            proj: linear(&vb.pp("embed_vision"), hidden, text_hidden, "embedding_projection")?,
            layers,
            hidden,
            heads,
            head_dim,
            patch,
            pool: u("pooling_kernel_size"),
            image_tokens,
            inv_freq,
            dtype,
        })
    }

    pub fn patch(&self) -> usize {
        self.patch
    }

    pub fn pool(&self) -> usize {
        self.pool
    }

    pub fn image_tokens(&self) -> usize {
        self.image_tokens
    }

    /// The soft tokens of pictures that share one grid: (b, tokens, text hidden), in the weights' type.
    pub fn soft_tokens(&self, pics: &[Patches]) -> anyhow::Result<Tensor> {
        let dev = self.table.device().clone();
        let (rows, cols) = pics[0].grid;
        if pics.iter().any(|p| p.grid != (rows, cols)) {
            bail!("pictures of different sizes in one batch");
        }
        let (b, n) = (pics.len(), rows * cols);
        let px: Vec<f32> = pics.iter().flat_map(|p| p.pixels.iter().map(|v| 2.0 * (v - 0.5))).collect();
        let px = Tensor::from_vec(px, (b, n, 3 * self.patch * self.patch), &dev)?.to_dtype(self.dtype)?;
        let xs: Vec<u32> = pics[0].pos.iter().map(|p| p.0).collect();
        let ys: Vec<u32> = pics[0].pos.iter().map(|p| p.1).collect();
        let (xi, yi) = (Tensor::new(xs.as_slice(), &dev)?, Tensor::new(ys.as_slice(), &dev)?);
        let pe = (self.table.i(0)?.index_select(&xi, 0)? + self.table.i(1)?.index_select(&yi, 0)?)?;
        let mut x = apply(&self.input_proj, &px)?.broadcast_add(&pe.unsqueeze(0)?)?;
        let rope = |pos: &[u32]| -> Result<(Tensor, Tensor)> {
            // rope_tables works on positions 0..n; gather each patch's row of it
            let (c, s) = rope_tables(&self.inv_freq, rows.max(cols), &dev, self.dtype)?;
            let i = Tensor::new(pos, &dev)?;
            Ok((c.index_select(&i, 0)?.contiguous()?, s.index_select(&i, 0)?.contiguous()?))
        };
        let (cx, sx) = rope(&xs)?;
        let (cy, sy) = rope(&ys)?;
        let mask = Tensor::zeros((1, 1, 1, n), DType::F32, &dev)?;
        for l in &self.layers {
            x = l.forward(&x, self, [&cx, &sx, &cy, &sy], &mask)?;
        }
        // the mean of each pool × pool block of patches, scaled by sqrt(hidden), in float32
        let k = self.pool;
        let x = x.to_dtype(DType::F32)?.reshape((b, rows / k, k, cols / k, k, self.hidden))?;
        let x = (x.sum(4)?.sum(2)? / (k * k) as f64)?.reshape((b, (rows / k) * (cols / k), self.hidden))?;
        let x = (x * (self.hidden as f64).sqrt())?;
        Ok(apply(&self.proj, &Rms::plain().forward(&x)?.to_dtype(self.dtype)?)?)
    }
}

impl Layer {
    fn forward(&self, x: &Tensor, v: &Vision, cs: [&Tensor; 4], mask: &Tensor) -> Result<Tensor> {
        let (b, n, _) = x.dims3()?;
        let (h, d) = (v.heads, v.head_dim);
        let y = self.input_norm.forward(x)?;
        let split = |t: Tensor| t.reshape((b, n, h, d));
        let rot = |t: Tensor| -> Result<Tensor> {
            let t = heads_first(&t)?;
            let a = candle_nn::rotary_emb::rope(&t.narrow(3, 0, d / 2)?.contiguous()?, cs[0], cs[1])?;
            let c = candle_nn::rotary_emb::rope(&t.narrow(3, d / 2, d / 2)?.contiguous()?, cs[2], cs[3])?;
            Tensor::cat(&[a, c], 3)
        };
        let q = rot(self.q_norm.forward(&split(apply(&self.q, &y)?)?)?)?;
        let k = rot(self.k_norm.forward(&split(apply(&self.k, &y)?)?)?)?;
        let vv = heads_first(&Rms::plain().forward(&split(apply(&self.v, &y)?)?)?)?;
        let a = attend(&q, &k, &vv, mask, 1)?;
        let a = apply(&self.o, &a.transpose(1, 2)?.reshape((b, n, h * d))?)?;
        let x = (x + self.post_attn_norm.forward(&a)?)?;
        let y = self.pre_ff_norm.forward(&x)?;
        let m = apply(&self.down, &(apply(&self.gate, &y)?.gelu()? * apply(&self.up, &y)?)?)?;
        x + self.post_ff_norm.forward(&m)?
    }
}
