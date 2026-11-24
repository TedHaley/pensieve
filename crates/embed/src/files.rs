//! Where EmbeddingGemma 2's files are, in the Hugging Face cache, and their download (0.21's
//! `gemma_mlx.download_part`). The weights come in three parts, each in its own file beside the model's config: the
//! text model always, the image encoder and the sound encoder only when they are switched on. A safetensors file
//! starts with a JSON index of where each tensor is, so a part is fetched as byte ranges of the full file.

use std::io::Write;
use std::path::{Path, PathBuf};

use anyhow::{Context, bail};
use serde_json::{Map, Value};

pub const MODEL: &str = "google/embeddinggemma-2";

/// A part of the model's weights.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Part {
    Text,
    Vision,
    Audio,
}

impl Part {
    pub const ALL: [Part; 3] = [Part::Text, Part::Vision, Part::Audio];

    pub fn name(self) -> &'static str {
        match self {
            Part::Text => "text",
            Part::Vision => "vision",
            Part::Audio => "audio",
        }
    }

    pub fn file(self) -> &'static str {
        match self {
            Part::Text => "text.safetensors",
            Part::Vision => "vision.safetensors",
            Part::Audio => "audio.safetensors",
        }
    }

    /// The prefixes of its tensors' names in the full file.
    pub fn prefixes(self) -> &'static [&'static str] {
        match self {
            Part::Text => &["language_model."],
            Part::Vision => &["vision_tower.", "embed_vision."],
            Part::Audio => &["audio_tower.", "embed_audio."],
        }
    }

    /// Its size in MB, for the settings page before it is downloaded.
    pub fn mb(self) -> u64 {
        match self {
            Part::Text => 542,
            Part::Vision => 335,
            Part::Audio => 611,
        }
    }
}

/// The Hugging Face cache: $HF_HUB_CACHE, else $HF_HOME/hub, else ~/.cache/huggingface/hub.
pub fn hub_cache() -> PathBuf {
    if let Some(p) = std::env::var_os("HF_HUB_CACHE") {
        return PathBuf::from(p);
    }
    if let Some(p) = std::env::var_os("HF_HOME") {
        return PathBuf::from(p).join("hub");
    }
    store::home().join(".cache/huggingface/hub")
}

fn repo_dir(name: &str) -> PathBuf {
    hub_cache().join(format!("models--{}", name.replace('/', "--")))
}

/// The model's folder (the snapshot that refs/main names) when its config is here.
pub fn model_folder(name: &str) -> Option<PathBuf> {
    let repo = repo_dir(name);
    let rev = std::fs::read_to_string(repo.join("refs/main")).ok()?;
    let folder = repo.join("snapshots").join(rev.trim());
    folder.join("config.json").exists().then_some(folder)
}

/// The file that holds `part` in `folder`: its own file, or the full model's.
pub fn part_file(folder: &Path, part: Part) -> Option<PathBuf> {
    [part.file(), "model.safetensors"].iter().map(|f| folder.join(f)).find(|p| p.exists())
}

/// Whether `part` of the model is downloaded.
pub fn part_here(name: &str, part: Part) -> bool {
    model_folder(name).is_some_and(|f| part_file(&f, part).is_some())
}

/// The Hugging Face token that the Python tools saved, if any: $HF_TOKEN, else the cache's token file.
fn token() -> Option<String> {
    let t = std::env::var("HF_TOKEN").ok().or_else(|| {
        let home = std::env::var_os("HF_HOME").map(PathBuf::from).unwrap_or_else(|| store::home().join(".cache/huggingface"));
        std::fs::read_to_string(home.join("token")).ok()
    })?;
    let t = t.trim().to_string();
    (!t.is_empty()).then_some(t)
}

fn get(url: &str, range: Option<(u64, u64)>) -> anyhow::Result<Vec<u8>> {
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .timeout_global(Some(std::time::Duration::from_secs(120)))
        .build()
        .into();
    let mut req = agent.get(url);
    if let Some(t) = token() {
        req = req.header("Authorization", &format!("Bearer {t}"));
    }
    if let Some((a, b)) = range {
        req = req.header("Range", &format!("bytes={a}-{b}"));
    }
    let r = req.call().with_context(|| format!("GET {url}"))?;
    if range.is_some() && r.status() != 206 {
        bail!("the server ignored the byte range");
    }
    let mut out = Vec::new();
    std::io::copy(&mut r.into_body().into_reader(), &mut out)?;
    Ok(out)
}

/// The config and tokenizer files that the model needs, as `snapshot_download(allow_patterns=["*.json",
/// "tokenizer*"])` fetches them. Returns the snapshot folder.
fn download_config(name: &str) -> anyhow::Result<PathBuf> {
    if let Some(f) = model_folder(name).filter(|f| f.join("tokenizer.json").exists()) {
        return Ok(f);
    }
    let info: Value = serde_json::from_slice(&get(&format!("https://huggingface.co/api/models/{name}/revision/main"), None)?)?;
    let rev = info["sha"].as_str().context("no revision for the model")?.to_string();
    let repo = repo_dir(name);
    let folder = repo.join("snapshots").join(&rev);
    for s in info["siblings"].as_array().into_iter().flatten() {
        let Some(f) = s["rfilename"].as_str() else { continue };
        let leaf = f.rsplit('/').next().unwrap_or(f);
        if !(leaf.ends_with(".json") || leaf.starts_with("tokenizer")) || folder.join(f).exists() {
            continue;
        }
        let body = get(&format!("https://huggingface.co/{name}/resolve/{rev}/{f}"), None)?;
        let out = folder.join(f);
        std::fs::create_dir_all(out.parent().unwrap_or(&folder))?;
        std::fs::write(&out, body)?;
    }
    std::fs::create_dir_all(repo.join("refs"))?;
    std::fs::write(repo.join("refs/main"), &rev)?;
    Ok(folder)
}

/// The model's config and tokenizer, and the weights of `part`, written to the part's file beside them. Only the
/// part's tensors are fetched, as byte ranges, neighbours in one request. `progress(done, total)` gets the bytes so
/// far. Returns the part's file.
pub fn download_part(name: &str, part: Part, progress: &mut dyn FnMut(u64, u64)) -> anyhow::Result<PathBuf> {
    let folder = download_config(name)?;
    let out = folder.join(part.file());
    if out.exists() {
        return Ok(out);
    }
    let url = format!("https://huggingface.co/{name}/resolve/main/model.safetensors");
    write_part(&mut |a, b| get(&url, Some((a, b))), part, &out, progress)?;
    Ok(out)
}

/// The part's file at `out`, from `range(a, b)`: bytes a..=b of the full model file.
pub fn write_part(range: &mut dyn FnMut(u64, u64) -> anyhow::Result<Vec<u8>>, part: Part, out: &Path,
                  progress: &mut dyn FnMut(u64, u64)) -> anyhow::Result<()> {
    let n = u64::from_le_bytes(range(0, 7)?.try_into().map_err(|_| anyhow::anyhow!("short header"))?);
    let header: Map<String, Value> = serde_json::from_slice(&range(8, 8 + n - 1)?)?;
    let (head, keep, total) = part_header(&header, part)?;
    let tmp = out.with_extension("part");
    let mut f = std::io::BufWriter::new(std::fs::File::create(&tmp)?);
    f.write_all(&(head.len() as u64).to_le_bytes())?;
    f.write_all(&head)?;
    let base = 8 + n;
    let mut done = 0;
    let mut i = 0;
    while i < keep.len() {
        let (a, mut b) = keep[i];
        let mut j = i + 1;
        while j < keep.len() && keep[j].0 == b && b - a < 16 << 20 {
            b = keep[j].1;
            j += 1;
        }
        f.write_all(&range(base + a, base + b - 1)?)?;
        done += b - a;
        progress(done, total);
        i = j;
    }
    f.flush()?;
    drop(f);
    std::fs::rename(&tmp, out)?;
    Ok(())
}

/// The header of a part's file, made from the full file's: its tensors in the order of their data, at new offsets,
/// padded with spaces so that the data starts 8-byte aligned. Also returns each tensor's byte range in the full
/// file's data, and the part's size.
pub fn part_header(header: &Map<String, Value>, part: Part) -> anyhow::Result<(Vec<u8>, Vec<(u64, u64)>, u64)> {
    let off = |v: &Value| -> (u64, u64) {
        let o = &v["data_offsets"];
        (o[0].as_u64().unwrap_or(0), o[1].as_u64().unwrap_or(0))
    };
    let mut keep: Vec<(&String, &Value)> =
        header.iter().filter(|(k, _)| part.prefixes().iter().any(|p| k.starts_with(p))).collect();
    if keep.is_empty() {
        bail!("the model has no {} weights", part.name());
    }
    keep.sort_by_key(|(_, v)| off(v).0);
    let mut new = Map::new();
    let mut pos = 0;
    let mut ranges = Vec::new();
    for (k, v) in &keep {
        let (a, b) = off(v);
        let mut v = (*v).clone();
        v["data_offsets"] = serde_json::json!([pos, pos + b - a]);
        new.insert((*k).clone(), v);
        pos += b - a;
        ranges.push((a, b));
    }
    // with serde_json's preserve_order the JSON is byte for byte the one Python's json.dumps writes
    let mut head = serde_json::to_vec(&Value::Object(new))?;
    head.resize(head.len().next_multiple_of(8), b' ');
    Ok((head, ranges, pos))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_part_header_keeps_its_tensors_packed_and_aligned() {
        let h: Map<String, Value> = serde_json::from_str(
            r#"{"language_model.a": {"dtype": "BF16", "shape": [2], "data_offsets": [8, 12]},
                "vision_tower.b": {"dtype": "BF16", "shape": [4], "data_offsets": [0, 8]},
                "language_model.c": {"dtype": "BF16", "shape": [1], "data_offsets": [12, 14]}}"#,
        )
        .unwrap();
        let (head, ranges, total) = part_header(&h, Part::Text).unwrap();
        assert_eq!(head.len() % 8, 0);
        assert_eq!(ranges, vec![(8, 12), (12, 14)]);
        assert_eq!(total, 6);
        let back: Value = serde_json::from_slice(&head).unwrap();
        assert_eq!(back["language_model.c"]["data_offsets"], serde_json::json!([4, 6]));
        assert!(back.get("vision_tower.b").is_none());
    }
}
