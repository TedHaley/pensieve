//! Video and sound files decoded into what EmbeddingGemma 2 reads (0.21's `media.py`): frames and samples, in
//! windows of at most VIDEO_SEGMENT (video) or AUDIO_SEGMENT (sound) seconds, since the model reads 32 frames or 30
//! seconds of sound at most. Each window becomes one chunk of the file.
//!
//! The decoder is `pensieve-media` (AVFoundation: every format macOS plays). It is built into the pensieve
//! binary, which writes it to data_dir/bin. PENSIEVE_MEDIA_TOOL names another copy. Without the decoder, video and
//! sound are not read.

use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{Context, bail};

use crate::audio::RATE;
use crate::image::Rgb;

pub const VIDEO_FPS: f64 = 1.0;
pub const VIDEO_MAX_FRAMES: usize = 32;
/// Soft tokens per video frame.
pub const VIDEO_FRAME_TOKENS: usize = 140;
pub const VIDEO_SEGMENT: f64 = VIDEO_MAX_FRAMES as f64 / VIDEO_FPS;
pub const AUDIO_SEGMENT: f64 = crate::audio::MAX_SECONDS as f64;
/// Windows per file: two hours of sound, or of video at one window per 32 s; the rest is left out.
pub const MAX_SEGMENTS: usize = 240;
/// Part of a video or sound file's index mode: bump to read every such file again.
pub const VERSION: &str = "m2";

/// The pensieve-media decoder, or None.
pub fn tool() -> Option<PathBuf> {
    let env = std::env::var_os("PENSIEVE_MEDIA_TOOL").map(PathBuf::from);
    [env, Some(installed_tool())].into_iter().flatten().find(|p| is_executable(p))
}

pub fn installed_tool() -> PathBuf {
    store::data_dir().join("bin").join("pensieve-media")
}

fn is_executable(p: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(p).is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
}

/// "pensieve-media", or None when no decoder is here.
pub fn decoder() -> Option<&'static str> {
    tool().map(|_| "pensieve-media")
}

fn run(args: &[&str]) -> anyhow::Result<String> {
    let t = tool().context("no video and sound decoder (pensieve-media)")?;
    let o = Command::new(t).args(args).output()?;
    if !o.status.success() {
        let e = String::from_utf8_lossy(&o.stderr).trim().to_string();
        bail!("{}", if e.is_empty() { format!("pensieve-media {} failed", args[0]) } else { e });
    }
    Ok(String::from_utf8_lossy(&o.stdout).trim().to_string())
}

/// What a file has.
#[derive(Clone, Debug, serde::Deserialize, serde::Serialize)]
pub struct Info {
    /// Seconds; 0 when unknown.
    pub duration: f64,
    pub video: bool,
    pub audio: bool,
}

pub fn info(path: &Path) -> anyhow::Result<Info> {
    Ok(serde_json::from_str(&run(&["info", &path.to_string_lossy()])?)?)
}

/// (start, seconds) windows that cover `duration`; the last one ends with the file. A file shorter than a window is
/// one window, and so is a file whose duration is unknown.
pub fn segments(duration: f64, length: f64, cap: usize) -> Vec<(f64, f64)> {
    if duration == 0.0 {
        return vec![(0.0, length)];
    }
    let (d, l) = (duration as i64, length as i64);
    let n = (-((-d).div_euclid(l))).clamp(1, cap as i64) as usize;
    (0..n).map(|i| (i as f64 * length, length.min(duration - i as f64 * length))).collect()
}

fn scratch(what: &str) -> anyhow::Result<PathBuf> {
    static N: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let n = N.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let d = std::env::temp_dir().join(format!("embed-{what}-{}-{n}", std::process::id()));
    std::fs::create_dir_all(&d)?;
    Ok(d)
}

/// The frames of a video window as RGB pictures: VIDEO_FPS a second, at most VIDEO_MAX_FRAMES.
pub fn frames(path: &Path, start: f64, seconds: f64) -> anyhow::Result<Vec<Rgb>> {
    let d = scratch("frames")?;
    let out = (|| {
        run(&["frames", &path.to_string_lossy(), &d.to_string_lossy(), &py_float(start), &py_float(seconds),
              &VIDEO_FPS.to_string(), &VIDEO_MAX_FRAMES.to_string()])?;
        let mut files: Vec<PathBuf> = std::fs::read_dir(&d)?.flatten().map(|e| e.path())
            .filter(|p| p.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.starts_with("frame") && n.ends_with(".png")))
            .collect();
        files.sort();
        files.iter().map(|f| crate::image::open_rgb(f)).collect()
    })();
    let _ = std::fs::remove_dir_all(&d);
    out
}

/// Python's str() of a float, as 0.21 passes times to the helper.
fn py_float(v: f64) -> String {
    if v.fract() == 0.0 { format!("{v:.1}") } else { format!("{v}") }
}

/// A window of the sound, mono float32 at 16 kHz.
pub fn samples(path: &Path, start: f64, seconds: f64) -> anyhow::Result<Vec<f32>> {
    let d = scratch("sound")?;
    let out = (|| {
        let f = d.join("sound.f32");
        let said = run(&["audio", &path.to_string_lossy(), &f.to_string_lossy(), &py_float(start), &py_float(seconds)])?;
        let rate: usize = said.split_whitespace().next().and_then(|r| r.parse().ok()).context("no sample rate")?;
        let raw = std::fs::read(&f)?;
        let s: Vec<f32> = raw.chunks_exact(4).map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]])).collect();
        let mut s = resample(&s, rate, RATE);
        s.truncate((seconds * RATE as f64) as usize);
        Ok(s)
    })();
    let _ = std::fs::remove_dir_all(&d);
    out
}

fn gcd(a: usize, b: usize) -> usize {
    if b == 0 { a } else { gcd(b, a % b) }
}

/// The zeroth-order modified Bessel function of the first kind, by its series.
fn bessel_i0(x: f64) -> f64 {
    let (mut sum, mut term, q) = (1.0, 1.0, x * x / 4.0);
    for k in 1..200 {
        term *= q / (k * k) as f64;
        sum += term;
        if term < sum * 1e-17 {
            break;
        }
    }
    sum
}

/// scipy's `firwin(taps, cutoff, window=("kaiser", 5.0))`: a low-pass filter, scaled to gain 1 at 0 Hz.
fn firwin(taps: usize, cutoff: f64) -> Vec<f64> {
    let alpha = (taps - 1) as f64 / 2.0;
    let sinc = |x: f64| if x == 0.0 { 1.0 } else { (std::f64::consts::PI * x).sin() / (std::f64::consts::PI * x) };
    let beta = 5.0;
    let i0b = bessel_i0(beta);
    let mut h: Vec<f64> = (0..taps).map(|n| {
        let m = n as f64 - alpha;
        let r = 2.0 * n as f64 / (taps - 1) as f64 - 1.0;
        cutoff * sinc(cutoff * m) * bessel_i0(beta * (1.0 - r * r).max(0.0).sqrt()) / i0b
    }).collect();
    let s: f64 = h.iter().sum();
    h.iter_mut().for_each(|v| *v /= s);
    h
}

/// scipy's `resample_poly(s, to / g, rate / g)`: samples at `rate` to `to`, with a polyphase filter (0.21's
/// `media.resample`). The filter is rounded to float32, as scipy rounds it to the samples' type.
pub fn resample(s: &[f32], rate: usize, to: usize) -> Vec<f32> {
    if rate == to || s.is_empty() {
        return s.to_vec();
    }
    let g = gcd(rate, to);
    let (up, down) = (to / g, rate / g);
    let n_in = s.len();
    let n_out = (n_in * up).div_ceil(down);
    let half_len = 10 * up.max(down);
    let h: Vec<f64> = firwin(2 * half_len + 1, 1.0 / up.max(down) as f64).iter()
        .map(|v| (*v as f32 * up as f32) as f64).collect();
    let pre_pad = down - half_len % down;
    let pre_remove = (half_len + pre_pad) / down;
    // with the filter padded by pre_pad zeros in front, output k reads upsampled input k * down - j at tap j
    let len_h = h.len() + pre_pad;
    let tap = |j: usize| if j < pre_pad { 0.0 } else { h[j - pre_pad] };
    (pre_remove..pre_remove + n_out).map(|k| {
        let i = k * down;
        let mut acc = 0.0;
        // taps j with (i - j) a multiple of up, and the input index (i - j) / up in range
        let mut j = i % up;
        while j < len_h && j <= i {
            let x = (i - j) / up;
            if x < n_in {
                acc += tap(j) * s[x] as f64;
            }
            j += up;
        }
        acc as f32
    }).collect()
}

/// A window as m:ss-m:ss (h:mm:ss past an hour), for a chunk's text.
pub fn label(start: f64, seconds: f64) -> String {
    let t = |s: f64| {
        let s = s as i64;
        if s >= 3600 { format!("{}:{:02}:{:02}", s / 3600, s / 60 % 60, s % 60) } else { format!("{}:{:02}", s / 60, s % 60) }
    };
    format!("{}-{}", t(start), t(start + seconds))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_cover_the_file() {
        assert_eq!(segments(0.0, 32.0, 240), vec![(0.0, 32.0)]);
        assert_eq!(segments(10.5, 32.0, 240), vec![(0.0, 10.5)]);
        assert_eq!(segments(70.0, 30.0, 240), vec![(0.0, 30.0), (30.0, 30.0), (60.0, 10.0)]);
        assert_eq!(segments(1e6, 30.0, 240).len(), 240);
    }

    #[test]
    fn labels_read_as_minutes_or_hours() {
        assert_eq!(label(0.0, 32.0), "0:00-0:32");
        assert_eq!(label(3590.0, 30.0), "59:50-1:00:20");
    }

    #[test]
    fn a_resample_keeps_a_slow_tone() {
        let rate = 48000;
        let s: Vec<f32> = (0..rate).map(|i| (2.0 * std::f32::consts::PI * 440.0 * i as f32 / rate as f32).sin()).collect();
        let r = resample(&s, rate, RATE);
        assert_eq!(r.len(), RATE);
        let want = (2.0 * std::f32::consts::PI * 440.0 * 8000.0 / RATE as f32).sin();
        assert!((r[8000] - want).abs() < 1e-2);
    }
}
