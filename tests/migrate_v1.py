"""Tests of the move from v1 to v2 (pensieve/migrate.py). Run: python tests/migrate_v1.py

Each test makes a small v1 data folder in a temporary PENSIEVE_DATA, with made-up data."""
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

DATA = Path(tempfile.mkdtemp(prefix="pensieve-migrate-test-"))
os.environ["PENSIEVE_DATA"] = str(DATA)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import migrate, store  # noqa: E402
from pensieve.store import migration  # noqa: E402

NOW = 1_790_000_000


def make_v1(d: Path):
    (d / "people").mkdir(parents=True, exist_ok=True)
    (d / "people" / "overrides.json").write_text(json.dumps(
        {"merge": [["email:sam@example.com", "gh:slee"]], "split": ["name:sam"], "me": ["email:me@example.com"]}))
    (d / "people" / "org_evidence.json").write_text(json.dumps(dict(
        v=1, prs={"https://example.com/o/r/pull/1": dict(repo="o/r", ts=NOW, author="slee", reviewers=["ana"],
                                                          approvers=["ana"])},
        titles={"slack:U1": {"Engineer": {"first": NOW, "last": NOW, "n": 2}}}, managers={},
        first={"email:sam@example.com": NOW}, teams={})))
    db = sqlite3.connect(d / "pensieve.db")
    db.execute("CREATE TABLE task_states(task_id TEXT, state TEXT, by TEXT, reason TEXT, at REAL)")
    db.executemany("INSERT INTO task_states VALUES (?,?,?,?,?)", [("t:A-1", "etched", "user", "done", NOW),
                                                                  ("t:A-2", "discarded", "user", None, NOW + 1),
                                                                  ("t:A-3", "etched", "agent", None, NOW)])
    db.execute("CREATE TABLE chunks(id INTEGER PRIMARY KEY, text TEXT, vec BLOB)")
    db.commit()
    db.close()
    (d / "feedback.jsonl").write_text(json.dumps(dict(id="a", ts=NOW, kind="bug", summary="It broke", details="x",
                                                      source="some-agent", status="new", version="0.11.0")) + "\n")
    (d / "work_notes.json").write_text(json.dumps({
        "t:A-1": dict(v=10, title="T", problem="P", goal="G", state="S", next="N", question="", sig="s1", at=NOW,
                      also=["more"]),
        "s:old": dict(v=5, recap="r", sig="s0", at=NOW)}))
    (d / "work_done.json").write_text(json.dumps({"t:A-1": {"key": "k", "done": {"Ship it.": "abc123"}}}))
    c = d / "connections" / "claude_ai_Example"
    c.mkdir(parents=True, exist_ok=True)
    (c / "A-1.md").write_text('---\nsource: "Example"\nkey: "A-1"\ntitle: "One"\n---\n\nbody\n')
    (d / "connections" / ".oauth").mkdir(exist_ok=True)
    (d / "connections" / ".oauth" / "own.json").write_text("{}")
    for n in ("code.tv", "index.tv", "work.log", "server.log"):
        (d / n).write_text("x")
    (d / "settings.json").write_text("{}")
    (d / "index.db").touch()
    for n in ("projects", "threads", "git"):
        (d / n).mkdir(exist_ok=True)
        (d / n / "f").write_text("x")


def test_import_twice():
    make_v1(DATA)
    s = store.store_db()
    assert migrate.v1_detected()
    r = migrate.import_v1(DATA, s, dry=False)
    want = dict(person_overrides=3, card_states=2, feedback=1, card_notes=1, card_done=1, org_evidence=5, doc_paths=1)
    for k, n in want.items():
        assert r[k]["status"] == "done" and r[k]["count"] == n, (k, r[k])
    assert r["usage"]["status"] == "skipped"  # no usage_calls table in this build yet
    assert s.one("SELECT state FROM card_states WHERE card_id = 't:A-2'")[0] == "forgotten"
    assert "some-agent" in s.one("SELECT text FROM feedback")[0]
    assert json.loads(s.one("SELECT extra FROM card_notes")[0]) == {"also": ["more"]}
    again = migrate.import_v1(DATA, s, dry=False)
    assert set(again) >= set(want) and all(again[k]["status"] == "done" for k in want)
    assert s.one("SELECT COUNT(*) FROM person_overrides")[0] == 3
    # a step whose row is gone runs again and adds nothing twice
    s.write(lambda c: c.execute("DELETE FROM migration_steps WHERE step = 'card_states'"))
    r3 = migrate.import_v1(DATA, s, dry=False)
    assert r3["card_states"]["count"] == 0 and s.one("SELECT COUNT(*) FROM card_states")[0] == 2


def test_dry_run_writes_nothing():
    d = Path(tempfile.mkdtemp(prefix="pensieve-migrate-dry-"))
    make_v1(d)
    r = migrate.import_v1(d, None, dry=True)
    assert r["person_overrides"] == dict(status="would import", count=3)
    p = migrate.delete_v1(d, None, dry=True, pending={"usage"})
    assert {x["path"] for x in p["removed"]} >= {"people", "work_notes.json", "connections"}
    assert not (d / "v2").exists()


def test_delete():
    s = store.store_db()
    assert migrate.delete_v1(DATA, s)["status"] == "waiting for the first full index"
    for part in migrate.PARTS:
        migration.set_meta(store.index_db(), f"first_index:{part}", str(NOW))
    assert migrate.first_index_done()
    plan = migrate.plan_delete(DATA, migrate.pending_steps(s))
    kept = {k["path"]: k["why"] for k in plan["keep"]}
    assert "embed_cache" in kept["pensieve.db"]  # its import waits for embedcache (phase 6)
    assert "v1 code" in kept["index.tv"]  # config.INDEX_PATH still names it
    live = migrate._live_paths
    migrate._live_paths = lambda: set()
    try:
        r = migrate.delete_v1(DATA, s)
    finally:
        migrate._live_paths = live
    assert r["status"] == "partly done" and {k["path"] for k in r["keep"]} == {"pensieve.db"}, r
    for n in ("code.tv", "index.tv", "people", "projects", "threads", "git", "work_notes.json", "work_done.json",
              "index.db"):
        assert not (DATA / n).exists(), n
    for n in ("settings.json", "server.log", "work.log", "feedback.jsonl", "v2/store.db", "connections/.oauth/own.json"):
        assert (DATA / n).exists(), n
    assert not (DATA / "connections" / "claude_ai_Example").exists()
    assert not (DATA / "v2" / migrate.DELETED_MARK).exists()
    s.write(lambda c: c.execute("UPDATE migration_steps SET status = 'done' WHERE status = 'skipped'"))
    migrate._live_paths = lambda: set()
    try:
        r = migrate.delete_v1(DATA, s)
    finally:
        migrate._live_paths = live
    assert r["status"] == "done" and not (DATA / "pensieve.db").exists(), r
    assert (DATA / "v2" / migrate.DELETED_MARK).exists() and not migrate.v1_detected()
    assert migrate.delete_v1(DATA, s)["status"] == "already done"


def test_compat_id():
    s = store.store_db()
    from pensieve.models import SourceKind, SourceOrigin
    from pensieve.store import sources
    sources.ensure(s, "claude_ai_Example", SourceKind.MCP, "Example", SourceOrigin.ENGINE)
    s.write(lambda c: c.execute("INSERT INTO documents (source_id, key, kind, title, text, raw, hash) VALUES "
                                "('claude_ai_Example', 'A-1', 'ticket', 'One', 'body', '{}', 'h')"))
    doc = s.one("SELECT id FROM documents WHERE key = 'A-1'")[0]
    old = f"file:{DATA}/connections/claude_ai_Example/A-1.md"
    assert migrate.compat_id(old) == f"doc:{doc}"
    assert migrate.compat_id("file:/tmp/other.md") == "file:/tmp/other.md"
    assert migrate.compat_id("code:12") == "code:12"


if __name__ == "__main__":
    tests = [test_import_twice, test_dry_run_writes_nothing, test_delete, test_compat_id]
    for t in tests:
        t0 = time.time()
        t()
        print(f"ok   {t.__name__}  {round((time.time() - t0) * 1000)} ms")
    store.close_all()
    print(f"{len(tests)} passed")
