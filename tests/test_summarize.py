import importlib
import sys
import threading
import types
import unittest
from unittest import mock

fake_llm = types.ModuleType("pensieve.llm")
fake_llm.complete = None
sys.modules["pensieve.llm"] = fake_llm
summarize = importlib.import_module("pensieve.summarize")


class FakeDB:
    def __init__(self):
        self.update = None
        self.committed = False

    def execute(self, query, values):
        self.update = (query, values)

    def commit(self):
        self.committed = True


class FakeStore:
    def __init__(self, turns):
        self.turns = turns
        self.lock = threading.Lock()
        self.db = FakeDB()

    def session(self, _sid):
        return {"turns": [{"role": role, "text": text} for role, text in self.turns]}


def filler_turns(count=20):
    return [("assistant", f"Intermediate investigation {n}: " + "x" * 600) for n in range(count)]


class TranscriptTest(unittest.TestCase):
    def test_long_transcript_keeps_opening_and_final_outcome_within_budget(self):
        turns = [("user", "Investigate the indexing bug.")]
        turns += filler_turns()
        turns += [
            ("user", "Run the isolated verification and report the actual result."),
            ("assistant", "FINAL RESULT: the isolated verification passed and the bug is fixed."),
        ]

        transcript = summarize._transcript(turns)

        self.assertLessEqual(len(transcript), 7000)
        self.assertIn("Investigate the indexing bug.", transcript)
        self.assertIn("middle turns omitted", transcript)
        self.assertIn("FINAL RESULT: the isolated verification passed", transcript)

    def test_summarize_session_passes_later_correction_to_model(self):
        turns = [("user", "Store the result in SQLite."), ("assistant", "I will use SQLite.")]
        turns += filler_turns()
        turns += [
            ("user", "CORRECTION: use Postgres instead of SQLite."),
            ("assistant", "Implemented and verified with Postgres."),
        ]
        store = FakeStore(turns)
        answer = {"summary": "The storage target was corrected to Postgres and verified.", "tags": ["storage", "postgres"]}

        with mock.patch.object(summarize.llm, "complete", return_value=answer) as complete:
            summarize.summarize_session(store, "session-1")

        transcript, system_prompt = complete.call_args.args[:2]
        self.assertIn("Store the result in SQLite.", transcript)
        self.assertIn("CORRECTION: use Postgres instead of SQLite.", transcript)
        self.assertIn("Implemented and verified with Postgres.", transcript)
        self.assertIn("Treat later corrections as the final state", system_prompt)
        self.assertTrue(store.db.committed)
        self.assertEqual(store.db.update[1], (answer["summary"], "storage,postgres", "session-1"))

    def test_prompt_does_not_assume_an_unseen_outcome(self):
        self.assertIn("if none is shown, say so", summarize.SESSION_SYS)


if __name__ == "__main__":
    unittest.main()
