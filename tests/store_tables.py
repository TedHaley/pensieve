"""Tests of storage v2's tables and Db (pensieve/store/). Run: python tests/store_tables.py

Each test opens new files in a temporary PENSIEVE_DATA. Every model makes a round trip through its table, and each
CHECK, STRICT type and json_valid rule rejects a bad value."""
import os
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import fields, replace
from enum import StrEnum
from pathlib import Path
from typing import get_type_hints

os.environ["PENSIEVE_DATA"] = tempfile.mkdtemp(prefix="pensieve-store-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import models  # noqa: E402
from pensieve import store  # noqa: E402
from pensieve.store import cards, documents, rows, schema, sources  # noqa: E402
from pensieve.store.db import Db  # noqa: E402

JSON = {"raw", "view", "card", "extra", "evidence", "context", "args", "results", "laps", "schema"}
REFS = {"doc_id": 1, "source_id": "src", "card_id": "c1", "thread_id": "th1", "snapshot_id": 1, "person_id": "p1",
        "key": "email:sam@example.com", "url": "https://example.com/pull/1", "goal_of": None}
FIXED_ID = {models.Card: "c1", models.Thread: "th1", models.Source: "src", models.Person: "p1",
            models.DirectoryPerson: "p1", models.Feedback: "f1", models.AgentJob: "j1", models.SlackUser: "U1"}
PARENTS = (models.Source, models.Document, models.Card, models.Thread, models.Person, models.Identity,
           models.ThreadSnapshot, models.OrgPullRequest, models.DirectoryPerson)


def _base(t):
    return [a for a in (getattr(t, "__args__", None) or (t,)) if a is not type(None)][0]


def sample(cls):
    """A row of every field's type, with the keys that the other samples refer to."""
    hints, kw = get_type_hints(cls), {}
    for f in fields(cls):
        base = _base(hints[f.name])
        if f.name == "id":
            v = FIXED_ID.get(cls)
        elif f.name in REFS:
            v = REFS[f.name]
        elif f.name in JSON:
            v = '{"a": 1}'
        elif isinstance(base, type) and issubclass(base, StrEnum):
            v = list(base)[0]
        elif base is bool:
            v = True
        elif base is int:
            v = 7
        elif base is float:
            v = 0.5
        elif base is bytes:
            v = b"\x01\x02"
        else:
            v = f"{f.name}-x"
        kw[f.name] = v
    return cls(**kw)


def fresh() -> tuple[Db, Db]:
    store.close_all()
    for p in store.root().glob("*.db*"):
        p.unlink()
    return store.store_db(), store.index_db()


def test_round_trip():
    s, i = fresh()
    for db, group in ((s, models.STORE_MODELS), (i, models.INDEX_MODELS)):
        for cls in group:
            obj = sample(cls)
            rid = db.write(lambda c: rows.insert(c, obj))
            got = rows.select(db.conn, cls)
            assert len(got) == 1, (cls, got)
            want = replace(obj, id=rid) if "id" in models.columns(cls) and obj.id is None else obj
            assert got[0] == want, (cls, got[0], want)
            for f in fields(cls):
                assert type(getattr(got[0], f.name)) is type(getattr(want, f.name)), (cls, f.name)


def _rejects(db, cls, name, value) -> bool:
    """True when the table rejects the sample row with `value` in column `name`."""
    cols, vals = models.columns(cls), list(models.values(sample(cls)))
    vals[cols.index(name)] = value
    try:
        db.write(lambda c: c.execute(f"INSERT OR REPLACE INTO {cls.__table__} ({', '.join(cols)}) VALUES "
                                     f"({', '.join('?' * len(cols))})", vals))
    except sqlite3.IntegrityError:
        return True
    return False


def test_checks():
    s, i = fresh()
    for cls in PARENTS:
        s.write(lambda c, o=sample(cls): rows.insert(c, o, or_="REPLACE"))
    checked = 0
    for db, group in ((s, models.STORE_MODELS), (i, models.INDEX_MODELS)):
        for cls in group:
            hints = get_type_hints(cls)
            for f in fields(cls):
                base = _base(hints[f.name])
                bad = None
                if isinstance(base, type) and issubclass(base, StrEnum):
                    bad = "not-a-value"
                elif f.name in JSON:
                    bad = "{not json"
                elif base is bool:
                    bad = 2
                elif base is int and f.name != "id" and f.name not in REFS:
                    bad = "seven"  # STRICT: text in an INTEGER column
                if bad is not None:
                    assert _rejects(db, cls, f.name, bad), f"{cls.__name__}.{f.name} took {bad!r}"
                    checked += 1
    assert checked > 60, checked
    assert _rejects(s, models.Document, "source_id", "nowhere"), "foreign keys are on"


def test_writer():
    s, _ = fresh()
    commits = []
    s.after_commit.append(lambda results: commits.append(len(results)))
    s.write(lambda c: c.execute("CREATE TABLE t (n INTEGER, k INTEGER, PRIMARY KEY (n, k)) STRICT"))

    def worker(n):
        for k in range(100):
            s.write(lambda c, k=k: c.execute("INSERT INTO t VALUES (?, ?)", (n, k)))
    ts = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert s.one("SELECT count(*) FROM t")[0] == 1000
    assert len(commits) < 1000, "jobs that wait together commit together"
    futs = [s.write(lambda c: c.execute("INSERT INTO t VALUES (99, 1)"), wait=False),
            s.write(lambda c: (c.execute("INSERT INTO t VALUES (99, 2)"), 1 / 0), wait=False),
            s.write(lambda c: c.execute("INSERT INTO t VALUES (99, 3)"), wait=False)]
    for f in futs:
        try:
            f.result()
        except ZeroDivisionError:
            pass
    assert {r[0] for r in s.query("SELECT k FROM t WHERE n = 99")} == {1, 3}, "a failed job rolls back alone"
    started, release = threading.Event(), threading.Event()

    def slow(c):
        c.execute("INSERT INTO t VALUES (98, 1)")
        started.set()
        release.wait(5)
    f = s.write(slow, wait=False)
    started.wait(5)
    t0 = time.monotonic()
    assert s.one("SELECT count(*) FROM t WHERE n = 98")[0] == 0, "a reader sees only commits"
    assert time.monotonic() - t0 < 0.5, "a reader does not wait for the writer"
    release.set()
    f.result()
    assert s.one("SELECT count(*) FROM t WHERE n = 98")[0] == 1
    try:
        s.conn.execute("INSERT INTO t VALUES (97, 1)")
        raise AssertionError("the read connection wrote")
    except sqlite3.OperationalError:
        pass


def test_schema_version():
    s, _ = fresh()
    assert s.one("SELECT version FROM schema_version")[0] == schema.STORE_VERSION
    store.close_all()
    s = store.store_db()
    assert s.query("SELECT count(*) FROM schema_version")[0][0] == 1, "opening again adds no row"
    store.close_all()
    c = sqlite3.connect(store.store_path())
    c.execute("UPDATE schema_version SET version = 999")
    c.commit()
    c.close()
    try:
        store.store_db()
        raise AssertionError("a newer schema opened")
    except RuntimeError:
        pass
    store._open.clear()


def _w(title="Fix login", status="open", updated=1000, people=(), changes=()):
    return documents.Write(models.DocKind.TICKET, title, "https://example.com/ABC-1", status, None, 900, updated,
                           False, f"{title}\n{status}", {"key": "ABC-1", "status": status}, people, changes)


def test_documents():
    s, _ = fresh()
    sources.ensure(s, "jira", models.SourceKind.MCP, "Jira", models.SourceOrigin.ENGINE, "jira")
    assert not sources.switched_on(s, "jira")
    sam = (("email:sam@example.com", "assignee", "Sam"),)
    r = documents.upsert(s, "jira", "ABC-1", lambda old: _w(people=sam))
    assert r.changed and r.id
    assert sources.switched_on(s, "jira"), "on once it has data"
    sources.set_switched(s, "jira", models.Switched.OFF)
    assert not sources.switched_on(s, "jira"), "the user's off wins"
    sources.set_switched(s, "jira", None)
    r2 = documents.upsert(s, "jira", "ABC-1", lambda old: _w(people=sam))
    assert r2.id == r.id and not r2.changed, "the same hash changes nothing"
    r3 = documents.upsert(s, "jira", "ABC-1", lambda old: _w(status="done", updated=2000,
                                                             changes=(("status", old.status, "done"),)))
    assert r3.changed
    assert [(c.field, c.old, c.new) for c in documents.changes(s, r.id)] == [("status", "open", "done")]
    assert documents.people(s, r.id) == [], "the new copy's people replace the old ones"
    seen = documents.upsert(s, "jira", "ABC-1", lambda old: None, seen=documents.Seen("claude:abc", 3000))
    assert seen.seen and not seen.changed
    again = documents.upsert(s, "jira", "ABC-1", lambda old: None, seen=documents.Seen("claude:abc", 3500))
    assert not again.seen
    assert documents.of_session(s, "claude:abc") == [r.id]
    assert [d.key for d in documents.window(s, 1500)] == ["ABC-1"]
    assert documents.window(s, 2500) == []
    plan = s.query("EXPLAIN QUERY PLAN SELECT id FROM documents WHERE coalesce(updated_at, seen_at) > ?", (0,))
    assert any("documents_time" in str(p) for p in plan), plan
    assert documents.count(s, "jira") == 1
    sources.delete(s, "jira")
    assert documents.count(s) == 0 and s.one("SELECT count(*) FROM document_sessions")[0] == 0


def test_connections_store():
    import json
    from pensieve import connections
    fresh()
    connections._sources_seen.clear()
    item = {"key": "ABC-7", "summary": "Fix the login page", "status": "Open", "updated": "2026-01-02T03:04:05Z",
            "assignee": {"displayName": "Sam Lee", "emailAddress": "sam@example.com"}}
    text = json.dumps({"issues": [item]})
    assert connections.store("jira_x", "Jira", "search_issues", {}, text) == 1
    assert connections.store("jira_x", "Jira", "search_issues", {}, text) == 0, "an unchanged item writes nothing"
    assert connections.store("jira_x", "Jira", "search_issues", {}, json.dumps({"issues": [dict(item, status="Done")]})) == 1
    s = store.store_db()
    d = documents.by_key(s, "jira_x", "ABC-7")
    assert d.kind is models.DocKind.TICKET and d.status == "Done" and d.title == "Fix the login page"
    assert d.updated_at == 1767323045
    assert [(p.key, p.role.value) for p in documents.people(s, d.id)] == [("email:sam@example.com", "assignee")]
    md = documents.markdown(s, d.id)
    assert "# Fix the login page" in md and "status changed from Open to Done" in md, md
    assert connections.store("agent_sessions", "Agent sessions", "x.get_issue", {}, json.dumps(item),
                             session="claude:s1", seen_at="2026-01-05T00:00:00Z") == 1
    assert "claude:s1" in documents.markdown(s, documents.by_key(s, "agent_sessions", "ABC-7").id)


def test_cards():
    s, _ = fresh()
    M = models
    main = M.Card("t:ABC-1", M.CardKind.TICKET, None, "Fix login", M.State.OPEN, 1, 2, 3, "sig", "{}")
    goal = M.Card("t:ABC-1~pr2", M.CardKind.GOAL, "t:ABC-1", "Docs", M.State.TODO, 1, 2, 3, "sig2", "{}")
    cards.save(s, [(goal, [M.CardMember(goal.id, M.MemberKind.PULL_REQUEST, "https://example.com/pull/2", 2)]),
                   (main, [M.CardMember(main.id, M.MemberKind.SESSION, "claude:abc", 2),
                           M.CardMember(main.id, M.MemberKind.TICKET_KEY, "ABC-1", None)])])
    assert cards.with_members(s, M.MemberKind.SESSION, ["claude:abc", "x"]) == {"t:ABC-1"}
    assert cards.ids(s) == {"t:ABC-1", "t:ABC-1~pr2"}
    cards.save(s, [(replace(main, title="Fix the login"), [M.CardMember(main.id, M.MemberKind.SESSION, "claude:new")])])
    assert [m.ref for m in cards.members(s, main.id)] == ["claude:new"]
    cards.save(s, [], delete=[main.id])
    assert cards.ids(s) == set(), "a goal card goes with its main card"
    cards.add_state(s, M.CardStateSet(None, "t:ABC-1", M.StateSet.COMPLETE, M.SetBy.USER, "done", 10))
    cards.add_state(s, M.CardStateSet(None, "t:ABC-1", M.StateSet.AUTO, M.SetBy.USER, "", 11))
    assert cards.latest_states(s)["t:ABC-1"].state is M.StateSet.AUTO
    assert len(cards.state_history(s, "t:ABC-1")) == 2


def main():
    tests = [(k, v) for k, v in globals().items() if k.startswith("test_")]
    for name, fn in tests:
        t0 = time.perf_counter()
        fn()
        print(f"ok   {name}  {(time.perf_counter() - t0) * 1000:.0f} ms")
    store.close_all()
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    main()
