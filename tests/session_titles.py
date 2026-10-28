"""Tests of a Claude session's title (parsers.title_line): the name from /rename wins over the ai-title, also when
Claude Code writes its ai-title again later, and an empty /rename gives the ai-title back. Both readers are tested:
the indexer's parse and the threads' facts, each a line at a time, as a transcript grows.
Run: python tests/session_titles.py"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["PENSIEVE_DATA"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import parsers, transcripts  # noqa: E402

PROMPT = {"type": "user", "cwd": "/code/app", "timestamp": "2026-10-28T00:00:00Z",
          "message": {"role": "user", "content": "fix the login bug please"}}
STEPS = [  # a transcript line, and the title after it
    ({"type": "ai-title", "aiTitle": "Login bug fix"}, "Login bug fix"),
    ({"type": "custom-title", "customTitle": "My rename"}, "My rename"),
    ({"type": "ai-title", "aiTitle": "Login bug fix again"}, "My rename"),
    ({"type": "custom-title", "customTitle": "Second rename"}, "Second rename"),
    ({"type": "agent-name", "agentName": "reviewer"}, "reviewer"),
    ({"type": "agent-name", "agentName": ""}, "Second rename"),
    ({"type": "custom-title", "customTitle": ""}, "Login bug fix again"),
]


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))
    return ok


def write(path: Path, line: dict):
    with open(path, "a") as f:
        f.write(json.dumps(line) + "\n")


def run():
    results = []
    folder = Path(tempfile.mkdtemp())
    for reader in ("parse", "facts"):
        path = folder / f"{reader}.jsonl"
        write(path, PROMPT)
        title = lambda s=None: parsers.parse("claude", path, s) if reader == "parse" else transcripts.facts(str(path))
        s = title()
        results.append(check(f"{reader}: the first prompt", s.title, "fix the login bug please"))
        for line, want in STEPS:
            write(path, line)
            s = title(s)
            results.append(check(f"{reader}: after {line['type']} {list(line.values())[1]!r}", s.title, want))
        results.append(check(f"{reader}: the whole file at once", parsers.parse("claude", path).title, STEPS[-1][1]))

    path = folder / "no-ai-title.jsonl"
    write(path, PROMPT)
    write(path, {"type": "custom-title", "customTitle": "Only rename"})
    s = parsers.parse("claude", path)
    results.append(check("a rename with no ai-title", s.title, "Only rename"))
    write(path, {"type": "custom-title", "customTitle": ""})
    results.append(check("the rename cleared, no ai-title: the first prompt", parsers.parse("claude", path, s).title,
                         "fix the login bug please"))

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
