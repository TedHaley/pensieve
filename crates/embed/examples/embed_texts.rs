//! Embeds a JSON list of texts as documents and writes the vectors as little-endian f32 rows:
//! `cargo run --release -p embed --example embed_texts -- texts.json out.f32`.

use embed::{Embedder, Kind};

fn main() -> anyhow::Result<()> {
    let a: Vec<String> = std::env::args().collect();
    let texts: Vec<String> = serde_json::from_slice(&std::fs::read(&a[1])?)?;
    let e = Embedder::load_cached()?;
    let t = std::time::Instant::now();
    let refs: Vec<&str> = texts.iter().map(String::as_str).collect();
    let mut out = Vec::new();
    for (i, batch) in refs.chunks(64).enumerate() {
        for v in e.embed_texts(batch, Kind::Document(None))? {
            out.extend(v.iter().flat_map(|x| x.to_le_bytes()));
        }
        eprintln!("{} / {} in {:.0?}", (i + 1) * 64, texts.len(), t.elapsed());
    }
    std::fs::write(&a[2], out)?;
    Ok(())
}
