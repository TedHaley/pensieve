//! The plain text of a document, by its extension: text files as they are, PDF, the Office zip formats, mail, and
//! the formats that macOS converts (textutil for RTF, Word 97 and OpenDocument; Spotlight's importer for iWork).
//! Also who wrote a document, from its own metadata, Spotlight or the file's owner.

use std::io::Read;
use std::path::Path;
use std::process::Command;

use regex::Regex;

pub const TEXT_EXT: &[&str] = &[
    ".txt", ".md", ".markdown", ".rst", ".org", ".tex", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".conf", ".xml", ".html", ".htm", ".css", ".scss", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt",
    ".swift", ".go", ".rs", ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".cs", ".scala", ".sql", ".sh",
    ".zsh", ".bash", ".r", ".jl", ".lua", ".pl", ".ipynb", ".log", ".env.example", ".srt", ".vtt",
];
pub const TEXTUTIL_EXT: &[&str] = &[".rtf", ".doc", ".odt"];
pub const SPOTLIGHT_EXT: &[&str] = &[".pages", ".key", ".numbers"];

/// A path's extension as Python's `Path.suffix` gives it: leading dots of the name don't count, and `foo.` has `.`.
pub fn suffix(path: &Path) -> String {
    let name = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    let s = name.trim_start_matches('.');
    match s.rfind('.') {
        Some(i) if i > 0 => s[i..].to_string(),
        _ => String::new(),
    }
}

pub fn ext_lower(path: &Path) -> String {
    suffix(path).to_lowercase()
}

fn re(s: &str) -> Regex {
    Regex::new(s).unwrap_or_else(|_| Regex::new("$^").expect("empty regex"))
}

/// The text of XML parts of a zip: paragraphs and spreadsheet strings end lines, tags go. Entities stay as written,
/// as 0.21 left them.
fn xml_text(z: &mut zip::ZipArchive<std::fs::File>, names: &[String]) -> Option<String> {
    let ends = re(r"</w:p>|</a:p>|</si>");
    let tags = re(r"<[^>]+>");
    let mut out = Vec::new();
    for n in names {
        let mut b = Vec::new();
        z.by_name(n).ok()?.read_to_end(&mut b).ok()?;
        let x = String::from_utf8_lossy(&b);
        let x = ends.replace_all(&x, "\n");
        out.push(tags.replace_all(&x, "").into_owned());
    }
    Some(out.join("\n"))
}

fn zip_names(z: &zip::ZipArchive<std::fs::File>) -> Vec<String> {
    z.file_names().filter_map(|n| n.ok().map(|c| c.into_owned())).collect()
}

fn office(path: &Path, e: &str) -> Option<String> {
    let mut z = zip::ZipArchive::new(std::fs::File::open(path).ok()?).ok()?;
    match e {
        ".docx" => xml_text(&mut z, &["word/document.xml".to_string()]),
        ".pptx" => {
            let slide = re(r"^ppt/slides/slide\d+\.xml$");
            let num = re(r"\d+");
            let mut slides: Vec<String> = zip_names(&z).into_iter().filter(|n| slide.is_match(n)).collect();
            slides.sort_by_key(|n| num.find_iter(n).last().and_then(|m| m.as_str().parse::<u64>().ok()).unwrap_or(0));
            xml_text(&mut z, &slides)
        }
        _ => {
            if zip_names(&z).iter().any(|n| n == "xl/sharedStrings.xml") {
                xml_text(&mut z, &["xl/sharedStrings.xml".to_string()])
            } else {
                Some(String::new())
            }
        }
    }
}

/// The text of each page, pages joined by a blank line as 0.21 joined pypdf's. A page that the reader fails on
/// (it panics on some fonts) is left empty; when no page gives text, Spotlight's PDF importer reads the file.
fn pdf(path: &Path) -> Option<String> {
    let p = path.to_path_buf();
    let doc = std::panic::catch_unwind(move || pdf_extract::Document::load(&p).ok()).ok().flatten();
    let pages: Vec<String> = match doc {
        Some(doc) => {
            let n = doc.get_pages().len() as u32;
            (1..=n).map(|i| {
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    let mut s = String::new();
                    let mut out = pdf_extract::PlainTextOutput::new(&mut s);
                    pdf_extract::output_doc_page(&doc, &mut out, i).ok().map(|_| s)
                })).ok().flatten().unwrap_or_default()
            }).collect()
        }
        None => Vec::new(),
    };
    if pages.iter().all(|t| t.trim().is_empty()) {
        return Some(spotlight(path));
    }
    // pdf-extract puts a blank line between text runs where pypdf puts a line break; blank lines end chunks early,
    // so they would cut a page into many more chunks than 0.21 made
    let runs = re(r"\n[ \t]*(\n[ \t]*)+");
    let pages: Vec<String> = pages.iter().map(|t| runs.replace_all(t.trim_matches('\n'), "\n").into_owned()).collect();
    Some(pages.join("\n\n"))
}

/// The text Spotlight's importer reads from a file (iWork, and anything else it has an importer for).
fn spotlight(path: &Path) -> String {
    let Ok(o) = Command::new("mdimport").args(["-t", "-d3"]).arg(path).output() else { return String::new() };
    let all = format!("{}{}", String::from_utf8_lossy(&o.stdout), String::from_utf8_lossy(&o.stderr));
    let Some(m) = re(r#"kMDItemTextContent = "((?:[^"\\]|\\.)*)""#).captures(&all) else { return String::new() };
    let s = re(r"\\U([0-9a-fA-F]{4})").replace_all(&m[1], |c: &regex::Captures| {
        u32::from_str_radix(&c[1], 16).ok().and_then(char::from_u32).map(String::from).unwrap_or_default()
    });
    re(r"\\(.)").replace_all(&s, |c: &regex::Captures| match &c[1] {
        "n" => "\n".to_string(),
        "t" => "\t".to_string(),
        "r" => String::new(),
        other => other.to_string(),
    }).into_owned()
}

/// Plain text of a document, or "" when it has none that can be read.
pub fn extract(path: &Path, max_bytes: u64) -> String {
    let e = ext_lower(path);
    let e = e.as_str();
    let out = match e {
        ".pdf" => pdf(path),
        ".docx" | ".pptx" | ".xlsx" => office(path, e),
        _ if TEXTUTIL_EXT.contains(&e) => Command::new("textutil").args(["-convert", "txt", "-stdout"]).arg(path)
            .output().ok().map(|o| String::from_utf8_lossy(&o.stdout).into_owned()),
        ".eml" => std::fs::read(path).ok().map(|b| mail::text(&b)),
        _ if SPOTLIGHT_EXT.contains(&e) => Some(spotlight(path)),
        _ if TEXT_EXT.contains(&e) || e.is_empty() => {
            let mut b = Vec::new();
            std::fs::File::open(path).ok().and_then(|f| f.take(max_bytes).read_to_end(&mut b).ok()).map(|_| {
                if b[..b.len().min(4000)].contains(&0) { String::new() } else { String::from_utf8_lossy(&b).into_owned() }
            })
        }
        _ => None,
    };
    out.unwrap_or_default()
}

/// The owner's name, as `pwd.getpwuid(uid).pw_gecos` (its first field) or the login name.
fn owner(path: &Path) -> Option<String> {
    use std::os::unix::fs::MetadataExt;
    let uid = std::fs::metadata(path).ok()?.uid();
    let o = Command::new("id").args(["-P", &uid.to_string()]).output().ok()?;
    let line = String::from_utf8_lossy(&o.stdout).trim().to_string();
    let f: Vec<&str> = line.split(':').collect();
    if f.len() < 8 {
        return None;
    }
    let gecos = f[7].split(',').next().unwrap_or("");
    Some(if gecos.is_empty() { f[0].to_string() } else { gecos.to_string() })
}

fn pdf_author(path: &Path) -> Option<String> {
    let p = path.to_path_buf();
    std::panic::catch_unwind(move || {
        let doc = pdf_extract::Document::load(&p).ok()?;
        let info = doc.trailer.get(b"Info").ok()?;
        let dict = match info {
            pdf_extract::Object::Reference(r) => doc.get_object(*r).ok()?.as_dict().ok()?.clone(),
            o => o.as_dict().ok()?.clone(),
        };
        let a = dict.get(b"Author").ok()?;
        let raw = match a {
            pdf_extract::Object::String(b, _) => b.clone(),
            pdf_extract::Object::Reference(r) => doc.get_object(*r).ok()?.as_str().ok()?.to_vec(),
            _ => return None,
        };
        let s = if raw.starts_with(&[0xFE, 0xFF]) {
            let u: Vec<u16> = raw[2..].chunks_exact(2).map(|c| u16::from_be_bytes([c[0], c[1]])).collect();
            String::from_utf16_lossy(&u)
        } else {
            raw.iter().map(|&b| b as char).collect()
        };
        Some(s.split_whitespace().collect::<Vec<_>>().join(" "))
    }).ok().flatten().filter(|s| !s.is_empty())
}

/// (author, source) of a document: its own metadata (PDF, Office), then Spotlight, then the file's owner.
pub fn author_of(path: &Path, kind: &str) -> (Option<String>, Option<String>) {
    let e = ext_lower(path);
    if e == ".pdf" {
        if let Some(a) = pdf_author(path) {
            return (Some(a), Some("pdf".into()));
        }
    } else if [".docx", ".pptx", ".xlsx"].contains(&e.as_str()) {
        let core = std::fs::File::open(path).ok().and_then(|f| zip::ZipArchive::new(f).ok()).and_then(|mut z| {
            let mut s = String::new();
            z.by_name("docProps/core.xml").ok()?.read_to_string(&mut s).ok().map(|_| s)
        });
        if let Some(core) = core {
            for tag in ["dc:creator", "cp:lastModifiedBy"] {
                if let Some(m) = re(&format!("<{tag}>([^<]+)</{tag}>")).captures(&core) {
                    let v = m[1].trim();
                    if !v.is_empty() {
                        return (Some(v.to_string()), Some("office".into()));
                    }
                }
            }
        }
    }
    if ["doc", "pdf", "slides", "data"].contains(&kind) {
        if let Ok(o) = Command::new("mdls").args(["-raw", "-name", "kMDItemAuthors"]).arg(path).output() {
            let out = String::from_utf8_lossy(&o.stdout).trim().to_string();
            let names: Vec<String> = if out.is_empty() || out == "(null)" {
                Vec::new()
            } else {
                let quoted: Vec<String> = re(r#""([^"]+)""#).captures_iter(&out).map(|c| c[1].to_string()).collect();
                if quoted.is_empty() {
                    out.trim_matches(|c| c == '(' || c == ')').split(',').map(|x| x.trim().to_string())
                        .filter(|x| !x.is_empty()).collect()
                } else {
                    quoted
                }
            };
            if !names.is_empty() {
                return (Some(names.into_iter().take(3).collect::<Vec<_>>().join(", ")), Some("spotlight".into()));
            }
        }
    }
    match owner(path) {
        Some(o) => (Some(o), Some("owner".into())),
        None => (None, None),
    }
}

pub mod mail {
    //! A mail file (.eml) as text: its Subject, From, To and Date, then its plain-text body (or its HTML body
    //! without tags).

    use super::re;

    fn split(raw: &[u8]) -> (String, Vec<u8>) {
        let s = raw;
        let at = s.windows(4).position(|w| w == b"\r\n\r\n").map(|i| (i, 4))
            .or_else(|| s.windows(2).position(|w| w == b"\n\n").map(|i| (i, 2)));
        match at {
            Some((i, n)) => (String::from_utf8_lossy(&s[..i]).into_owned(), s[i + n..].to_vec()),
            None => (String::from_utf8_lossy(s).into_owned(), Vec::new()),
        }
    }

    /// Headers with folded lines joined, names in lowercase.
    fn headers(head: &str) -> Vec<(String, String)> {
        let mut out: Vec<(String, String)> = Vec::new();
        for line in head.lines() {
            if line.starts_with([' ', '\t']) {
                if let Some(last) = out.last_mut() {
                    last.1.push(' ');
                    last.1.push_str(line.trim());
                }
            } else if let Some((k, v)) = line.split_once(':') {
                out.push((k.trim().to_lowercase(), v.trim().to_string()));
            }
        }
        out
    }

    fn get<'a>(h: &'a [(String, String)], k: &str) -> Option<&'a str> {
        h.iter().find(|(n, _)| n == k).map(|(_, v)| v.as_str())
    }

    fn param(v: &str, name: &str) -> Option<String> {
        v.split(';').skip(1).find_map(|p| {
            let (k, val) = p.split_once('=')?;
            (k.trim().eq_ignore_ascii_case(name)).then(|| val.trim().trim_matches('"').to_string())
        })
    }

    fn b64(s: &str) -> Vec<u8> {
        let mut out = Vec::new();
        let (mut buf, mut bits) = (0u32, 0);
        for c in s.bytes() {
            let v = match c {
                b'A'..=b'Z' => c - b'A',
                b'a'..=b'z' => c - b'a' + 26,
                b'0'..=b'9' => c - b'0' + 52,
                b'+' | b'-' => 62,
                b'/' | b'_' => 63,
                _ => continue,
            } as u32;
            buf = (buf << 6) | v;
            bits += 6;
            if bits >= 8 {
                bits -= 8;
                out.push((buf >> bits) as u8);
                buf &= (1 << bits) - 1;
            }
        }
        out
    }

    fn qp(s: &[u8], header: bool) -> Vec<u8> {
        let mut out = Vec::new();
        let mut i = 0;
        while i < s.len() {
            match s[i] {
                b'=' if i + 2 < s.len() + 1 && s.get(i + 1) == Some(&b'\n') => i += 2,
                b'=' if s.get(i + 1) == Some(&b'\r') && s.get(i + 2) == Some(&b'\n') => i += 3,
                b'=' if i + 2 < s.len() && s[i + 1].is_ascii_hexdigit() && s[i + 2].is_ascii_hexdigit() => {
                    let h = std::str::from_utf8(&s[i + 1..i + 3]).ok().and_then(|h| u8::from_str_radix(h, 16).ok());
                    out.push(h.unwrap_or(b'?'));
                    i += 3;
                }
                b'_' if header => {
                    out.push(b' ');
                    i += 1;
                }
                c => {
                    out.push(c);
                    i += 1;
                }
            }
        }
        out
    }

    fn charset_decode(b: &[u8], _charset: &str) -> String {
        String::from_utf8(b.to_vec()).unwrap_or_else(|_| b.iter().map(|&c| c as char).collect())
    }

    /// RFC 2047 encoded words in a header (`=?utf-8?B?...?=`).
    fn header_text(v: &str) -> String {
        let rx = re(r"=\?([^?]+)\?([bBqQ])\?([^?]*)\?=(\s+(?:=\?))?");
        rx.replace_all(v, |c: &regex::Captures| {
            let raw = if c[2].eq_ignore_ascii_case("b") { b64(&c[3]) } else { qp(c[3].as_bytes(), true) };
            let mut s = charset_decode(&raw, &c[1]);
            if c.get(4).is_some() {
                s.push_str("=?");
            }
            s
        }).into_owned()
    }

    struct Part {
        ctype: String,
        text: String,
    }

    fn parts(raw: &[u8], out: &mut Vec<Part>) {
        let (head, body) = split(raw);
        let h = headers(&head);
        let ct = get(&h, "content-type").unwrap_or("text/plain").to_string();
        let mime = ct.split(';').next().unwrap_or("").trim().to_lowercase();
        if mime.starts_with("multipart/") {
            let Some(b) = param(&ct, "boundary") else { return };
            let sep = format!("--{b}");
            let body = String::from_utf8_lossy(&body).into_owned();
            for chunk in body.split(sep.as_str()).skip(1) {
                if chunk.starts_with("--") {
                    break;
                }
                let chunk = chunk.strip_prefix("\r\n").or_else(|| chunk.strip_prefix('\n')).unwrap_or(chunk);
                let chunk = chunk.strip_suffix("\r\n").or_else(|| chunk.strip_suffix('\n')).unwrap_or(chunk);
                parts(chunk.as_bytes(), out);
            }
            return;
        }
        if get(&h, "content-disposition").is_some_and(|d| d.to_lowercase().starts_with("attachment")) {
            return;
        }
        let enc = get(&h, "content-transfer-encoding").unwrap_or("").to_lowercase();
        let bytes = match enc.as_str() {
            "base64" => b64(&String::from_utf8_lossy(&body)),
            "quoted-printable" => qp(&body, false),
            _ => body,
        };
        let cs = param(&ct, "charset").unwrap_or_else(|| "utf-8".into());
        out.push(Part { ctype: mime, text: charset_decode(&bytes, &cs) });
    }

    /// A Date header as Python's mail parser writes it back (`Mon, 05 Oct 2026 10:00:00 -0400`).
    fn date(v: &str) -> String {
        chrono::DateTime::parse_from_rfc2822(v.trim()).map(|d| d.format("%a, %d %b %Y %H:%M:%S %z").to_string())
            .unwrap_or_else(|_| v.to_string())
    }

    pub fn text(raw: &[u8]) -> String {
        let (head, _) = split(raw);
        let h = headers(&head);
        let top: Vec<String> = ["subject", "from", "to", "date"].iter().zip(["Subject", "From", "To", "Date"])
            .filter_map(|(k, name)| get(&h, k).filter(|v| !v.is_empty()).map(|v| {
                let v = if *k == "date" { date(v) } else { header_text(v) };
                format!("{name}: {v}")
            }))
            .collect();
        let mut ps = Vec::new();
        parts(raw, &mut ps);
        let body = ps.iter().find(|p| p.ctype == "text/plain").or_else(|| ps.iter().find(|p| p.ctype == "text/html"));
        let text = match body {
            Some(p) if p.ctype == "text/html" => re(r"<[^>]+>").replace_all(&p.text, " ").into_owned(),
            Some(p) => p.text.clone(),
            None => String::new(),
        };
        format!("{}\n\n{}", top.join("\n"), text)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn suffix_as_python() {
        assert_eq!(suffix(Path::new("a/foo.")), ".");
        assert_eq!(suffix(Path::new(".bashrc")), "");
        assert_eq!(suffix(Path::new(".env.example")), ".example");
        assert_eq!(suffix(Path::new("..x")), "");
        assert_eq!(suffix(Path::new("a.tar.GZ")), ".GZ");
    }

    #[test]
    fn mail_body_and_headers() {
        let m = b"Subject: =?utf-8?B?SGVsbG8=?=\r\nFrom: a@example.com\r\nContent-Type: multipart/alternative; boundary=\"x\"\r\n\r\n--x\r\nContent-Type: text/html\r\n\r\n<p>hi</p>\r\n--x\r\nContent-Type: text/plain\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\nplain =3D text\r\n--x--\r\n";
        let t = mail::text(m);
        assert!(t.starts_with("Subject: Hello\nFrom: a@example.com\n\n"));
        assert!(t.contains("plain = text"));
    }
}
