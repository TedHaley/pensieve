//! What the code index asks git, through the `git` CLI, and the pure helpers that cut files into chunks.

use std::collections::{BTreeMap, HashMap};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

use regex::Regex;
use std::sync::LazyLock;

pub const SKIP_EXT: &[&str] = &[".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".zip", ".gz", ".lock",
    ".woff", ".woff2", ".ttf", ".mp4", ".mov", ".parquet", ".pkl", ".csv", ".ipynb", ".map", ".snap"];
pub const SKIP_NAMES: &[&str] = &["package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock", "Cargo.lock"];
pub const SOURCE_EXT: &[&str] = &[".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".rb", ".go", ".rs", ".java",
    ".kt", ".swift", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".php", ".scala", ".tf", ".sh", ".vue", ".svelte", ".css",
    ".scss", ".m", ".mm"];
pub const READ_IMAGE_EXT: &[&str] = &[".png", ".jpg", ".jpeg", ".gif", ".heic", ".heif", ".webp", ".tif", ".tiff", ".bmp"];
pub const GENERATED_DIRS: &[&str] = &["dist", "build", "vendor", "third_party", "node_modules", "out", "target", ".venv"];
/// Bigger files are indexed only when they look written by hand (`written_source`).
pub const MAX_BYTES: usize = 200_000;
pub const MAX_SOURCE_BYTES: usize = 2_000_000;
pub const IMAGE_MAX_BYTES: u64 = 50_000_000;
pub const CODE_CHUNK_CHARS: usize = 1500;

static DEPLOY_SKIP: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|uv\.lock|poetry\.lock|Gemfile\.lock|Cargo\.lock|composer\.lock|go\.sum)$|\.min\.(js|css)$|\.map$")
        .expect("regex")
});

/// stdout of `git -C root args`, empty when git fails.
pub fn git(root: &str, args: &[&str]) -> String {
    Command::new("git").arg("-C").arg(root).args(args).stdin(Stdio::null()).output()
        .map(|o| String::from_utf8_lossy(&o.stdout).into_owned()).unwrap_or_default()
}

/// git that never waits for a password or a passphrase, killed after `timeout`. Returns (ok, stdout, stderr).
pub fn quiet_git(root: &str, args: &[&str], timeout: Duration) -> (bool, String, String) {
    let child = Command::new("git").arg("-C").arg(root).args(args)
        .env("GIT_TERMINAL_PROMPT", "0").env("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=10")
        .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn();
    let Ok(child) = child else { return (false, String::new(), String::new()) };
    wait_for(child, timeout)
}

fn wait_for(mut child: std::process::Child, timeout: Duration) -> (bool, String, String) {
    let start = std::time::Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if start.elapsed() < timeout => std::thread::sleep(Duration::from_millis(20)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return (false, String::new(), "git timed out".into());
            }
        }
    }
    match child.wait_with_output() {
        Ok(o) => (o.status.success(), String::from_utf8_lossy(&o.stdout).into_owned(), String::from_utf8_lossy(&o.stderr).into_owned()),
        Err(_) => (false, String::new(), String::new()),
    }
}

/// Python's `str.strip() == ""` (its whitespace includes the file separators \x1c-\x1f).
pub fn blank(s: &str) -> bool {
    s.chars().all(|c| c.is_whitespace() || ('\x1c'..='\x1f').contains(&c))
}

/// The extension of a file name, lowercased, as Python's `Path.suffix` gives it (`.gitignore` has none).
pub fn suffix(path: &str) -> String {
    let name = path.rsplit('/').next().unwrap_or(path).trim_start_matches('.');
    match name.rfind('.') {
        Some(i) if i > 0 => name[i..].to_lowercase(),
        _ => String::new(),
    }
}

pub fn file_name(path: &str) -> &str {
    path.trim_end_matches('/').rsplit('/').next().unwrap_or(path)
}

/// A big file worth indexing: source code with lines of normal length. Generated and minified files have long
/// lines; data files have no source extension.
pub fn written_source(raw: &[u8], path: &str) -> bool {
    if raw.len() > MAX_SOURCE_BYTES || !SOURCE_EXT.contains(&suffix(path).as_str()) {
        return false;
    }
    let lines: Vec<&[u8]> = raw.split(|b| *b == b'\n').collect();
    raw.len() as f64 / lines.len() as f64 <= 120.0
        && lines.iter().filter(|l| l.len() > 500).count() as f64 <= 0.01 * lines.len() as f64
}

/// Line-based chunks (first line, last line, text), 1-based, breaking at CODE_CHUNK_CHARS or at a blank line after
/// 700 characters.
pub fn chunk_code(text: &str) -> Vec<(i64, i64, String)> {
    let lines: Vec<&str> = text.split('\n').collect();
    let (mut out, mut start, mut size) = (Vec::new(), 0usize, 0usize);
    for (i, ln) in lines.iter().enumerate() {
        size += ln.chars().count() + 1;
        if size >= CODE_CHUNK_CHARS || (size > 700 && blank(ln)) {
            out.push((start as i64 + 1, i as i64 + 1, lines[start..=i].join("\n")));
            start = i + 1;
            size = 0;
        }
    }
    if start < lines.len() {
        let rest = lines[start..].join("\n");
        if !blank(&rest) {
            out.push((start as i64 + 1, lines.len() as i64, rest));
        }
    }
    out.retain(|c| !blank(&c.2));
    out
}

/// Python's fnmatch on a file name, ignoring case: `*`, `?` and `[...]`.
pub fn fnmatch(name: &str, pat: &str) -> bool {
    fn go(n: &[char], p: &[char]) -> bool {
        match p.first() {
            None => n.is_empty(),
            Some('*') => (0..=n.len()).any(|i| go(&n[i..], &p[1..])),
            Some('?') => !n.is_empty() && go(&n[1..], &p[1..]),
            Some('[') => {
                let Some(close) = p.iter().skip(2).position(|c| *c == ']').map(|i| i + 2) else {
                    return n.first() == Some(&'[') && go(&n[1..], &p[1..]);
                };
                let Some(&c) = n.first() else { return false };
                let set = &p[1..close];
                let (neg, set) = if set.first() == Some(&'!') { (true, &set[1..]) } else { (false, set) };
                let mut hit = false;
                let mut i = 0;
                while i < set.len() {
                    if i + 2 < set.len() && set[i + 1] == '-' {
                        hit |= set[i] <= c && c <= set[i + 2];
                        i += 3;
                    } else {
                        hit |= set[i] == c;
                        i += 1;
                    }
                }
                hit != neg && go(&n[1..], &p[close + 1..])
            }
            Some(&x) => n.first() == Some(&x) && go(&n[1..], &p[1..]),
        }
    }
    let n: Vec<char> = name.to_lowercase().chars().collect();
    let p: Vec<char> = pat.to_lowercase().chars().collect();
    go(&n, &p)
}

/// A file name that matches one of the `exclude_files` patterns (secrets, keys).
pub fn secret(name: &str, patterns: &[String]) -> bool {
    patterns.iter().any(|p| fnmatch(name, p))
}

pub fn is_bot(author: &str) -> bool {
    let a = author.to_lowercase();
    a.contains("[bot]") || a.starts_with("dependabot") || a.starts_with("renovate") || a.starts_with("github-actions")
}

/// Two-level area of a repo-relative path: `src/billing/x.py` -> `src/billing`.
pub fn area_of(path: &str) -> String {
    let parts: Vec<&str> = path.split('/').collect();
    if parts.len() > 2 { parts[..2].join("/") } else if parts.len() > 1 { parts[0].to_string() } else { ".".into() }
}

pub fn norm_remote(url: &str) -> String {
    static PRE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(git@|https?://|ssh://git@)").expect("regex"));
    let u = PRE.replace(url.trim(), "").replace(':', "/");
    u.strip_suffix(".git").unwrap_or(&u).to_lowercase()
}

/// The repo root of a folder; linked git worktrees and `.claude/worktrees` checkouts fold into the main repo. The
/// walk up stops at `home`.
pub fn project_root(cwd: &str, home: &Path) -> Option<String> {
    let mut p = PathBuf::from(if cwd.is_empty() { "/nonexistent" } else { cwd });
    let parts: Vec<String> = p.components().map(|c| c.as_os_str().to_string_lossy().into_owned()).collect();
    if let (Some(i), true) = (parts.iter().position(|x| x == ".claude"), parts.iter().any(|x| x == "worktrees")) {
        p = parts[..i].iter().collect();
    }
    let mut q: Option<&Path> = Some(p.as_path());
    while let Some(d) = q {
        let g = d.join(".git");
        if g.is_file() {
            if let Ok(t) = std::fs::read_to_string(&g) {
                if let Some(gd) = t.split("gitdir:").nth(1) {
                    let gd = PathBuf::from(gd.trim());
                    let comps: Vec<_> = gd.components().collect();
                    if let Some(i) = comps.iter().position(|c| c.as_os_str() == "worktrees") {
                        let main: PathBuf = comps[..i].iter().collect();
                        return main.parent().map(|x| x.to_string_lossy().into_owned());
                    }
                }
            }
            return Some(d.to_string_lossy().into_owned());
        }
        if g.exists() {
            return Some(d.to_string_lossy().into_owned());
        }
        if d == home {
            break;
        }
        q = d.parent();
    }
    None
}

/// Git repo roots at or below `base`, not going into a repo once found, nor into hidden or excluded folders, at
/// most `depth` levels down.
pub fn find_repos(base: &Path, depth: usize, exclude: &[String]) -> Vec<String> {
    if !base.is_dir() {
        return Vec::new();
    }
    let canon = |p: &Path| std::fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf()).to_string_lossy().into_owned();
    if base.join(".git").exists() {
        return vec![canon(base)];
    }
    let mut out = Vec::new();
    let mut stack = vec![(base.to_path_buf(), 0usize)];
    while let Some((d, level)) = stack.pop() {
        let Ok(rd) = std::fs::read_dir(&d) else { continue };
        let mut dirs = Vec::new();
        let mut git_here = false;
        for e in rd.flatten() {
            let name = e.file_name().to_string_lossy().into_owned();
            if name == ".git" {
                git_here = true;
                break;
            }
            if e.file_type().map(|t| t.is_dir()).unwrap_or(false) {
                dirs.push(name);
            }
        }
        if git_here {
            out.push(canon(&d));
            continue;
        }
        if level < depth {
            dirs.sort();
            for name in dirs.into_iter().rev() {
                if !name.starts_with('.') && !exclude.contains(&name) {
                    stack.push((d.join(name), level + 1));
                }
            }
        }
    }
    out
}

/// Repo-relative path -> blob hash of what is on disk now: tracked files (modified ones hashed again) and untracked
/// files that are not ignored. Deleted files are left out.
pub fn tree_state(checkout: &str) -> BTreeMap<String, String> {
    static ENTRY: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\d+ (\w+) \d+\t(.+)").expect("regex"));
    let mut state = BTreeMap::new();
    for ln in git(checkout, &["ls-files", "-s", "-z"]).split('\0') {
        if let Some(m) = ENTRY.captures(ln) {
            state.insert(m[2].to_string(), m[1].to_string());
        }
    }
    let deleted: std::collections::HashSet<String> =
        git(checkout, &["ls-files", "-d", "-z"]).split('\0').filter(|x| !x.is_empty()).map(String::from).collect();
    let changed: Vec<String> = git(checkout, &["ls-files", "-m", "-o", "--exclude-standard", "-z"]).split('\0')
        .filter(|x| !x.is_empty() && !deleted.contains(*x) && Path::new(checkout).join(x).is_file()
            && !SKIP_EXT.contains(&suffix(x).as_str()))
        .take(5000).map(String::from).collect();
    for p in &deleted {
        state.remove(p);
    }
    if !changed.is_empty() {
        let child = Command::new("git").args(["-C", checkout, "hash-object", "--stdin-paths"])
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn();
        if let Ok(mut child) = child {
            if let Some(mut stdin) = child.stdin.take() {
                let _ = stdin.write_all(changed.join("\n").as_bytes());
            }
            if let Ok(o) = child.wait_with_output() {
                for (p, h) in changed.iter().zip(String::from_utf8_lossy(&o.stdout).split_whitespace()) {
                    state.insert(p.clone(), h.to_string());
                }
            }
        }
    }
    state
}

/// The signature of a tree state, as 0.21 stores it in `tree:<checkout>`.
pub fn sig(state: &BTreeMap<String, String>) -> String {
    let s: Vec<String> = state.iter().map(|(p, b)| format!("{p} {b}")).collect();
    crate::text::sha1_hex(&s.join("\n"))
}

/// (path, branch) of each checkout of the repo, from `git worktree list` (with `.claude/worktrees`).
pub fn worktree_list(root: &str) -> Vec<(String, String)> {
    let (mut out, mut path, mut branch): (Vec<(String, String)>, Option<String>, Option<String>) = (Vec::new(), None, None);
    let text = git(root, &["worktree", "list", "--porcelain"]);
    for ln in text.split('\n').chain(std::iter::once("")) {
        if let Some(p) = ln.strip_prefix("worktree ") {
            path = Some(p.to_string());
            branch = None;
        } else if let Some(b) = ln.strip_prefix("branch ") {
            branch = Some(b.strip_prefix("refs/heads/").unwrap_or(b).to_string());
        } else if ln.is_empty() {
            if let Some(p) = path.take() {
                out.push((p, branch.take().unwrap_or_else(|| "detached".into())));
            }
        }
    }
    out
}

/// The folder of a repo's shadow git dir (see `blame_shadow`).
pub fn shadow_dir(data: &Path, root: &str) -> PathBuf {
    data.join("git").join(format!("{}.git", &crate::text::sha1_hex(root)[..16]))
}

/// A bare repo in Pensieve's data that borrows `root`'s objects and keeps a commit-graph with changed-path filters,
/// so blame walks history much faster without writing into the user's repo. Returns (shadow git dir, HEAD of the
/// checkout), or None to blame the plain way.
pub fn blame_shadow(data: &Path, root: &str, checkout: &str) -> Option<(String, String)> {
    let head = git(checkout, &["rev-parse", "HEAD"]).trim().to_string();
    if head.len() != 40 {
        return None;
    }
    let d = shadow_dir(data, root);
    if !d.join("objects").exists() {
        let common = PathBuf::from(git(root, &["rev-parse", "--path-format=absolute", "--git-common-dir"]).trim());
        if !common.join("objects").is_dir() {
            return None;
        }
        let ok = Command::new("git").args(["init", "-q", "--bare"]).arg(&d).output().ok()?.status.success();
        if !ok {
            return None;
        }
        std::fs::write(d.join("objects/info/alternates"), format!("{}\n", common.join("objects").display())).ok()?;
    }
    let mark = d.join("pensieve-graph-head");
    let seen = std::fs::read_to_string(&mark).unwrap_or_default();
    if !seen.split_whitespace().any(|h| h == head) {
        let mut child = Command::new("git").arg("--git-dir").arg(&d)
            .args(["commit-graph", "write", "--stdin-commits", "--changed-paths", "--split"])
            .stdin(Stdio::piped()).stdout(Stdio::null()).stderr(Stdio::null()).spawn().ok()?;
        child.stdin.take()?.write_all(head.as_bytes()).ok()?;
        let (ok, _, _) = wait_for(child, Duration::from_secs(600));
        if !ok {
            return None;
        }
        std::fs::write(&mark, format!("{seen}{head}\n")).ok()?;
    }
    Some((d.to_string_lossy().into_owned(), head))
}

/// Line number (1-based) -> (author `Name <email>`, epoch) of the file as it is on disk. Lines not committed yet go
/// to `me` (name, email).
pub fn blame_lines(root: &str, path: &str, me: Option<&(String, String)>, shadow: Option<&(String, String)>) -> HashMap<usize, (String, i64)> {
    let out = match shadow {
        Some((dir, head)) => {
            let file = Path::new(root).join(path);
            Command::new("git").arg("--git-dir").arg(dir)
                .args(["blame", "-w", "--porcelain", "--contents"]).arg(&file).args([head.as_str(), "--", path])
                .stdin(Stdio::null()).output().map(|o| String::from_utf8_lossy(&o.stdout).into_owned()).unwrap_or_default()
        }
        None => git(root, &["blame", "-w", "--porcelain", "--", path]),
    };
    parse_blame(&out, me)
}

pub fn parse_blame(out: &str, me: Option<&(String, String)>) -> HashMap<usize, (String, i64)> {
    enum C {
        Open(String, String, i64),
        Done(String, i64),
    }
    let mut res = HashMap::new();
    let mut commits: HashMap<String, C> = HashMap::new();
    let mut cur: Option<String> = None;
    let mut n = 0;
    for ln in out.split('\n') {
        if ln.starts_with('\t') {
            n += 1;
            if let Some(c) = cur.as_ref().and_then(|c| commits.get(c)) {
                let v = match c {
                    C::Open(a, m, t) => (format!("{a} <{m}>"), *t),
                    C::Done(a, t) => (a.clone(), *t),
                };
                res.insert(n, v);
            }
        } else if ln.len() > 41 && ln.as_bytes()[40] == b' ' && ln.is_char_boundary(40) && ln[..40].chars().all(|c| c.is_alphanumeric()) {
            let sha = ln[..40].to_string();
            commits.entry(sha.clone()).or_insert_with(|| C::Open(String::new(), String::new(), 0));
            cur = Some(sha);
        } else if let Some(c) = cur.as_ref().and_then(|c| commits.get_mut(c)) {
            if let C::Open(a, m, t) = c {
                if let Some(x) = ln.strip_prefix("author-mail ") {
                    *m = x.trim_matches(|c| c == '<' || c == '>').to_string();
                } else if let Some(x) = ln.strip_prefix("author-time ") {
                    *t = x.trim().parse().unwrap_or(0);
                } else if let Some(x) = ln.strip_prefix("author ") {
                    *a = x.to_string();
                } else if ln.starts_with("filename ") {
                    let mine = a == "Not Committed Yet" || a == "External file (--contents)";
                    *c = match (mine, me) {
                        (true, Some((name, email))) => C::Done(format!("{name} <{email}>"), *t),
                        _ => C::Done(format!("{a} <{m}>"), *t),
                    };
                }
            }
        }
    }
    res
}

/// The branch origin's HEAD points to, from the last fetch; None without an origin.
pub fn default_branch(root: &str) -> Option<String> {
    let r = git(root, &["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"]);
    let r = r.trim();
    if !r.is_empty() {
        return r.rsplit('/').next().map(String::from);
    }
    ["main", "master"].into_iter()
        .find(|b| !git(root, &["rev-parse", "--verify", "--quiet", &format!("refs/remotes/origin/{b}")]).trim().is_empty())
        .map(String::from)
}

/// The ref whose commits count as deployed: origin's default branch, else a local main or master.
pub fn deploy_ref(root: &str) -> Option<String> {
    if let Some(b) = default_branch(root) {
        return Some(format!("refs/remotes/origin/{b}"));
    }
    ["main", "master"].into_iter()
        .find(|x| !git(root, &["rev-parse", "--verify", "--quiet", &format!("refs/heads/{x}")]).trim().is_empty())
        .map(|x| format!("refs/heads/{x}"))
}

/// (time, author email, lines added) of the non-merge commits reachable from `r` in the last `days`, without lock
/// files, minified files and generated folders.
pub fn deployed_commits(root: &str, r: &str, days: i64) -> Vec<(i64, String, i64)> {
    let out = git(root, &["log", r, &format!("--since={days}.days"), "--no-merges", "--numstat", "--format=%x1e%at%x1f%ae"]);
    let mut rows = Vec::new();
    for rec in out.split('\x1e').skip(1) {
        let (head, body) = rec.split_once('\n').unwrap_or((rec, ""));
        let (ts, email) = head.split_once('\x1f').unwrap_or((head, ""));
        let mut added = 0;
        for ln in body.lines() {
            let parts: Vec<&str> = ln.split('\t').collect();
            if parts.len() < 3 || parts[0].is_empty() || !parts[0].bytes().all(|b| b.is_ascii_digit()) {
                continue;
            }
            let path = parts[2];
            let dirs: Vec<&str> = path.split('/').collect();
            if DEPLOY_SKIP.is_match(path) || dirs[..dirs.len() - 1].iter().any(|p| GENERATED_DIRS.contains(p)) {
                continue;
            }
            added += parts[0].parse::<i64>().unwrap_or(0);
        }
        if let Ok(t) = ts.trim().parse::<i64>() {
            rows.push((t, email.trim().to_lowercase(), added));
        }
    }
    rows
}

/// How far the local default branch is behind origin's, as of the last fetch: {branch, behind, current}.
pub fn upstream(root: &str) -> Option<serde_json::Value> {
    let b = default_branch(root)?;
    if git(root, &["rev-parse", "--verify", "--quiet", &format!("refs/heads/{b}")]).trim().is_empty() {
        return None;
    }
    let n = git(root, &["rev-list", "--count", &format!("{b}..origin/{b}")]);
    Some(serde_json::json!({"branch": b, "behind": n.trim().parse::<i64>().unwrap_or(0),
        "current": git(root, &["branch", "--show-current"]).trim()}))
}

/// Fetch only origin's default branch; the working tree is not touched.
pub fn fetch_default(root: &str) -> bool {
    match default_branch(root) {
        Some(b) => quiet_git(root, &["fetch", "--quiet", "--no-tags", "origin", &b], Duration::from_secs(30)).0,
        None => false,
    }
}

/// The branches that origin's protection rules name exactly, read from GitHub through `gh`. Wildcard rules are
/// left out. Empty without gh or a GitHub origin.
pub fn protected_branches(root: &str) -> Vec<String> {
    static GH: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$").expect("regex"));
    let url = git(root, &["remote", "get-url", "origin"]);
    let Some(m) = GH.captures(url.trim()) else { return Vec::new() };
    let (owner, name) = (m[1].to_string(), m[2].to_string());
    let gh = |args: &[&str]| -> String {
        let child = Command::new("gh").arg("api").args(args).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn();
        match child {
            Ok(c) => {
                let (ok, out, _) = wait_for(c, Duration::from_secs(60));
                if ok { out } else { String::new() }
            }
            Err(_) => String::new(),
        }
    };
    let (o, n) = (format!("o={owner}"), format!("n={name}"));
    let mut names: Vec<String> = gh(&["graphql", "-f", "query=query($o:String!,$n:String!){repository(owner:$o,name:$n){branchProtectionRules(first:100){nodes{pattern}}}}",
        "-f", &o, "-f", &n, "-q", ".data.repository.branchProtectionRules.nodes[].pattern"])
        .split_whitespace().map(String::from).collect();
    let rulesets = gh(&[&format!("repos/{owner}/{name}/rulesets"), "-q", r#".[] | select(.target == "branch") | .id"#]);
    for rid in rulesets.split_whitespace() {
        let inc = gh(&[&format!("repos/{owner}/{name}/rulesets/{rid}"), "-q", ".conditions.ref_name.include[]?"]);
        names.extend(inc.split_whitespace().map(|x| x.strip_prefix("refs/heads/").unwrap_or(x).to_string()));
    }
    let mut out: Vec<String> = names.into_iter().filter(|n| !n.is_empty() && !n.contains(['*', '?', '[', ']', '~'])).collect();
    out.sort();
    out.dedup();
    out
}

/// Bring the local default branch up to origin's, fast-forward only. Never merges. Returns (ok, message).
pub fn pull_default(root: &str) -> (bool, String) {
    let Some(u) = upstream(root) else { return (false, "no origin branch to pull from".into()) };
    let b = u["branch"].as_str().unwrap_or("").to_string();
    let current = u["current"].as_str().unwrap_or("").to_string();
    let ahead = git(root, &["rev-list", "--count", &format!("origin/{b}..{b}")]);
    if !matches!(ahead.trim(), "" | "0") {
        return (false, format!("{b} has commits that origin doesn't have: merge or rebase it yourself"));
    }
    let spec = format!("{b}:{b}");
    let args: Vec<&str> = if current == b { vec!["pull", "--ff-only", "--quiet", "origin", &b] } else { vec!["fetch", "--quiet", "origin", &spec] };
    let (ok, out, err) = quiet_git(root, &args, Duration::from_secs(120));
    if !ok {
        let msg = if err.trim().is_empty() { out } else { err };
        if msg == "git timed out" {
            return (false, msg);
        }
        return (false, msg.trim().lines().last().map(String::from).unwrap_or_else(|| "git failed".into()));
    }
    let tail = if current == b { String::new() } else { format!(" (you stay on {})", if current.is_empty() { "a detached HEAD" } else { &current }) };
    (true, format!("{b} is up to date with origin{tail}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chunks_break_on_blank_lines_after_700_chars() {
        let line = "x".repeat(99);
        let mut text: Vec<String> = (0..8).map(|_| line.clone()).collect();
        text.push(String::new());
        text.extend((0..3).map(|_| line.clone()));
        let c = chunk_code(&text.join("\n"));
        assert_eq!(c.iter().map(|x| (x.0, x.1)).collect::<Vec<_>>(), vec![(1, 9), (10, 12)]);
        assert!(chunk_code("\n\n  \n").is_empty());
    }

    #[test]
    fn globs_and_suffixes() {
        assert!(fnmatch("my-secret.txt", "*secret*"));
        assert!(fnmatch("ID_RSA.pub", "id_rsa*"));
        assert!(!fnmatch("notes.md", "*.key"));
        assert_eq!(suffix("a/b.tar.GZ"), ".gz");
        assert_eq!(suffix(".gitignore"), "");
    }

    #[test]
    fn reads_porcelain_blame() {
        let sha = "a".repeat(40);
        let out = format!("{sha} 1 1 2\nauthor Ann\nauthor-mail <ann@x.org>\nauthor-time 100\nfilename f.py\n\tone\n{sha} 2 2\n\ttwo\n");
        let b = parse_blame(&out, None);
        assert_eq!(b[&2], ("Ann <ann@x.org>".to_string(), 100));
    }

    #[test]
    fn written_source_rejects_long_lines() {
        assert!(written_source(b"def f():\n    return 1\n", "a.py"));
        assert!(!written_source(&vec![b'x'; 1000], "a.py"));
        assert!(!written_source(b"a\n", "a.json"));
    }
}
