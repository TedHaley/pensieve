//! Pensieve: a background index of the user's code, documents and agent sessions, searched by agents through
//! MCP. It has no interface. See GOALS.md.

mod engine;
mod http;
mod install;
mod mcp;
mod media;
mod open;
mod server;
mod settings;
mod usage;
mod watch;

use serde_json::{Value, json};

const USAGE: &str = "usage: pensieve <command> [options]

    install              start the daemon at login, add the MCP server to Claude Code and Codex, add the skill
    status               what the daemon indexes now
    uninstall [--purge]  remove what install added; --purge also removes the index
    search <query>       search the index, as an agent does [--kind code|doc|session] [--limit 8]
    thoughts [query]     your work across sessions, as a numbered list, with its pull requests, tickets and files
                         [--limit 8] [--json]
    watch [query]        the newest thoughts, refreshed every 15 s; click one to resume it in a new tab, or click
                         [fork] to start a new session that reads it
    open <thought>       continue a thought in a new tab of this terminal's app: it resumes the newest session
                         [--new starts a new session that reads the thought] [--print only prints the command]
    recall <thought>     the full context of one thought
    name <thought> <name>  give a thought a name; an empty name gives the automatic name back
                         <thought> is a number of the last list, an id (th_...), a pensieve:// link, or words
                         that describe the work
                         (`name` takes a number or an id)
    serve               run the daemon in this terminal (launchd runs it after install)
    mcp                  MCP over stdio, bridged to the daemon (agents run this)

Options: --port N (or PENSIEVE_PORT, default 8790), --data DIR (or PENSIEVE_DATA).";

/// The options that take a value, so that their values are not taken for items.
const WITH_VALUE: [&str; 5] = ["--port", "--data", "--scope", "--kind", "--limit"];

/// Call an MCP tool of the daemon, as an agent does. Ok is the text of its answer, and Err is the text of its error.
fn tool(port: u16, name: &str, args: Value) -> Result<String, String> {
    let cwd = std::env::current_dir().map(|p| p.to_string_lossy().to_string()).unwrap_or_default();
    let url = format!("/mcp?scope=auto&cwd={}&client=cli", http::encode(&cwd));
    let body = json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}).to_string();
    let r = http::local(port, "POST", &url, Some(&body), std::time::Duration::from_secs(120))
        .map_err(|e| format!("Pensieve is not running on port {port}: {e}"))?;
    let v: Value = serde_json::from_str(&r.body).unwrap_or(Value::Null);
    let res = &v["result"];
    let text = res["content"].as_array().into_iter().flatten().filter_map(|c| c["text"].as_str()).collect::<Vec<_>>().join("\n");
    if res["isError"] == true || res.is_null() { Err(text) } else { Ok(text) }
}

fn show(answer: Result<String, String>) -> bool {
    match answer {
        Ok(text) => {
            println!("{text}");
            true
        }
        Err(e) => {
            eprintln!("{e}");
            false
        }
    }
}

fn search(port: u16, query: &str, kind: Option<&str>, limit: u64) -> bool {
    let mut args = json!({"query": query, "limit": limit});
    if let Some(k) = kind {
        args["kind"] = json!(k);
    }
    show(tool(port, "search", args))
}

/// The cards of the `thoughts` tool as text for a terminal.
fn cards(answer: &str) -> String {
    let v: Value = serde_json::from_str(answer).unwrap_or(Value::Null);
    let text = |c: &Value, k: &str| c[k].as_str().filter(|s| !s.is_empty()).map(|s| {
        if k != "resume" && s.chars().count() > 150 { format!("{}…", s.chars().take(150).collect::<String>()) } else { s.to_string() }
    });
    let mut out: Vec<String> = Vec::new();
    for c in v["thoughts"].as_array().into_iter().flatten() {
        let day = |k: &str| c[k].as_str().map(|s| s.chars().take(10).collect::<String>()).unwrap_or_default();
        let sessions = c["sessions"].as_i64().unwrap_or(0);
        let mut head = vec![text(c, "name").unwrap_or_default()];
        head.extend(text(c, "project"));
        head.push(format!("{sessions} session{}", if sessions == 1 { "" } else { "s" }));
        head.push(if day("started") == day("updated") { day("updated") } else { format!("{} to {}", day("started"), day("updated")) });
        let mut lines = vec![format!("{:>2}. {}  [{}]", out.len() + 1, head.join(" · "), c["id"].as_str().unwrap_or(""))];
        let mut row = |label: &str, value: Option<String>| {
            if let Some(v) = value.filter(|v| !v.is_empty()) {
                lines.push(format!("    {label:<10}{v}"));
            }
        };
        row("matched", text(c, "matched"));
        row("asked", text(c, "asked"));
        row("left off", text(c, "left_off").filter(|l| Some(l) != text(c, "asked").as_ref()));
        row("ended", text(c, "ended_with"));
        let list = |k: &str, f: &dyn Fn(&Value) -> Option<String>| {
            Some(c[k].as_array().into_iter().flatten().filter_map(f).take(4).collect::<Vec<_>>().join(", "))
        };
        row("PRs", list("pull_requests", &|p| Some(format!("{}#{}", p["repo"].as_str()?, p["number"]))));
        row("tickets", list("tickets", &|t| t["key"].as_str().map(String::from)));
        row("files", list("files", &|f| f.as_str().map(String::from)));
        row("link", text(c, "link"));
        row("session", text(c, "session_link"));
        out.push(lines.join("\n"));
    }
    if out.is_empty() {
        return "No thought matches.".into();
    }
    format!("{}\n\n`pensieve open <number>` continues a thought in a new tab. `pensieve recall <number>` prints its context.\n\
             Paste a link into a new agent session to continue that thought, or that session, there.",
            out.join("\n\n"))
}

/// The file with the ids of the thoughts that `thoughts` printed last, so that `open 2` is the second of them.
fn listed_file() -> std::path::PathBuf {
    store::data_dir().join("listed-thoughts.json")
}

fn keep_listed(answer: &str) {
    let v: Value = serde_json::from_str(answer).unwrap_or(Value::Null);
    let ids: Vec<&str> = v["thoughts"].as_array().into_iter().flatten().filter_map(|c| c["id"].as_str()).collect();
    let _ = std::fs::write(listed_file(), json!(ids).to_string());
}

/// The id of the thought that `what` names: a number of the last list, an id, or words that describe the work.
fn pick(port: u16, what: &str) -> Result<String, String> {
    let what = what.trim();
    match index::thoughts::link(what) {
        Some(index::thoughts::Link::Thought(id)) => return Ok(id),
        // the thoughts tool reads session links itself
        Some(index::thoughts::Link::Session(_)) => return Ok(what.to_string()),
        None => {}
    }
    if what.starts_with("th_") {
        return Ok(what.to_string());
    }
    if let Ok(n) = what.parse::<usize>() {
        let ids: Vec<String> = std::fs::read_to_string(listed_file()).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default();
        return ids.get(n.wrapping_sub(1)).cloned().ok_or_else(|| format!("the last list of `pensieve thoughts` has no thought {n}"));
    }
    if what.is_empty() {
        return Err("give a number of the last list, a thought id (th_...), or words that describe the work".into());
    }
    let answer = tool(port, "thoughts", json!({"query": what, "limit": 1}))?;
    let v: Value = serde_json::from_str(&answer).unwrap_or(Value::Null);
    v["thoughts"][0]["id"].as_str().map(String::from).ok_or_else(|| format!("no thought matches \"{what}\""))
}

/// Continue a thought in a new tab. Without `new`, the tab resumes the newest session, which holds the most
/// context. A thought with no session to resume starts a new session that reads the thought.
fn open_thought(port: u16, what: &str, new: bool, print: bool) -> bool {
    let card = pick(port, what).and_then(|id| tool(port, "thoughts", json!({"query": id})))
        .map(|a| serde_json::from_str::<Value>(&a).unwrap_or(Value::Null)["thoughts"][0].clone());
    let card = match card {
        Ok(c) if c.is_object() => c,
        Ok(_) => {
            eprintln!("no such thought");
            return false;
        }
        Err(e) => {
            eprintln!("{e}");
            return false;
        }
    };
    let name = card["name"].as_str().unwrap_or("");
    let command = if new { card["start"].as_str() } else { card["resume"].as_str().or(card["start"].as_str()) };
    let Some(command) = command else {
        eprintln!("Pensieve has no agent command for \"{name}\": its sessions are not from Claude Code or Codex.");
        return false;
    };
    if print {
        println!("{command}");
        return true;
    }
    match open::tab(command, &name.chars().take(40).collect::<String>()) {
        Ok(app) => {
            println!("Opened \"{name}\" in a new {app} tab.");
            true
        }
        Err(e) => {
            eprintln!("No tab opened: {e}. This command continues \"{name}\":");
            println!("{command}");
            false
        }
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let opt = |name: &str| args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned();
    let flag = |name: &str| args.iter().any(|a| a == name);
    let mut items: Vec<String> = Vec::new();
    let mut i = 0;
    while i < args.len() {
        if WITH_VALUE.contains(&args[i].as_str()) {
            i += 2;
            continue;
        }
        if !args[i].starts_with("--") {
            items.push(args[i].clone());
        }
        i += 1;
    }
    if let Some(d) = opt("--data") {
        // SAFETY: set once at start, before any thread runs
        unsafe { std::env::set_var("PENSIEVE_DATA", d) };
    }
    let port: u16 = opt("--port").or_else(|| std::env::var("PENSIEVE_PORT").ok()).and_then(|v| v.parse().ok()).unwrap_or(8790);
    let rest = items.get(1..).unwrap_or_default();
    if matches!(items.first().map(String::as_str), Some("status" | "search" | "thoughts" | "recall")) {
        // A command that only prints stops without a message when its reader closes the pipe (`| head`). The
        // commands that change something keep Rust's default, so that a closed pipe cannot stop them halfway.
        // SAFETY: set once at start, before any thread runs
        unsafe { libc::signal(libc::SIGPIPE, libc::SIG_DFL) };
    }
    let ok = match items.first().map(String::as_str) {
        Some("install") => install::install(port),
        Some("uninstall") => install::uninstall(flag("--purge")),
        Some("status") => install::status(port),
        Some("search") => search(port, &rest.join(" "), opt("--kind").as_deref(), opt("--limit").and_then(|v| v.parse().ok()).unwrap_or(8)),
        Some("thoughts") => {
            let limit: u64 = opt("--limit").and_then(|v| v.parse().ok()).unwrap_or(8);
            let answer = tool(port, "thoughts", json!({"query": rest.join(" "), "limit": limit}));
            if let Ok(a) = &answer {
                keep_listed(a);
            }
            show(if flag("--json") { answer } else { answer.map(|a| cards(&a)) })
        }
        Some("watch") => watch::run(port, &rest.join(" ")),
        Some("open") => open_thought(port, &rest.join(" "), flag("--new"), flag("--print")),
        Some("name") => show(pick(port, rest.first().map_or("", String::as_str))
            .and_then(|id| tool(port, "name_thought", json!({"id": id, "name": rest.get(1..).unwrap_or_default().join(" ")})))),
        Some("recall") => {
            let what = rest.join(" ");
            show(if what.trim().parse::<usize>().is_ok() { pick(port, &what) } else { Ok(what) }
                .and_then(|thought| tool(port, "recall", json!({"thought": thought}))))
        }
        Some("mcp") => {
            mcp::stdio(port, &opt("--scope").unwrap_or_else(|| "auto".into()));
            true
        }
        Some("serve") => match server::run(port) {
            Ok(()) => true,
            Err(e) => {
                eprintln!("{e}");
                false
            }
        },
        Some("help") | None => {
            println!("{USAGE}");
            true
        }
        _ => {
            eprintln!("{USAGE}");
            std::process::exit(2);
        }
    };
    if !ok {
        std::process::exit(1);
    }
}
