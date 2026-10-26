"""Tests of Slack on threads: messages join the threads they name (pensieve/chat_joins.py), and the Slack connection
reads messages and threads into items (connectors.collect_slack, slack_parse). Slack's API is stubbed.
Run: python tests/chat_joins.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import chat_joins, connectors  # noqa: E402


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))
    return ok


def msg(path, text, updated, private=False):
    return dict(path=path, title=text[:40], text=text, url=f"https://x.slack.com/{path}", updated=updated,
                private=private)


def run():
    results = []
    a = dict(keys=["ABC-12"], prs=[dict(url="https://github.com/org/app/pull/7")],
             branches=[dict(branch="feat-search-cache")])
    b = dict(keys=[], prs=[], branches=[dict(branch="main"), dict(branch="fix")])
    items = [msg("m1", "Is ABC-12 done yet?", 5), msg("m2", "see org/app#7 please", 4),
             msg("m3", "pushed feat-search-cache, take a look", 3), msg("m4", "ABC-123 and XABC-12 are other tickets", 2),
             msg("m5", "the main fix is in", 1), msg("m6", "DM: ABC-12 is blocked", 6, private=True)]
    chat_joins.attach([a, b], items)
    results.append(check("a message joins the thread whose ticket, PR or branch it names, newest first",
                         [(r["path"], r["why"]) for r in a["chat"]],
                         [("m6", "names ABC-12"), ("m1", "names ABC-12"), ("m2", "links PR #7"),
                          ("m3", "names feat-search-cache")]))
    results.append(check("a direct message stays private on the card", a["chat"][0]["private"], True))
    results.append(check("short or default branch names join nothing", b["chat"], []))
    many = [msg(f"n{i}", "ABC-12", i) for i in range(chat_joins.LIMIT + 3)]
    chat_joins.attach([a], many)
    results.append(check("a thread shows the newest messages and counts the rest",
                         (len(a["chat"]), a["chat_more"]), (chat_joins.LIMIT, 3)))

    calls = []
    pages = {"from:<@U1> after:2026-10-19": [
        dict(channel=dict(id="C1", name="eng"), user="U2", ts="1792000000.000100", text="ABC-12 <@U1> ready?",
             permalink="https://x.slack.com/archives/C1/p1792000000000100?thread_ts=1792000000.000100"),
        dict(channel=dict(id="D9", is_im=True), user="U2", ts="1792000001.000100", text="private note",
             permalink="https://x.slack.com/archives/D9/p1")]}

    def api(token, method, **p):
        calls.append(method)
        if method == "auth.test":
            return dict(ok=True, user_id="U1")
        if method == "users.info":
            return dict(ok=True, user=dict(id=p["user"], real_name={"U1": "Me Person", "U2": "Pat Lee"}[p["user"]]))
        if method == "search.messages":
            return dict(ok=True, messages=dict(matches=pages.get(p["query"], []), paging=dict(pages=1)))
        if method == "conversations.replies":
            return dict(ok=True, messages=[dict(user="U2", ts=p["ts"], text="ABC-12 <@U1> ready?"),
                                           dict(user="U1", ts="1792000002.000100", text="yes")])
        raise AssertionError(method)
    real = connectors.slack_api
    connectors.slack_api = api
    try:
        res, users, me = connectors.collect_slack("xoxp-test", "2026-10-20T00:00:00Z")
    finally:
        connectors.slack_api = real
    items = connectors.slack_parse("slack_api_search", {}, json.loads(res[0][2]))
    results.append(check("search matches are items keyed by channel and ts, with mentions as names",
                         [(i["id"], i["text"], i.get("is_private", False)) for i in items],
                         [("C1-1792000000.000100", "ABC-12 @Me Person ready?", False),
                          ("D9-1792000001.000100", "private note", True)]))
    results.append(check("the thread of a channel message is read whole, never one in a direct message",
                         [(t, a.get("channel_id")) for t, a, _ in res[1:]], [("slack_api_thread", "C1")]))
    thread = connectors.slack_parse("slack_api_thread", {}, json.loads(res[1][2]))[0]
    results.append(check("a thread item holds its messages and its reply count",
                         (thread["reply_count"], [m["from"] for m in thread["messages"]]),
                         (1, ["Pat Lee (ID: U2)", "Me Person (ID: U1)"])))
    results.append(check("the people come back with the user's own id, and only read methods were called",
                         (sorted(u["name"] for u in users), me, set(calls) <= connectors.SLACK_READS),
                         (["Me Person", "Pat Lee"], "U1", True)))
    try:
        connectors.slack_api("xoxp-test", "chat.postMessage")
        refused = False
    except ValueError:
        refused = True
    results.append(check("a write method is refused before any call", refused, True))
    results.append(check("a bot token is refused: the connection reads as the user",
                         connectors.slack_ready("xoxb-1")[0], False))

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
