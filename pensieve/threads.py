"""Threads: one goal followed over time (docs/threads.md).

A task as work.tasks builds it is a snapshot: its members (tickets, PRs, feature branches, commits, sessions, edited
files) at one time. The linker gives each snapshot a thread: the thread of its best match among the threads it
already knows, by weighted member overlap, when the goals are also near. Otherwise the snapshot starts a thread, or
forks one (the members match, the goal moved). Embeddings never join snapshots alone; a close goal with no shared
member is only a suggestion.

  members(t)          {member: weight}
  goal_text(t, note)  the text whose embedding stands for the goal
  Linker.step(snaps)  link one build of snapshots; returns {snapshot: thread} and records the events"""
import hashlib
import re
import time
from dataclasses import dataclass, field

import numpy as np

WEIGHT = {"ticket": 3.0, "pr": 3.0, "branch": 3.0, "commit": 1.5, "session": 1.5, "mention": 1.0, "file": 0.5}
STRONG = ("ticket:", "pr:", "branch:")


def members(t: dict) -> dict[str, float]:
    """A task's members and their weights. A PR that a session only mentions counts as a mention, not as a PR; a PR
    that a session's title names ("PR 2062 CI job") is the PR that session works on."""
    m = {f"ticket:{k}": WEIGHT["ticket"] for k in t["keys"]}
    m.update({f"branch:{b['repo']}@{b['branch']}": WEIGHT["branch"] for b in t["branches"]})
    named = {n for s in t["sessions"] for n in re.findall(r"(?:\bPR\s*#?|#)(\d+)", s["title"])}
    for p in t["prs"]:
        own = p.get("head") and any(p["head"] == b["branch"] for b in t["branches"]) or \
            p.get("source") not in (None, "session") or p["url"].rstrip("/").rsplit("/", 1)[-1] in named
        m[f"pr:{p['url']}"] = WEIGHT["pr"] if own else WEIGHT["mention"]
    m.update({f"commit:{c['sha']}": WEIGHT["commit"] for c in t["commits"]})
    m.update({f"session:{s['id']}": WEIGHT["session"] for s in t["sessions"]})
    m.update({f"file:{f}": WEIGHT["file"] for f in t["files"]})
    return m


def snapshot_id(m: dict) -> str:
    return hashlib.sha1("\n".join(sorted(m)).encode()).hexdigest()[:12]


def goal_text(t: dict, note: dict | None = None) -> str:
    """The text that stands for a snapshot's goal: the note's problem and goal when there is a note, or else the
    title, the ticket titles and what its sessions were about."""
    from pensieve import work
    core = work.core(note or {})
    if core.get("problem") or core.get("goal"):
        return f"{t['title']}. {core.get('problem', '')} {core.get('goal', '')}".strip()
    parts = [t["title"]] + [k["title"] for k in t["tickets"] if k.get("title")]
    parts += [s["title"] for s in t["sessions"][:4]] + [re.sub(r"\s+", " ", t.get("last_summary") or "")[:400]]
    return ". ".join(p for p in parts if p)[:1200]


def overlap(a: dict, b: dict) -> tuple[float, bool]:
    """The weighted overlap of two member sets (shared weight over the smaller set's weight), and whether they share
    a strong member (a ticket, a PR they own, a feature branch)."""
    shared = a.keys() & b.keys()
    if not shared:
        return 0.0, False
    w = sum(min(a[k], b[k]) for k in shared)
    strong = any(k.startswith(STRONG) and a[k] >= WEIGHT["pr"] and b[k] >= WEIGHT["pr"] for k in shared)
    return w / max(min(sum(a.values()), sum(b.values())), 1e-9), strong


@dataclass
class Rules:
    """Set from a 30-day replay of one Mac's work, 551 snapshot pairs labelled same goal or different goal
    (bench/threads_replay.py): members alone got F1 0.74 at join 0.4; the goal check added nothing between 0.5 and
    0.7 and stays as a fork guard; suggestions at 0.85 found one missed link and no wrong one."""
    join: float = 0.4       # the overlap that joins a snapshot to a thread without a shared strong member
    same_goal: float = 0.6  # the goal similarity under which a match is a fork, not the same thread
    suggest: float = 0.85   # the goal similarity, with no shared member, at which a link is suggested


@dataclass
class Thread:
    id: str
    members: dict
    vec: np.ndarray
    goal: str
    created: float
    updated: float
    snapshots: list = field(default_factory=list)
    parent: str = ""
    merged_into: str = ""
    repos: tuple = ()


class Linker:
    def __init__(self, rules: Rules | None = None):
        self.rules = rules or Rules()
        self.threads: dict[str, Thread] = {}
        self.events: list[dict] = []
        self.saved_events = 0  # the events up to here are in store.db

    def _new(self, s: dict, at: float, parent: str = "") -> str:
        tid, n = f"th_{s['snapshot']}", 1  # named after its first snapshot, so the id holds across restarts
        while tid in self.threads:
            n, tid = n + 1, f"th_{s['snapshot']}_{n + 1}"
        self.threads[tid] = Thread(tid, s["members"], s["vec"], s["goal"], at, at, [s["snapshot"]], parent,
                                   repos=tuple(s.get("repos", ())))
        return tid

    def step(self, snaps: list[dict], at: float | None = None) -> dict[str, str]:
        """Link one build of snapshots ({snapshot, members, vec, goal, repo}) to threads. Returns {snapshot: thread}."""
        at, r = at or time.time(), self.rules
        live = [t for t in self.threads.values() if not t.merged_into]
        claims = []  # (overlap, snapshot index, thread id, similarity)
        for i, s in enumerate(snaps):
            for t in live:
                ov, strong = overlap(s["members"], t.members)
                if strong or ov >= r.join:
                    claims.append((ov + (1 if strong else 0), i, t.id, float(s["vec"] @ t.vec)))
        out, taken = {}, {}
        for score, i, tid, sim in sorted(claims, key=lambda c: -c[0]):
            s = snaps[i]
            if s["snapshot"] in out:
                if out[s["snapshot"]] != tid and sim >= r.same_goal and tid not in taken:  # two threads, one goal
                    keep = out[s["snapshot"]]
                    self.threads[tid].merged_into = keep
                    self.events.append(dict(at=at, event="merge", thread=keep, other=tid))
                continue
            if sim < r.same_goal:  # the members match, the goal moved
                continue
            if tid in taken:  # this thread already went to a better match: one goal turned out to be two
                new = self._new(s, at, parent=tid)
                self.events.append(dict(at=at, event="split", thread=new, parent=tid))
                out[s["snapshot"]] = new
                continue
            taken[tid] = i
            t = self.threads[tid]
            changed = t.snapshots[-1] != s["snapshot"]
            if changed:
                self.events.append(dict(at=at, event="continue", thread=tid, sim=round(sim, 3)))
                t.snapshots.append(s["snapshot"])
            t.members, t.vec, t.goal, t.updated = s["members"], s["vec"], s["goal"], at if changed else t.updated
            t.repos = tuple(s.get("repos", ())) or t.repos
            out[s["snapshot"]] = tid
        for i, s in enumerate(snaps):
            if s["snapshot"] in out:
                continue
            fork = next((tid for score, j, tid, sim in sorted(claims, key=lambda c: -c[0]) if j == i), "")
            tid = self._new(s, at, parent=fork)
            self.events.append(dict(at=at, event="fork" if fork else "start", thread=tid, **({"parent": fork} if fork else {})))
            if not fork:  # no shared member: a close goal in the same repo is only a suggestion
                near = max(((float(s["vec"] @ t.vec), t.id) for t in live if t.id != tid
                            and set(t.repos) & set(s.get("repos", ()))), default=(0, ""))
                if near[0] >= r.suggest:
                    self.events.append(dict(at=at, event="suggest", thread=tid, other=near[1], sim=round(near[0], 3)))
            out[s["snapshot"]] = tid
        return out


def save(linker: Linker, of_card: dict[str, str] | None = None) -> None:
    """The linker's threads and its new events, in the thread tables of store.db; and which card is in which thread
    now (`of_card`)."""
    from pensieve import models, store
    from pensieve.store import threads as rows
    out = [(models.Thread(t.id, t.goal, np.asarray(t.vec, dtype=np.float32).tobytes(), int(t.created),
                          None, int(t.updated), t.parent or None, t.merged_into or None,
                          t.snapshots[-1] if t.snapshots else None), t.members, tuple(t.repos))
           for t in linker.threads.values()]
    new = [models.ThreadEventRow(None, int(e["at"]), models.ThreadEvent(e["event"]), e["thread"],
                                 e.get("other") or e.get("parent"), e.get("sim"))
           for e in linker.events[linker.saved_events:]]
    rows.save(store.store_db(), out, new, of_card)
    linker.saved_events = len(linker.events)


def load(rules: Rules | None = None) -> Linker:
    """The linker of the thread tables (an empty one before the first save)."""
    from pensieve import store
    from pensieve.store import threads as rows
    linker = Linker(rules)
    db = store.store_db()
    for t, members, repos in rows.load(db):
        vec = np.frombuffer(t.goal_vec, dtype=np.float32).copy() if t.goal_vec else np.zeros(1, np.float32)
        linker.threads[t.id] = Thread(t.id, members, vec, t.goal, float(t.created_at), float(t.updated_at),
                                      [t.snapshot] if t.snapshot else [], t.parent or "", t.merged_into or "", repos)
    linker.events = [dict(at=e.at, event=e.event.value, thread=e.thread_id,
                          **({"other": e.other} if e.other else {}), **({"sim": e.sim} if e.sim is not None else {}))
                     for e in rows.events(db)]
    linker.saved_events = len(linker.events)
    return linker


def snapshot(t: dict, vec: np.ndarray, note: dict | None = None) -> dict:
    m = members(t)
    return dict(snapshot=snapshot_id(m), members=m, vec=vec, goal=goal_text(t, note), task=t["id"],
                title=t["title"], repos=t["repos"])
