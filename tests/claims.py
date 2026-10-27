"""Tests of branch claims (pensieve/claims.py): the session with most surviving lines holds the branch; the others lose it.
Transcripts are stubbed. Git is stubbed, except in one temporary checkout.
Run: python tests/claims.py"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import claims, work  # noqa: E402

ROOT = "/code/app"
A_LINE = "a line that session a wrote in full"
B_LINE = "a line that session b wrote in full"
GONE = "a line that session b wrote and later removed"


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))
    return ok


def run():
    results = []
    lines = {"a": [(f"{ROOT}/x.py", A_LINE)], "b": [(f"{ROOT}/x.py", B_LINE), (f"{ROOT}/x.py", GONE)]}
    head = {"x.py": f"{A_LINE}\n{B_LINE}\n"}
    sessions = [dict(id="a", updated=1), dict(id="b", updated=2)]
    res = claims.shares(sessions, ROOT, "feat", lines_of=lambda s: lines[s["id"]], text_of=lambda rel: head[rel])
    results.append(check("a and b keep one line each, a tie: the later session, b, holds the branch",
                         (res["a"]["surviving"], res["b"]["surviving"], res["a"]["holds"], res["b"]["holds"]),
                         (1, 1, False, True)))

    head["x.py"] = f"{A_LINE}\n{B_LINE}\n{GONE}\n"
    res = claims.shares(sessions, ROOT, "feat", lines_of=lambda s: lines[s["id"]], text_of=lambda rel: head[rel])
    results.append(check("b with two surviving lines against a's one: b holds the branch",
                         (res["b"]["holds"], res["a"]["holds"]), (True, False)))

    lines3 = {"a": [(f"{ROOT}/x.py", "a1"), (f"{ROOT}/x.py", "a2")], "b": [(f"{ROOT}/x.py", "b1")],
              "c": [(f"{ROOT}/x.py", "c1")]}
    s3 = [dict(id="a", updated=1), dict(id="b", updated=2), dict(id="c", updated=3)]
    res = claims.shares(s3, ROOT, "feat", lines_of=lambda s: lines3[s["id"]], text_of=lambda rel: "a1\na2\nb1\nc1\n")
    results.append(check("the largest share holds the branch without more than half of it (2 of 4 lines)",
                         [k for k, r in res.items() if r["holds"]], ["a"]))
    res = claims.shares(s3, ROOT, "feat", lines_of=lambda s: lines3[s["id"]], text_of=lambda rel: "")
    results.append(check("no surviving line: nobody holds the branch", any(r["holds"] for r in res.values()), False))

    # a real checkout: two sessions wrote to a branch and nobody committed yet
    repo = Path(tempfile.mkdtemp())
    git = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *a],
                                    check=True, capture_output=True)
    (repo / "x.py").write_text("base\n")
    git("init", "-q", "-b", "main")
    git("add", ".")
    git("commit", "-qm", "init")
    git("switch", "-qc", "feat")
    (repo / "x.py").write_text(f"base\n{A_LINE}\n{B_LINE}\n{GONE}\n")
    unc = {"a": [(f"{repo}/x.py", A_LINE)], "b": [(f"{repo}/x.py", B_LINE), (f"{repo}/x.py", GONE)]}
    res = claims.shares(sessions, str(repo), "feat", lines_of=lambda s: unc[s["id"]])
    results.append(check("uncommitted lines on the open branch count: b holds it, a does not",
                         (res["a"]["surviving"], res["b"]["surviving"], res["b"]["holds"], res["a"]["holds"]),
                         (1, 2, True, False)))
    git("stash", "-q")
    git("switch", "-q", "main")
    git("stash", "pop", "-q")
    work.forget_git([str(repo)])  # as a session event does: the branch that is open now
    res = claims.shares(sessions, str(repo), "feat", lines_of=lambda s: unc[s["id"]])
    results.append(check("a branch that is not open reads its commits only: the lines on main are not its",
                         any(r["surviving"] for r in res.values()), False))

    threads = [dict(_sessions=[dict(id="a", path="", updated=1)], branches=[dict(repo="r", branch="feat")],
                    places=[dict(branch="feat", git=True, path=ROOT)]),
               dict(_sessions=[dict(id="b", path="", updated=2)], branches=[dict(repo="r", branch="feat")],
                    places=[dict(branch="feat", git=True, path=ROOT)])]
    claims.shares = lambda sessions, root, branch: {
        "a": dict(surviving=1, written=1, share=0.25, holds=False),
        "b": dict(surviving=3, written=3, share=0.75, holds=True)}
    claims.apply(threads)
    results.append(check("the losing thread drops the branch from its sinks",
                         ([b["branch"] for b in threads[0]["branches"]], [b["branch"] for b in threads[1]["branches"]]),
                         ([], ["feat"])))
    results.append(check("the losing thread keeps its claim record", threads[0]["claim"]["lost"], True))
    results.append(check("the losing thread keeps the branch as what it contributed to",
                         [b["branch"] for b in threads[0]["contributed"]["branches"]], ["feat"]))
    results.append(check("the winner contributed nothing to another thread", "contributed" in threads[1], False))

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
