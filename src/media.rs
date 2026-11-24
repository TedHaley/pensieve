//! The video and sound decoder, inside this binary. `install` and `serve` write it to the data folder, where
//! `embed::media::tool` finds it.

use std::os::unix::fs::PermissionsExt;

static DECODER: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/pensieve-media"));

/// Writes the decoder to data_dir/bin when the copy there is missing or is not this build's.
pub fn ensure() -> Result<String, String> {
    let to = embed::media::installed_tool();
    if std::fs::read(&to).ok().as_deref() != Some(DECODER) {
        std::fs::create_dir_all(to.parent().ok_or("no folder")?).map_err(|e| e.to_string())?;
        // A half-written decoder must never be at the path that the indexer runs.
        let tmp = to.with_extension("new");
        std::fs::write(&tmp, DECODER).map_err(|e| e.to_string())?;
        std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o755)).map_err(|e| e.to_string())?;
        std::fs::rename(&tmp, &to).map_err(|e| e.to_string())?;
    }
    Ok(to.display().to_string())
}
