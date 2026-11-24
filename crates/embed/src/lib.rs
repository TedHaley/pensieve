//! EmbeddingGemma 2 on the Mac's GPU (Metal, through candle): one 768-number vector for a text, an image, a window
//! of video or a window of sound, all in one space, as 0.21's MLX port (`gemma_mlx.py`) makes them. Also the parts
//! download (`files`), which kinds of files are read by content (`content`), the video and sound decoder (`media`)
//! and the cache of text vectors (`cache`).
//!
//! Threading: `Embedder` is Send + Sync. Each call runs on the GPU under one lock, so calls from several threads
//! take turns, as 0.21's `_embed_lock` makes them. An encoder part loads at its first use.

pub mod audio;
pub mod cache;
pub mod content;
pub mod files;
pub mod image;
pub mod media;
mod nn;
pub mod text;
pub mod vision;

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use anyhow::{Context, bail};
use candle_core::{DType, Device, Tensor};
use candle_nn::VarBuilder;
use serde_json::Value;

pub use files::{MODEL, Part};

/// The most tokens a text is read to; an image, a video or a sound can be longer.
pub const MAX_TOKENS: usize = 512;
/// Texts per model call.
pub const BATCH: usize = 16;
/// The query instruction when the model's config gives none ("query" prompt).
pub const QUERY_PROMPT: &str = "task: search result | query: ";

/// How the model reads a text: as a search query (with the query instruction), as a document (with its title, a
/// path, or "none"), or as is (a text that is already in the model's format, such as a cache's key text).
#[derive(Clone, Copy, Debug)]
pub enum Kind<'a> {
    Query,
    Document(Option<&'a str>),
    Raw,
}

/// The text as EmbeddingGemma reads it (0.21's `indexer.model_texts`).
pub fn model_text(text: &str, kind: Kind, query_prompt: &str) -> String {
    match kind {
        Kind::Query => format!("{query_prompt}{text}"),
        Kind::Document(title) => format!("title: {} | text: {text}", title.unwrap_or("none")),
        Kind::Raw => text.to_string(),
    }
}

struct Model {
    text: text::TextModel,
    fast: Option<text::fast::Fast>,
    tok: tokenizers::Tokenizer,
    vision: Option<vision::Vision>,
    audio: Option<audio::Audio>,
}

pub struct Embedder {
    folder: PathBuf,
    cfg: Value,
    prompts: Value,
    files: Vec<(Part, PathBuf)>,
    model: Mutex<Model>,
    path: String,
    bos: u32,
    eos: u32,
}

impl Embedder {
    /// The model from the Hugging Face cache, with its text part. The image and sound parts load at their first use,
    /// from their own files there.
    pub fn load_cached() -> anyhow::Result<Embedder> {
        let folder = files::model_folder(MODEL).with_context(|| format!("{MODEL} is not downloaded"))?;
        Embedder::load(&folder, &[])
    }

    /// The model in `folder` (config, tokenizer, weights). `files` gives a part's weights file when it is not in the
    /// folder (a full model.safetensors elsewhere, in tests).
    pub fn load(folder: &Path, files: &[(Part, PathBuf)]) -> anyhow::Result<Embedder> {
        Embedder::load_as(folder, files, DType::F32)
    }

    /// `load`, with the type the weights compute in. The weights are bfloat16; float32 is the default.
    pub fn load_as(folder: &Path, files: &[(Part, PathBuf)], dtype: DType) -> anyhow::Result<Embedder> {
        let cfg: Value = serde_json::from_slice(&std::fs::read(folder.join("config.json"))?)?;
        if cfg["model_type"] != "embedding_gemma2" {
            bail!("not an EmbeddingGemma 2 model: {}", cfg["model_type"]);
        }
        let prompts = std::fs::read(folder.join("config_sentence_transformers.json")).ok()
            .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
            .map(|v| v["prompts"].clone())
            .unwrap_or(Value::Null);
        let files = files.to_vec();
        let weights = Self::weights(folder, &files, Part::Text)?;
        let cpu = std::env::var_os("EMBED_CPU").is_some();
        let device = if cpu { Device::Cpu } else { Device::new_metal(0).unwrap_or(Device::Cpu) };
        // SAFETY: the weights file is mmapped read-only; it is not changed while the model is loaded
        let vb = unsafe { VarBuilder::from_mmaped_safetensors(&[weights], dtype, &device)? };
        let model = text::TextModel::load(text::Config::from_json(&cfg["text_config"])?, vb.pp("language_model"))?;
        let mut tok = tokenizers::Tokenizer::from_file(folder.join("tokenizer.json")).map_err(anyhow::Error::msg)?;
        tok.with_truncation(Some(tokenizers::TruncationParams { max_length: MAX_TOKENS, ..Default::default() }))
            .map_err(anyhow::Error::msg)?;
        tok.with_padding(None);
        let id = |t: &str, d: u32| tok.token_to_id(t).unwrap_or(d);
        let (bos, eos) = (id("<bos>", 2), id("<eos>", 1));
        // the fast path for texts, on Metal in float32; EMBED_NO_FAST keeps every text on candle
        let (fast, path) = match (device.is_metal() && dtype == DType::F32, std::env::var_os("EMBED_NO_FAST")) {
            (true, None) => match text::fast::Fast::new(&model) {
                Ok(f) => (Some(f), "metal+mps".to_string()),
                Err(e) => (None, format!("candle ({e:#})")),
            },
            _ => (None, "candle".to_string()),
        };
        Ok(Embedder {
            folder: folder.to_path_buf(),
            cfg,
            prompts,
            files,
            model: Mutex::new(Model { text: model, fast, tok, vision: None, audio: None }),
            path,
            bos,
            eos,
        })
    }

    fn weights(folder: &Path, files: &[(Part, PathBuf)], part: Part) -> anyhow::Result<PathBuf> {
        files.iter().find(|(p, _)| *p == part).map(|(_, f)| f.clone())
            .or_else(|| files::part_file(folder, part))
            .with_context(|| format!("the model's {} part isn't downloaded", part.name()))
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Model> {
        self.model.lock().unwrap_or_else(|e| e.into_inner())
    }

    pub fn dim(&self) -> usize {
        self.cfg["text_config"]["embedding_dim"].as_u64().unwrap_or(768) as usize
    }

    /// The same model with texts on candle only: the reference the fast path is checked against.
    pub fn without_fast(self) -> Embedder {
        let mut m = self.model.into_inner().unwrap_or_else(|e| e.into_inner());
        m.fast = None;
        Embedder { model: Mutex::new(m), path: "candle".into(), ..self }
    }

    /// How texts run: "metal+mps" (the fast path), or "candle" with the reason the fast path is off.
    pub fn text_path(&self) -> &str {
        &self.path
    }

    pub fn folder(&self) -> &Path {
        &self.folder
    }

    pub fn query_prompt(&self) -> &str {
        self.prompts["query"].as_str().unwrap_or(QUERY_PROMPT)
    }

    pub fn model_text(&self, text: &str, kind: Kind) -> String {
        model_text(text, kind, self.query_prompt())
    }

    /// One normalized vector per text, read as `kind` says.
    pub fn embed_texts(&self, texts: &[&str], kind: Kind) -> anyhow::Result<Vec<Vec<f32>>> {
        let texts: Vec<String> = texts.iter().map(|t| self.model_text(t, kind)).collect();
        self.embed_model_texts(&texts)
    }

    /// One normalized vector per text that is already in the model's format (`model_text`). Texts of similar
    /// lengths share a batch of BATCH, padded to a multiple of 64 tokens, as 0.21 does: each new shape builds new
    /// GPU kernels.
    pub fn embed_model_texts(&self, texts: &[String]) -> anyhow::Result<Vec<Vec<f32>>> {
        let mut out = vec![Vec::new(); texts.len()];
        let mut order: Vec<usize> = (0..texts.len()).collect();
        order.sort_by_key(|&i| std::cmp::Reverse(texts[i].chars().count()));
        let m = self.lock();
        for idx in order.chunks(BATCH) {
            let enc = m.tok.encode_batch(idx.iter().map(|&i| texts[i].as_str()).collect::<Vec<_>>(), true)
                .map_err(anyhow::Error::msg)?;
            let n = enc.iter().map(|e| e.get_ids().len()).max().unwrap_or(1).next_multiple_of(64);
            let mut ids = vec![0u32; idx.len() * n];
            let mut keep = vec![0f32; idx.len() * n];
            for (r, e) in enc.iter().enumerate() {
                for (c, &t) in e.get_ids().iter().enumerate() {
                    ids[r * n + c] = t;
                    keep[r * n + c] = 1.0;
                }
            }
            let v = match &m.fast {
                Some(f) => pool_rows(&f.forward(&m.text, &ids, &keep, idx.len(), n)?, &keep, idx.len(), n),
                None => {
                    let dev = m.text.device();
                    let ids = Tensor::from_vec(ids, (idx.len(), n), dev)?;
                    let keep = Tensor::from_vec(keep, (idx.len(), n), dev)?;
                    pool(&m.text.forward(&m.text.embed(&ids)?, &keep)?, &keep)?
                }
            };
            for (r, &i) in idx.iter().enumerate() {
                out[i] = v[r].clone();
            }
        }
        Ok(out)
    }

    /// One vector per image file, or an error for a file that can't be decoded. Each image runs alone: sizes
    /// differ.
    pub fn embed_images(&self, paths: &[&Path]) -> Vec<anyhow::Result<Vec<f32>>> {
        let mut m = self.lock();
        if let Err(e) = self.ensure_vision(&mut m) {
            return paths.iter().map(|_| Err(anyhow::anyhow!("{e:#}"))).collect();
        }
        paths.iter().map(|p| self.image_vector(&m, p)).collect()
    }

    fn image_vector(&self, m: &Model, path: &Path) -> anyhow::Result<Vec<f32>> {
        let v = m.vision.as_ref().context("no image encoder")?;
        let pic = image::open_rgb(path)?;
        let soft = v.soft_tokens(&[image::patches(&pic, v.patch(), v.pool(), v.image_tokens())])?;
        let e = self.marker_embeds(m, self.cfg["boi_token_id"].as_u64(), self.cfg["eoi_token_id"].as_u64())?;
        let x = Tensor::cat(&[&e.narrow(0, 0, 2)?, &soft.squeeze(0)?.to_dtype(e.dtype())?, &e.narrow(0, 2, 2)?], 0)?;
        self.pool_embeds(m, &x)
    }

    /// One vector for a window of video: its frames (RGB pictures, one a second, at most 32), each frame's soft
    /// tokens between begin and end tokens, all frames in order, through the text model.
    pub fn embed_frames(&self, frames: &[image::Rgb]) -> anyhow::Result<Vec<f32>> {
        if frames.is_empty() {
            bail!("no frames");
        }
        let mut m = self.lock();
        self.ensure_vision(&mut m)?;
        let v = m.vision.as_ref().context("no image encoder")?;
        let cut: Vec<_> = frames.iter().map(|f| image::patches(f, v.patch(), v.pool(), media::VIDEO_FRAME_TOKENS)).collect();
        let mut soft = Vec::new();
        for part in cut.chunks(8) {
            soft.push(v.soft_tokens(part)?);
        }
        let soft = Tensor::cat(&soft, 0)?;
        let e = self.marker_embeds(&m, self.cfg["boi_token_id"].as_u64(), self.cfg["eoi_token_id"].as_u64())?;
        let soft = soft.to_dtype(e.dtype())?;
        let mut parts = vec![e.narrow(0, 0, 1)?];
        for f in 0..soft.dim(0)? {
            parts.extend([e.narrow(0, 1, 1)?, soft.get(f)?, e.narrow(0, 2, 1)?]);
        }
        parts.push(e.narrow(0, 3, 1)?);
        self.pool_embeds(&m, &Tensor::cat(&parts, 0)?)
    }

    /// One vector per window of a video file, from the decoder: [(start, seconds, vector)].
    pub fn embed_video(&self, path: &Path) -> anyhow::Result<Vec<(f64, f64, Vec<f32>)>> {
        let info = media::info(path)?;
        let mut out = Vec::new();
        if !info.video {
            return Ok(out);
        }
        for (start, secs) in media::segments(info.duration, media::VIDEO_SEGMENT, media::MAX_SEGMENTS) {
            let frames = media::frames(path, start, secs)?;
            if !frames.is_empty() {
                out.push((start, secs, self.embed_frames(&frames)?));
            }
        }
        Ok(out)
    }

    /// One vector for a sound of at most 30 seconds: mono samples at 16 kHz. Its frames (one per 40 ms) between
    /// begin and end tokens, through the text model.
    pub fn embed_samples(&self, samples: &[f32]) -> anyhow::Result<Vec<f32>> {
        let mut m = self.lock();
        if m.audio.is_none() {
            let w = Self::weights(&self.folder, &self.files, Part::Audio)?;
            let (dt, dev) = (m.text.dtype(), m.text.device().clone());
            m.audio = Some(audio::Audio::load(&self.cfg["audio_config"], &w, dt, &dev, self.text_hidden())?);
        }
        let a = m.audio.as_ref().context("no sound encoder")?;
        let n = samples.len().min(audio::RATE * audio::MAX_SECONDS);
        let soft = a.soft_tokens(&samples[..n])?;
        let e = self.marker_embeds(&m, self.cfg["boa_token_id"].as_u64(), self.cfg["eoa_token_index"].as_u64())?;
        let x = Tensor::cat(&[&e.narrow(0, 0, 2)?, &soft.to_dtype(e.dtype())?, &e.narrow(0, 2, 2)?], 0)?;
        self.pool_embeds(&m, &x)
    }

    /// One vector per window of a sound file's sound, from the decoder: [(start, seconds, vector)]. Windows shorter
    /// than a tenth of a second are left out.
    pub fn embed_audio(&self, path: &Path) -> anyhow::Result<Vec<(f64, f64, Vec<f32>)>> {
        let info = media::info(path)?;
        let mut out = Vec::new();
        if !info.audio {
            return Ok(out);
        }
        for (start, secs) in media::segments(info.duration, media::AUDIO_SEGMENT, media::MAX_SEGMENTS) {
            let s = media::samples(path, start, secs)?;
            if s.len() >= audio::RATE / 10 {
                out.push((start, secs, self.embed_samples(&s)?));
            }
        }
        Ok(out)
    }

    fn text_hidden(&self) -> usize {
        self.cfg["text_config"]["hidden_size"].as_u64().unwrap_or(512) as usize
    }

    fn ensure_vision(&self, m: &mut Model) -> anyhow::Result<()> {
        if m.vision.is_none() {
            let w = Self::weights(&self.folder, &self.files, Part::Vision)?;
            let (dt, dev) = (m.text.dtype(), m.text.device().clone());
            let tokens = self.cfg["vision_soft_tokens_per_image"].as_u64().unwrap_or(280) as usize;
            m.vision = Some(vision::Vision::load(&self.cfg["vision_config"], &w, dt, &dev, self.text_hidden(), tokens)?);
        }
        Ok(())
    }

    /// The embeddings of bos, a begin marker, an end marker and eos: (4, hidden).
    fn marker_embeds(&self, m: &Model, begin: Option<u64>, end: Option<u64>) -> anyhow::Result<Tensor> {
        let (b, e) = (begin.context("no begin token")? as u32, end.context("no end token")? as u32);
        let ids = Tensor::new(&[[self.bos, b, e, self.eos]], m.text.device())?;
        Ok(m.text.embed(&ids)?.squeeze(0)?)
    }

    /// The mean of the text model's output over all positions of `x` (n, hidden), normalized.
    fn pool_embeds(&self, m: &Model, x: &Tensor) -> anyhow::Result<Vec<f32>> {
        let n = x.dim(0)?;
        let keep = Tensor::ones((1, n), DType::F32, x.device())?;
        let h = m.text.forward(&x.unsqueeze(0)?, &keep)?;
        Ok(pool(&h, &keep)?.remove(0))
    }
}

/// `pool` on the CPU, for hidden states that the fast path read back: (b · n · d) floats -> b vectors.
fn pool_rows(h: &[f32], keep: &[f32], b: usize, n: usize) -> Vec<Vec<f32>> {
    let d = h.len() / (b * n).max(1);
    (0..b).map(|r| {
        let mut v = vec![0f32; d];
        let mut w = 0f32;
        for t in 0..n {
            let k = keep[r * n + t];
            if k > 0.0 {
                w += k;
                v.iter_mut().zip(&h[(r * n + t) * d..(r * n + t + 1) * d]).for_each(|(a, x)| *a += x * k);
            }
        }
        v.iter_mut().for_each(|a| *a /= w.max(1.0));
        let norm = v.iter().map(|a| a * a).sum::<f32>().sqrt().max(1e-12);
        v.iter_mut().for_each(|a| *a /= norm);
        v
    }).collect()
}

/// Mean pooling over the kept positions, then L2 normalization: (b, n, d) -> b vectors.
fn pool(h: &Tensor, keep: &Tensor) -> candle_core::Result<Vec<Vec<f32>>> {
    let w = keep.to_dtype(DType::F32)?.unsqueeze(2)?;
    let s = h.broadcast_mul(&w)?.sum(1)?;
    let v = s.broadcast_div(&w.sum(1)?.clamp(1.0, f64::MAX)?)?;
    let norm = v.sqr()?.sum_keepdim(1)?.sqrt()?.clamp(1e-12, f64::MAX)?;
    v.broadcast_div(&norm)?.to_vec2()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn model_texts_match_the_formats_of_0_21() {
        assert_eq!(model_text("x", Kind::Query, QUERY_PROMPT), "task: search result | query: x");
        assert_eq!(model_text("x", Kind::Document(None), QUERY_PROMPT), "title: none | text: x");
        assert_eq!(model_text("x", Kind::Document(Some("a/b.rs")), QUERY_PROMPT), "title: a/b.rs | text: x");
    }
}
