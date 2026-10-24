"""Tests of the chains of a thread (pensieve/chains.py): pairs in one direction, branch -> PR -> ticket.
Run: python tests/chains.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import chains  # noqa: E402

PR_URL = "https://github.com/org/repo/pull/5"


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))
    return ok


def run():
    results = []

    t = dict(branches=[dict(repo="/code/repo", branch="DP-1-fix")],
             prs=[dict(url=PR_URL, state="open", head="DP-1-fix", title="DP-1 fix the parser")],
             tickets=[dict(key="DP-1", status="In Progress", title="Parser")])
    status = {"branch /code/repo@DP-1-fix": "EXISTS", "pr org/repo#5": "OPEN", "ticket DP-1": "In Progress"}
    pairs = chains.build(t, status)
    shape = [(p["a"]["kind"], p["b"]["kind"] if p["b"] else None, p["open"]) for p in pairs]
    results.append(check("branch -> PR and PR -> ticket, both open", shape,
                         [("branch", "pr", True), ("pr", "ticket", True)]))
    results.append(check("PR short name is its number", pairs[0]["b"]["short"], "#5"))
    results.append(check("ticket word is its Jira status, in lower case", pairs[1]["b"]["word"], "in progress"))

    merged = dict(branches=[dict(repo="/code/repo", branch="old")],
                  prs=[dict(url=PR_URL, state="merged", head="old", title="old work")], tickets=[])
    mstatus = {"branch /code/repo@old": "MERGED", "pr org/repo#5": "MERGED"}
    mp = chains.build(merged, mstatus)
    results.append(check("merged branch and PR: one closed pair", [(p["open"]) for p in mp], [False]))
    results.append(check("merged words", (mp[0]["a"]["word"], mp[0]["b"]["word"]), ("merged", "merged")))

    unknown = dict(branches=[], prs=[], tickets=[dict(key="ABC-9")])
    up = chains.build(unknown, {})
    results.append(check("ticket with no status: alone, closed", [(p["a"]["word"], p["open"]) for p in up],
                         [("unknown", False)]))

    bare = dict(branches=[], prs=[dict(url=PR_URL, state="open", head="other", title="x")], tickets=[])
    bp = chains.build(bare, {"pr org/repo#5": "OPEN"})
    results.append(check("PR whose branch is not listed: alone, open", [(p["b"], p["open"]) for p in bp],
                         [(None, True)]))

    branch_only = dict(branches=[dict(repo="/code/repo", branch="scratch")], prs=[], tickets=[])
    bo = chains.build(branch_only, {"branch /code/repo@scratch": "EXISTS"})
    results.append(check("local branch with no PR: alone, open", [(p["a"]["word"], p["open"]) for p in bo],
                         [("open", True)]))

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
