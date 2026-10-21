"""A local history of context cards: each thread's card, saved when it changes, so a thread's past can be read and an
old idea started again.

Every CHECK seconds, the server links the current cards to threads (pensieve/threads.py) and saves a thread's card
when one of these is true:
  - an event: the thread started, forked, split, merged or came back from done or left (revive);
  - its state changed (in progress, gone quiet, done, left);
  - a ticket, PR, branch, commit or session came or went (edited files alone don't count);
  - a push: the remote branch of one of its folders moved;
  - the embedding of its core (problem and goal) moved DRIFT or more since the last save: the note says something new.
A thread is saved at most once every GAP seconds, except for an event, a state change or a push.

A 30-day replay of one Mac's work (bench/threads_replay.py backup) gave 29 saves with these rules, against 1,365 for a
save at every 12-hour build: most cards don't change from one day to the next.

Storage: the thread tables of store.db (storage v2). `threads`, `thread_members`, `thread_repos` and `thread_events`
hold the linker; `thread_cards` holds each current card's thread (the MCP server reads it too); `thread_snapshots`
holds the saves (time, reasons, snapshot, the local card) and what the next check compares with. After KEEP_DAYS, one
save a day is kept for each thread, and every save with an event or a state change."""
import hashlib
import json
import time

import numpy as np

from pensieve import store as v2
from pensieve import threads, work
from pensieve.models import State, ThreadSnapshot
from pensieve.store import threads as rows

CHECK = 600
GAP = 1800
DRIFT = 0.05
KEEP_DAYS = 30
EVENTS = {"start", "fork", "split", "merge", "revive"}
ENDED = {"complete", "forgotten"}


current = None  # the server's Keeper, once it runs: its of_task maps each current task to its thread


def _of_task() -> dict:
    """Each current task's thread: the server's Keeper's, or (another process) the `thread_cards` table."""
    if current:
        return current.of_task
    return rows.of_cards(v2.store_db())


def thread_of(task_id: str) -> str:
    return _of_task().get(task_id, "")


def task_of(thread: str) -> str:
    """The current task of a thread, or "" (the thread ended or merged, or the keeper hasn't run yet)."""
    return next((t for t, th in _of_task().items() if th == thread), "")


def saves(thread: str) -> int:
    return rows.count(v2.store_db(), thread)


def _hash(items) -> str:
    return hashlib.sha1("\n".join(items).encode()).hexdigest()[:16]


class Keeper:
    def __init__(self, store):
        self.store = store
        self.linker = threads.load()
        self.of_task = rows.of_cards(v2.store_db())  # task id -> thread id, from the last pass
        self._vecs = {}  # goal text hash -> embedding

    def _embed(self, texts: list[str]) -> list[np.ndarray]:
        from pensieve import indexer
        keys = [hashlib.sha1(t.encode()).hexdigest() for t in texts]
        todo = [(k, t) for k, t in zip(keys, texts) if k not in self._vecs]
        if todo:
            vs = np.asarray(indexer.embed([t for _, t in todo]), dtype=np.float32)
            for (k, _), v in zip(todo, vs):
                self._vecs[k] = v / (np.linalg.norm(v) + 1e-9)
        return [self._vecs[k] for k in keys]

    def run_once(self, now: float | None = None, tasks: list[dict] | None = None, notes: dict | None = None) -> list[dict]:
        """Link the current cards and save the ones that changed. Returns what was saved: {thread, reasons}. A replay
        gives the tasks and notes of a past time, and that time as `now`."""
        from pensieve import cards, task_states
        now = now or time.time()
        notes = work.notes() if notes is None else notes
        ts = [t for t in (work.tasks(self.store, 30) if tasks is None else tasks)
              if t["files"] or t["branches"] or t["prs"] or t["keys"] or t["commits"]]  # a question alone is not a thread
        if not ts:
            return []
        db = v2.store_db()
        vecs = self._embed([threads.goal_text(t, notes.get(t["id"])) for t in ts])
        snaps = [threads.snapshot(t, v, notes.get(t["id"])) for t, v in zip(ts, vecs)]
        before = len(self.linker.events)
        out = self.linker.step(snaps, now)
        threads.save(self.linker)  # the rows of new threads, before their first saves
        events = {}
        for e in self.linker.events[before:]:
            events.setdefault(e["thread"], set()).add(e["event"])
        last = rows.last_saves(db)
        saved = []
        for t, s in zip(ts, snaps):
            tid = out[s["snapshot"]]
            self.of_task[t["id"]] = tid
            prev = last.get(tid)
            structure = _hash(sorted(k for k in s["members"] if not k.startswith("file:")))
            pushed = " ".join(sorted({p["pushed"] for p in t.get("places", []) if p.get("pushed")}))
            why = set(events.get(tid, set()) & EVENTS)
            if prev and prev.state.value in ENDED and t["state"] not in ENDED:
                why.add("revive")
                self.linker.events.append(dict(at=now, event="revive", thread=tid))
            if prev:
                if prev.state.value != t["state"]:
                    why.add("state")
                if prev.structure != structure:
                    why.add("members")
                if pushed and (prev.pushed or pushed) != pushed:
                    why.add("push")
                if prev.goal_vec and 1 - float(np.frombuffer(prev.goal_vec, np.float32) @ s["vec"]) >= DRIFT:
                    why.add("goal")
            elif not why:
                why.add("start")
            if not why or (prev and now - prev.at < GAP and not why & (EVENTS | {"state", "push"})):
                continue
            note = notes.get(t["id"], {})
            card = dict(cards.build(t, note, task_states.history(self.store, t["id"])), thread_id=tid)
            rows.add_save(db, ThreadSnapshot(None, tid, int(now), s["snapshot"], State(task_states.RENAMED.get(t["state"], t["state"])), structure, pushed,
                                             np.asarray(s["vec"], dtype=np.float32).tobytes(), json.dumps(card)), why)
            saved.append(dict(thread=tid, task=t["id"], reasons=sorted(why)))
        threads.save(self.linker, dict(self.of_task))
        return saved


def prune(now: float | None = None) -> int:
    """After KEEP_DAYS, keep one save a day for each thread, and every save with an event or a state change.
    Returns how many saves were dropped."""
    now = now or time.time()
    return rows.prune(v2.store_db(), int(now - KEEP_DAYS * 86400), EVENTS | {"state"},
                      lambda at: time.strftime("%Y-%m-%d", time.localtime(at)))


def history(thread: str) -> list[dict]:
    """A thread's saves, oldest first: {at, reasons, snapshot, card}."""
    return [dict(at=float(s.at), reasons=why, snapshot=s.snapshot, card=json.loads(s.card))
            for s, why in rows.saves(v2.store_db(), thread)]


def listing() -> list[dict]:
    """Each thread with saves: its id, title, how many saves, the first and last."""
    db = v2.store_db()
    cards = rows.last_cards(db)
    out = [dict(thread=th, title=json.loads(cards.get(th, "{}")).get("title", ""), saves=n, first=float(a),
                last=float(b)) for th, (n, a, b) in rows.counts(db).items()]
    return sorted(out, key=lambda x: -x["last"])
