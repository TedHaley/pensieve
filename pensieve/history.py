"""A local history of context cards: each thread's card, saved when it changes, so a thread's past can be read and an
old idea started again.

Every CHECK seconds, the server links the current cards to threads (pensieve/threads.py) and saves a thread's card
when one of these is true:
  - an event: the thread started, forked, split, merged or came back from done or left (revive);
  - its state changed (in progress, gone quiet, done, left);
  - a ticket, PR, branch, commit or session came or went (edited files alone don't count);
  - the embedding of its core (problem and goal) moved DRIFT or more since the last save: the note says something new.
A thread is saved at most once every GAP seconds, except for an event or a state change.

A 30-day replay of one Mac's work (bench/threads_replay.py backup) gave 29 saves with these rules, against 1,365 for a
save at every 12-hour build: most cards don't change from one day to the next.

Files: <data>/threads/state.json (the linker and the last save of each thread) and <data>/threads/<thread>.jsonl (one
line a save: time, reasons, snapshot, the local card). After KEEP_DAYS, one save a day is kept for each thread, and
every save with an event or a state change."""
import hashlib
import json
import time
from pathlib import Path

import numpy as np

from pensieve import config, threads, work

CHECK = 600
GAP = 1800
DRIFT = 0.05
KEEP_DAYS = 30
EVENTS = {"start", "fork", "split", "merge", "revive"}
ENDED = {"etched", "forgotten"}


current = None  # the server's Keeper, once it runs: its of_task maps each current task to its thread


def _dir() -> Path:
    return config.DATA_DIR / "threads"


def thread_of(task_id: str) -> str:
    return current.of_task.get(task_id, "") if current else ""


def task_of(thread: str) -> str:
    """The current task of a thread, or "" (the thread ended or merged, or the keeper hasn't run yet)."""
    return next((t for t, th in (current.of_task.items() if current else []) if th == thread), "")


def saves(thread: str) -> int:
    f = _dir() / f"{Path(thread).name}.jsonl"
    return sum(1 for x in f.read_text().splitlines() if x.strip()) if f.exists() else 0


class Keeper:
    def __init__(self, store):
        self.store = store
        self.linker = threads.load(_dir() / "state.json")
        try:
            self.last = json.loads((_dir() / "last.json").read_text())
        except (OSError, ValueError):
            self.last = {}  # thread -> {at, snapshot, structural, vec, state}
        self.of_task = {}  # task id -> thread id, from the last pass
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
        vecs = self._embed([threads.goal_text(t, notes.get(t["id"])) for t in ts])
        snaps = [threads.snapshot(t, v, notes.get(t["id"])) for t, v in zip(ts, vecs)]
        before = len(self.linker.events)
        out = self.linker.step(snaps, now)
        events = {}
        for e in self.linker.events[before:]:
            events.setdefault(e["thread"], set()).add(e["event"])
        saved = []
        for t, s in zip(ts, snaps):
            tid = out[s["snapshot"]]
            self.of_task[t["id"]] = tid
            last = self.last.get(tid)
            structural = sorted(k for k in s["members"] if not k.startswith("file:"))
            why = set(events.get(tid, set()) & EVENTS)
            if last and last.get("state") in ENDED and t["state"] not in ENDED:
                why.add("revive")
                self.linker.events.append(dict(at=now, event="revive", thread=tid))
            if last:
                if last.get("state") != t["state"]:
                    why.add("state")
                if last.get("structural") != structural:
                    why.add("members")
                if 1 - float(np.asarray(last["vec"], dtype=np.float32) @ s["vec"]) >= DRIFT:
                    why.add("goal")
            elif not why:
                why.add("start")
            if not why or (last and now - last["at"] < GAP and not why & (EVENTS | {"state"})):
                continue
            note = notes.get(t["id"], {})
            card = dict(cards.build(t, note, task_states.history(self.store, t["id"])), thread_id=tid)
            line = dict(at=now, reasons=sorted(why), snapshot=s["snapshot"], card=card)
            _dir().mkdir(parents=True, exist_ok=True)
            with (_dir() / f"{tid}.jsonl").open("a") as f:
                f.write(json.dumps(line) + "\n")
            self.last[tid] = dict(at=now, snapshot=s["snapshot"], structural=structural, state=t["state"],
                                  vec=[round(float(x), 5) for x in s["vec"]])
            saved.append(dict(thread=tid, task=t["id"], reasons=sorted(why)))
        threads.save(self.linker, _dir() / "state.json")
        (_dir() / "last.json").write_text(json.dumps(self.last))
        return saved


def prune(now: float | None = None) -> int:
    """After KEEP_DAYS, keep one save a day for each thread, and every save with an event or a state change.
    Returns how many saves were dropped."""
    now, dropped = now or time.time(), 0
    for f in _dir().glob("th_*.jsonl"):
        lines = [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
        keep, days = [], {}
        for i, x in enumerate(lines):
            day = time.strftime("%Y-%m-%d", time.localtime(x["at"]))
            if now - x["at"] < KEEP_DAYS * 86400 or set(x["reasons"]) & (EVENTS | {"state"}):
                keep.append(x)
            else:
                days[day] = x  # the day's last save
        keep = sorted(keep + list(days.values()), key=lambda x: x["at"])
        dropped += len(lines) - len(keep)
        if len(keep) != len(lines):
            f.write_text("".join(json.dumps(x) + "\n" for x in keep))
    return dropped


def history(thread: str) -> list[dict]:
    """A thread's saves, oldest first: {at, reasons, snapshot, card}."""
    f = _dir() / f"{Path(thread).name}.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines() if x.strip()] if f.exists() else []


def listing() -> list[dict]:
    """Each thread with saves: its id, title, how many saves, the first and last."""
    out = []
    for f in sorted(_dir().glob("th_*.jsonl")):
        lines = f.read_text().splitlines()
        if not lines:
            continue
        first, last = json.loads(lines[0]), json.loads(lines[-1])
        out.append(dict(thread=f.stem, title=last["card"].get("title", ""), saves=len(lines), first=first["at"], last=last["at"]))
    return sorted(out, key=lambda x: -x["last"])
