"""Tests of the kin of a thread (pensieve/work.py: _adopt, _kin, _title): the sessions of one piece of work, what
each did on the shared branch, and which one names the thread. Transcripts are stubbed.
Run: python tests/kin.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import session_threads as st, work  # noqa: E402


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))
    return ok


def thread(sid, title, updated, state, branch, writes, claim=None, branches=()):
    return dict(id=st.thread_id(sid), title=title, updated=updated, state=state, active=False,
                sessions=[dict(id=sid, title=title)], branches=[dict(repo="r", branch=b) for b in branches],
                _sessions=[dict(id=sid, title=title, path=f"/x/{sid}.jsonl", updated=updated, branch=branch)],
                _siblings=[], _writes=list(writes), _shared=[], shared_sessions=[], events=[], claim=claim)


def run():
    results = []
    a = thread("a", "A: the branch", 10, "open", "feat", [("branch", "r@feat")], dict(branch="feat", lost=False, share=0.8), ["feat"])
    b = thread("b", "B: a fix", 20, "thoughts", "feat", [("branch", "r@feat")], dict(branch="feat", lost=True, share=0.2))
    c = thread("c", "C: a look", 30, "thoughts", "feat", [])
    d = thread("d", "D: elsewhere", 40, "open", "other", [("branch", "r@other")], None, ["other"])
    for t in (a, b, c):
        t["_siblings"] = [dict(id=s, title="", updated=0) for s in "abc" if s != t["_sessions"][0]["id"]]
    ev = {"/x/a.jsonl": {("branch", "r@feat"): ["created feat", "git commit", "git push feat"]},
          "/x/b.jsonl": {("branch", "r@feat"): ["git commit"]}}
    st.evidence = lambda path: ev.get(path, {})

    work._adopt([a, b, c, d])
    results.append(check("the holder of the branch takes the session that committed to it as shared",
                         [s["id"] for s in a["shared_sessions"]], ["b"]))
    results.append(check("and its activity: the holder is as fresh as that session", a["updated"], 20))
    results.append(check("a session that only ran on the branch is not shared", [e["ref"] for e in a["events"]], ["b"]))
    results.append(check("a thread that lost the branch adopts nothing", (b["updated"], b["shared_sessions"]), (20, [])))

    work._kin([a, b, c, d])
    results.append(check("kin: the sessions of the branch, oldest first, this one marked",
                         [(k["id"][-8:] == a["id"][-8:] and k["me"], k["title"]) for k in a["kin"]],
                         [(True, "A: the branch"), (False, "B: a fix"), (False, "C: a look")]))
    results.append(check("what each did there", [k["did"] for k in a["kin"]],
                         ["created, committed and pushed feat", "committed feat", "worked on feat, no changes"]))
    results.append(check("each row has its thread's state and share", [(k["state"], k["share"]) for k in a["kin"]],
                         [("open", 0.8), ("thoughts", 0.2), ("thoughts", None)]))
    results.append(check("a thought on the branch lists the same family, itself marked",
                         [k["me"] for k in c["kin"]], [False, False, True]))
    results.append(check("a thread with no shared sink has no kin", d["kin"], []))

    a["_lead_title"] = b["_lead_title"] = "A: the branch"
    work._title(a)
    work._title(b)
    results.append(check("the holder is named by the lead session", a["title"], "A: the branch"))
    results.append(check("a thread that lost the branch keeps its own session's title", b["title"], "B: a fix"))

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
