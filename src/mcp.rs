//! The MCP server, at /mcp on the daemon (streamable HTTP, JSON answers) and through `pensieve mcp` (stdio,
//! bridged to the daemon). It serves the five search tools of Pensieve 0.21, with 0.21's descriptions and schemas.

use std::sync::{Arc, LazyLock};

use regex::Regex;
use serde_json::{Map, Value, json};

use index::thoughts::Link;

use crate::engine::Engine;
use crate::usage;

const PROTOCOL: &str = "2025-06-18";

pub const INSTRUCTIONS: &str = "Pensieve indexes the user's repos, documents and past agent sessions on this Mac. To find \
code, call search with what the code does plus the names you would guess it uses, before Grep or find. The first hit is \
usually right; open it at its line. Use grep to list every use of a name you already know. When the user pastes a \
pensieve://thought/ or pensieve://session/ link, call recall with the link, then continue that work.";

static TOOLS: LazyLock<Vec<Value>> = LazyLock::new(|| serde_json::from_str(include_str!("../static/mcp-tools.json")).unwrap_or_default());

/// The connection's query: `?scope=`, `?cwd=`, `?client=`, `?conn=`.
#[derive(Default, Clone)]
pub struct Conn {
    pub scope: String,
    pub cwd: String,
    pub client: String,
    pub id: String,
}

/// One JSON-RPC message. None for a notification, which gets no answer.
pub fn handle(msg: &Value, conn: &Conn, engine: Option<&Arc<Engine>>) -> Option<Value> {
    let id = msg.get("id")?.clone();
    let p = &msg["params"];
    let result = match msg["method"].as_str().unwrap_or("") {
        "initialize" => Ok(json!({"protocolVersion": p["protocolVersion"].as_str().unwrap_or(PROTOCOL),
            "capabilities": {"tools": {"listChanged": false}},
            "serverInfo": {"name": "pensieve", "version": env!("CARGO_PKG_VERSION")}, "instructions": INSTRUCTIONS})),
        "ping" => Ok(json!({})),
        "tools/list" => Ok(json!({"tools": *TOOLS})),
        "tools/call" => {
            let name = p["name"].as_str().unwrap_or("");
            let args = p["arguments"].as_object().cloned().unwrap_or_default();
            let t0 = std::time::Instant::now();
            usage::take_note();
            let out = match engine {
                Some(e) => call(name, &args, conn, e),
                None => Err("Pensieve is opening its index; try again in a few seconds".into()),
            };
            let (n_results, results) = usage::take_note().unzip();
            usage::record(usage::Call {
                n_results, results,
                source: "mcp", tool: name.to_string(), client: conn.client.clone(), conn: conn.id.clone(), cwd: conn.cwd.clone(),
                scope: conn.scope.clone(), args: Value::Object(args.clone()), ms: t0.elapsed().as_secs_f64() * 1000.0,
                ok: out.is_ok(), error: out.as_ref().err().cloned(),
                out_chars: out.as_ref().map(|v| v.as_str().map_or_else(|| v.to_string().len(), str::len)).unwrap_or(0),
            });
            Ok(match out {
                Ok(Value::String(s)) => json!({"content": [{"type": "text", "text": s}], "isError": false}),
                Ok(v) => json!({"content": [{"type": "text", "text": v.to_string()}], "structuredContent": v, "isError": false}),
                Err(e) => json!({"content": [{"type": "text", "text": e}], "isError": true}),
            })
        }
        m => Err(json!({"code": -32601, "message": format!("method not found: {m}")})),
    };
    Some(match result {
        Ok(r) => json!({"jsonrpc": "2.0", "id": id, "result": r}),
        Err(e) => json!({"jsonrpc": "2.0", "id": id, "error": e}),
    })
}

static HIT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(\S.*?)  .*\[([a-z]+:[^\]]+)\]$").expect("regex"));

/// [id, where] of each hit line of the search tool's answer, for the usage log.
fn hits(text: &str) -> Vec<Value> {
    text.lines().filter_map(|l| HIT.captures(l)).map(|c| {
        let path = Some(c[1].split(':').next().unwrap_or("")).filter(|p| !p.starts_with('"') && !p.starts_with("session \""));
        json!([c[2].to_string(), path])
    }).collect()
}

fn s(a: &Map<String, Value>, k: &str) -> String {
    a.get(k).and_then(Value::as_str).unwrap_or("").to_string()
}

fn n(a: &Map<String, Value>, k: &str, d: i64) -> i64 {
    a.get(k).and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64))).unwrap_or(d)
}

fn call(name: &str, a: &Map<String, Value>, conn: &Conn, e: &Arc<Engine>) -> Result<Value, String> {
    let ix = &e.ix;
    let cwd = if conn.cwd.is_empty() { None } else { Some(conn.cwd.as_str()) };
    let sc = e.scope(&conn.scope, &conn.cwd).map_err(|err| format!("unknown Pensieve scope: {err}"))?;
    let sc = sc.as_ref();
    match name {
        "search" => {
            let kind = a.get("kind").and_then(Value::as_str);
            let out = ix.mcp_search(&s(a, "query"), n(a, "limit", 8).clamp(1, 50) as usize, kind, sc, cwd)?;
            let found = hits(&out);
            usage::note(found.len() as i64, Value::Array(found));
            Ok(Value::String(out))
        }
        "read" => ix.mcp_read(&s(a, "id"), n(a, "max_chars", 12000).max(200) as usize, sc),
        "similar" => ix.mcp_similar(&s(a, "id"), n(a, "limit", 8).clamp(1, 50) as usize, sc, cwd).map(Value::String),
        "thoughts" => {
            let query = s(a, "query");
            let query = match index::thoughts::link(&query) {
                Some(Link::Session(sid)) => {
                    let card = ix.session_card(&sid).ok_or_else(|| format!("no thought holds session {sid}"))?;
                    return Ok(Value::String(json!({"thoughts": [card], "total": 1, "query": query}).to_string()));
                }
                Some(Link::Thought(id)) => id,
                None => query.trim().to_string(),
            };
            let query = query.as_str();
            if query.starts_with("th_") && !query.contains(char::is_whitespace) {
                let card = ix.thought_card(query, None).ok_or_else(|| format!("no thought {query}"))?;
                return Ok(Value::String(json!({"thoughts": [card], "total": 1, "query": query}).to_string()));
            }
            Ok(Value::String(ix.thought_cards(query, n(a, "limit", 8).clamp(1, 30) as usize).to_string()))
        }
        "name_thought" => {
            let (id, name) = (s(a, "id"), s(a, "name"));
            let t = ix.thought_rename(&id, &name)?;
            Ok(Value::String(format!("Thought {id} now has the name \"{}\".", t["name"].as_str().unwrap_or(""))))
        }
        "recall" => {
            let what = s(a, "thought");
            let what = match index::thoughts::link(&what) {
                Some(Link::Session(sid)) => {
                    return ix.session_brief(&sid).map(Value::String).ok_or_else(|| format!("Pensieve has no session {sid}"));
                }
                Some(Link::Thought(id)) => id,
                None => what.trim().to_string(),
            };
            let id = if what.starts_with("th_") {
                what
            } else {
                ix.thoughts_list(&what, 1)["thoughts"][0]["id"].as_str().map(String::from)
                    .ok_or_else(|| format!("no thought matches \"{what}\""))?
            };
            ix.thought_brief(&id).map(Value::String).ok_or_else(|| format!("no thought {id}"))
        }
        "list_repos" => Ok(ix.mcp_list_repos(&|root| index::scopes::repo_allowed(sc, root))),
        "repo" => {
            let part = a.get("part").and_then(Value::as_str).unwrap_or("overview");
            let repo = s(a, "repo");
            let root = if repo.is_empty() {
                match sc.and_then(|s| s.repos.as_ref()).filter(|r| r.len() == 1) {
                    Some(r) => r.iter().next().cloned().unwrap_or_default(),
                    None => return Err("name the repo: list_repos lists them".into()),
                }
            } else {
                let root = ix.repo_root(&repo).ok_or_else(|| format!("unknown repo '{repo}'"))?;
                if !index::scopes::repo_allowed(sc, &root) {
                    return Err(format!("repo '{repo}' is outside this connection's scope ({})", sc.map_or("all", |s| s.name.as_str())));
                }
                root
            };
            ix.mcp_repo(&root, part, n(a, "inactive_days", 180), None)
        }
        _ => Err(format!("unknown tool {name}")),
    }
}

/// `pensieve mcp`: read JSON-RPC lines on stdin, send each to the daemon's /mcp, and write its answers.
pub fn stdio(port: u16, scope: &str) {
    use std::io::{BufRead, Write};
    let cwd = std::env::current_dir().map(|p| p.to_string_lossy().to_string()).unwrap_or_default();
    let conn = format!("{:x}", (store::now() * 1e6) as u64 & 0xffff_ffff_ffff);
    let mut client = String::new();
    let stdout = std::io::stdout();
    for line in std::io::stdin().lock().lines().map_while(Result::ok) {
        if line.trim().is_empty() {
            continue;
        }
        let msg = serde_json::from_str::<Value>(&line).ok();
        let id = msg.as_ref().and_then(|v| v.get("id").cloned());
        if client.is_empty() {
            client = msg.as_ref().and_then(|m| m["params"]["clientInfo"]["name"].as_str()).unwrap_or("").to_string();
        }
        let url = format!("/mcp?scope={}&cwd={}&conn={conn}&client={}", crate::http::encode(scope),
                          crate::http::encode(&cwd), crate::http::encode(&client));
        let answer = crate::http::local(port, "POST", &url, Some(&line), std::time::Duration::from_secs(120));
        let out = match answer {
            Ok(r) if r.status == 202 || r.body.trim().is_empty() => continue,
            Ok(r) => r.body,
            Err(e) => match id {
                Some(id) => json!({"jsonrpc": "2.0", "id": id, "error": {"code": -32000,
                    "message": format!("Pensieve is not running on port {port}: {e}")}}).to_string(),
                None => continue,
            },
        };
        let mut o = stdout.lock();
        let _ = writeln!(o, "{}", out.trim()).and_then(|_| o.flush());
    }
}
