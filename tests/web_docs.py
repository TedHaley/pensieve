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
ART = "https://claude.ai/artifact/Plan123abc"
ART2 = "https://claude.ai/artifact/Other456def"


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
        rec("assistant", [{"type": "tool_use", "id": "t3", "name": "mcp__claude_ai_Slack__slack_read_thread",
                           "input": {"channel_id": "C0SUPPORT", "message_ts": "1792000226.014169"}}], 7),
        rec("user", [{"type": "tool_result", "tool_use_id": "t3", "content": [{"type": "text", "text": json.dumps(
            {"messages": "=== THREAD PARENT MESSAGE ===\nFrom: A teammate\nMessage TS: 1792000226.014169\n"
                         "Renewals fail for EU cards again\n\n=== THREAD REPLIES ==="})}]}], 8),
        rec("assistant", [{"type": "tool_use", "id": "t4", "name": "mcp__claude_ai_Gmail__get_thread",
                           "input": {"thread_id": "18f2a9c0d1e2b3a4"}}], 9),
        rec("user", [{"type": "tool_result", "tool_use_id": "t4", "content": [{"type": "text", "text":
            "From: billing@example.com\nSubject: Soft decline rate up 12%\n\nHi team"}]}], 10),
        rec("assistant", [{"type": "tool_use", "id": "t5", "name": "mcp__claude_ai_Slack__slack_search_public",
                           "input": {"query": f"see {SLACK}2"}}], 11),
        rec("user", [{"type": "tool_result", "tool_use_id": "t5", "content": "many results"}], 12),
        rec("assistant", [{"type": "tool_use", "id": "t6", "name": "Artifact",
                           "input": {"file_path": "/tmp/x/plan.html", "description": "The rollout plan", "icon": "map"}}], 13),
        {**rec("user", [{"type": "tool_result", "tool_use_id": "t6", "content": f"Published at {ART}"}], 14),
         "toolUseResult": {"url": ART, "title": "Rollout Plan", "updated": False, "seq": 1}},
        rec("assistant", [{"type": "tool_use", "id": "t7", "name": "Artifact",
                           "input": {"file_path": "/tmp/x/plan.html"}}], 15),
        {**rec("user", [{"type": "tool_result", "tool_use_id": "t7", "content": f"Published at {ART}"}], 16),
         "toolUseResult": {"url": ART, "title": "Rollout Plan v2", "updated": True, "seq": 2}},
        rec("assistant", [{"type": "tool_use", "id": "t8", "name": "Artifact",
                           "input": {"action": "read", "url": ART2}}], 17),
        {**rec("user", [{"type": "tool_result", "tool_use_id": "t8", "content": "raw HTML"}], 18),
         "toolUseResult": {"read": {}, "artifactRead": {"slug": "x"}}},
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
    thread = next((d for d in f.docs if "C0SUPPORT" in d[0]), None)
    results.append(check("a Slack thread the agent read is context, with its first line as the title",
                         thread and (thread[0], thread[2], thread[3]),
                         (f"{transcripts.SLACK_HOST}/archives/C0SUPPORT/p1792000226014169", "read",
                          "Renewals fail for EU cards again")))
    mail = next((d for d in f.docs if d[1] == "Gmail"), None)
    results.append(check("an email the agent read links to Gmail, with its subject",
                         mail and (mail[0], mail[3]),
                         ("https://mail.google.com/mail/u/0/#all/18f2a9c0d1e2b3a4", "Soft decline rate up 12%")))
    results.append(check("a search tool adds nothing", any(d[0] == SLACK + "2" for d in f.docs), False))
    tf = transcripts.text_facts(f)
    note = DATA / "spec.md"
    note.write_text("x")
    code = DATA / "retry.py"
    code.write_text("x")
    results.append(check("an agent's memory or tool output is not context",
                         (work._agent_own(str(Path.home() / ".claude/projects/x/memory/a.md")), work._agent_own(str(note))),
                         (True, True)))
    own, work._agent_own = work._agent_own, lambda p: False  # the test's files sit in a temp folder
    arts = work._artifacts({"edited": [str(code)]}, ["https://claude.ai/artifact/abc"], tf["docs"], [str(note)])
    work._agent_own = own
    results.append(check("an edited code file is not on the list", any(a.get("path") == str(code) for a in arts), False))
    results.append(check("a document read as context is on the list", arts[-1].get("path"), str(note)))
    results.append(check("the Files list: made first, then Artifacts, then the others",
                         [a.get("url") for a in arts][:2], [NOTION, "https://claude.ai/artifact/abc"]))
    results.append(check("a Notion page is named by its title", arts[0]["name"], "Launch plan"))
    results.append(check("a Slack link is a message", next(a["name"] for a in arts if a["url"] == SLACK), "Slack message"))
    results.append(check("a sheet says what it is", next(a["name"] for a in arts if a["url"] == SHEET), "Google Sheet"))
    first, last = transcripts._ts("2026-10-20T10:00:14Z"), transcripts._ts("2026-10-20T10:00:16Z")
    results.append(check("a publish keeps the latest title, the description, both times, and that it was created here",
                         f.artifacts.get(ART), ["Rollout Plan v2", "The rollout plan", first, last, True]))
    results.append(check("a published Artifact is a link even when no text names it", ART in f.links, True))
    results.append(check("reading an Artifact adds nothing", (ART2 in f.artifacts, ART2 in f.links), (False, False)))
    arts = work._artifacts({}, tf["links"] + ["https://claude.ai/artifact/abc"], published=tf["artifacts"])
    pub = next(a for a in arts if a["url"] == ART)
    results.append(check("the Files list names a published Artifact by its title, with when it was created",
                         (pub["name"], pub["created"], pub["updated"], pub["description"]),
                         ("Rollout Plan v2", first, last, "The rollout plan")))
    named = next(a for a in arts if a["url"] == "https://claude.ai/artifact/abc")
    results.append(check("an Artifact only named in the text has no title or time",
                         (named["name"], named["created"], named["updated"]), ("Artifact", 0, 0)))

    # worked_in: a session worked in a checkout when it read, searched or ran git there, not when it only started there
    repo = "/code/app"

    def session(name, calls):
        out = [dict(rec("user", "go", 1), cwd=repo)]
        for n, (tool, args) in enumerate(calls, 2):
            out += [dict(rec("assistant", [{"type": "tool_use", "id": f"w{n}", "name": tool, "input": args}], n), cwd=repo),
                    dict(rec("user", [{"type": "tool_result", "tool_use_id": f"w{n}", "content": "ok"}], n), cwd=repo)]
        p = DATA / f"{name}.jsonl"
        p.write_text("\n".join(json.dumps(x) for x in out) + "\n")
        return transcripts.worked_in(str(p), repo)
    results.append(check("a session that only answered a question did not work in the checkout",
                         session("weather", [("WebSearch", {"query": "weather"})]), False))
    results.append(check("a session that wrote only outside the checkout did not work in it",
                         session("scratch", [("Write", {"file_path": "/tmp/pad/mk.py", "content": "x"}),
                                             ("Bash", {"command": "cd /tmp/pad && python mk.py"})]), False))
    results.append(check("a file read in the checkout is work in it",
                         session("read", [("Read", {"file_path": f"{repo}/x.py"})]), True))
    results.append(check("a search with no path searches the checkout", session("grep", [("Grep", {"pattern": "x"})]), True))
    results.append(check("a git call in the checkout is work in it", session("git", [("Bash", {"command": "git log -3"})]), True))
    results.append(check("a git call after cd counts in the folder it went to",
                         session("cd", [("Bash", {"command": "cd /code/other && git status"})]), False))
    results.append(check("git named only in a quoted string is not a git call",
                         session("quoted", [("Bash", {"command": 'echo "git log"'})]), False))
    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
