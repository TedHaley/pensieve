"""Tests of the app's sink status (pensieve/sink_status.py): PR keys, sinks of a task, and closing by sinks.
Run: python tests/sink_status.py

GitHub and the agent are replaced by fakes. The cache is a temporary file."""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import sink_status as ss  # noqa: E402
from pensieve import thread_model as tm  # noqa: E402

NOW = time.time()


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))
    return ok


def use_cache(entries: dict) -> None:
    path = Path(tempfile.mkdtemp()) / "sink_status.json"
    path.write_text(json.dumps({k: dict(value=v, at=NOW) for k, v in entries.items()}))
    ss.CACHE = path


def task(prs=(), branches=(), tickets=()):
    return dict(prs=list(prs), branches=list(branches), tickets=list(tickets))


def run():
    results = []
    results.append(check("PR key from a URL", ss.pr_key("https://github.com/org/repo/pull/12"), "org/repo#12"))
    results.append(check("no PR key from other text", ss.pr_key("see docs"), ""))

    t = task(prs=[dict(url="https://github.com/org/repo/pull/12", state="merged")],
             branches=[dict(repo="org/repo", branch="mac-app")], tickets=[dict(key="ABC-7", status="Done")])
    keys = [s.label for s in ss.sinks_of_task(t)]
    results.append(check("sinks of a task: PR, branch, ticket", keys,
                         ["pr org/repo#12", "branch org/repo@mac-app", "ticket ABC-7"]))

    def state(x):
        return ss.task_state(x)[0]

    use_cache({"branch org/repo@mac-app": "MERGED", "ticket ABC-7": "Done"})
    results.append(check("all sinks done: closed", state(t) == tm.ThreadState.CLOSED, True))

    use_cache({"branch org/repo@mac-app": "EXISTS", "ticket ABC-7": "Done"})
    results.append(check("branch still exists, PR merged: open or loose ends, not closed",
                         state(t) != tm.ThreadState.CLOSED, True))

    open_pr = task(prs=[dict(url="https://github.com/org/repo/pull/12", state="open")])
    use_cache({})
    results.append(check("open PR: not closed", state(open_pr) == tm.ThreadState.CLOSED, False))

    ticket_only = task(tickets=[dict(key="ABC-9", status="In Progress")])
    results.append(check("ticket in progress: open, not closed", state(ticket_only) == tm.ThreadState.CLOSED, False))

    results.append(check("ticket with no status: unknown, so closed",
                         state(task(tickets=[dict(key="ABC-10")])) == tm.ThreadState.CLOSED, True))

    ss.branch_status = lambda repo, branch: "DELETED"
    use_cache({})
    results.append(check("deleted branch counts as done",
                         state(task(branches=[dict(repo="org/repo", branch="old")])) == tm.ThreadState.CLOSED, True))

    results.append(check("no sinks: no state from sinks", ss.task_state(task())[0], None))

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
