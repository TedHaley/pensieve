//! A minimal HTTP/1.1 client for the daemon on 127.0.0.1. It opens one socket per request and starts no process.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

pub struct Response {
    pub status: u16,
    pub body: String,
}

/// One request to `path` on 127.0.0.1:`port`, with a JSON body when `body` is Some.
pub fn local(port: u16, method: &str, path: &str, body: Option<&str>, timeout: Duration) -> Result<Response, String> {
    let mut s = TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_secs(2)).map_err(|e| e.to_string())?;
    s.set_read_timeout(Some(timeout)).map_err(|e| e.to_string())?;
    let body = body.unwrap_or("");
    let req = format!("{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n\
                       Content-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
    s.write_all(req.as_bytes()).map_err(|e| e.to_string())?;
    let mut raw = Vec::new();
    s.read_to_end(&mut raw).map_err(|e| e.to_string())?;
    let raw = String::from_utf8_lossy(&raw);
    let (head, body) = raw.split_once("\r\n\r\n").ok_or("no HTTP answer")?;
    let status = head.split_whitespace().nth(1).and_then(|c| c.parse().ok()).ok_or("no HTTP status")?;
    Ok(Response { status, body: body.to_string() })
}

pub fn encode(s: &str) -> String {
    s.bytes().map(|b| match b {
        b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' | b'/' => (b as char).to_string(),
        _ => format!("%{b:02X}"),
    }).collect()
}
