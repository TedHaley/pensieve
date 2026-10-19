"""Tests of how a task's work in the default branch closes its thread (work._integrated, task_states.derive).
Run: python tests/integrated.py

The repo, branches and PR numbers are made up."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ["PENSIEVE_DATA"] = tempfile.mkdtemp(prefix="pensieve-integrated-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import task_states, work  # noqa: E402


def git(root, *a):
    return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, check=True).stdout.strip()


def commit(root, name, msg):
    Path(root, name).write_text(msg)
    git(root, "add", name)
    git(root, "commit", "-qm", msg)
    return git(root, "rev-parse", "--short", "HEAD")


def repo():
    root = tempfile.mkdtemp(prefix="pensieve-repo-")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "a@example.com")
    git(root, "config", "user.name", "A")
    git(root, "remote", "add", "origin", "https://github.com/example/app.git")
    commit(root, "a.txt", "first")
    return root


def task(root, branch, shas, **kw):
    return dict(id=f"t-{branch}", prs=[], tickets=[], places=[], files=["a.txt"], updated=time.time() - 5 * 86400,
                branches=[dict(repo=root, branch=branch)] if branch else [],
                commits=[dict(repo=root, sha=s, subject="", ts=0) for s in shas], **kw)


def run(ts, notes=None, done=None):
    work._done_items = lambda: done or {}
    work._integrated(ts, notes or {})
    return [task_states.derive(t, (notes or {}).get(t["id"], {})) for t in ts]


def test_merged_branch():
    """A branch merged with a GitHub merge commit closes its task, and the card names the PR."""
    root = repo()
    git(root, "checkout", "-qb", "feat")
    sha = commit(root, "b.txt", "feature")
    git(root, "checkout", "-q", "main")
    git(root, "merge", "-q", "--no-ff", "feat", "-m", "Merge pull request #7 from example/feat")
    t = task(root, "feat", [sha])
    (state, why), = run([t])
    assert state == task_states.State.COMPLETE, why
    assert why == "its branch feat is merged into main (#7)", why
    assert t["prs"][0]["url"] == "https://github.com/example/app/pull/7"


def test_unmerged_branch():
    """A branch with a commit that is not in main keeps its task open."""
    root = repo()
    git(root, "checkout", "-qb", "wip")
    sha = commit(root, "b.txt", "not merged")
    git(root, "checkout", "-q", "main")
    t = task(root, "wip", [sha])
    (state, _), = run([t])
    assert state != task_states.State.COMPLETE and "integrated" not in t


def test_squashed_branch():
    """A branch squash-merged into main closes its task."""
    root = repo()
    git(root, "checkout", "-qb", "sq")
    sha = commit(root, "b.txt", "squash me")
    git(root, "checkout", "-q", "main")
    git(root, "merge", "-q", "--squash", "sq")
    git(root, "commit", "-qm", "Squash me (#9)")
    t = task(root, "sq", [sha])
    (state, why), = run([t])
    assert state == task_states.State.COMPLETE and "squash-merged" in why, why


def test_shared_branch_needs_nothing_left():
    """Without a feature branch, commits in main close the task only when its note says nothing is left. A model's
    "done in <commit>" guess (check_done) is not evidence."""
    root = repo()
    sha = commit(root, "b.txt", "part one")
    later = commit(root, "c.txt", "part two")
    t = task(root, "", [sha])
    (state, _), = run([t])  # no note: no evidence that the work is done
    assert state != task_states.State.COMPLETE and "integrated" not in t
    t = task(root, "", [sha])
    note = {"t-": {"goal": "Two parts", "next": "Add part two."}}
    done = {"t-": dict(key="", done={"Add part two.": f"done in {later} (part two)"})}
    (state, _), = run([t], note, done)
    assert state != task_states.State.COMPLETE and "integrated" not in t
    t = task(root, "", [sha])
    (state, why), = run([t], {"t-": {"goal": "Two parts", "next": "Nothing"}})
    assert state == task_states.State.COMPLETE and "nothing is left" in why, why


def test_outside_git_steps():
    """check_done never asks a model whether a commit marked a PR ready or closed a ticket."""
    assert all(work.OUTSIDE_GIT.search(s) for s in ("Mark #2085 ready for review.", "Merge the PR.", "Close ABC-12.",
                                                     "Ask the user which ideas go on the roadmap."))
    assert not work.OUTSIDE_GIT.search("Add a time limit to the first full folder sweep.")


def test_open_pr_keeps_it_open():
    root = repo()
    sha = commit(root, "b.txt", "on main")
    t = task(root, "", [sha])
    t["prs"] = [dict(url="https://github.com/example/app/pull/3", state="open")]
    (state, _), = run([t])
    assert state != task_states.State.COMPLETE and "integrated" not in t


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t0 = time.time()
        t()
        print(f"ok   {t.__name__}  {round((time.time() - t0) * 1000)} ms")
    print(f"{len(tests)} passed")
