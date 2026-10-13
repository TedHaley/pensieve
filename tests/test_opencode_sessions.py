import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pensieve import config, onboarding, parsers, settings


class OpenCodeSessionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "opencode"
        self.root.mkdir()
        self.database = self.root / "opencode.db"
        with sqlite3.connect(self.database) as db:
            db.executescript("""
                CREATE TABLE session(
                    id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, title TEXT,
                    time_created INTEGER, time_updated INTEGER
                );
                CREATE TABLE message(
                    id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT
                );
                CREATE TABLE part(
                    id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                    time_created INTEGER, time_updated INTEGER, data TEXT
                );
            """)
            db.executemany("INSERT INTO session VALUES(?,?,?,?,?,?)", [
                ("ses_root", None, "/work/project", "OpenCode title", 1_700_000_000_000, 1_700_000_003_000),
                ("ses_child", "ses_root", "/work/project", "Child agent", 1_700_000_001_000, 1_700_000_002_000),
            ])
            db.executemany("INSERT INTO message VALUES(?,?,?,?,?)", [
                ("msg_user", "ses_root", 1_700_000_000_000, 1_700_000_000_000,
                 json.dumps({"role": "user", "time": {"created": 1_700_000_000_000}})),
                ("msg_assistant", "ses_root", 1_700_000_001_000, 1_700_000_001_000,
                 json.dumps({"role": "assistant", "time": {"created": 1_700_000_001_000},
                             "path": {"cwd": "/work/project", "root": "/work/project"}})),
            ])
            db.executemany("INSERT INTO part VALUES(?,?,?,?,?,?)", [
                ("prt_user", "msg_user", "ses_root", 1, 1,
                 json.dumps({"type": "text", "text": "find the orange sentinel"})),
                ("prt_synthetic", "msg_user", "ses_root", 2, 2,
                 json.dumps({"type": "text", "text": "internal prompt", "synthetic": True})),
                ("prt_assistant", "msg_assistant", "ses_root", 3, 3,
                 json.dumps({"type": "text", "text": "the orange sentinel is here"})),
                ("prt_tool", "msg_assistant", "ses_root", 4, 4,
                 json.dumps({"type": "tool", "tool": "edit", "state": {
                     "status": "completed", "input": {"filePath": "/work/project/app.py"}}})),
            ])

    def tearDown(self):
        self.temp.cleanup()

    def test_discovers_root_sessions_and_parses_current_sqlite_format(self):
        found = list(parsers.discover_source("opencode", self.root, "opencode.db"))

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].source, "opencode")
        self.assertEqual(str(found[0].path), f"{self.database}#ses_root")
        self.assertEqual(found[0].mtime, 1_700_000_003)

        session = parsers.parse(found[0].source, found[0].path)
        self.assertEqual(session.id, "opencode:ses_root")
        self.assertEqual(session.project, "/work/project")
        self.assertEqual(session.title, "OpenCode title")
        self.assertEqual(session.updated, "2023-11-14T22:13:23Z")
        self.assertEqual(session.turns, [
            ("user", "find the orange sentinel"),
            ("assistant", "the orange sentinel is here"),
        ])
        self.assertEqual(session.touched, {"/work/project/app.py": True})

    def test_session_update_changes_discovery_fingerprint_and_parsed_turns(self):
        before = next(parsers.discover_source("opencode", self.root, "opencode.db"))
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE session SET time_updated=? WHERE id='ses_root'", (1_700_000_004_000,))
            db.execute("INSERT INTO message VALUES(?,?,?,?,?)", (
                "msg_user_2", "ses_root", 1_700_000_004_000, 1_700_000_004_000,
                json.dumps({"role": "user", "time": {"created": 1_700_000_004_000}}),
            ))
            db.execute("INSERT INTO part VALUES(?,?,?,?,?,?)", (
                "prt_user_2", "msg_user_2", "ses_root", 5, 5,
                json.dumps({"type": "text", "text": "second turn"}),
            ))

        after = next(parsers.discover_source("opencode", self.root, "opencode.db"))
        self.assertNotEqual((before.mtime, before.size), (after.mtime, after.size))
        self.assertEqual(parsers.parse(after.source, after.path).turns[-1], ("user", "second turn"))

    def test_detects_default_opencode_data_folder(self):
        self.assertEqual(config.detect_sessions(self.root), ("opencode", self.root))
        self.assertEqual(config.detect_sessions(self.root.parent), ("opencode", self.root))

    def test_setup_scan_counts_opencode_sessions(self):
        old_sources, old_settings = config.SOURCES, settings._data
        try:
            config.SOURCES = [("opencode", self.root, "opencode.db")]
            settings._data = {**settings.DEFAULTS, "disabled": []}
            onboarding._cache.clear()
            onboarding._scanned.clear()

            groups = onboarding.scan(["agents"], fresh=True)

            self.assertEqual(groups[0]["items"], [{
                "id": "agents/opencode", "label": "OpenCode", "detail": str(self.root),
                "size": 1, "unit": "sessions", "available": True, "checked": True,
            }])
        finally:
            config.SOURCES, settings._data = old_sources, old_settings
            onboarding._cache.clear()
            onboarding._scanned.clear()


class ExistingSessionSourcesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_file_based_sources_still_discover_and_parse(self):
        claude_root = self.root / "claude"
        claude_file = claude_root / "project" / "claude.jsonl"
        claude_file.parent.mkdir(parents=True)
        claude_file.write_text(json.dumps({
            "type": "user", "cwd": "/work/claude", "timestamp": "2026-01-01T00:00:00Z",
            "message": {"content": "claude sentinel"},
        }) + "\n")

        qwen_root = self.root / "qwen"
        qwen_file = qwen_root / "project" / "chats" / "qwen.jsonl"
        qwen_file.parent.mkdir(parents=True)
        qwen_file.write_text(json.dumps({
            "type": "assistant", "cwd": "/work/qwen", "timestamp": "2026-01-01T00:00:01Z",
            "message": {"parts": [{"text": "qwen sentinel"}]},
        }) + "\n")

        codex_root = self.root / "codex"
        codex_file = codex_root / "2026" / "rollout-test.jsonl"
        codex_file.parent.mkdir(parents=True)
        codex_file.write_text("\n".join([
            json.dumps({"type": "session_meta", "payload": {"cwd": "/work/codex"}}),
            json.dumps({"timestamp": "2026-01-01T00:00:02Z", "payload": {
                "type": "message", "role": "user", "content": [{"type": "text", "text": "codex sentinel"}],
            }}),
        ]) + "\n")

        cases = [
            ("claude", claude_root, "*/*.jsonl", "claude sentinel"),
            ("qwen", qwen_root, "*/chats/*.jsonl", "qwen sentinel"),
            ("codex", codex_root, "**/rollout-*.jsonl", "codex sentinel"),
        ]
        for source, root, pattern, expected in cases:
            with self.subTest(source=source):
                found = list(parsers.discover_source(source, root, pattern))
                self.assertEqual(len(found), 1)
                self.assertEqual(parsers.parse(found[0].source, found[0].path).turns[0][1], expected)


if __name__ == "__main__":
    unittest.main()
