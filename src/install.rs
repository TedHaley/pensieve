//! `install`, `status` and `uninstall`: the LaunchAgent that runs the daemon, the MCP server in each agent, and the
//! skill. These are the only commands that a user runs.

use std::path::{Path, PathBuf};
use std::process::Command;

use regex::Regex;
use serde_json::{Value, json};

const LABEL: &str = "tools.pensieve";
const SERVER: &str = "pensieve";
const SKILLS: [(&str, &str); 2] = [
    ("pensieve-search", include_str!("../static/integrations/skills/pensieve-search/SKILL.md")),
    ("pensieve-thoughts", include_str!("../static/integrations/skills/pensieve-thoughts/SKILL.md")),
];
/// A file in each folder that Pensieve made, so that uninstall removes only its own folders.
const MARK: &str = ".pensieve";
const TOML_START: &str = "# pensieve:start";
const TOML_END: &str = "# pensieve:end";

fn home() -> PathBuf {
    store::home()
}

fn plist_path() -> PathBuf {
    home().join("Library/LaunchAgents").join(format!("{LABEL}.plist"))
}

/// Install keeps its own copy of the program, so that `cargo clean` or a moved download does not break the
/// LaunchAgent or the agents' MCP entries.
fn installed_exe() -> PathBuf {
    store::data_dir().join("bin/pensieve")
}

fn exe() -> String {
    let p = installed_exe();
    if p.is_file() {
        return p.to_string_lossy().to_string();
    }
    std::env::current_exe().map(|p| p.to_string_lossy().to_string()).unwrap_or_else(|_| "pensieve".into())
}

/// Copy through a temporary name, so that a daemon that runs the old copy never sees a half-written file.
fn copy_exe() -> Result<String, String> {
    let me = std::env::current_exe().map_err(|e| e.to_string())?;
    let to = installed_exe();
    if std::fs::canonicalize(&me).ok() != std::fs::canonicalize(&to).ok() {
        std::fs::create_dir_all(to.parent().ok_or("no folder")?).map_err(|e| e.to_string())?;
        let tmp = to.with_extension("new");
        std::fs::copy(&me, &tmp).map_err(|e| e.to_string())?;
        std::fs::rename(&tmp, &to).map_err(|e| e.to_string())?;
    }
    Ok(to.display().to_string())
}

/// With PENSIEVE_NO_LAUNCHD set, install writes the files but does not load the LaunchAgent (tests).
fn launchd_allowed() -> bool {
    std::env::var_os("PENSIEVE_NO_LAUNCHD").is_none()
}

fn domain() -> String {
    format!("gui/{}", unsafe { libc::getuid() })
}

fn esc(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

/// The daemon at standard priority: the indexing threads put themselves in the background band (engine::spawn).
/// A process in the background band throttles all of its threads, and then /status and MCP calls took up to 7 s
/// while indexing ran. launchd starts the daemon again if it stops.
fn plist(port: u16) -> String {
    let log = store::data_dir().join("serve.log");
    format!(r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>{}</string><string>serve</string><string>--port</string><string>{port}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Standard</string>
  <key>StandardOutPath</key><string>{}</string>
  <key>StandardErrorPath</key><string>{}</string>
</dict>
</plist>
"#, esc(&exe()), esc(&log.to_string_lossy()), esc(&log.to_string_lossy()))
}

fn which(name: &str) -> Option<String> {
    let path = std::env::var("PATH").unwrap_or_default();
    let mut dirs: Vec<PathBuf> = path.split(':').map(PathBuf::from).collect();
    dirs.extend([home().join(".local/bin"), home().join(".claude/local"), PathBuf::from("/opt/homebrew/bin"), PathBuf::from("/usr/local/bin")]);
    dirs.into_iter().map(|d| d.join(name)).find(|p| p.is_file()).map(|p| p.to_string_lossy().to_string())
}

fn write(f: &Path, text: &str) -> Result<(), String> {
    if let Some(d) = f.parent() {
        std::fs::create_dir_all(d).map_err(|e| e.to_string())?;
    }
    let tmp = f.with_extension("pensieve-tmp");
    std::fs::write(&tmp, text).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, f).map_err(|e| e.to_string())
}

fn run(cmd: &str, args: &[&str]) -> Result<String, String> {
    let out = Command::new(cmd).args(args).output().map_err(|e| format!("{cmd}: {e}"))?;
    let text = String::from_utf8_lossy(if out.stderr.is_empty() { &out.stdout } else { &out.stderr }).trim().to_string();
    if out.status.success() { Ok(text) } else { Err(if text.is_empty() { format!("{cmd} failed") } else { text }) }
}

fn launch_agent(port: u16, on: bool) -> Result<(), String> {
    let p = plist_path();
    if launchd_allowed() && p.exists() {
        let service = format!("{}/{LABEL}", domain());
        let _ = run("launchctl", &["bootout", &service]);
        // launchd refuses a bootstrap while the old daemon stops
        for _ in 0..50 {
            if run("launchctl", &["print", &service]).is_err() {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(200));
        }
    }
    if !on {
        if p.exists() {
            std::fs::remove_file(&p).map_err(|e| e.to_string())?;
        }
        return Ok(());
    }
    std::fs::create_dir_all(store::data_dir()).map_err(|e| e.to_string())?;
    write(&p, &plist(port))?;
    if launchd_allowed() {
        run("launchctl", &["bootstrap", &domain(), &p.to_string_lossy()])?;
    }
    Ok(())
}

/// Claude Code, through its CLI, which owns ~/.claude.json. A server named pensieve that runs another program
/// stays: Pensieve does not change what it did not add.
fn claude_mcp(on: bool) -> Result<&'static str, String> {
    let Some(claude) = which("claude") else { return Ok("Claude Code is not installed") };
    let have = run(&claude, &["mcp", "get", SERVER]).ok();
    let ours = have.as_deref().is_some_and(|t| t.contains(&exe()));
    if have.is_some() && !ours {
        return Err(format!("Claude Code has an MCP server named {SERVER} that Pensieve did not add; remove it with `claude mcp remove --scope user {SERVER}` and run install again"));
    }
    if on && !ours {
        run(&claude, &["mcp", "add", "--scope", "user", SERVER, "--", &exe(), "mcp"])?;
    } else if !on && ours {
        run(&claude, &["mcp", "remove", "--scope", "user", SERVER])?;
    }
    Ok(if on { "added to Claude Code" } else { "removed from Claude Code" })
}

/// Codex reads ~/.codex/config.toml. Pensieve's block sits between two marker lines.
fn codex_mcp(on: bool) -> Result<&'static str, String> {
    let dir = home().join(".codex");
    if !dir.is_dir() {
        return Ok("Codex is not installed");
    }
    let f = dir.join("config.toml");
    let text = std::fs::read_to_string(&f).unwrap_or_default();
    let block_re = Regex::new(&format!(r"(?s)\n*{}.*?{}\n?", regex::escape(TOML_START), regex::escape(TOML_END))).map_err(|e| e.to_string())?;
    let rest = block_re.replace_all(&text, "\n").trim_matches('\n').to_string();
    let theirs = Regex::new(&format!(r"(?m)^\s*\[mcp_servers\.{SERVER}\]")).map_err(|e| e.to_string())?.is_match(&rest);
    if theirs {
        return Err(format!("{} has an MCP server named {SERVER} that Pensieve did not add", f.display()));
    }
    let block = format!("{TOML_START}\n[mcp_servers.{SERVER}]\ncommand = {}\nargs = [\"mcp\"]\n{TOML_END}\n", json!(exe()));
    let out = if on { format!("{}{block}", if rest.is_empty() { String::new() } else { format!("{rest}\n\n") }) }
              else if rest.is_empty() { String::new() } else { format!("{rest}\n") };
    if out != text {
        write(&f, &out)?;
    }
    Ok(if on { "added to Codex" } else { "removed from Codex" })
}

fn skills_dir() -> PathBuf {
    home().join(".agents/skills")
}

/// Agents that read only their own skills folder get a link to the shared copy.
fn skill_links(name: &str) -> Vec<PathBuf> {
    [home().join(".claude/skills"), home().join(".codex/skills")].into_iter()
        .filter(|d| d.parent().is_some_and(Path::is_dir)).map(|d| d.join(name)).collect()
}

fn skills(on: bool) -> Result<String, String> {
    for (name, text) in SKILLS {
        skill(name, text, on)?;
    }
    Ok(if on { SKILLS.map(|s| s.0).join(", ") } else { "removed".into() })
}

fn skill(name: &str, text: &str, on: bool) -> Result<(), String> {
    let d = skills_dir().join(name);
    if d.exists() && !d.join(MARK).exists() {
        return Err(format!("{} exists and Pensieve did not make it; leaving it alone", d.display()));
    }
    for link in skill_links(name) {
        if link.is_symlink() {
            let _ = std::fs::remove_file(&link);
        } else if link.join(MARK).exists() {
            let _ = std::fs::remove_dir_all(&link);
        }
    }
    if d.exists() {
        std::fs::remove_dir_all(&d).map_err(|e| e.to_string())?;
    }
    if !on {
        return Ok(());
    }
    std::fs::create_dir_all(&d).map_err(|e| e.to_string())?;
    std::fs::write(d.join("SKILL.md"), text).map_err(|e| e.to_string())?;
    std::fs::write(d.join(MARK), "Pensieve made this folder. `pensieve uninstall` removes it.\n").map_err(|e| e.to_string())?;
    for link in skill_links(name) {
        if link.exists() {
            continue; // someone else's skill of the same name
        }
        if let Some(parent) = link.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        std::os::unix::fs::symlink(&d, &link).map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn report(step: &str, r: Result<impl std::fmt::Display, String>) -> bool {
    match r {
        Ok(m) => {
            let m = m.to_string();
            println!("  {step}{}", if m.is_empty() { String::new() } else { format!(": {m}") });
            true
        }
        Err(e) => {
            eprintln!("  {step}: {e}");
            false
        }
    }
}

pub fn install(port: u16) -> bool {
    println!("Installing Pensieve");
    if !report("program", copy_exe()) {
        return false;
    }
    let mut ok = report("decoder", crate::media::ensure());
    ok &= report("daemon", launch_agent(port, true).map(|_| "starts at login; indexing runs at background priority"));
    ok &= report("MCP", claude_mcp(true));
    ok &= report("MCP", codex_mcp(true));
    ok &= report("skills", skills(true));
    println!("Indexing starts now in the background. `pensieve status` shows the progress.");
    ok
}

pub fn uninstall(purge: bool) -> bool {
    println!("Removing Pensieve");
    let mut ok = report("daemon", launch_agent(0, false).map(|_| "stopped"));
    ok &= report("MCP", claude_mcp(false));
    ok &= report("MCP", codex_mcp(false));
    ok &= report("skills", skills(false));
    if purge {
        let d = store::data_dir();
        ok &= report("index", std::fs::remove_dir_all(&d).map(|_| format!("removed {}", d.display())).map_err(|e| e.to_string()));
    } else {
        println!("  The index stays in {}. `pensieve uninstall --purge` removes it.", store::data_dir().display());
    }
    ok
}

pub fn status(port: u16) -> bool {
    let loaded = launchd_allowed() && run("launchctl", &["print", &format!("{}/{LABEL}", domain())]).is_ok();
    println!("daemon: {}", if loaded { "loaded in launchd" } else if plist_path().exists() { "installed, not loaded" } else { "not installed" });
    let answer = crate::http::local(port, "GET", "/status", None, std::time::Duration::from_secs(5));
    match answer.ok().and_then(|r| serde_json::from_str::<Value>(&r.body).ok()) {
        Some(s) if s["opening"] == true => println!("index: opening"),
        Some(s) if s.get("sessions").is_none() => println!("index: counting, ask again in a few seconds"),
        Some(s) => {
            println!("index: {} repos, {} code chunks, {} files, {} sessions", s["repos"], s["code_chunks"], s["files"], s["sessions"]);
            let busy: Vec<&str> = s["busy"].as_array().into_iter().flatten().filter_map(Value::as_str).collect();
            println!("now: {}", if busy.is_empty() { "idle".to_string() } else { format!("indexing {}", busy.join(", ")) });
            if let Some(d) = s.get("downloading") {
                println!("model download: {} of {} bytes", d["done"], d["total"]);
            }
        }
        None => println!("index: not answering on port {port}"),
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plist_runs_serve_at_standard_priority() {
        let p = plist(8790);
        assert!(p.contains("<string>serve</string>"));
        assert!(p.contains("<key>ProcessType</key><string>Standard</string>"));
        assert!(!p.contains("LowPriorityIO"));
    }
}
