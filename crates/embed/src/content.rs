//! What the search model reads besides text: images, video and sound (0.21's `content.py`). Each kind is off until
//! the user switches it on; then its part of EmbeddingGemma 2 downloads, and once it is here, those files are read
//! by what they show or say. A file of a kind that is off, or not installed yet, is found by its name.
//!
//! A kind's switch is its file type's `index` setting (settings.file_types): "content" = on. The app owns the
//! settings: it passes `on(kind)` in, and writes `settings_patch` when the user switches a kind.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use anyhow::bail;
use serde_json::{Value, json};

use crate::files::{self, MODEL, Part};

/// A kind of file read by content.
pub struct KindInfo {
    pub id: &'static str,
    pub parts: &'static [Part],
    /// Whether it needs the video and sound decoder.
    pub decoder: bool,
    pub label: &'static str,
    pub description: &'static str,
}

pub const KINDS: [KindInfo; 3] = [
    KindInfo { id: "images", parts: &[Part::Vision], decoder: false, label: "Images",
               description: "Screenshots, diagrams and photos, by what they show." },
    KindInfo { id: "video", parts: &[Part::Vision], decoder: true, label: "Video",
               description: "Screen recordings and videos, by what they show, 32 seconds at a time. With audio on, by what is said in them too." },
    KindInfo { id: "audio", parts: &[Part::Audio], decoder: true, label: "Audio",
               description: "Voice notes, meetings and other sound, by what is said or heard, 30 seconds at a time." },
];

pub fn kind(id: &str) -> Option<&'static KindInfo> {
    KINDS.iter().find(|k| k.id == id)
}

/// The settings change that switches a kind on or off (merged into settings by the app).
pub fn settings_patch(kind: &str, on: bool) -> Value {
    json!({"file_types": {kind: {"index": if on { "content" } else { "name" }, "map": on}}})
}

#[derive(Clone, Debug, Default)]
struct Job {
    done: u64,
    total: u64,
    error: Option<String>,
}

/// The downloads of model parts that run or failed, and the callbacks to run when a part is here (the app re-reads
/// the files that waited for it).
#[derive(Clone, Default)]
pub struct Installer {
    jobs: Arc<Mutex<HashMap<Part, Job>>>,
    on_installed: Arc<Mutex<Vec<Arc<dyn Fn() + Send + Sync>>>>,
}

impl Installer {
    pub fn on_installed(&self, f: impl Fn() + Send + Sync + 'static) {
        self.on_installed.lock().unwrap_or_else(|e| e.into_inner()).push(Arc::new(f));
    }

    fn jobs(&self) -> std::sync::MutexGuard<'_, HashMap<Part, Job>> {
        self.jobs.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Whether files of `kind` are read by content now: switched on, its parts downloaded and, for video and sound,
    /// a decoder here.
    pub fn ready(kind: &KindInfo, on: bool) -> bool {
        on && kind.parts.iter().all(|p| files::part_here(MODEL, *p)) && (!kind.decoder || crate::media::decoder().is_some())
    }

    /// GET /api/content: each kind, whether it is on and ready, and what stands in the way (needs, installing with
    /// its progress, error).
    pub fn status(&self, on: &dyn Fn(&str) -> bool) -> Value {
        let mut out = vec![json!({"id": "text", "label": "Text", "on": true, "ready": true, "size_mb": 0, "locked": true,
                                  "description": "Code, documents, notes and agent sessions."})];
        let jobs = self.jobs().clone();
        for k in &KINDS {
            let missing: Vec<Part> = k.parts.iter().copied().filter(|p| !files::part_here(MODEL, *p)).collect();
            let mine: Vec<(Part, &Job)> = k.parts.iter().filter_map(|p| jobs.get(p).map(|j| (*p, j))).collect();
            let mut o = json!({"id": k.id, "label": k.label, "description": k.description, "on": on(k.id),
                               "ready": Self::ready(k, on(k.id)), "size_mb": k.parts.iter().map(|p| p.mb()).sum::<u64>(),
                               "installed": missing.is_empty()});
            if k.decoder && crate::media::decoder().is_none() {
                o["needs"] = json!("decoder");
            }
            if let Some(e) = mine.iter().find_map(|(_, j)| j.error.clone()) {
                o["error"] = json!(e);
            } else if !missing.is_empty() && mine.iter().any(|(_, j)| j.error.is_none()) {
                let done: u64 = mine.iter().map(|(_, j)| j.done).sum();
                let total: u64 = mine.iter().map(|(p, j)| if j.total > 0 { j.total } else { p.mb() << 20 }).sum();
                o["installing"] = json!({"done": done, "total": total});
            }
            out.push(o);
        }
        json!({"kinds": out, "model": MODEL})
    }

    /// Download the parts `kind` needs that aren't here, in the background. Returns at once.
    pub fn install(&self, kind: &KindInfo) {
        for &part in kind.parts {
            {
                let mut jobs = self.jobs();
                let running = jobs.get(&part).is_some_and(|j| j.error.is_none());
                if files::part_here(MODEL, part) || running {
                    continue;
                }
                jobs.insert(part, Job::default());
            }
            let me = self.clone();
            let _ = std::thread::Builder::new().name(format!("install-{}", part.name())).spawn(move || me.download(part));
        }
    }

    fn download(&self, part: Part) {
        let jobs = self.jobs.clone();
        let r = files::download_part(MODEL, part, &mut |done, total| {
            if let Some(j) = jobs.lock().unwrap_or_else(|e| e.into_inner()).get_mut(&part) {
                j.done = done;
                j.total = total;
            }
        });
        match r {
            Ok(_) => {
                self.jobs().remove(&part);
                let fs = self.on_installed.lock().unwrap_or_else(|e| e.into_inner()).clone();
                fs.iter().for_each(|f| f());
            }
            Err(e) => {
                self.jobs().insert(part, Job { error: Some(format!("{e:#}")), ..Job::default() });
            }
        }
    }

    /// PUT /api/content: the settings patch for switching `kind`, after which the app calls `status`. Switching on
    /// installs what the kind needs; a failed download is tried again.
    pub fn switch(&self, kind_id: &str, on: bool) -> anyhow::Result<Value> {
        let Some(k) = kind(kind_id) else {
            bail!("unknown kind {kind_id:?}; one of: {}", KINDS.iter().map(|k| k.id).collect::<Vec<_>>().join(", "));
        };
        {
            let mut jobs = self.jobs();
            for p in k.parts {
                if jobs.get(p).is_some_and(|j| j.error.is_some()) {
                    jobs.remove(p);
                }
            }
        }
        if on {
            self.install(k);
        }
        Ok(settings_patch(kind_id, on))
    }

    /// DELETE /api/content/{part}: delete a downloaded part that no switched-on kind needs, to free its space.
    pub fn remove(&self, part: &str, on: &dyn Fn(&str) -> bool) -> anyhow::Result<()> {
        let Some(p) = Part::ALL.into_iter().find(|p| p.name() == part) else { bail!("unknown part {part:?}") };
        if KINDS.iter().any(|k| on(k.id) && k.parts.contains(&p)) {
            bail!("{part} is in use");
        }
        if let Some(f) = files::model_folder(MODEL).map(|d| d.join(p.file())).filter(|f| f.exists()) {
            std::fs::remove_file(f)?;
        }
        Ok(())
    }

    /// At start, and when settings.file_types change: install what switched-on kinds still need.
    pub fn resume(&self, on: &dyn Fn(&str) -> bool) {
        for k in &KINDS {
            if on(k.id) {
                self.install(k);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_settings_patch_matches_0_21() {
        assert_eq!(settings_patch("video", true), json!({"file_types": {"video": {"index": "content", "map": true}}}));
        assert!(Installer::default().switch("pdf", true).is_err());
    }

    #[test]
    fn a_part_in_use_is_not_removed() {
        let i = Installer::default();
        assert!(i.remove("vision", &|k| k == "images").is_err());
        assert!(i.remove("nope", &|_| false).is_err());
    }
}
