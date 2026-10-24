"""Revive eval: how well does a briefing let an agent pick up a session, against the full transcript?

  .venv/bin/python bench/revive_eval.py cases     # cut real sessions; write bench/out/revive/cases.json

A case cuts one real session after exchange k. The truth is the user's next message (exchange k+1). Each case has
three contexts, built only from what was before the cut:
  full      the transcript up to the cut, each turn cut at FULL_CHARS (the resume baseline)
  summary   the session's components up to the cut: each one's title and top words
  title     only the first user message's first words (the cheapest briefing)

Then answers are written by agents and scored by a judge.
Limits: the full context is capped per turn, so it is an approximation of a true resume."""
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "bench" / "out" / "revive"
FULL_CHARS = 1200
MIN_EXCHANGES, PER_SESSION, SESSIONS = 6, 2, 60


def exchanges_of(s):
    from pensieve import segment
    out = []
    for g in segment.exchanges(s.turns):
        user = " ".join(s.turns[i][1] for i in g if s.turns[i][0] == "user")
        agent = " ".join(s.turns[i][1] for i in g if s.turns[i][0] != "user")
        out.append((user, agent, g[0], g[-1]))
    return out


def cases():
    from pensieve import parsers, segment, digest
    random.seed(7)
    picked = []
    for source, path in parsers.discover():
        if str(path).endswith(".md") or source != "claude":
            continue
        try:
            s = parsers.parse(source, path)
        except Exception:
            continue
        if len(s.turns) < 12 or s.turns[0][1].startswith("You ") or (s.project or "").startswith(("/private", "/tmp")):
            continue
        picked.append(s)
    random.shuffle(picked)
    out = []
    for s in picked[:SESSIONS]:
        ex = exchanges_of(s)
        if len(ex) < MIN_EXCHANGES:
            continue
        comps = segment.components(s.turns)
        cuts = sorted(random.sample(range(3, len(ex) - 1), min(PER_SESSION, len(ex) - 4)))
        for k in cuts:
            if not ex[k + 1][0].strip():
                continue
            full = "\n\n".join(f"USER: {u[:FULL_CHARS]}\nAGENT: {a[:FULL_CHARS]}" for u, a, _, _ in ex[:k + 1])
            summary = []
            for c in comps:
                lo, hi = c["turns"]
                if any(lo <= last < hi for _, _, _, last in ex[k + 1:]) or hi > ex[k][3] + 1:
                    continue
                part = s.turns[lo:hi]
                first = next((t for r, t in part if r == "user"), "")
                summary.append(dict(title=" ".join(first.split())[:80],
                                    words=digest.top_words(" ".join(t for _, t in part if t))))
            out.append(dict(
                id=f"{s.id}#{k}",
                session=s.id,
                cut=k,
                title_only=" ".join(ex[0][0].split())[:200],
                full=full,
                summary=summary,
                truth=" ".join(ex[k + 1][0].split())[:800],
            ))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "cases.json").write_text(json.dumps(out, indent=1))
    print(f"cases: {len(out)} from {len({c['session'] for c in out})} sessions -> {OUT / 'cases.json'}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "cases":
        cases()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
