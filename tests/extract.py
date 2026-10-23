"""Tests of the extractors (pensieve/extract): git and gh writes, and text mentions.
Run: python tests/extract.py

The transcripts are made up. The repo folder is not read: origin_repo is replaced by a fake."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import extract  # noqa: E402
from pensieve.extract import git_tools, mentions  # noqa: E402,F401


def bash(tool_id, cmd, cwd="/Users/x/repo"):
    return {"cwd": cwd, "message": {"content": [{"type": "tool_use", "id": tool_id, "name": "Bash",
                                                  "input": {"command": cmd}}]}}


def result(tool_id, text):
    return {"message": {"content": [{"type": "tool_result", "tool_use_id": tool_id,
                                     "content": [{"type": "text", "text": text}]}]}}


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))
    return ok


def run():
    results = []
    git_tools.origin_repo = lambda folder: "acme/widget"
    records = [
        bash("1", "git checkout -b feature/retry"), result("1", ""),
        bash("2", "git commit -m 'retry'"), result("2", "[feature/retry abc123] retry"),
        bash("3", "git push -u origin feature/retry"), result("3", "branch set up"),
        bash("4", "gh pr create --title 'Retry' --body 'x'"),
        result("4", "https://github.com/acme/widget/pull/42"),
        bash("5", "gh pr merge 42 --squash"), result("5", "merged"),
    ]
    s = extract.Session("claude:t1", [("user", "Retry the webhook, see ABB-9 and acme/other#7.")], records)
    edges = list(git_tools.git_tools(s))
    keys = {(e.kind, e.key, e.strength) for e in edges}
    results.append(check("branch created and pushed is a write",
                         ("branch", "acme/widget@feature/retry", "write") in keys, True))
    results.append(check("PR opened is a write, with its number from the URL",
                         ("pr", "acme/widget#42", "write") in keys, True))
    results.append(check("PR merged is a write", sum(1 for e in edges if e.evidence == "gh pr merge") == 1, True))
    results.append(check("commit is a write to the checked-out branch",
                         any(e.evidence == "git commit" and e.key == "acme/widget@feature/retry" for e in edges), True))

    m = list(mentions.mentions(s))
    mk = {(e.kind, e.key, e.strength) for e in m}
    results.append(check("ticket named in text is a mention", ("ticket", "ABB-9", "mention") in mk, True))
    results.append(check("PR named in text is a mention", ("pr", "acme/other#7", "mention") in mk, True))
    results.append(check("mentions never write", all(e.strength == "mention" for e in m), True))

    empty = extract.Session("claude:t2", [("user", "hello")], [])
    results.append(check("a session with no tool calls has no writes", list(git_tools.git_tools(empty)), []))

    # a session that starts on a branch never checks it out: the commit goes to the branch the agent recorded
    on_branch = [dict(bash("1", "git add -A && git commit -m 'cards'"), gitBranch="feature/cards"),
                 result("1", "[feature/cards abc123] cards"),
                 dict(bash("2", "git push"), gitBranch="feature/cards"), result("2", "To github.com:acme/widget.git"),
                 dict(bash("3", "git commit -m 'on main'"), gitBranch="main"), result("3", "[main def456] on main")]
    s3 = extract.Session("claude:t3", [("user", "commit")], on_branch)
    e3 = [(e.key, e.evidence) for e in git_tools.git_tools(s3)]
    results.append(check("a commit with no checkout writes the recorded branch",
                         ("acme/widget@feature/cards", "git commit") in e3, True))
    results.append(check("a bare push writes the recorded branch", ("acme/widget@feature/cards", "git push feature/cards") in e3, True))
    results.append(check("a commit on a default branch is no write", any(k.endswith("@main") for k, _ in e3), False))
    results.append(check("nothing else came of it", len(e3), 2))

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
