//! Which files the user works with. Images, video and sound cost much more to read by content than text, so the
//! files index reads them by content only when they are in use. Three signals count:
//!
//! - Opens: the open count and the last open date that macOS keeps (Spotlight `kMDItemUseCount` and
//!   `kMDItemLastUsedDate`). macOS counts only opens from Finder, a double-click or Open Recent.
//! - Edits: saves that the file watcher or a sweep saw, at most one each EDIT_GAP seconds.
//! - Agent use: agent sessions that read or edited the file (`session_files`).
//!
//! A file is in use with OFTEN opens, OFTEN edits or one agent session. One open or one edit is not enough, because
//! many files are opened once and never again. The other files in the folder of a file in use are in use too,
//! because the clips and photos of a project are seldom opened one at a time. An indexed root folder (such as ~/Documents) does not count as such a
//! folder.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::RwLock;

use store::rusqlite::params;

use crate::Index;

/// Saves closer together than this count as one edit (an app that saves each minute is one edit).
pub const EDIT_GAP: f64 = 3600.0;
/// Opens or edits that put a file in use.
pub const OFTEN: i64 = 2;
/// Paths given to one mdls call.
const MDLS_BATCH: usize = 200;

#[derive(Default)]
pub struct InUse {
    files: HashSet<String>,
    folders: HashSet<String>,
}

#[derive(Default)]
pub struct State(RwLock<InUse>);

/// The use of one file.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Use {
    pub opens: i64,
    pub last_open: Option<f64>,
    pub edits: i64,
    pub last_edit: Option<f64>,
    pub agent_sessions: i64,
}

impl Use {
    pub fn in_use(&self) -> bool {
        self.opens >= OFTEN || self.edits >= OFTEN || self.agent_sessions >= 1
    }
}

/// The open count and the last open time of each file under `roots` that macOS counted an open for.
pub fn spotlight_opens(roots: &[PathBuf]) -> Vec<(String, i64, Option<f64>)> {
    let mut paths = Vec::new();
    for root in roots {
        let Ok(o) = Command::new("mdfind").arg("-onlyin").arg(root).arg("kMDItemUseCount >= 1").output() else { continue };
        paths.extend(String::from_utf8_lossy(&o.stdout).lines().filter(|l| !l.is_empty()).map(String::from));
    }
    let mut out = Vec::new();
    for batch in paths.chunks(MDLS_BATCH) {
        let Ok(o) = Command::new("mdls")
            .args(["-raw", "-nullMarker", "", "-name", "kMDItemUseCount", "-name", "kMDItemLastUsedDate"])
            .args(batch)
            .output() else { continue };
        let text = String::from_utf8_lossy(&o.stdout);
        let values: Vec<&str> = text.split('\0').collect();
        if values.len() != batch.len() * 2 {
            continue;
        }
        for (path, pair) in batch.iter().zip(values.chunks(2)) {
            // mdls gives the values in name order: kMDItemLastUsedDate, then kMDItemUseCount.
            let count = pair[1].trim().parse::<i64>().unwrap_or(0);
            let last = chrono::DateTime::parse_from_str(pair[0].trim(), "%Y-%m-%d %H:%M:%S %z")
                .ok().map(|t| t.timestamp() as f64);
            if count > 0 {
                out.push((path.clone(), count, last));
            }
        }
    }
    out
}

/// The folder whose other files a file in use pulls in, or None for a file directly in a root or in the home folder.
fn project_folder(path: &str, roots: &[PathBuf], home: &Path) -> Option<String> {
    let parent = Path::new(path).parent()?;
    if parent == home || roots.iter().any(|r| r == parent) {
        return None;
    }
    Some(parent.to_string_lossy().into_owned())
}

impl Index {
    /// The use of each file that has any.
    pub fn files_use(&self) -> HashMap<String, Use> {
        let mut out: HashMap<String, Use> = HashMap::new();
        let _ = self.db.read(|c| {
            let mut st = c.prepare("SELECT path, opens, last_open, edits, last_edit FROM file_use")?;
            let mut rows = st.query([])?;
            while let Some(r) = rows.next()? {
                let u = out.entry(r.get(0)?).or_default();
                (u.opens, u.last_open, u.edits, u.last_edit) = (r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?);
            }
            let mut st = c.prepare("SELECT path, COUNT(*) FROM session_files WHERE path LIKE '/%' GROUP BY path")?;
            let mut rows = st.query([])?;
            while let Some(r) = rows.next()? {
                out.entry(r.get(0)?).or_default().agent_sessions = r.get(1)?;
            }
            Ok(())
        });
        out
    }

    /// Read the open counts from Spotlight, then work out again which files and folders are in use. Returns
    /// whether that changed.
    pub fn files_refresh_use(&self) -> bool {
        let roots = self.files_roots();
        let opens = spotlight_opens(&roots);
        let _ = self.db.tx(|t| {
            for (path, count, last) in &opens {
                t.execute("INSERT INTO file_use(path, opens, last_open) VALUES(?,?,?) \
                           ON CONFLICT(path) DO UPDATE SET opens=excluded.opens, last_open=excluded.last_open",
                          params![path, count, last])?;
            }
            Ok(())
        });
        self.files_recompute_use(&roots)
    }

    fn files_recompute_use(&self, roots: &[PathBuf]) -> bool {
        let home = self.config().home.clone();
        let mut next = InUse::default();
        for (path, u) in self.files_use() {
            if u.in_use() {
                if let Some(f) = project_folder(&path, roots, &home) {
                    next.folders.insert(f);
                }
                next.files.insert(path);
            }
        }
        let mut cur = self.file_use.0.write().unwrap_or_else(|e| e.into_inner());
        let changed = cur.files != next.files || cur.folders != next.folders;
        *cur = next;
        changed
    }

    /// Count a save of `path` at `mtime`. Returns true when this puts a new folder in use, so that the files in
    /// it are read again.
    pub fn files_note_edit(&self, path: &str, mtime: f64) -> bool {
        let _ = self.db.tx(|c| {
            c.execute("INSERT INTO file_use(path, edits, last_edit) VALUES(?1, 1, ?2) \
                       ON CONFLICT(path) DO UPDATE SET \
                         edits = edits + (last_edit IS NULL OR ?2 - last_edit >= ?3), \
                         last_edit = max(coalesce(last_edit, 0), ?2)",
                      params![path, mtime, EDIT_GAP])
        });
        let u = self.db.read(|c| {
            c.query_row("SELECT opens, last_open, edits, last_edit, \
                           (SELECT COUNT(*) FROM session_files WHERE path=?1) FROM file_use WHERE path=?1", [path],
                        |r| Ok(Use { opens: r.get(0)?, last_open: r.get(1)?, edits: r.get(2)?, last_edit: r.get(3)?,
                                     agent_sessions: r.get(4)? }))
        }).unwrap_or_default();
        if !u.in_use() {
            return false;
        }
        let folder = project_folder(path, &self.files_roots(), &self.config().home);
        let mut cur = self.file_use.0.write().unwrap_or_else(|e| e.into_inner());
        cur.files.insert(path.to_string());
        folder.is_some_and(|f| cur.folders.insert(f))
    }

    /// Whether `path` is in use, or is in the folder of a file in use.
    pub fn files_in_use(&self, path: &Path) -> bool {
        let u = self.file_use.0.read().unwrap_or_else(|e| e.into_inner());
        let p = path.to_string_lossy();
        u.files.contains(p.as_ref()) || path.parent().is_some_and(|d| u.folders.contains(d.to_string_lossy().as_ref()))
    }

    /// Whether a file of this media kind (images, video, audio) is in use but indexed by name only, because the
    /// kind's model part is not here yet.
    pub fn files_waiting(&self, kind: &str) -> bool {
        let paths: Vec<String> = self.db.read(|c| {
            let mut st = c.prepare("SELECT path FROM files WHERE mode='name'")?;
            st.query_map([], |r| r.get(0))?.collect()
        }).unwrap_or_default();
        paths.iter().any(|p| crate::files::type_of(p) == kind && self.files_in_use(Path::new(p)))
    }

    /// (files in use, folders in use), for the status.
    pub fn files_in_use_count(&self) -> (usize, usize) {
        let u = self.file_use.0.read().unwrap_or_else(|e| e.into_inner());
        (u.files.len(), u.folders.len())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn in_use_needs_repeat_use() {
        assert!(!Use::default().in_use());
        assert!(!Use { opens: 1, edits: 1, ..Default::default() }.in_use());
        assert!(Use { opens: 2, ..Default::default() }.in_use());
        assert!(Use { edits: 2, ..Default::default() }.in_use());
        assert!(Use { agent_sessions: 1, ..Default::default() }.in_use());
    }

    /// Reads this Mac's Spotlight open counts: `cargo test -p index -- --ignored spotlight`.
    #[test]
    #[ignore]
    fn spotlight_gives_open_counts() {
        let docs = PathBuf::from(std::env::var("HOME").unwrap()).join("Documents");
        let opens = spotlight_opens(&[docs]);
        println!("{} files with an open count, {} with a date", opens.len(), opens.iter().filter(|o| o.2.is_some()).count());
        assert!(opens.iter().all(|o| o.1 > 0));
    }

    #[test]
    fn a_root_folder_is_not_a_project() {
        let roots = [PathBuf::from("/h/Documents")];
        let home = Path::new("/h");
        assert_eq!(project_folder("/h/Documents/a.mov", &roots, home), None);
        assert_eq!(project_folder("/h/notes.md", &roots, home), None);
        assert_eq!(project_folder("/h/Documents/shoot/a.mov", &roots, home).as_deref(), Some("/h/Documents/shoot"));
    }
}
