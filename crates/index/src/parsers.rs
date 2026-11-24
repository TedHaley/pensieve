//! Agent transcripts (Claude Code, Codex, Qwen Code, Claude Code memory files) read into sessions of turns.
//!
//! Transcripts are append-only JSONL: `parse_from` reads only the lines written after an earlier parse, and a last
//! line without its newline is left for the next read.

use std::collections::{BTreeMap, HashMap};
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde_json::Value;

use crate::tokens;

#[derive(Debug, Clone, Default)]
pub struct Session {
    pub id: String,
    pub source: String,
    pub path: String,
    pub project: String,
    pub title: String,
    pub started: String,
    pub updated: String,
    /// (role, text): role is user, assistant or memory.
    pub turns: Vec<(String, String)>,
    /// Parallel to `turns`. An entry is empty when the transcript line has no timestamp.
    pub turn_ts: Vec<String>,
    /// (index in `turns`, git branch of the working folder) of each turn whose transcript line names a branch.
    pub turn_branch: Vec<(usize, String)>,
    /// (index in `turns`, absolute path) of each tool call on a file. The index is that of the last turn before
    /// the call, because a reply that holds only tool calls adds no turn.
    pub turn_files: Vec<(usize, String)>,
    /// Absolute path -> whether a tool edited it (else it only read it).
    pub touched: BTreeMap<String, bool>,
    /// `[full MCP tool name, input, result content]` of each MCP call that did not fail.
    pub mcp: Vec<Value>,
    /// Bytes of the transcript read so far: whole lines only.
    pub offset: u64,
    pub state: ParseState,
}

/// A parser's own state between two reads of one transcript.
#[derive(Debug, Clone, Default)]
pub struct ParseState {
    titles: BTreeMap<&'static str, String>,
    pub auto_title: Option<String>,
    mcp_uses: HashMap<String, (String, Value)>,
    pub tok: tokens::Usage,
}

pub fn str_of<'a>(v: &'a Value, key: &str) -> &'a str {
    v.get(key).and_then(Value::as_str).unwrap_or("")
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// The tag of a reminder that opens at `rest` ("<" already passed), when its opening tag is whole.
fn reminder_tag(rest: &str) -> Option<&str> {
    for fixed in ["system-reminder", "task-notification"] {
        if rest.starts_with(fixed) && rest[fixed.len()..].starts_with('>') {
            return Some(&rest[..fixed.len()]);
        }
    }
    for prefix in ["local-command-", "command-"] {
        if let Some(after) = rest.strip_prefix(prefix) {
            let n: usize = after.chars().take_while(|c| is_word(*c)).map(char::len_utf8).sum();
            if n > 0 && after[n..].starts_with('>') {
                return Some(&rest[..prefix.len() + n]);
            }
        }
    }
    None
}

/// The text without the reminders and notices that the harness puts into turns (`<system-reminder>…</…>`).
pub fn clean(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while let Some(off) = text[i..].find('<') {
        let at = i + off;
        if let Some(tag) = reminder_tag(&text[at + 1..]) {
            let body = at + 1 + tag.len() + 1;
            if let Some(end) = text[body..].find(&format!("</{tag}>")) {
                out.push_str(&text[i..at]);
                i = body + end + tag.len() + 3;
                continue;
            }
        }
        out.push_str(&text[i..at + 1]);
        i = at + 1;
    }
    out.push_str(&text[i..]);
    out.trim().to_string()
}

/// The JSON objects of a transcript from `s.offset` on; the offset moves past each whole line.
fn lines(path: &Path, s: &mut Session) -> std::io::Result<Vec<Value>> {
    let mut f = std::fs::File::open(path)?;
    if s.offset > 0 {
        f.seek(SeekFrom::Start(s.offset))?;
    }
    let mut r = BufReader::new(f);
    let mut out = Vec::new();
    let mut raw = Vec::new();
    loop {
        raw.clear();
        if r.read_until(b'\n', &mut raw)? == 0 || raw.last() != Some(&b'\n') {
            break;
        }
        s.offset += raw.len() as u64;
        if let Ok(d @ Value::Object(_)) = serde_json::from_str::<Value>(&String::from_utf8_lossy(&raw)) {
            out.push(d);
        }
    }
    Ok(out)
}

/// Every JSON object of a file, also a last line without its newline.
fn all_lines(path: &Path) -> Vec<Value> {
    let mut buf = Vec::new();
    if std::fs::File::open(path).and_then(|mut f| f.read_to_end(&mut buf)).is_err() {
        return Vec::new();
    }
    String::from_utf8_lossy(&buf).lines()
        .filter_map(|l| serde_json::from_str::<Value>(l).ok().filter(Value::is_object))
        .collect()
}

fn add(s: &mut Session, role: &str, text: &str, ts: &str) {
    let text = clean(text);
    if text.is_empty() {
        return;
    }
    s.turns.push((role.to_string(), text));
    s.turn_ts.push(ts.to_string());
    if s.started.is_empty() {
        s.started = ts.to_string();
    }
    if !ts.is_empty() {
        s.updated = ts.to_string();
    }
}

const EDIT_TOOLS: [&str; 7] = ["edit", "write", "multiedit", "notebookedit", "write_file", "replace", "apply_patch"];
const PATH_KEYS: [&str; 4] = ["file_path", "absolute_path", "notebook_path", "path"];

fn note_tool(s: &mut Session, name: &str, args: Option<&Value>) {
    let Some(args) = args.filter(|a| a.is_object()) else { return };
    let edited = EDIT_TOOLS.contains(&name.to_lowercase().as_str());
    for k in PATH_KEYS {
        if let Some(v) = args.get(k).and_then(Value::as_str).filter(|v| v.starts_with('/')) {
            let e = s.touched.entry(v.to_string()).or_insert(false);
            *e = *e || edited;
            let at = (s.turns.len().saturating_sub(1), v.to_string());
            if s.turn_files.last() != Some(&at) {
                s.turn_files.push(at);
            }
        }
    }
}

/// The names Claude Code gives a session, first wins: an agent's name, the name from /rename, the ai-title.
const TITLE_KEYS: [(&str, &str); 3] = [("agent-name", "agentName"), ("custom-title", "customTitle"), ("ai-title", "aiTitle")];

/// The title after transcript line `d`. The last line of each kind wins, and an empty one clears that name. With
/// no name left, the title from the first prompt comes back.
fn title_line(d: &Value, state: &mut ParseState, title: &str) -> String {
    let t = str_of(d, "type");
    let Some((_, key)) = TITLE_KEYS.iter().find(|(k, _)| *k == t) else { return title.to_string() };
    let name = [d.get(*key), d.get("title")].into_iter().flatten().find(|v| truthy(Some(v)))
        .and_then(Value::as_str).unwrap_or("");
    state.titles.insert(key, name.to_string());
    TITLE_KEYS.iter().filter_map(|(_, k)| state.titles.get(k).filter(|v| !v.is_empty()).cloned()).next()
        .unwrap_or_else(|| state.auto_title.clone().unwrap_or_default())
}

fn new_session(source: &str, path: &Path) -> Session {
    let stem = path.file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
    Session { id: format!("{source}:{stem}"), source: source.into(), path: path.to_string_lossy().into(), ..Default::default() }
}

fn parse_claude(path: &Path, mut s: Session) -> std::io::Result<Session> {
    for d in lines(path, &mut s)? {
        tokens::claude_line(&d, &mut s.state.tok);
        s.title = title_line(&d, &mut s.state, &s.title);
        let t = str_of(&d, "type");
        if !(t == "user" || t == "assistant") || truthy(d.get("isMeta")) || truthy(d.get("isSidechain")) {
            continue;
        }
        if s.project.is_empty() {
            s.project = str_of(&d, "cwd").to_string();
        }
        let content = d.get("message").and_then(|m| m.get("content"));
        let text = match content {
            Some(Value::Array(blocks)) => {
                for b in blocks {
                    match str_of(b, "type") {
                        "tool_use" => {
                            let name = str_of(b, "name").to_string();
                            note_tool(&mut s, &name, b.get("input"));
                            if name.starts_with("mcp__") {
                                let input = b.get("input").filter(|v| truthy(Some(v))).cloned()
                                    .unwrap_or_else(|| Value::Object(Default::default()));
                                s.state.mcp_uses.insert(str_of(b, "id").to_string(), (name, input));
                            }
                        }
                        "tool_result" if !truthy(b.get("is_error")) => {
                            if let Some((name, input)) = s.state.mcp_uses.get(str_of(b, "tool_use_id")) {
                                let c = b.get("content").cloned().unwrap_or(Value::Null);
                                s.mcp.push(Value::Array(vec![Value::String(name.clone()), input.clone(), c]));
                            }
                        }
                        _ => {}
                    }
                }
                Some(blocks.iter().filter(|b| str_of(b, "type") == "text").map(|b| str_of(b, "text")).collect::<Vec<_>>().join("\n"))
            }
            Some(Value::String(c)) => Some(c.clone()),
            _ => None,
        };
        if let Some(text) = text {
            let ts = str_of(&d, "timestamp").to_string();
            let before = s.turns.len();
            add(&mut s, t, &text, &ts);
            let branch = str_of(&d, "gitBranch");
            if s.turns.len() > before && !branch.is_empty() {
                s.turn_branch.push((before, branch.to_string()));
            }
        }
    }
    tokens::subagents(path, &mut s.state.tok);
    Ok(s)
}

fn parse_qwen(path: &Path, mut s: Session) -> std::io::Result<Session> {
    for d in lines(path, &mut s)? {
        tokens::qwen_line(&d, &mut s.state.tok);
        let t = str_of(&d, "type");
        let sub = str_of(&d, "subtype");
        if !(t == "user" || t == "assistant") || sub == "notification" || sub == "cron" {
            continue;
        }
        if s.project.is_empty() {
            s.project = str_of(&d, "cwd").to_string();
        }
        let empty = Vec::new();
        let parts = d.get("message").and_then(|m| m.get("parts")).and_then(Value::as_array).unwrap_or(&empty);
        for p in parts {
            if let Some(fc) = p.get("functionCall").filter(|v| truthy(Some(v))) {
                note_tool(&mut s, str_of(fc, "name"), fc.get("args"));
            }
        }
        let text = parts.iter().filter(|p| p.is_object() && !truthy(p.get("thought")))
            .map(|p| str_of(p, "text")).collect::<Vec<_>>().join("\n");
        let ts = str_of(&d, "timestamp").to_string();
        add(&mut s, t, &text, &ts);
    }
    Ok(s)
}

fn parse_codex(path: &Path, mut s: Session) -> std::io::Result<Session> {
    for d in lines(path, &mut s)? {
        tokens::codex_line(&d, &mut s.state.tok);
        let null = Value::Null;
        let p = d.get("payload").filter(|v| truthy(Some(v))).unwrap_or(&null);
        if str_of(&d, "type") == "session_meta" {
            if let Some(cwd) = p.get("cwd") {
                s.project = cwd.as_str().unwrap_or("").to_string();
            }
        }
        let role = str_of(p, "role");
        if str_of(p, "type") != "message" || !(role == "user" || role == "assistant") {
            continue;
        }
        let empty = Vec::new();
        let text = p.get("content").and_then(Value::as_array).unwrap_or(&empty).iter()
            .filter(|b| b.is_object()).map(|b| str_of(b, "text")).collect::<Vec<_>>().join("\n");
        let (role, ts) = (role.to_string(), str_of(&d, "timestamp").to_string());
        add(&mut s, &role, &text, &ts);
    }
    Ok(s)
}

static PROJECTS: Mutex<Option<HashMap<PathBuf, String>>> = Mutex::new(None);

/// The working folder of a Claude Code project folder, from the cwd in its newest transcripts, else from its name.
fn project_of(folder: &Path) -> String {
    if let Some(p) = PROJECTS.lock().ok().and_then(|g| g.as_ref().and_then(|m| m.get(folder).cloned())) {
        return p;
    }
    let mut files: Vec<(std::time::SystemTime, PathBuf)> = std::fs::read_dir(folder).into_iter().flatten().flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "jsonl"))
        .filter_map(|p| Some((p.metadata().ok()?.modified().ok()?, p)))
        .collect();
    files.sort_by(|a, b| b.0.cmp(&a.0));
    let mut cwd = String::new();
    'files: for (_, f) in files.iter().take(3) {
        for d in all_lines(f) {
            cwd = str_of(&d, "cwd").to_string();
            if !cwd.is_empty() {
                break 'files;
            }
        }
    }
    if cwd.is_empty() {
        cwd = decode(&folder.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default());
    }
    if let Ok(mut g) = PROJECTS.lock() {
        g.get_or_insert_with(HashMap::new).insert(folder.to_path_buf(), cwd.clone());
    }
    cwd
}

/// A project folder name back to a path that exists, each "-" read as "/", "-" or "." (longest folder first);
/// "" when no folder on disk fits.
pub fn decode(name: &str) -> String {
    let parts: Vec<&str> = name.trim_start_matches('-').split('-').collect();
    let mut cur = PathBuf::from("/");
    let mut i = 0;
    while i < parts.len() {
        let mut found = None;
        for j in (i + 1..=parts.len()).rev() {
            let dash = parts[i..j].join("-");
            let dot = parts[i..j].join(".");
            let mut hit = [dash.clone(), dot].into_iter().map(|n| cur.join(n)).find(|p| p.is_dir());
            if hit.is_none() && parts[i].is_empty() && j > i + 1 {
                let h = cur.join(format!(".{}", parts[i + 1..j].join("-")));
                hit = h.is_dir().then_some(h);
            }
            if let Some(h) = hit {
                found = Some((h, j));
                break;
            }
        }
        match found {
            Some((h, j)) => {
                cur = h;
                i = j;
            }
            None => return String::new(),
        }
    }
    cur.to_string_lossy().into_owned()
}

/// Python's `datetime.isoformat()` of a UTC time.
pub fn iso_utc(secs: f64) -> String {
    let whole = secs.floor() as i64;
    let micros = ((secs - whole as f64) * 1e6).round() as u32;
    let (whole, micros) = if micros >= 1_000_000 { (whole + 1, 0) } else { (whole, micros) };
    let t = chrono::DateTime::from_timestamp(whole, 0).unwrap_or_default();
    let base = t.format("%Y-%m-%dT%H:%M:%S").to_string();
    if micros > 0 { format!("{base}.{micros:06}+00:00") } else { format!("{base}+00:00") }
}

fn mtime(path: &Path) -> f64 {
    path.metadata().and_then(|m| m.modified()).ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

/// One Claude Code memory file (a fact, decision or preference that outlives its session) as a one-turn session.
pub fn parse_memory(path: &Path) -> std::io::Result<Session> {
    let text = String::from_utf8_lossy(&std::fs::read(path)?).into_owned();
    let mut meta: HashMap<&str, String> = HashMap::new();
    let mut body = text.as_str();
    if let Some(rest) = text.strip_prefix("---\n") {
        if let Some((head, after)) = rest.split_once("\n---\n") {
            body = after;
            for ln in head.lines() {
                let ln = ln.trim();
                let (k, v) = ln.split_once(':').unwrap_or((ln, ""));
                let v = v.trim();
                if ["name", "description", "modified"].contains(&k) && !v.is_empty() {
                    let v = v.trim_matches(|c| c == '"' || c == '\'');
                    meta.insert(if k == "name" { "name" } else if k == "description" { "description" } else { "modified" }, v.to_string());
                }
            }
        }
    }
    let ts = meta.get("modified").cloned().unwrap_or_else(|| iso_utc(mtime(path)));
    let folder = path.parent().and_then(Path::parent).unwrap_or(Path::new("/"));
    let stem = path.file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
    let fname = folder.file_name().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
    let label = meta.get("description").or(meta.get("name")).cloned().unwrap_or_else(|| stem.clone());
    let title: String = format!("Memory: {label}").chars().take(120).collect();
    let name = meta.get("name").cloned().unwrap_or_else(|| stem.clone());
    let desc = meta.get("description").cloned().unwrap_or_default();
    Ok(Session {
        id: format!("claude:memory/{fname}/{stem}"),
        source: "claude".into(),
        path: path.to_string_lossy().into(),
        project: project_of(folder),
        title,
        started: ts.clone(),
        updated: ts,
        turns: vec![("memory".into(), format!("{name}: {desc}\n\n{}", body.trim()))],
        turn_ts: vec![String::new()],
        ..Default::default()
    })
}

/// A transcript as a Session.
pub fn parse(source: &str, path: &Path) -> std::io::Result<Session> {
    parse_from(source, path, None)
}

/// A transcript as a Session. With `resume` (this path's Session from an earlier parse), only the lines written
/// after it are read. A transcript shorter than what `resume` read was written again, and is read whole.
pub fn parse_from(source: &str, path: &Path, resume: Option<Session>) -> std::io::Result<Session> {
    let mut s = if path.extension().is_some_and(|x| x == "md") {
        parse_memory(path)?
    } else {
        let size = path.metadata()?.len();
        let base = resume.filter(|r| r.path == path.to_string_lossy() && size >= r.offset)
            .unwrap_or_else(|| new_session(source, path));
        match source {
            "claude" => parse_claude(path, base)?,
            "qwen" => parse_qwen(path, base)?,
            "codex" => parse_codex(path, base)?,
            _ => return Err(std::io::Error::new(std::io::ErrorKind::InvalidInput, format!("unknown agent {source}"))),
        }
    };
    if s.title.is_empty() || s.state.auto_title.as_deref() == Some(s.title.as_str()) {
        let first = s.turns.iter().find(|(r, _)| r == "user").map(|(_, t)| t.as_str()).unwrap_or("");
        let joined = first.split_whitespace().collect::<Vec<_>>().join(" ");
        let mut title: String = joined.chars().take(80).collect();
        if title.is_empty() {
            title = path.file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
        }
        s.state.auto_title = Some(title.clone());
        s.title = title;
    }
    Ok(s)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cleans_reminders() {
        assert_eq!(clean("a <system-reminder>x\ny</system-reminder> b"), "a  b");
        assert_eq!(clean("<command-name>/x</command-name>hi"), "hi");
        assert_eq!(clean("<command-name>open"), "<command-name>open");
    }

    #[test]
    fn reads_a_claude_transcript_and_resumes() {
        let dir = std::env::temp_dir().join(format!("parsers-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("abc.jsonl");
        let l1 = r#"{"type":"user","cwd":"/work/app","timestamp":"2026-01-01T00:00:00Z","message":{"content":"Fix the   login bug"}}"#;
        let l2 = r#"{"type":"assistant","timestamp":"2026-01-01T00:01:00Z","message":{"id":"m1","model":"claude-opus-5","usage":{"input_tokens":10,"output_tokens":5},"content":[{"type":"text","text":"Done"},{"type":"tool_use","name":"Edit","input":{"file_path":"/work/app/a.py"}}]}}"#;
        std::fs::write(&p, format!("{l1}\n{l2}\n{{\"type\":\"ai-title\"")).unwrap();
        let s = parse("claude", &p).unwrap();
        assert_eq!(s.id, "claude:abc");
        assert_eq!(s.title, "Fix the login bug");
        assert_eq!(s.project, "/work/app");
        assert_eq!(s.turns.len(), 2);
        assert_eq!(s.touched.get("/work/app/a.py"), Some(&true));
        assert_eq!(tokens::rows(&s.state.tok).len(), 1);
        std::fs::write(&p, format!("{l1}\n{l2}\n{{\"type\":\"ai-title\",\"aiTitle\":\"Login fix\"}}\n")).unwrap();
        let s = parse_from("claude", &p, Some(s)).unwrap();
        assert_eq!(s.title, "Login fix");
        assert_eq!(s.turns.len(), 2);
        let _ = std::fs::remove_dir_all(dir);
    }
}
