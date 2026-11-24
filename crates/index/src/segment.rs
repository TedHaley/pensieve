//! Components: the parts of one session that are about the same theme.
//!
//! A session is a list of exchanges (a user turn and the agent's replies after it). `components` joins neighbouring
//! exchanges while they share words with the component so far, and starts a new one where the words change. Short
//! replies ("ok", "yes") join the component before them, because they carry no theme of their own.

use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

/// A new component starts when an exchange shares less than this much of its words with the component so far.
pub const SHIFT: f64 = 0.12;
/// A component keeps at least this many exchanges before it can end.
pub const MIN_EXCHANGES: usize = 2;
/// An exchange with fewer content words than this joins the component before it.
pub const MIN_WORDS: usize = 4;

static STOP: LazyLock<HashSet<&'static str>> = LazyLock::new(|| {
    "about above after again against also always and any are because been before being below between both but \
     can could did does doing done down during each few for from further get got had has have having her here hers \
     him his how into its itself just let like make more most much must myself need new not now off once only other \
     our ours out over own same she should some such than that the their theirs them then there these they this \
     those through too under until very was were what when where which while who whom why will with would yes you \
     your yours the its lets let's dont don't it's i'm i'll ok okay thanks sure look see also use using used one"
        .split_whitespace().collect()
});

pub type Counter = HashMap<String, usize>;

/// Turn positions grouped into exchanges: each starts at a user turn. Turns before the first user turn form one.
pub fn exchanges(turns: &[(String, String)]) -> Vec<Vec<usize>> {
    let mut out: Vec<Vec<usize>> = Vec::new();
    for (i, (role, _)) in turns.iter().enumerate() {
        match out.last_mut() {
            Some(last) if role != "user" => last.push(i),
            _ => out.push(vec![i]),
        }
    }
    out
}

/// Content words of a text, lower case (`[A-Za-z][A-Za-z0-9_]{2,}`, without stop words).
pub fn words(text: &str) -> Counter {
    let b = text.as_bytes();
    let mut out = Counter::new();
    let mut i = 0;
    while i < b.len() {
        if !b[i].is_ascii_alphabetic() {
            i += 1;
            continue;
        }
        let mut j = i + 1;
        while j < b.len() && (b[j].is_ascii_alphanumeric() || b[j] == b'_') {
            j += 1;
        }
        if j - i >= 3 {
            let w = text[i..j].to_lowercase();
            if !STOP.contains(w.as_str()) {
                *out.entry(w).or_default() += 1;
            }
        }
        i = j;
    }
    out
}

pub fn cosine(a: &Counter, b: &Counter) -> f64 {
    if a.is_empty() || b.is_empty() {
        return 0.0;
    }
    let dot: f64 = a.iter().filter_map(|(k, v)| b.get(k).map(|w| (*v * *w) as f64)).sum();
    let na = a.values().map(|v| (v * v) as f64).sum::<f64>().sqrt();
    let nb = b.values().map(|v| (v * v) as f64).sum::<f64>().sqrt();
    if na > 0.0 && nb > 0.0 { dot / (na * nb) } else { 0.0 }
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Component {
    pub index: usize,
    /// [first turn, last turn + 1)
    pub turns: (usize, usize),
    pub exchanges: usize,
}

/// `shift`: the least cosine to the component so far that keeps an exchange in it (`SHIFT`); `min_exchanges`: the
/// exchanges a component holds before it can end (`MIN_EXCHANGES`).
pub fn components(turns: &[(String, String)], shift: f64, min_exchanges: usize) -> Vec<Component> {
    let groups = exchanges(turns);
    if groups.is_empty() {
        return Vec::new();
    }
    let vecs: Vec<Counter> = groups.iter()
        .map(|g| words(&g.iter().map(|&i| turns[i].1.as_str()).collect::<Vec<_>>().join(" ")))
        .collect();
    let mut out: Vec<Vec<usize>> = Vec::new();
    let mut cur = vec![0];
    let mut centre = vecs[0].clone();
    for (k, v) in vecs.iter().enumerate().skip(1) {
        let thin = v.values().sum::<usize>() < MIN_WORDS;
        if !thin && cur.len() >= min_exchanges && cosine(v, &centre) < shift {
            out.push(std::mem::replace(&mut cur, vec![k]));
            centre = v.clone();
        } else {
            cur.push(k);
            for (w, n) in v {
                *centre.entry(w.clone()).or_default() += n;
            }
        }
    }
    out.push(cur);
    out.iter().enumerate().map(|(n, positions)| {
        let ids: Vec<usize> = positions.iter().flat_map(|&p| groups[p].iter().copied()).collect();
        let lo = ids.iter().copied().min().unwrap_or(0);
        let hi = ids.iter().copied().max().unwrap_or(0) + 1;
        Component { index: n, turns: (lo, hi), exchanges: positions.len() }
    }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn t(role: &str, text: &str) -> (String, String) {
        (role.into(), text.into())
    }

    #[test]
    fn splits_where_the_words_change() {
        let turns = vec![
            t("user", "parser tokens grammar lexer"), t("assistant", "parser lexer grammar tokens fixed"),
            t("user", "parser grammar lexer again"), t("assistant", "ok"),
            t("user", "deploy kubernetes cluster helm chart"), t("assistant", "kubernetes helm deploy cluster"),
        ];
        let c = components(&turns, SHIFT, MIN_EXCHANGES);
        assert_eq!(c.len(), 2);
        assert_eq!(c[0].turns, (0, 4));
        assert_eq!(c[1].turns, (4, 6));
        assert_eq!(words("The fetch_user123 and ab").get("fetch_user123"), Some(&1));
    }
}
