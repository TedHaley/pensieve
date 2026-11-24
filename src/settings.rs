//! Settings in `~/.pensieve/settings.json`, over the defaults of Pensieve 0.21. The file holds only the keys
//! that differ from the defaults.

use std::sync::Mutex;

use serde_json::{Map, Value, json};

const META: &str = include_str!("../static/settings-meta.json");

static LOCK: Mutex<()> = Mutex::new(());

fn read() -> Map<String, Value> {
    std::fs::read(store::data_dir().join("settings.json")).ok()
        .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
        .and_then(|v| v.as_object().cloned()).unwrap_or_default()
}

fn write(v: &Map<String, Value>) -> std::io::Result<()> {
    let path = store::data_dir().join("settings.json");
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, serde_json::to_vec_pretty(v)?)?;
    std::fs::rename(tmp, path)
}

/// 0.21's defaults, with indexing on: there is no setup step.
fn defaults() -> Map<String, Value> {
    let mut d = serde_json::from_str::<Value>(META).ok()
        .and_then(|m| m["defaults"].as_object().cloned()).unwrap_or_default();
    for (k, v) in [("mode", json!("developer")), ("setup_done", json!(true))] {
        d.insert(k.into(), v);
    }
    d
}

pub fn load() -> Map<String, Value> {
    let mut s = defaults();
    s.extend(read());
    s
}

/// Save the changes. Err names the first key that is not a setting.
pub fn update(changes: &Map<String, Value>) -> Result<Map<String, Value>, String> {
    let _g = LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let known = defaults();
    if let Some(k) = changes.keys().find(|k| !known.contains_key(*k)) {
        return Err(format!("{k} is not a setting"));
    }
    let mut saved = read();
    saved.extend(changes.clone());
    write(&saved).map_err(|e| e.to_string())?;
    Ok(load())
}
