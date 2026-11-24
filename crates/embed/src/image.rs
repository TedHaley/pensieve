//! Pictures as the model's image processor prepares them (0.21's `gemma_mlx.image_patches`): decoded to RGB,
//! resized with the aspect ratio kept to the largest size whose sides are multiples of patch × pool with at most
//! `max_tokens` soft tokens, then cut into patches. The resize is a port of Pillow's bicubic resample, with its
//! fixed-point arithmetic, so that the pixels match 0.21's.

use std::path::Path;

use anyhow::Context;

/// An RGB picture, 8 bits a channel, rows top to bottom.
#[derive(Clone, Debug)]
pub struct Rgb {
    pub w: usize,
    pub h: usize,
    pub data: Vec<u8>,
}

/// A picture cut into patches: each patch's pixels in 0..1 as (row, column, channel), each patch's (x, y), and the
/// grid's (rows, columns).
#[derive(Clone, Debug)]
pub struct Patches {
    pub pixels: Vec<f32>,
    pub pos: Vec<(u32, u32)>,
    pub grid: (usize, usize),
}

/// An image file as RGB. HEIC goes through macOS's `sips`, as in 0.21. The alpha channel is dropped, as Pillow's
/// `convert("RGB")` does. Unlike 0.21, a JPEG larger than 4096 pixels on both sides decodes at full size (Pillow's
/// `draft` decodes it at half size).
pub fn open_rgb(path: &Path) -> anyhow::Result<Rgb> {
    let ext = path.extension().and_then(|e| e.to_str()).unwrap_or("").to_lowercase();
    if ext == "heic" || ext == "heif" {
        let dir = std::env::temp_dir().join(format!("embed-heic-{}-{:?}", std::process::id(), std::thread::current().id()));
        std::fs::create_dir_all(&dir)?;
        let out = dir.join("image.png");
        let r = std::process::Command::new("sips").args(["-s", "format", "png"]).arg(path).arg("--out").arg(&out).output();
        let pic = match r {
            Ok(o) if o.status.success() => open_rgb(&out),
            Ok(o) => Err(anyhow::anyhow!("sips: {}", String::from_utf8_lossy(&o.stderr).trim())),
            Err(e) => Err(e.into()),
        };
        let _ = std::fs::remove_dir_all(&dir);
        return pic;
    }
    let im = ::image::ImageReader::open(path)?.with_guessed_format()?.decode()
        .with_context(|| format!("can't decode {}", path.display()))?;
    let im = im.to_rgb8();
    Ok(Rgb { w: im.width() as usize, h: im.height() as usize, data: im.into_raw() })
}

/// Python's float floor division, which differs from floor(a / b) when the quotient rounds to an integer.
fn floordiv(a: f64, b: f64) -> f64 {
    let m = a % b;
    let mut div = (a - m) / b;
    if m != 0.0 && ((b < 0.0) != (m < 0.0)) {
        div -= 1.0;
    }
    let f = div.floor();
    if div - f > 0.5 { f + 1.0 } else { f }
}

/// The size (height, width) a picture is resized to, as 0.21's `picture_patches` picks it.
pub fn target_size(w: usize, h: usize, patch: usize, pool: usize, max_tokens: usize) -> (usize, usize) {
    let side = patch * pool;
    let budget = (max_tokens * pool * pool * patch * patch) as f64;
    let f = (budget / (w * h) as f64).sqrt();
    let th = floordiv(h as f64 * f, side as f64) as usize * side;
    let tw = floordiv(w as f64 * f, side as f64) as usize * side;
    let longest = max_tokens * side;
    if th == 0 {
        (side, ((w / h) * side).min(longest))
    } else if tw == 0 {
        (((h / w) * side).min(longest), side)
    } else {
        (th, tw)
    }
}

/// 0.21's `picture_patches`: the picture at `target_size`, its pixels divided by 255 in float32.
pub fn patches(pic: &Rgb, patch: usize, pool: usize, max_tokens: usize) -> Patches {
    let (th, tw) = target_size(pic.w, pic.h, patch, pool, max_tokens);
    let resized;
    let pic = if (th, tw) != (pic.h, pic.w) {
        resized = resize_bicubic(pic, tw, th);
        &resized
    } else {
        pic
    };
    let (rows, cols) = (th / patch, tw / patch);
    let mut pixels = Vec::with_capacity(rows * cols * patch * patch * 3);
    let mut pos = Vec::with_capacity(rows * cols);
    for r in 0..rows {
        for c in 0..cols {
            for y in 0..patch {
                let row = (r * patch + y) * tw * 3;
                for x in 0..patch {
                    let i = row + (c * patch + x) * 3;
                    pixels.extend(pic.data[i..i + 3].iter().map(|&v| v as f32 / 255.0));
                }
            }
            pos.push((c as u32, r as u32));
        }
    }
    Patches { pixels, pos, grid: (rows, cols) }
}

const PRECISION_BITS: u32 = 32 - 8 - 2;

fn bicubic(x: f64) -> f64 {
    let a = -0.5;
    let x = x.abs();
    if x < 1.0 {
        ((a + 2.0) * x - (a + 3.0)) * x * x + 1.0
    } else if x < 2.0 {
        (((x - 5.0) * x + 8.0) * x - 4.0) * a
    } else {
        0.0
    }
}

/// Pillow's `precompute_coeffs` and `normalize_coeffs_8bpc`: per output pixel, the first input pixel, how many
/// it reads, and their fixed-point weights.
fn coeffs(in_size: usize, out_size: usize) -> (usize, Vec<(usize, usize)>, Vec<i32>) {
    let scale = in_size as f64 / out_size as f64;
    let filterscale = scale.max(1.0);
    let support = 2.0 * filterscale;
    let ksize = support.ceil() as usize * 2 + 1;
    let mut bounds = Vec::with_capacity(out_size);
    let mut kk = vec![0i32; out_size * ksize];
    let mut k = vec![0f64; ksize];
    for xx in 0..out_size {
        let center = (xx as f64 + 0.5) * scale;
        let ss = 1.0 / filterscale;
        // C's (int) cast truncates toward zero
        let xmin = ((center - support + 0.5) as i64).max(0) as usize;
        let xmax = ((center + support + 0.5) as i64).min(in_size as i64) as usize - xmin;
        let mut ww = 0.0;
        for x in 0..xmax {
            let w = bicubic((x as f64 + xmin as f64 - center + 0.5) * ss);
            k[x] = w;
            ww += w;
        }
        for x in 0..xmax {
            if ww != 0.0 {
                k[x] /= ww;
            }
            let v = k[x] * (1u64 << PRECISION_BITS) as f64;
            kk[xx * ksize + x] = if k[x] < 0.0 { (v - 0.5) as i32 } else { (v + 0.5) as i32 };
        }
        bounds.push((xmin, xmax));
    }
    (ksize, bounds, kk)
}

fn clip8(v: i64) -> u8 {
    if v >= (1i64 << PRECISION_BITS << 8) {
        255
    } else if v <= 0 {
        0
    } else {
        (v >> PRECISION_BITS) as u8
    }
}

/// Pillow's `Image.resize((w, h), Image.BICUBIC)` for an RGB picture: a horizontal pass when the width changes,
/// then a vertical one when the height does, each rounding to 8 bits.
pub fn resize_bicubic(pic: &Rgb, w: usize, h: usize) -> Rgb {
    let (kh, bh, ch) = coeffs(pic.w, w);
    let (kv, mut bv, cv) = coeffs(pic.h, h);
    let half = 1i64 << (PRECISION_BITS - 1);
    let mut src = pic.data.clone();
    if w != pic.w {
        // only the input rows that the vertical pass reads
        let first = bv[0].0;
        let last = bv[h - 1].0 + bv[h - 1].1;
        for b in bv.iter_mut() {
            b.0 -= first;
        }
        let rows = last - first;
        let mut tmp = vec![0u8; rows * w * 3];
        for y in 0..rows {
            let line = &pic.data[(y + first) * pic.w * 3..];
            for (xx, &(xmin, n)) in bh.iter().enumerate() {
                let k = &ch[xx * kh..xx * kh + n];
                for c in 0..3 {
                    let mut ss = half;
                    for (x, &kx) in k.iter().enumerate() {
                        ss += line[(x + xmin) * 3 + c] as i64 * kx as i64;
                    }
                    tmp[(y * w + xx) * 3 + c] = clip8(ss);
                }
            }
        }
        src = tmp;
        if h == pic.h {
            return Rgb { w, h, data: src };
        }
    }
    let mut out = vec![0u8; h * w * 3];
    for (yy, &(ymin, n)) in bv.iter().enumerate() {
        let k = &cv[yy * kv..yy * kv + n];
        for x in 0..w * 3 {
            let mut ss = half;
            for (y, &ky) in k.iter().enumerate() {
                ss += src[(y + ymin) * w * 3 + x] as i64 * ky as i64;
            }
            out[yy * w * 3 + x] = clip8(ss);
        }
    }
    Rgb { w, h, data: out }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sizes_follow_the_token_budget() {
        let (h, w) = target_size(1920, 1080, 16, 3, 280);
        assert_eq!((w % 48, h % 48), (0, 0));
        assert!((w / 48) * (h / 48) <= 280);
        // a very thin picture keeps one row of tokens
        assert_eq!(target_size(10000, 10, 16, 3, 280).0, 48);
    }

    #[test]
    fn a_flat_picture_stays_flat() {
        let pic = Rgb { w: 7, h: 5, data: vec![200; 7 * 5 * 3] };
        let out = resize_bicubic(&pic, 3, 9);
        assert_eq!((out.w, out.h), (3, 9));
        assert!(out.data.iter().all(|&v| v == 200));
    }

    #[test]
    fn patches_are_row_major_with_x_y_positions() {
        let pic = Rgb { w: 96, h: 48, data: (0..96 * 48 * 3).map(|i| (i % 251) as u8).collect() };
        // a small picture grows to the token budget
        let p = patches(&pic, 16, 3, 280);
        assert_eq!(p.grid, (33, 69));
        assert_eq!(p.pos[1], (1, 0));
        assert_eq!(p.pos[69], (0, 1));
        assert_eq!(p.pixels.len(), 33 * 69 * 16 * 16 * 3);
    }
}
