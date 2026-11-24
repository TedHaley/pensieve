//! `watch`: a full-screen list of the newest thoughts that refreshes itself. A click or a key opens a thought in a
//! new tab: it resumes the thought's newest session, or forks a new session that reads the thought.

use std::io::{Read, Write};
use std::sync::mpsc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{Value, json};

const REFRESH: Duration = Duration::from_secs(15);
const LIMIT: u64 = 20;
/// What a click or a key does to a thought.
#[derive(Debug, PartialEq, Clone, Copy)]
enum Act {
    Resume,
    Fork,
    CopyLink,
    CopySession,
}

/// The buttons at the right end of each thought's first line.
const BUTTONS: [(&str, Act); 3] = [(" [fork]", Act::Fork), (" [link]", Act::CopyLink), (" [session] ", Act::CopySession)];

/// Raw input, mouse reports, an alternate screen, no line wrap and no cursor while the value lives. Drop gives the
/// terminal back as it was.
struct Screen {
    saved: libc::termios,
}

impl Screen {
    fn enter() -> Result<Self, String> {
        // SAFETY: termios is plain data, and tcgetattr fills it
        let mut saved: libc::termios = unsafe { std::mem::zeroed() };
        // SAFETY: fd 0 and a valid pointer
        if unsafe { libc::tcgetattr(0, &mut saved) } != 0 {
            return Err("`pensieve watch` needs a terminal".into());
        }
        let mut raw = saved;
        raw.c_lflag &= !(libc::ICANON | libc::ECHO | libc::ISIG | libc::IEXTEN);
        raw.c_iflag &= !(libc::IXON | libc::ICRNL);
        raw.c_cc[libc::VMIN] = 0;
        raw.c_cc[libc::VTIME] = 0;
        // SAFETY: fd 0 and a valid pointer
        unsafe { libc::tcsetattr(0, libc::TCSANOW, &raw) };
        print!("\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[?1000h\x1b[?1006h");
        let _ = std::io::stdout().flush();
        Ok(Screen { saved })
    }
}

impl Drop for Screen {
    fn drop(&mut self) {
        print!("\x1b[?1006l\x1b[?1000l\x1b[?7h\x1b[?25h\x1b[?1049l");
        let _ = std::io::stdout().flush();
        // SAFETY: fd 0 and the settings that enter read
        unsafe { libc::tcsetattr(0, libc::TCSANOW, &self.saved) };
    }
}

/// Columns and rows of the terminal.
fn size() -> (usize, usize) {
    // SAFETY: winsize is plain data, and the ioctl fills it
    let mut w: libc::winsize = unsafe { std::mem::zeroed() };
    // SAFETY: fd 1 and a valid pointer
    if unsafe { libc::ioctl(1, libc::TIOCGWINSZ, &mut w) } == 0 && w.ws_col > 0 {
        (w.ws_col as usize, w.ws_row as usize)
    } else {
        (80, 24)
    }
}

/// The bytes that stdin has after a wait of at most `wait`.
fn input(wait: Duration) -> Vec<u8> {
    let mut fd = libc::pollfd { fd: 0, events: libc::POLLIN, revents: 0 };
    // SAFETY: one valid pollfd
    if unsafe { libc::poll(&mut fd, 1, wait.as_millis() as i32) } <= 0 {
        return Vec::new();
    }
    let mut buf = [0u8; 256];
    let n = std::io::stdin().read(&mut buf).unwrap_or(0);
    buf[..n].to_vec()
}

#[derive(Debug, PartialEq)]
enum Event {
    Open(usize),
    Up,
    Down,
    Enter,
    Do(Act),
    Refresh,
    Quit,
    /// A press of the left button, at 1-based column and row.
    Click(usize, usize),
}

/// The events in one read of stdin. A read can hold more than one: a mouse press and its release, or keys typed fast.
fn events(bytes: &[u8]) -> Vec<Event> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        let rest = &bytes[i..];
        // SGR mouse report: ESC [ < button ; column ; row, then M for a press or m for a release.
        if let Some(body) = rest.strip_prefix(b"\x1b[<") {
            let Some(end) = body.iter().position(|b| *b == b'M' || *b == b'm') else { break };
            let n: Vec<usize> = std::str::from_utf8(&body[..end]).unwrap_or("").split(';').filter_map(|s| s.parse().ok()).collect();
            if body[end] == b'M' && n.len() == 3 {
                match n[0] {
                    0 => out.push(Event::Click(n[1], n[2])),
                    64 => out.push(Event::Up),
                    65 => out.push(Event::Down),
                    _ => {}
                }
            }
            i += 3 + end + 1;
            continue;
        }
        let arrow = |c: u8| rest.starts_with(&[0x1b, b'[', c]) || rest.starts_with(&[0x1b, b'O', c]);
        if arrow(b'A') || arrow(b'B') {
            out.push(if arrow(b'A') { Event::Up } else { Event::Down });
            i += 3;
            continue;
        }
        match rest[0] {
            0x1b if rest.len() == 1 => out.push(Event::Quit),
            b'q' | 3 => out.push(Event::Quit),
            d @ b'1'..=b'9' => out.push(Event::Open((d - b'1') as usize)),
            b'k' => out.push(Event::Up),
            b'j' => out.push(Event::Down),
            b'\r' | b'\n' | b'o' => out.push(Event::Enter),
            b'f' | b'n' => out.push(Event::Do(Act::Fork)),
            b'c' => out.push(Event::Do(Act::CopyLink)),
            b's' => out.push(Event::Do(Act::CopySession)),
            b'r' => out.push(Event::Refresh),
            _ => {}
        }
        i += 1;
    }
    out
}

/// Seconds since 1970 of a time such as `2026-11-16T21:18:20.025Z`.
fn epoch(t: &str) -> Option<i64> {
    let n = |r: std::ops::Range<usize>| t.get(r)?.parse::<i64>().ok();
    let (y, m, d) = (n(0..4)?, n(5..7)?, n(8..10)?);
    let (hh, mm, ss) = (n(11..13)?, n(14..16)?, n(17..19)?);
    // Days from 1970-01-01, by the civil calendar of Howard Hinnant.
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    Some((era * 146_097 + doe - 719_468) * 86_400 + hh * 3600 + mm * 60 + ss)
}

fn ago(t: &str) -> String {
    let Some(then) = epoch(t) else { return String::new() };
    let now = SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_secs() as i64);
    match now - then {
        s if s < 60 => "now".into(),
        s if s < 3600 => format!("{}m ago", s / 60),
        s if s < 86_400 => format!("{}h ago", s / 3600),
        s => format!("{}d ago", s / 86_400),
    }
}

/// `s` cut to `width` characters, with … when cut, and with no line breaks.
fn fit(s: &str, width: usize) -> String {
    let s: String = s.chars().map(|c| if c.is_control() { ' ' } else { c }).collect();
    if s.chars().count() <= width {
        return s;
    }
    let mut out: String = s.chars().take(width.saturating_sub(1)).collect();
    out.push('…');
    out
}

fn text<'a>(c: &'a Value, k: &str) -> &'a str {
    c[k].as_str().unwrap_or("")
}

/// A part of the screen that a click acts on: thought `i` on `row`, from column `from` to column `to`.
struct Hit {
    row: usize,
    from: usize,
    to: usize,
    i: usize,
    act: Act,
}

struct View {
    cards: Vec<Value>,
    selected: usize,
    fetched: Option<Instant>,
    error: Option<String>,
    note: String,
    query: String,
    hits: Vec<Hit>,
}

impl View {
    fn draw(&mut self) {
        let (cols, rows) = size();
        self.hits.clear();
        let mut out = String::from("\x1b[H\x1b[2J");
        let mut row = 1;
        /// Add one screen line. The result is its 1-based row.
        fn line(out: &mut String, row: &mut usize, s: String) -> usize {
            *out += &s;
            *out += "\r\n";
            *row += 1;
            *row - 1
        }
        let title = if self.query.is_empty() { "Pensieve · your newest thoughts".to_string() }
                    else { format!("Pensieve · thoughts about \"{}\"", self.query) };
        let age = match self.fetched {
            Some(t) => format!("refreshed {}s ago", t.elapsed().as_secs()),
            None => "loading…".into(),
        };
        line(&mut out, &mut row, format!("\x1b[1m{}\x1b[0m\x1b[2m  {}\x1b[0m", fit(&title, cols.saturating_sub(age.len() + 2)), age));
        line(&mut out, &mut row, String::new());
        if let Some(e) = &self.error {
            line(&mut out, &mut row, format!("\x1b[31m{}\x1b[0m", fit(e, cols)));
        } else if self.cards.is_empty() && self.fetched.is_some() {
            line(&mut out, &mut row, "No thought matches.".into());
        }
        // Two lines a thought. The keys and the note take the last 3 rows.
        let width: usize = BUTTONS.iter().map(|b| b.0.len()).sum();
        let buttons_at = cols.saturating_sub(width) + 1;
        for (i, c) in self.cards.iter().enumerate() {
            if row + 1 > rows.saturating_sub(3) {
                break;
            }
            let sel = i == self.selected;
            let sessions = c["sessions"].as_i64().unwrap_or(0);
            let mut facts = vec![];
            if !text(c, "project").is_empty() {
                facts.push(text(c, "project").to_string());
            }
            facts.push(format!("{sessions} session{}", if sessions == 1 { "" } else { "s" }));
            facts.push(ago(text(c, "updated")));
            let facts = format!(" · {}", facts.join(" · "));
            let lead = format!("{} {:>2}. ", if sel { "›" } else { " " }, i + 1);
            let room = cols.saturating_sub(lead.chars().count() + width);
            let name = fit(text(c, "name"), room.saturating_sub(facts.chars().count()).max(10));
            let facts = fit(&facts, room.saturating_sub(name.chars().count()));
            let style = if sel { "\x1b[1;7m" } else { "\x1b[1m" };
            let labels: String = BUTTONS.iter().map(|b| b.0).collect();
            let head = format!("{lead}{style}{name}\x1b[0m\x1b[2m{facts}\x1b[0m\x1b[{row};{buttons_at}H\x1b[36m{labels}\x1b[0m");
            let first = line(&mut out, &mut row, head);
            let mut at = buttons_at;
            for (label, act) in BUTTONS {
                // the leading space of a label is a gap, not part of the button
                self.hits.push(Hit { row: first, from: at + 1, to: at + label.trim_end().len() - 1, i, act });
                at += label.len();
            }
            let left = [text(c, "left_off"), text(c, "ended_with"), text(c, "asked")].into_iter()
                .find(|s| s.chars().count() > 12).unwrap_or("");
            let second = line(&mut out, &mut row, format!("\x1b[2m      {}\x1b[0m", fit(left, cols.saturating_sub(6))));
            self.hits.push(Hit { row: first, from: 1, to: buttons_at.saturating_sub(1), i, act: Act::Resume });
            self.hits.push(Hit { row: second, from: 1, to: cols, i, act: Act::Resume });
        }
        out += &format!("\x1b[{};1H\x1b[2m{}\x1b[0m\r\n", rows.saturating_sub(1), fit(
            "click or 1-9: resume · [fork] f: new session · [link] c, [session] s: copy a link to paste into an agent · r: refresh · q: quit", cols));
        out += &fit(&self.note, cols);
        print!("{out}");
        let _ = std::io::stdout().flush();
    }

    fn click(&mut self, col: usize, row: usize) {
        if let Some(h) = self.hits.iter().find(|h| h.row == row && (h.from..=h.to).contains(&col)) {
            let (i, act) = (h.i, h.act);
            self.act(i, act);
        }
    }

    fn act(&mut self, i: usize, act: Act) {
        match act {
            Act::Resume => self.open(i, false),
            Act::Fork => self.open(i, true),
            Act::CopyLink | Act::CopySession => self.copy(i, if act == Act::CopyLink { "link" } else { "session_link" }),
        }
    }

    fn copy(&mut self, i: usize, key: &str) {
        let Some(c) = self.cards.get(i) else { return };
        self.selected = i;
        let link = text(c, key).to_string();
        if link.is_empty() {
            self.note = "This thought has no session link.".into();
            return;
        }
        let copied = std::process::Command::new("pbcopy").stdin(std::process::Stdio::piped()).spawn().and_then(|mut p| {
            p.stdin.take().map(|mut s| s.write_all(link.as_bytes())).transpose()?;
            p.wait()
        });
        self.note = match copied {
            Ok(s) if s.success() => format!("Copied {link}. Paste it into an agent session to continue there."),
            _ => format!("The clipboard is not available. The link is {link}"),
        };
    }

    /// Open a thought as `pensieve open` does: resume its newest session, or with `fork`, start a new session that
    /// reads the thought.
    fn open(&mut self, i: usize, fork: bool) {
        let Some(c) = self.cards.get(i) else { return };
        self.selected = i;
        let name = text(c, "name");
        let command = if fork { c["start"].as_str() } else { c["resume"].as_str().or(c["start"].as_str()) };
        let how = if fork || c["resume"].is_null() { "a new session of" } else { "the last session of" };
        self.note = match command {
            None => format!("\"{}\" has no agent command: its sessions are not from Claude Code or Codex.", fit(name, 40)),
            Some(command) => match crate::open::tab(command, &name.chars().take(40).collect::<String>()) {
                Ok(app) => format!("Opened {how} \"{}\" in a new {app} tab.", fit(name, 40)),
                Err(e) => format!("No tab opened: {e}. Run: {command}"),
            },
        };
    }
}

/// Show the newest thoughts, or those about `query`, until the user quits.
pub fn run(port: u16, query: &str) -> bool {
    let (tx, rx) = mpsc::channel::<Result<Vec<Value>, String>>();
    let (again, wake) = mpsc::channel::<()>();
    let q = query.to_string();
    std::thread::spawn(move || loop {
        let answer = crate::tool(port, "thoughts", json!({"query": q, "limit": LIMIT})).map(|a| {
            crate::keep_listed(&a);
            serde_json::from_str::<Value>(&a).ok().and_then(|v| v["thoughts"].as_array().cloned()).unwrap_or_default()
        });
        if tx.send(answer).is_err() {
            return;
        }
        if let Err(mpsc::RecvTimeoutError::Disconnected) = wake.recv_timeout(REFRESH) {
            return;
        }
    });
    let screen = match Screen::enter() {
        Ok(s) => s,
        Err(e) => {
            eprintln!("{e}");
            return false;
        }
    };
    let mut v = View { cards: vec![], selected: 0, fetched: None, error: None, note: String::new(), query: query.to_string(), hits: vec![] };
    let mut shown = (size(), u64::MAX);
    'outer: loop {
        let mut dirty = false;
        while let Ok(answer) = rx.try_recv() {
            let keep = v.cards.get(v.selected).map(|c| text(c, "id").to_string());
            match answer {
                Ok(cards) => {
                    v.selected = keep.and_then(|id| cards.iter().position(|c| text(c, "id") == id)).unwrap_or(0);
                    v.cards = cards;
                    v.error = None;
                }
                Err(e) => v.error = Some(e),
            }
            v.fetched = Some(Instant::now());
            if v.note == "Refreshing…" {
                v.note.clear();
            }
            dirty = true;
        }
        for e in events(&input(Duration::from_millis(250))) {
            dirty = true;
            match e {
                Event::Quit => break 'outer,
                Event::Open(i) => v.open(i, false),
                Event::Enter => v.open(v.selected, false),
                Event::Do(act) => v.act(v.selected, act),
                Event::Click(col, row) => v.click(col, row),
                Event::Up => v.selected = v.selected.saturating_sub(1),
                Event::Down => v.selected = (v.selected + 1).min(v.cards.len().saturating_sub(1)),
                Event::Refresh => {
                    let _ = again.send(());
                    v.note = "Refreshing…".into();
                }
            }
        }
        let now = (size(), v.fetched.map_or(u64::MAX, |t| t.elapsed().as_secs()));
        if dirty || now != shown {
            v.draw();
            shown = now;
        }
    }
    drop(screen);
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn epoch_reads_utc_times() {
        assert_eq!(epoch("1970-01-01T00:00:00Z"), Some(0));
        assert_eq!(epoch("2026-11-16T21:18:20.025Z"), Some(1_794_863_900));
        assert_eq!(epoch("2024-02-29T12:00:00Z"), Some(1_709_208_000));
        assert_eq!(epoch("bad"), None);
    }

    #[test]
    fn input_maps_to_events() {
        assert_eq!(events(b"3"), vec![Event::Open(2)]);
        assert_eq!(events(b"\x1b[A"), vec![Event::Up]);
        assert_eq!(events(b"\x1b"), vec![Event::Quit]);
        assert_eq!(events(b"\r"), vec![Event::Enter]);
        assert_eq!(events(b"0"), vec![]);
        assert_eq!(events(b"\x1b[<0;12;5M\x1b[<0;12;5m"), vec![Event::Click(12, 5)]);
        assert_eq!(events(b"\x1b[<65;1;1Mf"), vec![Event::Down, Event::Do(Act::Fork)]);
        assert_eq!(events(b"cs"), vec![Event::Do(Act::CopyLink), Event::Do(Act::CopySession)]);
    }

    #[test]
    fn fit_cuts_and_flattens() {
        assert_eq!(fit("a\nb", 10), "a b");
        assert_eq!(fit("abcdef", 4), "abc…");
    }
}
