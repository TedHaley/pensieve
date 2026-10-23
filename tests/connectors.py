"""Tests of the connector profiles (pensieve/connectors.py): how an Asana result becomes tickets with a key and a
status, and the calls planned after the recipe. Run: python tests/connectors.py"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import connectors  # noqa: E402


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))
    return ok


TASK = {"gid": "1209876543210123", "resource_type": "task", "name": "Rotate the keys", "completed": False,
        "modified_at": "2026-10-20T10:00:00.000Z", "created_at": "2026-10-01T10:00:00.000Z",
        "assignee": {"gid": "77", "name": "Me Myself"}, "permalink_url": "https://app.asana.com/0/1/1209876543210123",
        "memberships": [{"section": {"gid": "5", "name": "In progress"}}]}
USER = {"data": {"gid": "77", "name": "Me Myself", "workspaces": [{"gid": "11", "name": "Acme"}, {"gid": "12", "name": "Lab"}]}}


def run():
    results = []
    items = connectors.asana_parse("asana_search_tasks", {}, {"data": [TASK, {**TASK, "gid": "9", "completed": True}]})
    results.append(check("a search gives one item per task", len(items), 2))
    results.append(check("a task's key is ASANA- and the last six digits of its gid", items[0]["_key"], "ASANA-210123"))
    results.append(check("an open task's status is its section", items[0]["status"], "In progress"))
    results.append(check("a completed task is Done", items[1]["status"], "Done"))
    results.append(check("one task comes back as one item", len(connectors.asana_parse("asana_get_task", {}, {"data": TASK})), 1))
    n = connectors.normalize("asana", items[0])
    results.append(check("the title is the task's name", n["_title"], "Rotate the keys"))
    results.append(check("the times are the task's", (n["_created"], n["_updated"]),
                         ("2026-10-01T10:00:00Z", "2026-10-20T10:00:00Z")))
    results.append(check("the assignee is a person of the item", [(p["role"], p["name"]) for p in n["_people"]],
                         [("assignee", "Me Myself")]))
    results.append(check("a task from a search is fetched for its notes",
                         connectors.asana_follow(items[0], {"asana_get_task"})[1]["task_id"], "1209876543210123"))
    results.append(check("a task with notes is not fetched again",
                         connectors.asana_follow({**items[0], "notes": "x"}, {"asana_get_task"}), None))
    when = datetime(2026, 10, 1, tzinfo=timezone.utc)
    calls = connectors.then("asana", [("asana_get_user", {"user_id": "me"}, json.dumps(USER))], when)
    results.append(check("after the user: a search per workspace", [c[1]["workspace"] for c in calls], ["11", "12"]))
    results.append(check("the search starts at the start time", calls[0][1]["modified_at.after"], "2026-10-01T00:00:00Z"))
    results.append(check("a profile without a plan adds no call", connectors.then("linear", [], when), []))
    results.append(check("the server name picks the profile", connectors.match("claude.ai Asana"), "asana"))

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
