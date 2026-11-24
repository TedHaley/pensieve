//! `open`: run a command in a new tab of the app that this terminal runs in.

use std::process::Command;

/// The app that this terminal runs in, from the environment of the terminal.
#[derive(Debug, PartialEq)]
pub enum App {
    Tmux,
    Orca,
    ITerm,
    Terminal,
    Unknown(String),
}

/// tmux comes first: a tmux pane keeps the TERM_PROGRAM of the app that started tmux, and the new tab must be a
/// tmux window.
pub fn app(var: impl Fn(&str) -> Option<String>) -> App {
    if var("TMUX").is_some_and(|v| !v.is_empty()) {
        return App::Tmux;
    }
    match var("TERM_PROGRAM").unwrap_or_default().as_str() {
        "Orca" => App::Orca,
        "iTerm.app" => App::ITerm,
        "Apple_Terminal" => App::Terminal,
        other => App::Unknown(other.to_string()),
    }
}

fn run(cmd: &str, args: &[&str]) -> Result<(), String> {
    let out = Command::new(cmd).args(args).output().map_err(|e| format!("{cmd}: {e}"))?;
    if out.status.success() {
        return Ok(());
    }
    let text = String::from_utf8_lossy(if out.stderr.is_empty() { &out.stdout } else { &out.stderr }).trim().to_string();
    Err(if text.is_empty() { format!("{cmd} failed") } else { text })
}

/// A string as an AppleScript literal.
fn apple(s: &str) -> String {
    format!("\"{}\"", s.replace('\\', "\\\\").replace('"', "\\\""))
}

/// Run `command` in a new tab with `title`. Ok is the name of the app. Err says why no tab opened.
pub fn tab(command: &str, title: &str) -> Result<&'static str, String> {
    let var = |k: &str| std::env::var(k).ok();
    match app(var) {
        App::Tmux => run("tmux", &["new-window", "-n", title, command]).map(|_| "tmux"),
        App::Orca => {
            let orca = var("ORCA_CLI_BIN_DIR").map_or("orca".to_string(), |d| format!("{d}/orca"));
            run(&orca, &["terminal", "create", "--title", title, "--command", command, "--focus"]).map(|_| "Orca")
        }
        App::ITerm => run("osascript", &["-e", &format!(
            "tell application \"iTerm\"\n tell current window\n  create tab with default profile\n  tell current session to write text {}\n end tell\nend tell",
            apple(command))]).map(|_| "iTerm"),
        App::Terminal => run("osascript", &["-e", &format!("tell application \"Terminal\"\n activate\n do script {}\nend tell", apple(command))])
            .map(|_| "Terminal"),
        App::Unknown(name) => Err(if name.is_empty() { "this terminal does not say which app it runs in".to_string() }
                                  else { format!("Pensieve cannot open a tab in {name}") }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn app_comes_from_the_environment_and_tmux_wins() {
        let env = |pairs: &'static [(&'static str, &'static str)]| move |k: &str| pairs.iter().find(|p| p.0 == k).map(|p| p.1.to_string());
        assert_eq!(app(env(&[("TERM_PROGRAM", "Orca")])), App::Orca);
        assert_eq!(app(env(&[("TERM_PROGRAM", "iTerm.app"), ("TMUX", "/tmp/tmux-1/default,1,0")])), App::Tmux);
        assert_eq!(app(env(&[("TERM_PROGRAM", "Apple_Terminal"), ("TMUX", "")])), App::Terminal);
        assert_eq!(app(env(&[("TERM_PROGRAM", "vscode")])), App::Unknown("vscode".into()));
        assert_eq!(app(env(&[])), App::Unknown(String::new()));
    }

    #[test]
    fn apple_literal_keeps_quotes_and_backslashes() {
        assert_eq!(apple(r#"cd 'a b' && claude "x\y""#), r#""cd 'a b' && claude \"x\\y\"""#);
    }
}
