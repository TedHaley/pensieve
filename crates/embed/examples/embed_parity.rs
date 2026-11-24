//! Compares this crate's vectors with reference vectors that 0.21 wrote, and measures throughput. Run as
//! `cargo run --release -p embed --example embed_parity -- <mode> …`; EMBED_NO_FAST=1 keeps texts on candle.
//!
//!   parity text <dir> [f32|bf16]          texts.json + mlx_text.f32 in <dir>
//!   parity bench <dir> [f32|bf16]         texts/s at batch 16 on bench.json (256 texts)
//!   parity images <dir> [f32|bf16]        images.json (paths) + mlx_images.f32
//!   parity frames <dir> [f32|bf16]        frames.json (lists of PNG paths) + mlx_frames.f32
//!   parity sound <dir> [f32|bf16]         sounds.json (raw f32 16 kHz files) + mlx_sound.f32
//!   parity video|audio <file> <out>       vectors of a media file through the decoder
//!   parity realcache <index.db>           0.21's cached vectors of its session chunks
//!   parity part <full model> <out>        the text part cut from a full model file
//!   parity download                       the vision part into $HF_HUB_CACHE
//! The full model file for the image and sound parts comes from $FULL_MODEL when the cache has only the text part.

use std::path::{Path, PathBuf};
use std::time::Instant;

use candle_core::DType;
use embed::{Embedder, Kind, Part};

fn read_f32(p: &Path, dim: usize) -> Vec<Vec<f32>> {
    let b = std::fs::read(p).expect("reference vectors");
    let v: Vec<f32> = b.chunks_exact(4).map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect();
    v.chunks(dim).map(|c| c.to_vec()).collect()
}

fn cos(a: &[f32], b: &[f32]) -> f64 {
    let d: f64 = a.iter().zip(b).map(|(x, y)| *x as f64 * *y as f64).sum();
    let na: f64 = a.iter().map(|x| (*x as f64).powi(2)).sum::<f64>().sqrt();
    let nb: f64 = b.iter().map(|x| (*x as f64).powi(2)).sum::<f64>().sqrt();
    d / (na * nb)
}

fn report(name: &str, mine: &[Vec<f32>], want: &[Vec<f32>]) {
    let mut worst: f64 = 1.0;
    for (i, (a, b)) in mine.iter().zip(want).enumerate() {
        let c = cos(a, b);
        worst = worst.min(c);
        println!("{name} {i:3} cos {c:.6}");
    }
    println!("{name}: {} vectors, worst cos {worst:.6}", mine.len());
}

fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if args[1] == "download" {
        let t = Instant::now();
        let mut last = 0;
        let p = embed::files::download_part(embed::MODEL, Part::Vision, &mut |d, total| {
            if d - last > 50 << 20 || d == total {
                println!("progress {d} {total}");
                last = d;
            }
        })?;
        println!("{} in {:.1}s", p.display(), t.elapsed().as_secs_f64());
        return Ok(());
    }
    let dt = if args.iter().any(|a| a == "bf16") { DType::BF16 } else { DType::F32 };
    let folder = embed::files::model_folder(embed::MODEL).expect("model folder");
    let mut files = Vec::new();
    if let Some(f) = std::env::var_os("FULL_MODEL") {
        files = vec![(Part::Vision, PathBuf::from(&f)), (Part::Audio, PathBuf::from(&f))];
    }
    let t = Instant::now();
    let e = Embedder::load_as(&folder, &files, dt)?;
    println!("loaded in {:.2}s ({dt:?}, texts on {})", t.elapsed().as_secs_f64(), e.text_path());
    let dir = args.get(2).map(PathBuf::from).unwrap_or_default();
    match args[1].as_str() {
        "text" => {
            let texts: Vec<String> = serde_json::from_slice(&std::fs::read(dir.join("texts.json"))?)?;
            let t = Instant::now();
            let mine = e.embed_model_texts(&texts)?;
            println!("{} texts in {:.2}s", texts.len(), t.elapsed().as_secs_f64());
            report("text", &mine, &read_f32(&dir.join("mlx_text.f32"), e.dim()));
            let q = e.embed_texts(&["where is the embed cache"], Kind::Query)?;
            let i = texts.iter().position(|t| t.ends_with("where is the embed cache")).unwrap_or(0);
            println!("Kind::Query matches the model text: cos {:.6}", cos(&q[0], &mine[i]));
        }
        "bench" => {
            let bench: Vec<String> = serde_json::from_slice(&std::fs::read(dir.join("bench.json"))?)?;
            e.embed_model_texts(&bench[..16])?;
            let t = Instant::now();
            for _ in 0..2 {
                e.embed_model_texts(&bench)?;
            }
            println!("texts/s at batch 16: {:.1}", 512.0 / t.elapsed().as_secs_f64());
            let t = Instant::now();
            for c in bench.chunks(8) {
                e.embed_model_texts(c)?;
            }
            println!("texts/s in slices of 8: {:.1}", 256.0 / t.elapsed().as_secs_f64());
        }
        "images" => {
            let paths: Vec<PathBuf> = serde_json::from_slice(&std::fs::read(dir.join("images.json"))?)?;
            let t = Instant::now();
            let refs: Vec<&Path> = paths.iter().map(|p| p.as_path()).collect();
            let mine: Vec<Vec<f32>> = e.embed_images(&refs).into_iter().collect::<anyhow::Result<_>>()?;
            println!("{} images in {:.2}s", paths.len(), t.elapsed().as_secs_f64());
            report("image", &mine, &read_f32(&dir.join("mlx_images.f32"), e.dim()));
        }
        "realcache" => {
            // session chunks of 0.21's index (opened immutable, never written) against 0.21's cached vectors
            let uri = format!("file:{}?immutable=1", args[2]);
            let c = store::rusqlite::Connection::open_with_flags(uri, store::rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | store::rusqlite::OpenFlags::SQLITE_OPEN_URI)?;
            let mut st = c.prepare("SELECT text FROM chunks WHERE text IS NOT NULL AND vec IS NOT NULL ORDER BY id DESC LIMIT 300")?;
            let texts: Vec<String> = st.query_map([], |r| r.get::<_, String>(0))?.collect::<Result<_, _>>()?;
            let model: Vec<String> = texts.iter().map(|t| e.model_text(t, Kind::Document(None))).collect();
            let keys: Vec<String> = model.iter().map(|t| embed::cache::key(t)).collect();
            let mut want = Vec::new();
            let mut have = Vec::new();
            for (k, t) in keys.iter().zip(&model) {
                let v: Option<Vec<u8>> = c.query_row("SELECT vec FROM embed_cache WHERE model=? AND hash=?", [embed::MODEL, k.as_str()], |r| r.get(0)).ok();
                if let Some(v) = v {
                    want.push(embed::cache::from_f16(&v));
                    have.push(t.clone());
                }
            }
            let t = Instant::now();
            let mine = e.embed_model_texts(&have)?;
            println!("{} of {} chunks in the cache; embedded in {:.2}s", have.len(), texts.len(), t.elapsed().as_secs_f64());
            let cs: Vec<f64> = mine.iter().zip(&want).map(|(a, b)| cos(a, b)).collect();
            let mut sorted = cs.clone();
            sorted.sort_by(|a, b| a.total_cmp(b));
            if !sorted.is_empty() {
                println!("cos worst {:.6} p1 {:.6} median {:.6}", sorted[0], sorted[sorted.len() / 100], sorted[sorted.len() / 2]);
                println!("below 0.999: {}", sorted.iter().filter(|c| **c < 0.999).count());
            }
        }
        "part" => {
            // the text part cut from a local full model file, as the download cuts it, against 0.21's text part
            use std::io::{Read, Seek, SeekFrom};
            let mut full = std::fs::File::open(&args[2])?;
            let out = PathBuf::from(&args[3]);
            embed::files::write_part(&mut |a, b| {
                let mut buf = vec![0; (b - a + 1) as usize];
                full.seek(SeekFrom::Start(a))?;
                full.read_exact(&mut buf)?;
                Ok(buf)
            }, Part::Text, &out, &mut |_, _| {})?;
            let same = std::fs::read(&out)? == std::fs::read(folder.join("text.safetensors"))?;
            println!("text part byte-identical to 0.21's: {same}");
        }
        "pixels" => {
            let paths: Vec<PathBuf> = serde_json::from_slice(&std::fs::read(dir.join("images.json"))?)?;
            for p in &paths {
                let px = embed::image::patches(&embed::image::open_rgb(p)?, 16, 3, 280);
                let sum: f64 = px.pixels.iter().map(|v| *v as f64).sum();
                println!("{} {:?} {sum}", p.file_name().unwrap_or_default().to_string_lossy(), px.grid);
            }
        }
        "frames" => {
            let sets: Vec<Vec<PathBuf>> = serde_json::from_slice(&std::fs::read(dir.join("frames.json"))?)?;
            let mut mine = Vec::new();
            let t = Instant::now();
            for s in &sets {
                let pics: Vec<_> = s.iter().map(|p| embed::image::open_rgb(p)).collect::<anyhow::Result<_>>()?;
                mine.push(e.embed_frames(&pics)?);
            }
            println!("{} windows in {:.2}s", sets.len(), t.elapsed().as_secs_f64());
            report("video", &mine, &read_f32(&dir.join("mlx_frames.f32"), e.dim()));
        }
        "sound" => {
            let paths: Vec<PathBuf> = serde_json::from_slice(&std::fs::read(dir.join("sounds.json"))?)?;
            let mut mine = Vec::new();
            let t = Instant::now();
            for p in &paths {
                let b = std::fs::read(p)?;
                let s: Vec<f32> = b.chunks_exact(4).map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect();
                mine.push(e.embed_samples(&s)?);
            }
            println!("{} sounds in {:.2}s", paths.len(), t.elapsed().as_secs_f64());
            report("sound", &mine, &read_f32(&dir.join("mlx_sound.f32"), e.dim()));
        }
        "video" | "audio" => {
            let p = Path::new(&args[2]);
            let t = Instant::now();
            let v = if args[1] == "video" { e.embed_video(p)? } else { e.embed_audio(p)? };
            println!("{} windows in {:.2}s", v.len(), t.elapsed().as_secs_f64());
            let out: Vec<f32> = v.iter().flat_map(|(_, _, v)| v.iter().copied()).collect();
            let bytes: Vec<u8> = out.iter().flat_map(|x| x.to_le_bytes()).collect();
            std::fs::write(args.get(3).map(PathBuf::from).unwrap_or("rust_media.f32".into()), bytes)?;
        }
        m => anyhow::bail!("unknown mode {m}"),
    }
    Ok(())
}
