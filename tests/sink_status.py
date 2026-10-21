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

    use_cache({"branch org/repo@mac-app": "MERGED", "ticket ABC-7": "Done"})
    closed, why = ss.task_closed_by_sinks(t)
    results.append(check("all sinks done: closes", closed, True))

    use_cache({"branch org/repo@mac-app": "EXISTS", "ticket ABC-7": "Done"})
    results.append(check("branch still exists: not closed", ss.task_closed_by_sinks(t)[0], False))

    open_pr = task(prs=[dict(url="https://github.com/org/repo/pull/12", state="open")])
    use_cache({})
    results.append(check("open PR: not closed", ss.task_closed_by_sinks(open_pr)[0], False))

    ticket_only = task(tickets=[dict(key="ABC-9", status="In Progress")])
    use_cache({})

    def agent_silent(keys):
        raise RuntimeError("the agent has not answered")
    ss.ticket_statuses = agent_silent
    results.append(check("ticket the agent has not answered: unknown, not closed",
                         ss.task_closed_by_sinks(ticket_only)[0], False))

    ss.ticket_statuses = lambda keys: {k: "Done" for k in keys}
    results.append(check("ticket answered Done by the agent: closes",
                         ss.task_closed_by_sinks(task(tickets=[dict(key="ABC-9")]))[0], True))

    ss.branch_status = lambda repo, branch: "DELETED"
    use_cache({})
    results.append(check("deleted branch counts as done",
                         ss.task_closed_by_sinks(task(branches=[dict(repo="org/repo", branch="old")]))[0], True))

    results.append(check("no sinks: never closed by sinks", ss.task_closed_by_sinks(task())[0], False))

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
