"""Tests of branch claims (pensieve/claims.py): the session with most surviving lines holds the branch; the others lose it.
Transcripts and git are stubbed.
Run: python tests/claims.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import claims  # noqa: E402

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
    results.append(check("a holds the one line it still has, b holds one, tie: nobody has a majority",
                         (res["a"]["surviving"], res["b"]["surviving"], res["a"]["majority"], res["b"]["majority"]),
                         (1, 1, False, False)))

    head["x.py"] = f"{A_LINE}\n{B_LINE}\n{GONE}\n"
    res = claims.shares(sessions, ROOT, "feat", lines_of=lambda s: lines[s["id"]], text_of=lambda rel: head[rel])
    results.append(check("b with two surviving lines against a's one: b has the majority",
                         (res["b"]["majority"], res["a"]["majority"]), (True, False)))

    threads = [dict(_sessions=[dict(id="a", path="", updated=1)], branches=[dict(repo="r", branch="feat")],
                    places=[dict(branch="feat", git=True, path=ROOT)]),
               dict(_sessions=[dict(id="b", path="", updated=2)], branches=[dict(repo="r", branch="feat")],
                    places=[dict(branch="feat", git=True, path=ROOT)])]
    claims.shares = lambda sessions, root, branch: {
        "a": dict(surviving=1, written=1, share=0.25, majority=False),
        "b": dict(surviving=3, written=3, share=0.75, majority=True)}
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
