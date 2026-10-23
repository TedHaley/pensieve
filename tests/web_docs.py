"""Tests of the web documents a session made, opened or named (pensieve/transcripts.py), and how the Files list of a
thread shows them (work._artifacts). Run: python tests/web_docs.py"""
import json
import os
import sys
import tempfile
from pathlib import Path

DATA = Path(tempfile.mkdtemp())
os.environ["PENSIEVE_DATA"] = str(DATA)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import transcripts, work  # noqa: E402

NOTION = "https://www.notion.so/acme/Launch-plan-1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"
SLACK = "https://acme.slack.com/archives/C0123ABC/p1729000000123456"
DOC = "https://docs.google.com/document/d/abcDEF123"
SHEET = "https://docs.google.com/spreadsheets/d/xyz789"


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))
    return ok


def rec(role, content, n):
    return {"type": role, "uuid": f"u{n}", "timestamp": f"2026-10-20T10:00:{n:02d}Z", "cwd": "/tmp/x",
            "message": {"role": role, "content": content}}


def run():
    lines = [
        rec("user", f"Read the spec {DOC} and post the plan to Slack", 1),
        rec("assistant", [{"type": "tool_use", "id": "t1", "name": "mcp__claude_ai_Notion__notion-create-pages",
                           "input": {"pages": [{"title": "Launch plan"}]}}], 2),
        rec("user", [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": f"Created {NOTION}"}]}], 3),
        rec("assistant", [{"type": "tool_use", "id": "t2", "name": "mcp__claude_ai_Google_Drive__read_file",
                           "input": {"url": SHEET}}], 4),
        rec("user", [{"type": "tool_result", "tool_use_id": "t2", "content": "the sheet"}], 5),
        rec("assistant", [{"type": "text", "text": f"Posted it: {SLACK}."}], 6),
    ]
    p = DATA / "s1.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    f = transcripts.facts(str(p))
    got = {d[0]: d[2] for d in f.docs}
    results = [
        check("a page that a write tool made is made", got.get(NOTION), "made"),
        check("a document a read tool opened is read", got.get(SHEET), "read"),
        check("a link in the user's words is named", got.get(DOC), "named"),
        check("a link in the agent's reply is named, without the trailing dot", got.get(SLACK), "named"),
    ]
    tf = transcripts.text_facts(f)
    arts = work._artifacts({"edited": []}, ["https://claude.ai/artifact/abc"], tf["docs"])
    results.append(check("the Files list: made first, then Artifacts, then the others",
                         [a["url"] for a in arts][:2], [NOTION, "https://claude.ai/artifact/abc"]))
    results.append(check("a Notion page is named by its title", arts[0]["name"], "Launch plan"))
    results.append(check("a Slack link is a message", next(a["name"] for a in arts if a["url"] == SLACK), "Slack message"))
    results.append(check("a sheet says what it is", next(a["name"] for a in arts if a["url"] == SHEET), "Google Sheet"))
    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
