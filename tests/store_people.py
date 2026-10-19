"""Tests of storage v2's People, org, cache and other user data (pensieve/store/people.py, org.py, cache.py,
misc.py). Run: python tests/store_people.py

Each test opens new files in a temporary PENSIEVE_DATA."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ["PENSIEVE_DATA"] = tempfile.mkdtemp(prefix="pensieve-people-test-")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pensieve import models as M  # noqa: E402
from pensieve import store  # noqa: E402
from pensieve.store import cache, misc, org, people  # noqa: E402


def fresh():
    store.close_all()
    for p in store.root().glob("*.db*"):
        p.unlink()
    return store.store_db(), store.index_db()


def test_overrides():
    s, _ = fresh()
    o = {"merge": [["email:a@example.com", "gh:a"]], "split": ["name:sam lee"], "me": ["email:me@example.com"]}
    people.set_overrides(s, o, at=100)
    assert people.overrides(s) == o
    o2 = {**o, "split": ["name:sam lee", "gh:b"]}
    people.set_overrides(s, o2, at=200)
    assert people.overrides(s) == o2
    ats = dict(s.query("SELECT a, at FROM person_overrides"))
    assert ats["name:sam lee"] == 100 and ats["gh:b"] == 200, "a row that stays keeps its time"
    people.import_overrides(s, {"merge": [["gh:x", "gh:y"]], "split": ["gh:b"]})
    assert people.overrides(s)["merge"] == [["email:a@example.com", "gh:a"], ["gh:x", "gh:y"]]
    assert people.overrides(s)["split"] == ["name:sam lee", "gh:b"], "an import adds no duplicate"


def test_manager_overrides_go_in_the_migration():
    """A store.db of version 2 keeps its other overrides; its 'manager' rows go, and the CHECK refuses them."""
    store.close_all()
    for p in store.root().glob("*.db*"):
        p.unlink()
    import sqlite3
    from pensieve.store import schema
    path = store.root() / "store.db"
    c = sqlite3.connect(path)
    c.executescript("""CREATE TABLE schema_version (version INTEGER NOT NULL) STRICT;
INSERT INTO schema_version VALUES (2);
CREATE TABLE person_overrides (
  id      INTEGER PRIMARY KEY,
  action  TEXT NOT NULL CHECK (action IN ('merge', 'split', 'me', 'manager')),
  a       TEXT NOT NULL,
  b       TEXT,
  at      INTEGER NOT NULL
) STRICT;
INSERT INTO person_overrides VALUES (1, 'me', 'email:me@example.com', NULL, 100);
INSERT INTO person_overrides VALUES (2, 'manager', 'gh:boss', 'email:me@example.com', 100);""")
    c.commit()
    c.close()
    s = store.store_db()
    assert s.one("SELECT version FROM schema_version")[0] == schema.STORE_VERSION
    assert people.overrides(s) == {"merge": [], "split": [], "me": ["email:me@example.com"]}
    try:
        s.write(lambda c: c.execute("INSERT INTO person_overrides VALUES (3, 'manager', 'a', 'b', 1)"))
        assert False, "the CHECK takes a manager row"
    except sqlite3.IntegrityError:
        pass


def test_persons():
    s, i = fresh()
    people.put_identities(s, [M.Identity("email:a@example.com", M.IdentityKind.EMAIL, "A", None, None, "git", 10, 10)])
    people.put_identities(s, [M.Identity("email:a@example.com", M.IdentityKind.EMAIL, "A B", "Eng", None, "slack", 20,
                                         20)])
    got = people.identities(s)[0]
    assert (got.name, got.first_seen, got.last_seen) == ("A B", 10, 20), "first_seen stays"
    persons = [M.Person("p1", "A B", True, 5), M.Person("p2", "C", False, 6)]
    keys = [M.PersonKey("email:a@example.com", "p1", M.KeyReason.FACT),
            M.PersonKey("gh:c", "p2", M.KeyReason.EVIDENCE, 0.8, '{"reason": "same name"}')]
    people.save_persons(s, persons, keys, [M.PersonAlias("pold", "p2")],
                        [M.Identity("gh:c", M.IdentityKind.GH, None, None, None, "build", 1, 1)])
    prev = people.previous(s)["people"]
    assert prev == {"p1": {"keys": ["email:a@example.com"], "aliases": [], "since": 5},
                    "p2": {"keys": ["gh:c"], "aliases": ["pold"], "since": 6}}
    assert people.current_id(s, "pold") == "p2" and people.current_id(s, "p1") == "p1"
    people.save_persons(s, persons[:1], keys[:1], [])
    assert [p.id for p in people.persons(s)] == ["p1"], "a build replaces the persons"
    people.import_persons(s, {"p9": {"name": "N", "me": False, "keys": ["gh:n", "me:you"], "aliases": ["p8"],
                                     "since": 3}})
    assert people.previous(s)["people"] == {"p9": {"keys": ["gh:n"], "aliases": ["p8"], "since": 3}}
    e = {"a": "p1", "b": "p2", "kinds": {"code": 3, "dm": 1}, "last": 1000.5}
    people.save_edges(i, people.edge_rows(e, {"code": 1.0, "dm": 3.0}, 0.5))
    v = people.edges_view(i)
    assert v == [{"a": "p1", "b": "p2", "w": round((1.0 * 1.3862943611198906 + 3.0 * 0.6931471805599453) * 0.5, 4),
                  "kinds": {"code": 3, "dm": 1}, "last": 1000}], v


def test_slack_and_directory():
    s, _ = fresh()
    people.put_slack_users(s, [{"id": "U1", "name": "Sam", "title": "Eng", "avatar": "https://x/a.png", "bot": False}])
    people.put_slack_users(s, [{"id": "U1", "name": "", "email": "SAM@example.com"}], me="U1")
    d = people.slack_users(s)
    assert d["me"] == "U1" and d["users"]["U1"]["name"] == "Sam" and d["users"]["U1"]["email"] == "sam@example.com"
    rec = dict(id="d1", name="Sam Lee", emails=["sam@example.com"], login="slee", slack="U1", title="",
               avatar_url="", sources=["slack", "github"], found_at=100, seen_at=120)
    people.put_directory(s, [rec])
    assert people.directory(s) == [rec]
    people.put_directory(s, [{**rec, "emails": ["b@example.com"], "sources": ["github"]}])
    assert people.directory(s)[0]["emails"] == ["b@example.com"] and people.directory(s)[0]["sources"] == ["github"]


def test_org():
    s, _ = fresh()
    ev = dict(v=1, since=10, updated=30, builds=2, last_build=20, teams_at=20,
              prs={"https://example.com/pull/1": dict(repo="r", ts=1000, author="sam", reviewers=["ana", "bo"],
                                                      approvers=["bo"])},
              titles={"slack:U1": {"Engineering Manager": {"first": 10, "last": 20, "n": 2}}},
              managers={"slack:U1": {"Engineering Manager": {"first": 10, "last": 20, "n": 2}}},
              first={"email:sam@example.com": 500}, teams={"gh:ana\tgh:bo": 1.5})
    org.save(s, ev)
    got = org.load(s)
    assert got == ev, got
    assert s.one("SELECT updated_at FROM org_team_pairs")[0] == 20
    org.import_team_pairs(s, {"gh:ana\tgh:cy": 2.0}, 30)
    org.import_manager_titles(s, {"slack:U2": {"Director": {"first": 1, "last": 2, "n": 1}}})
    got = org.load(s)
    assert got["teams"]["gh:ana\tgh:cy"] == 2.0 and "slack:U2" in got["managers"]
    org.save(s, {**ev, "prs": {}})
    assert org.load(s)["prs"] == {} and s.one("SELECT count(*) FROM org_reviews")[0] == 0



def test_cache():
    _, i = fresh()
    cache.put_json(i, "gh:user", {"login": "sam"}, ttl=-cache.KEEP_EXPIRED - 10)
    cache.put_json(i, "gh:orgs", {"orgs": []}, ttl=60)
    assert cache.get_json(i, "gh:user") == {"login": "sam"}, "an expired row still answers"
    assert cache.purge(i) == 1 and cache.get(i, "gh:user") is None and cache.get(i, "gh:orgs")
    cache.set_meta_json(i, "people:graph", {"me": "p1"})
    assert cache.meta_json(i, "people:graph") == {"me": "p1"}
    cache.put_avatar(i, M.Avatar("a" * 16, "png", 1))
    assert cache.avatars(i)["a" * 16].ext == "png"


def test_feedback_and_usage():
    s, _ = fresh()
    x = dict(id="abc", ts=10, kind="bug", summary="It broke", details="d", source="claude-code", status="new",
             version="dev", context={"cwd": "/tmp"})
    assert misc.add_feedback(s, x) == x
    assert misc.set_feedback_status(s, "abc", M.FeedbackStatus.SENT)["status"] == "sent"
    assert misc.set_feedback_status(s, "nope", M.FeedbackStatus.SENT) is None
    misc.import_feedback(s, [x, {**x, "id": "def", "ts": 20}])
    assert [f["id"] for f in misc.feedback(s)] == ["abc", "def"] and misc.feedback(s)[0]["status"] == "sent"
    misc.add_usage(s, [misc.usage_row(dict(ts=time.time(), tool="search", ms=12.5, ok=1, args='{"query": "q"}',
                                           results=[["code:1", "a.py"]], n_results=1))], wait=True)
    misc.add_usage(s, [misc.usage_row(dict(ts=time.time(), tool="read", ms=1.0, ok=0, args="not json"))], wait=True)
    assert [r[0] for r in misc.usage_since(s, 0)] == ["read", "search"]
    assert misc.usage_recent(s, 0, 1)[0][1] in ("search", "read")


_OTHER = """
import os, sys
os.environ["PENSIEVE_DATA"] = sys.argv[1]
sys.path.insert(0, sys.argv[2])
from pensieve import agent_jobs, store
print(agent_jobs.take("p", "s", None, "name", 10))
print(len(agent_jobs.pending(5, "", "other")))
store.close_all()
"""


def test_agent_jobs():
    s, _ = fresh()
    from pensieve import agent_jobs
    assert agent_jobs.take("p", "s", None, "name", 10) is None
    assert agent_jobs.counts() == {"name": 1}
    jobs = agent_jobs.pending(5, "", "conn1")
    assert [j["id"] for j in jobs] == [agent_jobs.job_id("p", "s", None)]
    assert agent_jobs.pending(5, "", "conn2") == [], "another session does not get a claimed job"
    r = subprocess.run([sys.executable, "-I", "-c", _OTHER, os.environ["PENSIEVE_DATA"], str(ROOT)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["None", "0"], "a second process reads and writes the same queue while this one has it open"
    agent_jobs.submit(jobs[0]["id"], "an answer", "conn1", "claude-code")
    assert agent_jobs.sessions()[0]["answers"] == 1
    try:
        agent_jobs.submit(jobs[0]["id"], "again", "conn1")
        raise AssertionError("a job took two answers")
    except ValueError:
        pass
    assert agent_jobs.take("p", "s", None, "name", 10) == "an answer"
    assert agent_jobs.take("p", "s", None, "name", 10) is None, "an answer is given once"
    sch = {"type": "object", "required": ["name"]}
    agent_jobs.take("q", "s", sch, "name", 10)
    try:
        agent_jobs.submit(agent_jobs.job_id("q", "s", sch), '{"other": 1}')
        raise AssertionError("an answer without the schema's fields was kept")
    except ValueError:
        pass


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
