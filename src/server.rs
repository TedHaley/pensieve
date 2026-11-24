//! The daemon: a local HTTP server. POST /mcp is the MCP server, GET /status is for `pensieve status`, and the
//! /thoughts and /thought routes are for the panel (mac/). It answers only requests from this Mac. The index opens on a thread of its own, so the server answers from the first second.

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, OnceLock};

use serde_json::{Value, json};

use crate::engine::Engine;
use crate::mcp;

struct Request {
    method: String,
    path: String,
    query: HashMap<String, String>,
    body: Vec<u8>,
}

fn decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        match b[i] {
            b'+' => out.push(b' '),
            b'%' if i + 2 < b.len() => match std::str::from_utf8(&b[i + 1..i + 3]).ok().and_then(|h| u8::from_str_radix(h, 16).ok()) {
                Some(v) => {
                    out.push(v);
                    i += 2;
                }
                None => out.push(b'%'),
            },
            c => out.push(c),
        }
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Names under `.localhost` always resolve to this Mac.
fn local_host(h: &str) -> bool {
    let h = h.trim_start_matches('[');
    let name = h.split(']').next().unwrap_or(h);
    let name = if name.matches(':').count() == 1 { name.split(':').next().unwrap_or("") } else { name };
    ["127.0.0.1", "localhost", "::1"].contains(&name) || name.ends_with(".localhost")
}

/// Only this Mac's own programs: a page in a browser can reach 127.0.0.1 through DNS rebinding (the Host header
/// shows it) or with a cross-site request (the Origin header shows it).
fn read_request(stream: &TcpStream) -> Option<Result<Request, (u16, String)>> {
    let mut r = BufReader::new(stream);
    let mut line = String::new();
    r.read_line(&mut line).ok()?;
    let mut parts = line.split_whitespace();
    let (method, target) = (parts.next()?.to_string(), parts.next()?.to_string());
    let (mut len, mut host, mut origin) = (0usize, String::new(), None::<String>);
    loop {
        let mut h = String::new();
        if r.read_line(&mut h).ok()? == 0 || h.trim().is_empty() {
            break;
        }
        let (k, v) = h.split_once(':')?;
        match k.trim().to_ascii_lowercase().as_str() {
            "content-length" => len = v.trim().parse().unwrap_or(0),
            "host" => host = v.trim().to_string(),
            "origin" => origin = Some(v.trim().to_string()),
            _ => {}
        }
    }
    let origin_ok = origin.as_deref().is_none_or(|o| o.split_once("://").is_some_and(|(_, h)| local_host(h.split('/').next().unwrap_or(""))));
    if !local_host(&host) || !origin_ok {
        return Some(Err((403, "Pensieve only answers requests from this Mac".into())));
    }
    let mut body = vec![0; len.min(8 << 20)];
    r.read_exact(&mut body).ok()?;
    let (path, qs) = target.split_once('?').unwrap_or((&target, ""));
    let query = qs.split('&').filter(|p| !p.is_empty())
        .map(|p| { let (k, v) = p.split_once('=').unwrap_or((p, "")); (decode(k), decode(v)) }).collect();
    Some(Ok(Request { method, path: decode(path), query, body }))
}

fn write_reply(mut s: TcpStream, code: u16, body: Option<Value>) {
    let body = body.map(|v| serde_json::to_vec(&v).unwrap_or_default()).unwrap_or_default();
    let reason = match code { 200 => "OK", 202 => "Accepted", 400 => "Bad Request", 403 => "Forbidden", 404 => "Not Found", _ => "Error" };
    let head = format!("HTTP/1.1 {code} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n", body.len());
    let _ = s.write_all(head.as_bytes()).and_then(|_| s.write_all(&body));
}

fn route(req: &Request, engine: Option<&Arc<Engine>>) -> (u16, Option<Value>) {
    match (req.method.as_str(), req.path.as_str()) {
        ("POST", "/mcp") => {
            let q = |k: &str| req.query.get(k).cloned().unwrap_or_default();
            let conn = mcp::Conn { scope: q("scope"), cwd: q("cwd"), client: q("client"), id: q("conn") };
            let Ok(msg) = serde_json::from_slice::<Value>(&req.body) else {
                return (400, Some(json!({"jsonrpc": "2.0", "id": null, "error": {"code": -32700, "message": "parse error"}})));
            };
            let answers: Vec<Value> = match &msg {
                Value::Array(batch) => batch.iter().filter_map(|m| mcp::handle(m, &conn, engine)).collect(),
                m => mcp::handle(m, &conn, engine).into_iter().collect(),
            };
            match (msg.is_array(), answers.len()) {
                (_, 0) => (202, None),
                (false, _) => (200, answers.into_iter().next()),
                (true, _) => (200, Some(Value::Array(answers))),
            }
        }
        ("GET", "/status") => (200, Some(match engine {
            Some(e) => e.status(),
            None => json!({"opening": true}),
        })),
        ("GET", "/settings") => (200, Some(Value::Object(crate::settings::load()))),
        ("POST", "/settings") => match (engine, serde_json::from_slice::<Value>(&req.body).ok().and_then(|v| v.as_object().cloned())) {
            (Some(e), Some(changes)) => match e.change_settings(&changes) {
                Ok(v) => (200, Some(v)),
                Err(m) => (400, Some(json!({"detail": m}))),
            },
            (None, _) => (400, Some(json!({"detail": "Pensieve opens its index. Try again in a moment."}))),
            (_, None) => (400, Some(json!({"detail": "the body must be a JSON object of settings"}))),
        },
        _ => (404, Some(json!({"detail": "not found"}))),
    }
}

pub fn run(port: u16) -> std::io::Result<()> {
    let listener = TcpListener::bind(("127.0.0.1", port))?;
    let engine: Arc<OnceLock<Arc<Engine>>> = Arc::new(OnceLock::new());
    let slot = engine.clone();
    if let Err(e) = crate::media::ensure() {
        eprintln!("no video and sound decoder: {e}");
    }
    std::thread::Builder::new().name("open-index".into()).spawn(move || match Engine::open() {
        Ok(e) => {
            e.start();
            let _ = slot.set(e);
        }
        Err(err) => eprintln!("cannot open the index: {err}"),
    })?;
    eprintln!("pensieve {} on 127.0.0.1:{port}", env!("CARGO_PKG_VERSION"));
    for stream in listener.incoming().flatten() {
        let engine = engine.clone();
        let _ = std::thread::Builder::new().name("request".into()).spawn(move || {
            match read_request(&stream) {
                Some(Ok(req)) => {
                    let (code, body) = route(&req, engine.get());
                    write_reply(stream, code, body);
                }
                Some(Err((code, msg))) => write_reply(stream, code, Some(json!({"detail": msg}))),
                None => {}
            }
        });
    }
    Ok(())
}
