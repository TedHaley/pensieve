//! Words of code as the code word index holds them, and the other small text helpers that several parts share.

use sha1::{Digest, Sha1};

/// The parts of one ASCII word, as 0.21's `[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+` splits it: `fetchUserID` -> fetch,
/// User, ID; `HTTPServer` -> HTTP, Server; `v2` -> v, 2.
pub fn split_word(w: &str) -> Vec<&str> {
    let b = w.as_bytes();
    let (mut out, mut i) = (Vec::new(), 0);
    while i < b.len() {
        let c = b[i];
        if c.is_ascii_uppercase() {
            let mut j = i;
            while j < b.len() && b[j].is_ascii_uppercase() {
                j += 1;
            }
            let lower_next = j < b.len() && b[j].is_ascii_lowercase();
            if !lower_next {
                out.push(&w[i..j]);
                i = j;
            } else if j - i >= 2 {
                out.push(&w[i..j - 1]);
                i = j - 1;
            } else {
                let mut k = j;
                while k < b.len() && b[k].is_ascii_lowercase() {
                    k += 1;
                }
                out.push(&w[i..k]);
                i = k;
            }
        } else if c.is_ascii_lowercase() {
            let mut k = i;
            while k < b.len() && b[k].is_ascii_lowercase() {
                k += 1;
            }
            out.push(&w[i..k]);
            i = k;
        } else if c.is_ascii_digit() {
            let mut k = i;
            while k < b.len() && b[k].is_ascii_digit() {
                k += 1;
            }
            out.push(&w[i..k]);
            i = k;
        } else {
            i += 1;
        }
    }
    out
}

/// Runs of ASCII letters and digits. `letter_first`: a run starts at a letter (`[A-Za-z][A-Za-z0-9]*`).
fn ascii_runs(text: &str, letter_first: bool) -> Vec<&str> {
    let b = text.as_bytes();
    let (mut out, mut i) = (Vec::new(), 0);
    while i < b.len() {
        let start_ok = if letter_first { b[i].is_ascii_alphabetic() } else { b[i].is_ascii_alphanumeric() };
        if !start_ok {
            i += 1;
            continue;
        }
        let mut j = i + 1;
        while j < b.len() && b[j].is_ascii_alphanumeric() {
            j += 1;
        }
        out.push(&text[i..j]);
        i = j;
    }
    out
}

/// Lowercase words of code text with identifiers split: `fetchUserID_list` -> `fetch user id list`.
pub fn code_words(text: &str) -> String {
    let mut out: Vec<String> = Vec::new();
    for w in ascii_runs(text, true) {
        out.extend(split_word(w).into_iter().filter(|p| p.len() > 1).map(|p| p.to_lowercase()));
    }
    out.join(" ")
}

/// The words of code text that the code index holds: code_words, and also one-letter parts and numbers, so that
/// `s3a`, `404` and `2026-01-04` can be found: `k3s_v2` -> `k 3 s v 2`.
pub fn code_tokens(text: &str) -> String {
    let mut out: Vec<String> = Vec::new();
    for w in ascii_runs(text, false) {
        out.extend(split_word(w).into_iter().map(|p| p.to_lowercase()));
    }
    out.join(" ")
}

/// A made-up word for one repo, in the code word index's `repo` column, so that a search in one repo asks SQLite
/// for "these words AND this repo". Letters only, so FTS5 keeps it whole.
pub fn repo_token(root: &str) -> String {
    let h = Sha1::digest(root.as_bytes());
    let hex: String = h.iter().map(|b| format!("{b:02x}")).collect();
    let tail: String = hex[..12].chars().map(|c| (b'a' + c.to_digit(16).unwrap_or(0) as u8) as char).collect();
    format!("zzrepo{tail}")
}

pub fn sha1_hex(s: &str) -> String {
    Sha1::digest(s.as_bytes()).iter().map(|b| format!("{b:02x}")).collect()
}

/// `~` in place of the home folder at the start of a path.
pub fn tilde(path: &str, home: &str) -> String {
    match path.strip_prefix(home) {
        Some(rest) if !home.is_empty() => format!("~{rest}"),
        _ => path.to_string(),
    }
}

/// The first `n` characters (not bytes) of `s`.
pub fn head(s: &str, n: usize) -> &str {
    match s.char_indices().nth(n) {
        Some((i, _)) => &s[..i],
        None => s,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_identifiers_as_0_21_does() {
        assert_eq!(code_words("fetchUserID_list"), "fetch user id list");
        assert_eq!(code_tokens("k3s_v2"), "k 3 s v 2");
        assert_eq!(code_tokens("HTTPServer 2026-01-04"), "http server 2026 01 04");
        assert_eq!(split_word("ABCdef"), vec!["AB", "Cdef"]);
        assert_eq!(split_word("Adef"), vec!["Adef"]);
    }

    #[test]
    fn repo_token_is_letters() {
        let t = repo_token("/work/app");
        assert!(t.starts_with("zzrepo") && t.len() == 18 && t.chars().all(|c| c.is_ascii_lowercase()));
    }
}
