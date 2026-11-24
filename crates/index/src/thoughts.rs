//! Thoughts: one piece of work that the user explored, followed across agent sessions.
//!
//! An exchange is one prompt with the agent's reply and the files it touched. A part is a run of exchanges of one
//! session that are about one topic. A thought joins the parts, of any session, that are about the same topic.
//!
//! The steps, each measured on 81 real sessions before it was written here:
//! 1. Each exchange gets one vector. The mean vector of all exchanges is subtracted first: raw EmbeddingGemma
//!    cosines are 0.67 to 0.79 for any two exchanges, and no threshold separates topics.
//! 2. `split` cuts a session into parts. It finds the cuts with the lowest total distance inside the parts, at a
//!    fixed price for each part (Pk 0.118 on stitched sessions, against 0.263 for word overlap).
//! 3. `join` groups the parts by average linkage. The similarity of two parts is their cosine plus the evidence
//!    that they are one piece of work: shared files, and a pull request or ticket that both name. Evidence against
//!    it is subtracted: another project, another branch, and the time between them. Meaning alone thus joins only
//!    work that is near in time, and shared evidence joins work at any distance in time.
//! 4. A thought takes the title of its main session when it is that session's main work. Otherwise it takes the
//!    start of the user's first full prompt. A name from the user or an agent wins.
//!
//! The tables are `exchanges`, `thoughts`, `thought_parts` and `thought_names` of index.db.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::Path;
use std::sync::{Arc, LazyLock, Mutex, MutexGuard, RwLock};
use std::time::Instant;

use regex::Regex;
use serde_json::{Value, json};
use store::rusqlite::{self, OptionalExtension, params};

use crate::parsers::{self, Session};
use crate::text::sha1_hex;
use crate::{Index, segment, vectors};

/// The price of one more part in `split`. A higher price gives fewer, longer parts.
pub const PENALTY: f64 = 1.4;
pub const MIN_EXCHANGES: usize = 2;
/// `split` looks at most this many exchanges back for the start of a part, which bounds its time on long sessions.
const LOOKBACK: usize = 300;
/// Two groups of parts join while their mean similarity is at least this.
pub const JOIN: f32 = 0.35;
/// The weight of the file overlap (Jaccard) in the similarity of two parts.
pub const FILE_WEIGHT: f32 = 0.5;
/// Added to the similarity of two parts that name the same pull request or ticket.
pub const KEY_WEIGHT: f32 = 0.3;
/// A key that more exchanges than this name (an epic, a long-lived pull request) does not tell work apart.
const KEY_MAX: usize = 30;
/// Subtracted from the similarity of two parts of different project folders. It is small because the worktrees of
/// one repo are different folders.
pub const OTHER_PROJECT: f32 = 0.1;
/// Subtracted when both parts name a branch that is not a main branch and they share none.
pub const OTHER_BRANCH: f32 = 0.2;
/// Subtracted for parts a week or more apart, and in proportion for less.
pub const WEEK_APART: f32 = 0.3;
const WEEK: f64 = 7.0 * 86_400.0;
/// A branch that most work passes through says nothing about which work it is.
const MAIN_BRANCHES: [&str; 5] = ["main", "master", "HEAD", "develop", "trunk"];
/// A prompt with fewer words than this ("yes start it") never starts a part.
const SHORT_WORDS: usize = 8;
/// A session's title names a thought that holds at least this share of the session's exchanges.
const TITLE_SHARE: f32 = 0.5;
const NAME_CHARS: usize = 64;
const NAME_WORDS: usize = 6;
const PROMPT_CHARS: usize = 1500;
const REPLY_HEAD: usize = 1500;
const REPLY_TAIL: usize = 900;
const BRIEF_CHARS: usize = 16_000;
/// Claude Code starts a compacted session with this prompt. It is a summary, not the user's words.
const CONTINUED: &str = "This session is being continued from a previous conversation";

static IMAGE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\[Image #\d+\]").expect("regex"));
/// Words at the start of a prompt that say nothing about the work.
static LEAD: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)^(?:(?:ok|okay|hey|hi|so|and|now|also|great|yes|yeah|please|then|actually|well|alright|nice|cool|can you|could you|lets|let's)\b[\s,.!:]*)+")
        .expect("regex")
});
static SENTENCE_END: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[.?!]\s").expect("regex"));
static PASTED: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?s)<pasted_content.*?(</pasted_content>|$)").expect("regex"));
static TAG: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"</?[A-Za-z][^>]*>").expect("regex"));
static SESSION_ID: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[A-Za-z0-9-]{8,80}$").expect("regex"));
const FILLER: [&str; 24] = ["want", "think", "yeah", "going", "thing", "things", "way", "please", "maybe", "really", "all",
    "sure", "currently", "something", "everything", "anything", "still", "actually", "right", "good", "cool", "next", "able", "try"];

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

fn cut(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

#[derive(Default)]
pub struct State {
    build: Mutex<()>,
    cache: RwLock<Option<Arc<Cache>>>,
    /// Sessions whose transcript gave no exchange, so that the backfill reads each one once.
    empty: Mutex<HashSet<String>>,
}

/// What a search reads: each exchange's vector with the thought it belongs to, and each thought's words.
struct Cache {
    ids: Vec<String>,
    words: Vec<String>,
    of: Vec<usize>,
    at: Vec<(String, i64)>,
    vecs: Vec<Vec<f32>>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Exchange {
    pub ts: String,
    pub prompt: String,
    pub reply: String,
    pub files: Vec<String>,
    /// The git branch that the transcript names at the prompt. Empty when it names none.
    pub branch: String,
    pub hash: String,
}

/// The text of an exchange that the model reads: the prompt, the start of the reply, and the file names.
fn embed_text(prompt: &str, reply: &str, files: &[String]) -> String {
    let mut names: Vec<&str> = files.iter().map(|f| f.rsplit('/').next().unwrap_or(f)).collect();
    names.sort_unstable();
    names.dedup();
    names.truncate(12);
    let tail = if names.is_empty() { String::new() } else { format!("\n\nfiles: {}", names.join(" ")) };
    format!("{}\n\n{}{tail}", cut(prompt, 1200), cut(reply, REPLY_HEAD))
}

/// The exchanges of a session. A long reply keeps its start and its end: the end usually holds the result.
pub fn exchanges_of(s: &Session) -> Vec<Exchange> {
    let mut out = Vec::new();
    for g in segment::exchanges(&s.turns) {
        let (first, last) = (g[0], g[g.len() - 1]);
        let (role, prompt) = &s.turns[first];
        if role != "user" || noise(prompt) {
            continue;
        }
        let reply = g[1..].iter().filter(|&&i| s.turns[i].0 == "assistant").map(|&i| s.turns[i].1.as_str())
            .collect::<Vec<_>>().join("\n");
        let n = reply.chars().count();
        let reply = if n > REPLY_HEAD + REPLY_TAIL {
            format!("{}\n…\n{}", cut(&reply, REPLY_HEAD), reply.chars().skip(n - REPLY_TAIL).collect::<String>())
        } else {
            reply
        };
        let mut files: Vec<String> = s.turn_files.iter().filter(|(i, _)| (first..=last).contains(i)).map(|(_, p)| p.clone()).collect();
        files.sort_unstable();
        files.dedup();
        let prompt = cut(prompt, PROMPT_CHARS);
        let hash = sha1_hex(&embed_text(&prompt, &reply, &files));
        let branch = s.turn_branch.iter().find(|(i, _)| (first..=last).contains(i)).map(|(_, b)| b.clone()).unwrap_or_default();
        out.push(Exchange { ts: s.turn_ts.get(first).cloned().unwrap_or_default(), prompt, reply, files, branch, hash });
    }
    out
}

/// A user turn that the harness wrote: the summary after a compaction, a `!` shell command with its output, or the
/// notice of an interrupt.
fn noise(prompt: &str) -> bool {
    let p = prompt.trim_start();
    let head = cut(p, 300);
    p.starts_with("<bash-") || p.starts_with("[Request interrupted") || head.contains(CONTINUED)
        || head.contains("The summarized conversation")
}

fn short(prompt: &str) -> bool {
    prompt.split(|c: char| !c.is_ascii_alphabetic()).filter(|w| w.len() >= 3).count() < SHORT_WORDS
}

/// The positions where a new part starts. `x` holds one unit vector per exchange. A part holds at least
/// MIN_EXCHANGES exchanges, and no part starts at a `short` prompt.
pub fn split(x: &[Vec<f32>], short: &[bool], penalty: f64) -> Vec<usize> {
    let n = x.len();
    if n < 2 * MIN_EXCHANGES {
        return Vec::new();
    }
    let d = x[0].len();
    let mut sums = vec![vec![0f64; d]; n + 1];
    for i in 0..n {
        for t in 0..d {
            sums[i + 1][t] = sums[i][t] + x[i][t] as f64;
        }
    }
    let mut best = vec![f64::INFINITY; n + 1];
    let mut back = vec![0usize; n + 1];
    best[0] = 0.0;
    for j in 1..=n {
        for i in j.saturating_sub(LOOKBACK)..j {
            let whole = i == 0 && j == n;
            if (j - i < MIN_EXCHANGES && !whole) || (i > 0 && short[i]) || best[i].is_infinite() {
                continue;
            }
            let ss: f64 = (0..d).map(|t| (sums[j][t] - sums[i][t]).powi(2)).sum();
            let len = (j - i) as f64;
            // for unit vectors, the squared distance of a part to its mean is len - |sum|² / len
            let cost = best[i] + len - ss / len + penalty;
            if cost < best[j] {
                best[j] = cost;
                back[j] = i;
            }
        }
    }
    let mut out = Vec::new();
    let mut j = n;
    while j > 0 && best[n].is_finite() {
        j = back[j];
        if j > 0 {
            out.push(j);
        }
    }
    out.reverse();
    out
}

fn row_best(sim: &[f32], alive: &[bool], n: usize, i: usize) -> (f32, usize) {
    let mut best = (f32::NEG_INFINITY, usize::MAX);
    for m in 0..n {
        if m != i && alive[m] && sim[i * n + m] > best.0 {
            best = (sim[i * n + m], m);
        }
    }
    best
}

/// Average linkage: join the two nearest groups while their mean similarity is at least `tau`. `sim` is an n × n
/// matrix. Returns one group number for each item.
pub fn join(sim: &mut [f32], n: usize, tau: f32) -> Vec<usize> {
    let mut size = vec![1f32; n];
    let mut alive = vec![true; n];
    let mut parent: Vec<usize> = (0..n).collect();
    let mut best: Vec<(f32, usize)> = (0..n).map(|i| row_best(sim, &alive, n, i)).collect();
    loop {
        let Some(i) = (0..n).filter(|&i| alive[i] && best[i].0 >= tau).max_by(|&a, &b| best[a].0.total_cmp(&best[b].0)) else { break };
        let j = best[i].1;
        for m in 0..n {
            if alive[m] && m != i && m != j {
                let v = (sim[i * n + m] * size[i] + sim[j * n + m] * size[j]) / (size[i] + size[j]);
                sim[i * n + m] = v;
                sim[m * n + i] = v;
            }
        }
        size[i] += size[j];
        alive[j] = false;
        parent[j] = i;
        best[i] = row_best(sim, &alive, n, i);
        for m in 0..n {
            if !alive[m] || m == i {
                continue;
            }
            if best[m].1 == i || best[m].1 == j {
                best[m] = row_best(sim, &alive, n, m);
            } else if sim[m * n + i] > best[m].0 {
                best[m] = (sim[m * n + i], i);
            }
        }
    }
    (0..n).map(|mut i| {
        while parent[i] != i {
            i = parent[i];
        }
        i
    }).collect()
}

fn stop(w: &str) -> bool {
    w.len() < 3 || FILLER.contains(&w) || segment::words(w).is_empty()
}

/// A name for a thought from a prompt of the user: the first sentence of its first line, without the words that
/// only open a prompt ("ok, can you"), cut at a word.
pub fn prompt_name(prompt: &str) -> String {
    let text = PASTED.replace_all(prompt, " ");
    let text = TAG.replace_all(&text, " ");
    let text = IMAGE.replace_all(&text, " ");
    let lines = || text.lines().map(|l| l.trim().trim_start_matches(['❯', '>', ' '])).filter(|l| !l.is_empty());
    // a prompt often starts with a pasted log line ("FAILED tests/test_a.py"): the user's own words have plain words
    let plain = |l: &&str| l.split_whitespace().filter(|w| w.chars().all(|c| c.is_lowercase() || c == '\'' || c == ',')).count() >= 3;
    let line = lines().find(plain).or_else(|| lines().next()).unwrap_or("");
    let line = LEAD.replace(line, "");
    // a first sentence of a few words ("build a pr to resolve.") names nothing without the next one
    let end = SENTENCE_END.find_iter(&line).map(|m| m.start() + 1).find(|&e| line[..e].split_whitespace().count() >= NAME_WORDS)
        .unwrap_or(line.len());
    let sentence = line[..end].trim_end_matches('.');
    let name = if sentence.chars().count() > NAME_CHARS {
        let head = cut(sentence, NAME_CHARS);
        format!("{}…", head.rsplit_once(' ').map_or(head.as_str(), |h| h.0).trim_end_matches([',', ';', ':']))
    } else {
        sentence.to_string()
    };
    capital(&name)
}

/// The pull requests and tickets that an exchange names.
fn keys_of(prompt: &str, reply: &str) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for text in [prompt, reply] {
        out.extend(PULL.find_iter(text).map(|m| m.as_str().to_string()));
        out.extend(TICKET.captures_iter(text).filter(|c| !NOT_TICKETS.contains(&&c[1])).map(|c| c[0].to_string()));
    }
    out.sort_unstable();
    out.dedup();
    out
}

/// The ticket keys in the name of a branch or a folder, in the form of the tracker ("ABC-123"). `trackers` holds
/// the tracker projects that are known, in upper case: without it, "python-3" is a key.
fn name_keys(name: &str, trackers: &HashSet<&str>) -> Vec<String> {
    NAME_KEY.captures_iter(name).filter(|c| trackers.contains(c[1].to_uppercase().as_str()))
        .map(|c| format!("{}-{}", c[1].to_uppercase(), &c[2])).collect()
}

fn seconds(ts: &str) -> Option<f64> {
    chrono::DateTime::parse_from_rfc3339(ts).ok().map(|t| t.timestamp() as f64)
}

fn capital(s: &str) -> String {
    let mut c = s.chars();
    c.next().map(|f| f.to_uppercase().collect::<String>() + c.as_str()).unwrap_or_default()
}

fn quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\\''"))
}

fn mean_unit(rows: &[Vec<f32>]) -> Vec<f32> {
    let mut m = vec![0f32; rows.first().map_or(0, Vec::len)];
    for r in rows {
        for (a, b) in m.iter_mut().zip(r) {
            *a += b;
        }
    }
    vectors::unit(&m)
}

struct Row {
    session: String,
    k: i64,
    ts: String,
    prompt: String,
    files: Vec<String>,
    branch: String,
    keys: Vec<String>,
}

struct Part {
    session: String,
    lo: usize,
    hi: usize,
}

static PULL: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)").expect("regex"));
static TICKET_URL: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"https://[\w.-]+/browse/([A-Z][A-Z0-9]{1,9}-\d{1,6})\b").expect("regex"));
static NAME_KEY: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)(?:^|[^a-z0-9])([a-z][a-z0-9]{1,9})-(\d{1,6})(?:$|[^0-9])").expect("regex"));
static TICKET: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\b([A-Z][A-Z0-9]{1,9})-\d{1,6}\b").expect("regex"));
/// Names of standards and models that have the form of a ticket key, such as UTF-8.
const NOT_TICKETS: [&str; 24] = ["UTF", "SHA", "GPT", "ISO", "HTTP", "RFC", "CVE", "AES", "RSA", "TLS", "PEP", "WCAG", "MD", "CP", "ES",
                                 "HS", "RS", "COVID", "IPV", "MP", "ED", "BM", "UTC", "US"];
const LINKS: usize = 6;
const LINK_FILES: usize = 8;

/// The pull requests, tickets and files that the exchanges (prompt, reply, JSON list of files) name, each kind with
/// the most named first. A ticket counts when a prompt names it, when a reply has a link to it, or when one of
/// `names` (the branches and the folders of the work) holds its key. A key that only the replies name counts when
/// two exchanges or more name it, because one mention is often a version or a model name. It does not count when a
/// branch in `branches` (all branches in the index) holds it and `names` does not: then it is the ticket of other
/// work.
fn links(ex: &[(String, String, String)], names: &[String], branches: &[String], home: &str) -> Vec<(&'static str, Value)> {
    fn bump<'a>(seen: &mut Vec<(&'a str, usize)>, key: &'a str) {
        match seen.iter_mut().find(|s| s.0 == key) {
            Some(s) => s.1 += 1,
            None => seen.push((key, 1)),
        }
    }
    let (mut pulls, mut tickets, mut files): (Vec<(&str, usize)>, Vec<(&str, usize)>, Vec<(&str, usize)>) = (Vec::new(), Vec::new(), Vec::new());
    let mut ticket_urls: HashMap<&str, &str> = HashMap::new();
    let mut by_user: HashSet<&str> = HashSet::new();
    let keys = |list: &[String]| -> HashSet<String> {
        list.iter().flat_map(|n| NAME_KEY.captures_iter(n).map(|c| format!("{}-{}", c[1].to_uppercase(), &c[2])).collect::<Vec<_>>()).collect()
    };
    let (in_names, in_branches) = (keys(names), keys(branches));
    let mut paths: Vec<Vec<String>> = Vec::new();
    for (prompt, reply, _) in ex {
        let mut here: HashSet<&str> = HashSet::new();
        for text in [prompt, reply] {
            for m in PULL.find_iter(text) {
                if here.insert(m.as_str()) {
                    bump(&mut pulls, m.as_str());
                }
            }
            for c in TICKET_URL.captures_iter(text) {
                ticket_urls.insert(c.get(1).map_or("", |m| m.as_str()), c.get(0).map_or("", |m| m.as_str()));
            }
            for c in TICKET.captures_iter(text) {
                let key = c.get(0).map_or("", |m| m.as_str());
                if NOT_TICKETS.contains(&&c[1]) {
                    continue;
                }
                if std::ptr::eq(text, prompt) {
                    by_user.insert(key);
                }
                if here.insert(key) {
                    bump(&mut tickets, key);
                }
            }
        }
    }
    for (_, _, list) in ex {
        paths.push(serde_json::from_str::<Vec<String>>(list).unwrap_or_default());
    }
    for p in paths.iter().flatten() {
        // an agent's scratch files are not the user's work
        if !["/private/tmp/", "/tmp/", "/private/var/", "/var/folders/"].iter().any(|t| p.starts_with(t)) {
            bump(&mut files, p);
        }
    }
    tickets.retain(|t| by_user.contains(t.0) || ticket_urls.contains_key(t.0) || in_names.contains(t.0)
        || (t.1 >= 2 && !in_branches.contains(t.0)));
    for seen in [&mut pulls, &mut tickets, &mut files] {
        seen.sort_by(|a, b| b.1.cmp(&a.1));
    }
    let pulls: Vec<Value> = pulls.iter().take(LINKS).filter_map(|(url, _)| {
        let c = PULL.captures(url)?;
        Some(json!({"url": url, "repo": &c[1], "number": c[2].parse::<i64>().ok()}))
    }).collect();
    let tickets: Vec<Value> = tickets.iter().take(LINKS).map(|(key, _)| json!({"key": key, "url": ticket_urls.get(key)})).collect();
    let files: Vec<String> = files.iter().take(LINK_FILES)
        .map(|(p, _)| p.strip_prefix(home).map_or(p.to_string(), |rest| format!("~{rest}"))).collect();
    vec![("pull_requests", json!(pulls)), ("tickets", json!(tickets)), ("files", json!(files))]
}

/// The shell command that picks a session up in its own agent, when that agent can resume by id.
fn resume_command(session_id: &str, folder: &str) -> Option<String> {
    let (source, rest) = session_id.split_once(':')?;
    if !Path::new(folder).is_dir() {
        return None;
    }
    let (bin, id) = match source {
        "claude" => ("claude --resume", rest.to_string()),
        // rollout-<time>-<uuid>
        "codex" => ("codex resume", rest.chars().rev().take(36).collect::<Vec<_>>().into_iter().rev().collect()),
        _ => return None,
    };
    SESSION_ID.is_match(&id).then(|| format!("cd {} && {bin} {id}", quote(folder)))
}

/// The link to a thought, for the user to paste into an agent session or into `pensieve open`.
pub const THOUGHT_LINK: &str = "pensieve://thought/";
/// The link to one session, such as `pensieve://session/claude:<uuid>`.
pub const SESSION_LINK: &str = "pensieve://session/";

/// What a pasted link names.
#[derive(Debug, PartialEq)]
pub enum Link {
    Thought(String),
    Session(String),
}

/// The thought or session that the first Pensieve link in `text` names. The text can hold other words around it.
pub fn link(text: &str) -> Option<Link> {
    let at = text.find("pensieve://")?;
    let rest = &text[at..];
    let end = rest.find(|c: char| c.is_whitespace() || matches!(c, '`' | '"' | '\'' | ')' | ']' | '>' | '<')).unwrap_or(rest.len());
    let url = rest[..end].trim_end_matches(['.', ',', ';']);
    if let Some(id) = url.strip_prefix(THOUGHT_LINK).filter(|id| id.starts_with("th_")) {
        return Some(Link::Thought(id.to_string()));
    }
    url.strip_prefix(SESSION_LINK).filter(|s| s.contains(':')).map(|s| Link::Session(s.to_string()))
}

/// The shell command that starts a new session of the same agent on a thought. The prompt tells the agent to read
/// the thought with `recall`, which gives it the work of all sessions and not only of the last one.
fn start_command(session_id: &str, folder: &str, id: &str, name: &str) -> Option<String> {
    let bin = match session_id.split_once(':')?.0 {
        "claude" => "claude",
        "codex" => "codex",
        _ => return None,
    };
    let prompt = format!("Continue my earlier work \"{name}\". First call the Pensieve `recall` tool with the thought {id}. \
                          Then tell me in two sentences where the work is.");
    let cd = if Path::new(folder).is_dir() { format!("cd {} && ", quote(folder)) } else { String::new() };
    Some(format!("{cd}{bin} {}", quote(&prompt)))
}

impl Index {
    /// Save the exchanges of a session that indexing read. A changed exchange loses its vector, and
    /// `thoughts_rebuild` embeds it later, so that a reply that grows is not embedded at each save.
    pub fn store_exchanges(&self, s: &Session) -> rusqlite::Result<usize> {
        let ex = exchanges_of(s);
        let old: HashMap<i64, String> = self.db.read(|c| {
            let mut st = c.prepare("SELECT k, hash FROM exchanges WHERE session_id=?")?;
            let r = st.query_map([&s.id], |r| Ok((r.get(0)?, r.get::<_, Option<String>>(1)?.unwrap_or_default())))?.flatten().collect();
            Ok(r)
        })?;
        let new: Vec<usize> = (0..ex.len()).filter(|&k| old.get(&(k as i64)) != Some(&ex[k].hash)).collect();
        let unread: i64 = self.db.read(|c| c.query_row("SELECT COUNT(*) FROM exchanges WHERE session_id=? AND branch IS NULL", [&s.id], |r| r.get(0)))?;
        if new.is_empty() && old.len() == ex.len() && unread == 0 {
            return Ok(0);
        }
        self.db.tx(|t| {
            for &k in &new {
                let e = &ex[k];
                t.execute("INSERT OR REPLACE INTO exchanges(session_id, k, ts, hash, prompt, reply, files, vec) VALUES(?,?,?,?,?,?,?,NULL)",
                          params![s.id, k as i64, e.ts, e.hash, e.prompt, e.reply, json!(e.files).to_string()])?;
            }
            // the branch is not part of the hash, so that a known exchange keeps its vector
            for (k, e) in ex.iter().enumerate() {
                t.execute("UPDATE exchanges SET branch=? WHERE session_id=? AND k=? AND branch IS NOT ?", params![e.branch, s.id, k as i64, e.branch])?;
            }
            t.execute("DELETE FROM exchanges WHERE session_id=? AND k>=?", params![s.id, ex.len() as i64])?;
            Ok(())
        })?;
        Ok(new.len().max(unread as usize))
    }

    /// Read the transcripts of sessions whose exchanges are not stored, or are stored without their branch. Stops
    /// after `budget` seconds. Returns true when sessions are left.
    fn backfill_exchanges(&self, budget: f64) -> bool {
        let t0 = Instant::now();
        let todo: Vec<(String, String, String)> = self.db.read(|c| {
            let mut st = c.prepare("SELECT id, source, path FROM sessions WHERE n_chunks>0 AND id NOT LIKE '%:memory/%' \
                                    AND (id NOT IN (SELECT DISTINCT session_id FROM exchanges) \
                                         OR id IN (SELECT DISTINCT session_id FROM exchanges WHERE branch IS NULL))")?;
            let r = st.query_map([], |r| Ok((r.get(0)?, r.get::<_, Option<String>>(1)?.unwrap_or_default(),
                                             r.get::<_, Option<String>>(2)?.unwrap_or_default())))?.flatten().collect();
            Ok(r)
        }).unwrap_or_default();
        for (id, source, path) in todo {
            if lock(&self.thoughts.empty).contains(&id) {
                continue;
            }
            if t0.elapsed().as_secs_f64() > budget {
                return true;
            }
            let stored = parsers::parse(&source, Path::new(&path)).ok().and_then(|s| self.store_exchanges(&s).ok()).unwrap_or(0);
            if stored == 0 {
                lock(&self.thoughts.empty).insert(id);
            }
        }
        false
    }

    /// Embed the exchanges that have no vector. Stops after `budget` seconds. Returns true when exchanges are left.
    fn embed_exchanges(&self, budget: f64) -> bool {
        let t0 = Instant::now();
        loop {
            let rows: Vec<(String, i64, String, String, String)> = self.db.read(|c| {
                let mut st = c.prepare("SELECT session_id, k, prompt, reply, files FROM exchanges WHERE vec IS NULL LIMIT 64")?;
                let r = st.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)))?.flatten().collect();
                Ok(r)
            }).unwrap_or_default();
            if rows.is_empty() {
                return false;
            }
            let texts: Vec<String> = rows.iter().map(|(_, _, p, r, f)| {
                embed_text(p, r, &serde_json::from_str::<Vec<String>>(f).unwrap_or_default())
            }).collect();
            let vecs = self.embed_docs(&texts, None);
            // a model that did not load gives zero vectors: keep the exchanges for the next run
            if vecs.iter().any(|v| v.iter().all(|x| *x == 0.0)) {
                return true;
            }
            let saved = self.db.tx(|t| {
                for ((sid, k, ..), v) in rows.iter().zip(&vecs) {
                    t.execute("UPDATE exchanges SET vec=? WHERE session_id=? AND k=?", params![vectors::to_blob(v), sid, k])?;
                }
                Ok(())
            });
            if saved.is_err() || t0.elapsed().as_secs_f64() > budget {
                return true;
            }
        }
    }

    /// Build the thoughts again from the exchanges: embed the new ones, split each session, join the parts and
    /// name the thoughts. Returns (thoughts, whether work is left for the next call).
    pub fn thoughts_rebuild(&self, budget: f64, log: &dyn Fn(&str)) -> (usize, bool) {
        let _one = lock(&self.thoughts.build);
        let t0 = Instant::now();
        let left = self.backfill_exchanges(budget) | self.embed_exchanges(budget);
        let mut rows = Vec::new();
        let mut raw: Vec<Vec<f32>> = Vec::new();
        let loaded = self.db.read(|c| {
            // a session of one prompt with almost no reply is a script's call of an agent, not the user's work
            let mut st = c.prepare("SELECT session_id, k, ts, prompt, files, vec, reply, branch FROM exchanges e WHERE vec IS NOT NULL AND NOT \
                (length(reply) < 200 AND (SELECT COUNT(*) FROM exchanges o WHERE o.session_id = e.session_id) = 1) ORDER BY session_id, k")?;
            let mut q = st.query([])?;
            while let Some(r) = q.next()? {
                let prompt = r.get::<_, Option<String>>(3)?.unwrap_or_default();
                let keys = keys_of(&prompt, &r.get::<_, Option<String>>(6)?.unwrap_or_default());
                rows.push(Row { session: r.get(0)?, k: r.get(1)?, ts: r.get::<_, Option<String>>(2)?.unwrap_or_default(), prompt,
                                files: serde_json::from_str(&r.get::<_, Option<String>>(4)?.unwrap_or_default()).unwrap_or_default(),
                                branch: r.get::<_, Option<String>>(7)?.unwrap_or_default(), keys });
                raw.push(vectors::from_blob(&r.get::<_, Vec<u8>>(5)?));
            }
            Ok(())
        });
        if loaded.is_err() || rows.is_empty() {
            return (0, left);
        }
        let sessions: HashMap<String, (String, String)> = self.db.read(|c| {
            let mut st = c.prepare("SELECT id, title, project FROM sessions")?;
            let r = st.query_map([], |r| Ok((r.get(0)?, (r.get::<_, Option<String>>(1)?.unwrap_or_default(),
                                                       r.get::<_, Option<String>>(2)?.unwrap_or_default()))))?.flatten().collect();
            Ok(r)
        }).unwrap_or_default();

        let d = raw[0].len();
        let mut mu = vec![0f32; d];
        for v in &raw {
            for (a, b) in mu.iter_mut().zip(v) {
                *a += b / raw.len() as f32;
            }
        }
        let centre = |v: &[f32]| vectors::unit(&v.iter().zip(&mu).map(|(a, b)| a - b).collect::<Vec<_>>());
        let x: Vec<Vec<f32>> = raw.iter().map(|v| centre(v)).collect();

        let mut parts: Vec<Part> = Vec::new();
        let mut lo = 0;
        while lo < rows.len() {
            let hi = (lo..rows.len()).find(|&i| rows[i].session != rows[lo].session).unwrap_or(rows.len());
            let shorts: Vec<bool> = rows[lo..hi].iter().map(|r| short(&r.prompt)).collect();
            // `split` reads only the meaning, and it misses most changes of branch
            let mut edges = vec![lo];
            edges.extend((lo + 1..hi).filter(|&i| {
                let (a, b) = (&rows[i - 1].branch, &rows[i].branch);
                !a.is_empty() && !b.is_empty() && a != b
            }));
            edges.push(hi);
            for e in edges.windows(2) {
                let mut cuts = vec![0];
                cuts.extend(split(&x[e[0]..e[1]], &shorts[e[0] - lo..e[1] - lo], PENALTY));
                cuts.push(e[1] - e[0]);
                for w in cuts.windows(2) {
                    parts.push(Part { session: rows[lo].session.clone(), lo: e[0] + w[0], hi: e[0] + w[1] });
                }
            }
            lo = hi;
        }
        let n = parts.len();
        let pv: Vec<Vec<f32>> = parts.iter().map(|p| mean_unit(&x[p.lo..p.hi])).collect();
        let files: Vec<HashSet<&str>> = parts.iter()
            .map(|p| rows[p.lo..p.hi].iter().flat_map(|r| r.files.iter().map(String::as_str)).collect()).collect();
        // a worktree or a second clone of a repo is another folder, but it is the same project
        let mut repo_of: HashMap<&str, String> = HashMap::new();
        for (_, folder) in sessions.values() {
            repo_of.entry(folder.as_str()).or_insert_with(|| self.locate(folder).map_or_else(|| folder.clone(), |at| at.0));
        }
        let folder: Vec<&str> = parts.iter().map(|p| sessions.get(&p.session).map_or("", |s| s.1.as_str())).collect();
        let project: Vec<&str> = folder.iter().map(|f| repo_of.get(f).map_or(*f, String::as_str)).collect();
        let branches: Vec<HashSet<(&str, &str)>> = parts.iter().enumerate().map(|(i, p)| {
            rows[p.lo..p.hi].iter().map(|r| r.branch.as_str()).filter(|b| !b.is_empty() && !MAIN_BRANCHES.contains(b))
                .map(|b| (project[i], b)).collect()
        }).collect();
        let mut named_by: HashMap<&str, usize> = HashMap::new();
        for k in rows.iter().flat_map(|r| &r.keys) {
            *named_by.entry(k.as_str()).or_default() += 1;
        }
        // A branch or a folder often has its ticket in its name ("abc-123-release-build"). Such a key counts only
        // when the text of some exchange has a key of the same tracker project, which shows that "abc" is one.
        let trackers: HashSet<&str> = named_by.keys().filter(|k| !k.starts_with("http")).filter_map(|k| k.rsplit_once('-')).map(|k| k.0).collect();
        let keys: Vec<HashSet<String>> = parts.iter().enumerate().map(|(i, p)| {
            let mut of: HashSet<String> = rows[p.lo..p.hi].iter().flat_map(|r| r.keys.iter()).filter(|k| (2..=KEY_MAX).contains(&named_by[k.as_str()]))
                .cloned().collect();
            let names = rows[p.lo..p.hi].iter().map(|r| r.branch.as_str()).chain([folder[i].rsplit('/').next().unwrap_or("")]);
            for key in names.collect::<HashSet<_>>().into_iter().flat_map(|n| name_keys(n, &trackers)) {
                if named_by.get(key.as_str()).is_none_or(|n| *n <= KEY_MAX) {
                    of.insert(key);
                }
            }
            of
        }).collect();
        let when: Vec<Option<f64>> = parts.iter().map(|p| {
            let at: Vec<f64> = rows[p.lo..p.hi].iter().filter_map(|r| seconds(&r.ts)).collect();
            (!at.is_empty()).then(|| at.iter().sum::<f64>() / at.len() as f64)
        }).collect();
        let mut sim = vec![0f32; n * n];
        for i in 0..n {
            for j in i + 1..n {
                let both = if files[i].is_empty() || files[j].is_empty() { 0 } else { files[i].intersection(&files[j]).count() };
                let jac = if both == 0 { 0.0 } else { both as f32 / (files[i].len() + files[j].len() - both) as f32 };
                let mut v = vectors::dot(&pv[i], &pv[j]) + FILE_WEIGHT * jac;
                if !keys[i].is_disjoint(&keys[j]) {
                    v += KEY_WEIGHT;
                }
                if project[i] != project[j] {
                    v -= OTHER_PROJECT;
                }
                if !branches[i].is_empty() && !branches[j].is_empty() && branches[i].is_disjoint(&branches[j]) {
                    v -= OTHER_BRANCH;
                }
                if let (Some(a), Some(b)) = (when[i], when[j]) {
                    v -= WEEK_APART * ((a - b).abs() / WEEK).min(1.0) as f32;
                }
                sim[i * n + j] = v;
                sim[j * n + i] = v;
            }
        }
        let labels = join(&mut sim, n, JOIN);
        drop(sim);
        let mut groups: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
        for (i, l) in labels.iter().enumerate() {
            groups.entry(*l).or_default().push(i);
        }
        let start = |p: &Part| (rows[p.lo].ts.clone(), p.session.clone(), rows[p.lo].k);
        let mut thoughts: Vec<Vec<usize>> = groups.into_values().collect();
        for g in thoughts.iter_mut() {
            g.sort_by_key(|&i| start(&parts[i]));
        }
        let ids: Vec<String> = thoughts.iter().map(|g| {
            let p = &parts[g[0]];
            format!("th_{}", &sha1_hex(&format!("{}#{}", p.session, rows[p.lo].k))[..12])
        }).collect();
        // A session's title is about the session's main work. It names a thought only when the thought is that
        // work: a side topic of a long session takes the user's own words.
        let mut in_session: HashMap<&str, usize> = HashMap::new();
        for r in &rows {
            *in_session.entry(r.session.as_str()).or_default() += 1;
        }
        let names: Vec<String> = thoughts.iter().map(|g| {
            let mut held: BTreeMap<&str, usize> = BTreeMap::new();
            for &i in g {
                *held.entry(parts[i].session.as_str()).or_default() += parts[i].hi - parts[i].lo;
            }
            let share = |s: &str, n: usize| n as f32 / in_session[s] as f32;
            let main = held.iter().max_by(|a, b| a.1.cmp(b.1).then(share(a.0, *a.1).total_cmp(&share(b.0, *b.1)))).map(|(s, n)| (*s, *n));
            let title = main.filter(|(s, n)| share(s, *n) >= TITLE_SHARE).and_then(|(s, _)| sessions.get(s)).map(|s| s.0.trim())
                .filter(|t| !t.is_empty());
            if let Some(title) = title {
                return title.to_string();
            }
            let prompts = || g.iter().flat_map(|&i| rows[parts[i].lo..parts[i].hi].iter().map(|r| r.prompt.as_str()));
            let own = prompts().find(|p| !short(p)).or_else(|| prompts().next()).map(prompt_name).unwrap_or_default();
            if own.is_empty() { main.and_then(|(s, _)| sessions.get(s)).map(|s| s.0.clone()).unwrap_or_default() } else { own }
        }).collect();

        let given: Vec<(String, i64, String)> = self.db.read(|c| {
            let mut st = c.prepare("SELECT session_id, k, name FROM thought_names")?;
            let r = st.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?.flatten().collect();
            Ok(r)
        }).unwrap_or_default();
        let wrote = self.db.tx(|tx| {
            tx.execute("DELETE FROM thoughts", [])?;
            tx.execute("DELETE FROM thought_parts", [])?;
            for (t, g) in thoughts.iter().enumerate() {
                let holds = |sid: &str, k: i64| g.iter().any(|&i| parts[i].session == sid && (rows[parts[i].lo].k..=rows[parts[i].hi - 1].k).contains(&k));
                let name = given.iter().find(|(sid, k, _)| holds(sid, *k)).map(|x| x.2.clone());
                let of: HashSet<&str> = g.iter().map(|&i| parts[i].session.as_str()).collect();
                let updated = g.iter().map(|&i| rows[parts[i].hi - 1].ts.as_str()).max().unwrap_or("");
                let count: usize = g.iter().map(|&i| parts[i].hi - parts[i].lo).sum();
                tx.execute("INSERT OR REPLACE INTO thoughts(id, name, auto_name, started, updated, n_sessions, n_exchanges) VALUES(?,?,?,?,?,?,?)",
                           params![ids[t], name, names[t], rows[parts[g[0]].lo].ts, updated, of.len() as i64, count as i64])?;
                for &i in g {
                    let p = &parts[i];
                    tx.execute("INSERT OR REPLACE INTO thought_parts(thought_id, session_id, a, z, started, updated) VALUES(?,?,?,?,?,?)",
                               params![ids[t], p.session, rows[p.lo].k, rows[p.hi - 1].k + 1, rows[p.lo].ts, rows[p.hi - 1].ts])?;
                }
            }
            Ok(())
        });
        if let Err(e) = wrote {
            log(&format!("thoughts: {e}"));
            return (0, left);
        }
        *self.thoughts.cache.write().unwrap_or_else(|e| e.into_inner()) = None;
        log(&format!("thoughts: {} from {} parts of {} exchanges in {:.0} ms", thoughts.len(), n, rows.len(), t0.elapsed().as_secs_f64() * 1000.0));
        (thoughts.len(), left)
    }

    fn thought_cache(&self) -> Arc<Cache> {
        if let Some(c) = self.thoughts.cache.read().unwrap_or_else(|e| e.into_inner()).clone() {
            return c;
        }
        let mut c = Cache { ids: Vec::new(), words: Vec::new(), of: Vec::new(), at: Vec::new(), vecs: Vec::new() };
        let mut pos: HashMap<String, usize> = HashMap::new();
        let _ = self.db.read(|db| {
            let mut st = db.prepare("SELECT id, COALESCE(name, auto_name, '') FROM thoughts")?;
            for r in st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?.flatten() {
                pos.insert(r.0.clone(), c.ids.len());
                c.ids.push(r.0);
                c.words.push(r.1.to_lowercase());
            }
            let mut st = db.prepare("SELECT p.thought_id, e.session_id, e.k, e.prompt, e.vec FROM exchanges e JOIN thought_parts p \
                                     ON p.session_id=e.session_id AND e.k>=p.a AND e.k<p.z WHERE e.vec IS NOT NULL")?;
            let mut q = st.query([])?;
            while let Some(r) = q.next()? {
                let Some(&t) = pos.get(&r.get::<_, String>(0)?) else { continue };
                if c.words[t].len() < 6000 {
                    c.words[t].push(' ');
                    c.words[t].push_str(&cut(&r.get::<_, Option<String>>(3)?.unwrap_or_default(), 400).to_lowercase());
                }
                c.of.push(t);
                c.at.push((r.get(1)?, r.get(2)?));
                c.vecs.push(vectors::from_blob(&r.get::<_, Vec<u8>>(4)?));
            }
            Ok(())
        });
        let c = Arc::new(c);
        *self.thoughts.cache.write().unwrap_or_else(|e| e.into_inner()) = Some(c.clone());
        c
    }

    fn thought_row(&self, c: &rusqlite::Connection, id: &str, prompts: bool) -> rusqlite::Result<Option<Value>> {
        let head = c.query_row("SELECT name, auto_name, started, updated, n_sessions, n_exchanges FROM thoughts WHERE id=?", [id], |r| {
            Ok((r.get::<_, Option<String>>(0)?, r.get::<_, Option<String>>(1)?.unwrap_or_default(), r.get::<_, Option<String>>(2)?,
                r.get::<_, Option<String>>(3)?, r.get::<_, i64>(4)?, r.get::<_, i64>(5)?))
        }).optional()?;
        let Some((name, auto, started, updated, n_sessions, n_exchanges)) = head else { return Ok(None) };
        let mut st = c.prepare("SELECT p.session_id, p.a, p.z, p.started, p.updated, s.source, s.title, s.project, s.project_name \
                                FROM thought_parts p LEFT JOIN sessions s ON s.id=p.session_id WHERE p.thought_id=? ORDER BY p.updated DESC, p.a DESC")?;
        let mut q = st.query([id])?;
        let mut parts = Vec::new();
        while let Some(r) = q.next()? {
            let (sid, a, z): (String, i64, i64) = (r.get(0)?, r.get(1)?, r.get(2)?);
            let folder = r.get::<_, Option<String>>(7)?.unwrap_or_default();
            let mut part = json!({
                "session": sid, "a": a, "z": z, "started": r.get::<_, Option<String>>(3)?, "updated": r.get::<_, Option<String>>(4)?,
                "source": r.get::<_, Option<String>>(5)?, "title": r.get::<_, Option<String>>(6)?, "folder": folder,
                "project": r.get::<_, Option<String>>(8)?, "exchanges": z - a, "resumable": resume_command(&sid, &folder).is_some(),
            });
            if prompts {
                let mut ps = c.prepare("SELECT prompt FROM exchanges WHERE session_id=? AND k>=? AND k<? ORDER BY k")?;
                let all: Vec<String> = ps.query_map(params![sid, a, z], |r| r.get::<_, Option<String>>(0))?.flatten().flatten().collect();
                part["prompts"] = json!(all.iter().map(|p| cut(p, 240)).collect::<Vec<_>>());
            }
            parts.push(part);
        }
        let last = parts.first().map(|p| {
            c.query_row("SELECT prompt FROM exchanges WHERE session_id=? AND k<? ORDER BY k DESC LIMIT 1",
                        params![p["session"].as_str(), p["z"].as_i64()], |r| r.get::<_, Option<String>>(0)).optional()
        }).transpose()?.flatten().flatten().unwrap_or_default();
        Ok(Some(json!({
            "id": id, "name": name.clone().unwrap_or(auto.clone()), "auto_name": auto, "named": name.is_some(),
            "started": started, "updated": updated, "sessions": n_sessions, "exchanges": n_exchanges,
            "project": parts.first().map(|p| p["project"].clone()), "last_prompt": cut(&last, 300), "parts": parts,
        })))
    }

    /// Without a query: the thoughts, the newest first. With a query: the thoughts that match it by meaning or by
    /// words, the best first, each with `score` and the exchange that matched (`match`).
    pub fn thoughts_list(&self, query: &str, limit: usize) -> Value {
        let query = query.trim();
        let mut picked: Vec<(String, Option<(f32, String, i64)>)> = Vec::new();
        if query.is_empty() {
            picked = self.db.read(|c| {
                let mut st = c.prepare("SELECT id FROM thoughts ORDER BY updated DESC LIMIT ?")?;
                let r = st.query_map([limit as i64], |r| Ok((r.get(0)?, None)))?.flatten().collect();
                Ok(r)
            }).unwrap_or_default();
        } else {
            let cache = self.thought_cache();
            let q = self.embed_query(query);
            let words: Vec<String> = query.to_lowercase().split(|c: char| !c.is_alphanumeric()).filter(|w| !stop(w)).map(String::from).collect();
            let mut best: Vec<Option<(f32, usize)>> = vec![None; cache.ids.len()];
            for (e, v) in cache.vecs.iter().enumerate() {
                let s = if q.len() == v.len() { vectors::dot(&q, v) } else { 0.0 };
                if best[cache.of[e]].is_none_or(|b| s > b.0) {
                    best[cache.of[e]] = Some((s, e));
                }
            }
            let mut scored: Vec<(f32, usize, usize)> = best.iter().enumerate().filter_map(|(t, b)| {
                let (s, e) = (*b)?;
                let found = words.iter().filter(|w| cache.words[t].contains(w.as_str())).count();
                let lexical = if words.is_empty() { 0.0 } else { 0.08 * found as f32 / words.len() as f32 };
                Some((s + lexical, t, e))
            }).collect();
            scored.sort_by(|a, b| b.0.total_cmp(&a.0));
            let top = scored.first().map_or(0.0, |s| s.0);
            for (s, t, e) in scored.into_iter().take(limit).filter(|s| s.0 >= top - 0.12) {
                picked.push((cache.ids[t].clone(), Some((s, cache.at[e].0.clone(), cache.at[e].1))));
            }
        }
        let out: Vec<Value> = self.db.read(|c| {
            let mut out = Vec::new();
            for (id, hit) in &picked {
                let Some(mut v) = self.thought_row(c, id, false)? else { continue };
                if let Some((score, sid, k)) = hit {
                    let prompt: Option<String> = c.query_row("SELECT prompt FROM exchanges WHERE session_id=? AND k=?", params![sid, k], |r| r.get(0)).optional()?.flatten();
                    v["score"] = json!(score);
                    v["match"] = json!({"session": sid, "k": k, "prompt": cut(&prompt.unwrap_or_default(), 300)});
                }
                out.push(v);
            }
            Ok(out)
        }).unwrap_or_default();
        let total = self.db.read(|c| c.query_row("SELECT COUNT(*) FROM thoughts", [], |r| r.get::<_, i64>(0))).unwrap_or(0);
        json!({"thoughts": out, "total": total, "query": query})
    }

    /// One thought with its parts, the newest first, and the prompts of each part.
    pub fn thought(&self, id: &str) -> Option<Value> {
        self.db.read(|c| self.thought_row(c, id, true)).ok().flatten()
    }

    /// Give a thought a name, or take the user's name away with an empty one. The name stays with the exchange that
    /// the thought started at, so it survives when the thoughts are built again.
    pub fn thought_rename(&self, id: &str, name: &str) -> Result<Value, String> {
        let name = cut(name.trim(), 120);
        self.db.tx(|t| {
            let parts: Vec<(String, i64, i64)> = t.prepare("SELECT session_id, a, z FROM thought_parts WHERE thought_id=? ORDER BY started, a")?
                .query_map([id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?.flatten().collect();
            let Some(first) = parts.first().cloned() else { return Ok(false) };
            for (sid, a, z) in &parts {
                t.execute("DELETE FROM thought_names WHERE session_id=? AND k>=? AND k<?", params![sid, a, z])?;
            }
            if name.is_empty() {
                t.execute("UPDATE thoughts SET name=NULL WHERE id=?", [id])?;
            } else {
                t.execute("INSERT OR REPLACE INTO thought_names VALUES(?,?,?)", params![first.0, first.1, name])?;
                t.execute("UPDATE thoughts SET name=? WHERE id=?", params![name, id])?;
            }
            Ok(true)
        }).map_err(|e| e.to_string())?.then_some(()).ok_or("no such thought")?;
        *self.thoughts.cache.write().unwrap_or_else(|e| e.into_inner()) = None;
        self.thought(id).ok_or_else(|| "no such thought".into())
    }

    /// A thought as the user knows it: their own words at its start and at its end, the titles of its sessions, and
    /// the pull requests, tickets and files that its exchanges name. `hit` is the prompt that a search matched.
    pub fn thought_card(&self, id: &str, hit: Option<&str>) -> Option<Value> {
        let t = self.thought(id)?;
        let parts = t["parts"].as_array()?;
        let mut ex: Vec<(String, String, String)> = Vec::new();
        let mut names: Vec<String> = parts.iter().filter_map(|p| p["folder"].as_str()).map(|f| f.rsplit('/').next().unwrap_or(f).to_string()).collect();
        for p in parts.iter().rev() {
            let (sid, a, z) = (p["session"].as_str().unwrap_or(""), p["a"].as_i64().unwrap_or(0), p["z"].as_i64().unwrap_or(0));
            names.extend(self.db.read(|c| {
                let mut st = c.prepare("SELECT DISTINCT branch FROM exchanges WHERE session_id=? AND k>=? AND k<? AND branch<>''")?;
                let r: Vec<String> = st.query_map(params![sid, a, z], |r| r.get(0))?.flatten().collect();
                Ok(r)
            }).unwrap_or_default());
            ex.extend(self.db.read(|c| {
                let mut st = c.prepare("SELECT prompt, reply, files FROM exchanges WHERE session_id=? AND k>=? AND k<? ORDER BY k")?;
                let r: Vec<(String, String, String)> = st.query_map(params![sid, a, z], |r| Ok((r.get::<_, Option<String>>(0)?.unwrap_or_default(),
                    r.get::<_, Option<String>>(1)?.unwrap_or_default(), r.get::<_, Option<String>>(2)?.unwrap_or_default())))?.flatten().collect();
                Ok(r)
            }).unwrap_or_default());
        }
        // a prompt that the user pasted from a terminal starts with the terminal's prompt mark
        let flat = |s: &str, n: usize| cut(s.split_whitespace().collect::<Vec<_>>().join(" ").trim_start_matches(['❯', '>', ' ']), n);
        // "yes" or "keep going" does not tell the user which work this is
        let asked = ex.iter().map(|e| &e.0).find(|p| p.split_whitespace().count() >= SHORT_WORDS).or(ex.first().map(|e| &e.0));
        let ended = ex.last().and_then(|e| e.1.trim().rsplit("\n\n").find(|para| para.split_whitespace().count() >= 4).map(String::from));
        let mut titles: Vec<&str> = Vec::new();
        for title in parts.iter().filter_map(|p| p["title"].as_str()).filter(|s| !s.is_empty()) {
            if !titles.contains(&title) {
                titles.push(title);
            }
        }
        let resume = parts.iter().find_map(|p| resume_command(p["session"].as_str()?, p["folder"].as_str()?));
        let start = parts.iter().find_map(|p| start_command(p["session"].as_str()?, p["folder"].as_str().unwrap_or(""), id, t["name"].as_str().unwrap_or("")));
        let session_link = parts.first().and_then(|p| p["session"].as_str()).map(|s| format!("{SESSION_LINK}{s}"));
        let mut card = json!({
            "link": format!("{THOUGHT_LINK}{id}"), "session_link": session_link,
            "id": id, "name": t["name"], "named": t["named"], "project": t["project"],
            "started": t["started"], "updated": t["updated"], "sessions": t["sessions"], "exchanges": t["exchanges"],
            "asked": asked.map(|p| flat(p, 300)), "left_off": flat(t["last_prompt"].as_str().unwrap_or(""), 300),
            "ended_with": ended.map(|p| flat(&p, 300)), "matched": hit.map(|p| flat(p, 300)),
            "session_titles": titles.iter().take(6).collect::<Vec<_>>(), "resume": resume, "start": start,
        });
        let branches: Vec<String> = self.db.read(|c| {
            let mut st = c.prepare("SELECT DISTINCT branch FROM exchanges WHERE branch<>''")?;
            let r: Vec<String> = st.query_map([], |r| r.get(0))?.flatten().collect();
            Ok(r)
        }).unwrap_or_default();
        for (k, v) in links(&ex, &names, &branches, &store::home().to_string_lossy()) {
            card[k] = v;
        }
        Some(card)
    }

    /// The cards of the thoughts that match `query`, the best first. An empty `query` gives the newest thoughts.
    pub fn thought_cards(&self, query: &str, limit: usize) -> Value {
        let list = self.thoughts_list(query, limit);
        let cards: Vec<Value> = list["thoughts"].as_array().into_iter().flatten()
            .filter_map(|t| self.thought_card(t["id"].as_str()?, t["match"]["prompt"].as_str())).collect();
        json!({"thoughts": cards, "total": list["total"]})
    }

    /// The prompts of exchanges `a` to `z - 1` of a session, its files, and where its last reply ended.
    fn part_detail(&self, sid: &str, a: i64, z: i64) -> String {
        let ex: Vec<(String, String, String)> = self.db.read(|c| {
            let mut st = c.prepare("SELECT prompt, reply, files FROM exchanges WHERE session_id=? AND k>=? AND k<? ORDER BY k")?;
            let r = st.query_map(params![sid, a, z], |r| Ok((r.get::<_, Option<String>>(0)?.unwrap_or_default(),
                r.get::<_, Option<String>>(1)?.unwrap_or_default(), r.get::<_, Option<String>>(2)?.unwrap_or_default())))?.flatten().collect();
            Ok(r)
        }).unwrap_or_default();
        let mut b = String::new();
        let keep: Vec<usize> = (0..ex.len()).filter(|&i| i < 2 || i + 6 >= ex.len()).collect();
        b.push_str("\nPrompts:\n");
        for (n, &i) in keep.iter().enumerate() {
            if n > 0 && keep[n - 1] + 1 != i {
                b.push_str(&format!("- … ({} more)\n", i - keep[n - 1] - 1));
            }
            b.push_str(&format!("- {}\n", cut(&ex[i].0.replace('\n', " "), 400)));
        }
        let mut files: Vec<String> = ex.iter().flat_map(|e| serde_json::from_str::<Vec<String>>(&e.2).unwrap_or_default()).collect();
        files.sort_unstable();
        files.dedup();
        if !files.is_empty() {
            b.push_str(&format!("\nFiles: {}\n", files.iter().take(15).cloned().collect::<Vec<_>>().join(", ")));
        }
        if let Some(last) = ex.last().filter(|e| !e.1.is_empty()) {
            let n = last.1.chars().count();
            let tail: String = last.1.chars().skip(n.saturating_sub(1200)).collect();
            b.push_str(&format!("\nThe last reply ended with:\n\n> {}\n", tail.trim().replace('\n', "\n> ")));
        }
        b
    }

    /// A thought as Markdown for an agent that picks it up: each part in time order, with its prompts and where its
    /// last reply ended. The oldest parts lose their details first when the text is too long.
    pub fn thought_brief(&self, id: &str) -> Option<String> {
        let t = self.thought(id)?;
        let parts = t["parts"].as_array()?;
        let day = |v: &Value| cut(v.as_str().unwrap_or(""), 10);
        let mut blocks: Vec<String> = Vec::new();
        let mut used = 0;
        for p in parts {
            let (sid, a, z) = (p["session"].as_str().unwrap_or(""), p["a"].as_i64().unwrap_or(0), p["z"].as_i64().unwrap_or(0));
            let mut b = format!("## {} · {} · {}\n\nSession `{sid}`, exchanges {a} to {}.\n", day(&p["updated"]),
                                p["title"].as_str().filter(|s| !s.is_empty()).unwrap_or("Untitled session"), p["folder"].as_str().unwrap_or(""), z - 1);
            if used < BRIEF_CHARS {
                b.push_str(&self.part_detail(sid, a, z));
            }
            used += b.len();
            blocks.push(b);
        }
        blocks.reverse();
        Some(format!("# Thought: {}\n\n{} sessions, {} exchanges, from {} to {}. The parts are in time order.\n\n{}",
                     t["name"].as_str().unwrap_or(""), t["sessions"], t["exchanges"], day(&t["started"]), day(&t["updated"]), blocks.join("\n")))
    }

    /// The thought of the newest part of a session.
    pub fn session_thought(&self, sid: &str) -> Option<String> {
        self.db.read(|c| c.query_row("SELECT thought_id FROM thought_parts WHERE session_id=? ORDER BY a DESC LIMIT 1", [sid], |r| r.get(0)))
            .ok()
    }

    /// The card of the thought that holds the newest part of a session, with `resume` for that session.
    pub fn session_card(&self, sid: &str) -> Option<Value> {
        let mut card = self.thought_card(&self.session_thought(sid)?, None)?;
        let folder: String = self.db.read(|c| c.query_row("SELECT project FROM sessions WHERE id=?", [sid], |r| r.get(0))).ok()?;
        card["resume"] = json!(resume_command(sid, &folder));
        card["session_link"] = json!(format!("{SESSION_LINK}{sid}"));
        Some(card)
    }

    /// A whole session as Markdown for an agent that picks it up: each of its parts in time order, with the thought
    /// that the part belongs to. The oldest parts lose their details first when the text is too long.
    pub fn session_brief(&self, sid: &str) -> Option<String> {
        let (title, folder): (String, String) = self.db.read(|c| c.query_row(
            "SELECT COALESCE(title, ''), COALESCE(project, '') FROM sessions WHERE id=?", [sid], |r| Ok((r.get(0)?, r.get(1)?)))).ok()?;
        let parts: Vec<(String, i64, i64, String, String)> = self.db.read(|c| {
            let mut st = c.prepare("SELECT p.thought_id, p.a, p.z, COALESCE(p.updated, ''), COALESCE(t.name, t.auto_name, '') \
                                    FROM thought_parts p LEFT JOIN thoughts t ON t.id = p.thought_id WHERE p.session_id=? ORDER BY p.a DESC")?;
            let r = st.query_map([sid], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)))?.flatten().collect();
            Ok(r)
        }).unwrap_or_default();
        let mut blocks: Vec<String> = Vec::new();
        let mut used = 0;
        for (tid, a, z, updated, name) in &parts {
            let mut b = format!("## {} · {name} · thought {tid}\n\nExchanges {a} to {}.\n", cut(updated, 10), z - 1);
            if used < BRIEF_CHARS {
                b.push_str(&self.part_detail(sid, *a, *z));
            }
            used += b.len();
            blocks.push(b);
        }
        blocks.reverse();
        let resume = resume_command(sid, &folder).map(|c| format!(" The user can resume the session itself in a terminal with `{c}`."))
            .unwrap_or_default();
        Some(format!("# Session: {}\n\nSession `{sid}` in {folder}. It has {} parts, in time order.{resume}\n\n{}",
                     if title.is_empty() { "Untitled session" } else { &title }, parts.len(), blocks.join("\n")))
    }

    pub fn thought_count(&self) -> i64 {
        self.db.read(|c| c.query_row("SELECT COUNT(*) FROM thoughts", [], |r| r.get(0))).unwrap_or(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing;

    fn session(id: &str, title: &str, turns: &[(&str, &str)]) -> Session {
        Session {
            id: id.into(), source: "claude".into(), title: title.into(), project: "/work/app".into(),
            turns: turns.iter().map(|(r, t)| (r.to_string(), t.to_string())).collect(),
            turn_ts: (0..turns.len()).map(|i| format!("2026-01-0{}T00:0{i}:00Z", id.len() % 9 + 1)).collect(),
            ..Default::default()
        }
    }

    fn add(ix: &Index, s: &Session) {
        ix.db.tx(|t| t.execute("INSERT INTO sessions(id, source, project, project_name, title, n_chunks) VALUES(?,?,?,?,?,1)",
                               params![s.id, s.source, s.project, "app", s.title])).unwrap();
        ix.store_exchanges(s).unwrap();
    }

    const PARSER: [(&str, &str); 4] = [
        ("user", "the parser drops tokens when the lexer grammar has nested rules, fix the parser"),
        ("assistant", "the parser lexer grammar tokens are fixed for nested rules"),
        ("user", "now add a test for the parser grammar with nested lexer tokens please"),
        ("assistant", "added the parser grammar test for nested lexer tokens"),
    ];
    const DEPLOY: [(&str, &str); 4] = [
        ("user", "deploy the kubernetes cluster with the helm chart and set the ingress replicas"),
        ("assistant", "the helm chart deploys the kubernetes cluster with ingress replicas"),
        ("user", "the kubernetes ingress of the helm chart fails on the cluster, read the logs"),
        ("assistant", "the kubernetes ingress logs show the helm chart error on the cluster"),
    ];

    #[test]
    fn exchanges_skip_the_compaction_summary_and_keep_files() {
        let mut s = session("claude:aaaaaaaa", "", &[("user", "fix the login bug"), ("assistant", "done"),
            ("user", "This session is being continued from a previous conversation that ran out of context."), ("assistant", "ok")]);
        s.turn_files = vec![(0, "/work/app/login.py".into()), (1, "/work/app/login.py".into())];
        let ex = exchanges_of(&s);
        assert_eq!(ex.len(), 1);
        assert_eq!(ex[0].files, vec!["/work/app/login.py".to_string()]);
        assert_eq!(ex[0].reply, "done");
    }

    #[test]
    fn split_cuts_where_the_topic_changes() {
        let a = vec![1.0, 0.0];
        let b = vec![0.0, 1.0];
        let x = vec![a.clone(), a.clone(), a.clone(), b.clone(), b.clone()];
        assert_eq!(split(&x, &[false; 5], 0.5), vec![3]);
        assert_eq!(split(&x, &[false, false, true, true, false], 0.5), Vec::<usize>::new());
        assert_eq!(split(&x, &[false; 5], 10.0), Vec::<usize>::new());
    }

    #[test]
    fn join_groups_the_near_items() {
        let mut sim = vec![0.0, 0.9, 0.1, 0.9, 0.0, 0.2, 0.1, 0.2, 0.0];
        let l = join(&mut sim, 3, 0.5);
        assert_eq!(l[0], l[1]);
        assert_ne!(l[0], l[2]);
    }

    #[test]
    fn thoughts_join_sessions_keep_names_and_answer_a_search() {
        let ix = testing::index();
        let mut long: Vec<(&str, &str)> = PARSER.to_vec();
        long.extend(DEPLOY);
        add(&ix, &session("claude:aaaaaaaa", "Parser and deploy", &long));
        add(&ix, &session("claude:bbbbbbbbb", "Parser grammar", &PARSER));
        let (n, left) = ix.thoughts_rebuild(60.0, &|_| {});
        assert!(!left);
        assert_eq!(n, 2, "{}", ix.thoughts_list("", 10));
        let list = ix.thoughts_list("", 10);
        let parser = list["thoughts"].as_array().unwrap().iter().find(|t| t["sessions"] == 2).expect("a thought of two sessions").clone();
        let id = parser["id"].as_str().unwrap();
        assert_eq!(parser["exchanges"], 4);

        let found = ix.thoughts_list("kubernetes helm chart", 10);
        assert_ne!(found["thoughts"][0]["id"], parser["id"]);
        assert_eq!(found["thoughts"][0]["match"]["session"], "claude:aaaaaaaa");

        ix.thought_rename(id, "Nested grammar fix").unwrap();
        ix.thoughts_rebuild(60.0, &|_| {});
        let again = ix.thought(id).unwrap();
        assert_eq!(again["name"], "Nested grammar fix");
        assert_eq!(again["named"], true);
        assert_eq!(again["parts"][0]["prompts"].as_array().unwrap().len(), 2);

        let cards = ix.thought_cards("", 10);
        let card = cards["thoughts"].as_array().unwrap().iter().find(|c| c["id"] == id).expect("a card for the thought");
        assert_eq!(card["name"], "Nested grammar fix");
        assert_eq!(card["session_titles"].as_array().unwrap().len(), 2);
        assert!(card["asked"].as_str().is_some_and(|a| !a.is_empty()));

        let brief = ix.thought_brief(id).unwrap();
        assert!(brief.starts_with("# Thought: Nested grammar fix"));
        assert!(brief.contains("added the parser grammar test"));
    }

    #[test]
    fn a_title_names_only_the_main_work_of_its_session() {
        let ix = testing::index();
        let mut long: Vec<(&str, &str)> = PARSER.to_vec();
        long.extend(PARSER);
        long.extend(DEPLOY);
        add(&ix, &session("claude:aaaaaaaa", "Parser grammar", &long));
        ix.thoughts_rebuild(60.0, &|_| {});
        let list = ix.thoughts_list("", 10);
        let names: Vec<&str> = list["thoughts"].as_array().unwrap().iter().filter_map(|t| t["name"].as_str()).collect();
        assert!(names.contains(&"Parser grammar"), "{names:?}");
        assert!(names.contains(&"Deploy the kubernetes cluster with the helm chart and set the…"), "{names:?}");
    }

    #[test]
    fn names_give_ticket_keys_of_known_trackers_only() {
        let trackers: HashSet<&str> = ["ABC"].into();
        assert_eq!(name_keys("abc-123-release-build", &trackers), vec!["ABC-123"]);
        assert_eq!(name_keys("sam/ABC-12-fix", &trackers), vec!["ABC-12"]);
        assert_eq!(name_keys("app-abc-123", &trackers), vec!["ABC-123"]);
        assert!(name_keys("python-3-upgrade", &trackers).is_empty());
        assert!(name_keys("xabc-123", &trackers).is_empty());
    }

    #[test]
    fn prompt_names_drop_the_opening_words_and_stop_at_a_sentence() {
        assert_eq!(prompt_name("ok, can you fix the login bug on the phone app. Then deploy."), "Fix the login bug on the phone app");
        assert_eq!(prompt_name("build a pr. was the table there for a reason? yes."), "Build a pr. was the table there for a reason?");
        assert_eq!(prompt_name("❯ lets rename the <b>parser</b> module"), "Rename the  parser  module");
        assert_eq!(prompt_name("[Image #1]\n\nwhy is the header cut off?"), "Why is the header cut off?");
        assert_eq!(prompt_name("FAILED tests/test_a.py::test_b - KeyError\nwhy does this test fail now?"), "Why does this test fail now?");
    }

    #[test]
    fn exchanges_keep_the_branch_of_their_prompt() {
        let mut s = session("claude:aaaaaaaa", "", &[("user", "fix the login bug"), ("assistant", "done"), ("user", "now the logout"), ("assistant", "ok")]);
        s.turn_branch = vec![(0, "main".into()), (2, "fix-logout".into()), (3, "fix-logout".into())];
        let ex = exchanges_of(&s);
        assert_eq!((ex[0].branch.as_str(), ex[1].branch.as_str()), ("main", "fix-logout"));
    }

    #[test]
    fn links_find_pull_requests_tickets_and_files() {
        let e = |p: &str, r: &str, f: &str| (p.to_string(), r.to_string(), f.to_string());
        let ex = [
            e("fix ABC-12 with UTF-8 names", "Opened https://github.com/acme/app/pull/7 for ABC-12.", r#"["/home/u/app/a.rs","/tmp/b.rs","/srv/c.rs"]"#),
            e("is ABC-12 done?", "Yes: https://github.com/acme/app/pull/7 merged. https://acme.atlassian.net/browse/QQ-3 is like ZZ-1.", r#"["/home/u/app/a.rs"]"#),
            e("and the branch?", "It is for EF-5, and ZZ-1 is an example. GH-8 is next, after XY-9.", "[]"),
            e("go on", "GH-8 is open.", "[]"),
        ];
        let names = ["ef-5-fix-names".to_string()];
        let branches = ["ef-5-fix-names".to_string(), "zz-1-other-work".to_string()];
        let got: HashMap<&str, Value> = links(&ex, &names, &branches, "/home/u").into_iter().collect();
        assert_eq!(got["pull_requests"], json!([{"url": "https://github.com/acme/app/pull/7", "repo": "acme/app", "number": 7}]));
        assert_eq!(got["tickets"], json!([{"key": "ABC-12", "url": null}, {"key": "GH-8", "url": null},
                                          {"key": "QQ-3", "url": "https://acme.atlassian.net/browse/QQ-3"}, {"key": "EF-5", "url": null}]));
        assert_eq!(got["files"], json!(["~/app/a.rs", "/srv/c.rs"]));
    }

    #[test]
    fn resume_needs_a_folder_and_a_plain_id() {
        let here = std::env::temp_dir().to_string_lossy().into_owned();
        assert_eq!(resume_command("claude:0123abcd-ef", &here), Some(format!("cd '{here}' && claude --resume 0123abcd-ef")));
        assert_eq!(resume_command("claude:0123abcd; rm", &here), None);
        assert_eq!(resume_command("claude:0123abcd-ef", "/no/such/folder"), None);
        assert_eq!(resume_command("qwen:0123abcd-ef", &here), None);
    }

    #[test]
    fn link_finds_the_thought_or_session_in_pasted_text() {
        assert_eq!(link("pensieve://thought/th_ab12"), Some(Link::Thought("th_ab12".into())));
        assert_eq!(link("continue `pensieve://thought/th_ab12`."), Some(Link::Thought("th_ab12".into())));
        assert_eq!(link("pensieve://session/claude:1f2e-33"), Some(Link::Session("claude:1f2e-33".into())));
        assert_eq!(link("pensieve://thought/x"), None);
        assert_eq!(link("pensieve://session/abc"), None);
        assert_eq!(link("th_ab12"), None);
    }

    #[test]
    fn start_gives_the_agent_the_thought_and_quotes_the_name() {
        let here = std::env::temp_dir().to_string_lossy().into_owned();
        let got = start_command("codex:rollout-1", &here, "th_1", "Fix the user's login").unwrap_or_default();
        assert!(got.starts_with(&format!("cd '{here}' && codex 'Continue my earlier work \"Fix the user'\\''s login\".")), "{got}");
        assert!(got.contains("the thought th_1."));
        assert!(start_command("claude:1", "/no/such/folder", "th_1", "x").is_some_and(|c| c.starts_with("claude '")));
        assert_eq!(start_command("qwen:1", &here, "th_1", "x"), None);
    }
}
