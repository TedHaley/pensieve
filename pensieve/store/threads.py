"""The thread tables (docs/threads.md): the linker's threads with their members, repos and events, which card is in
which thread now (`thread_cards`), and the saved cards of each thread (`thread_snapshots` and their reasons)."""
from pensieve.models import (SaveReason, Thread, ThreadCard, ThreadEventRow, ThreadMember, ThreadRepo, ThreadSnapshot,
                             ThreadSnapshotReason)
from pensieve.store import rows
from pensieve.store.db import Db

KEEP_EVENTS = 2000


def load(db: Db) -> list[tuple[Thread, dict[str, float], tuple[str, ...]]]:
    """Every thread with its members ({member: weight}) and repos."""
    members, repos = {}, {}
    for m in rows.select(db.conn, ThreadMember):
        members.setdefault(m.thread_id, {})[m.member] = m.weight
    for r in rows.select(db.conn, ThreadRepo, order="thread_id, repo"):
        repos.setdefault(r.thread_id, []).append(r.repo)
    return [(t, members.get(t.id, {}), tuple(repos.get(t.id, ()))) for t in rows.select(db.conn, Thread)]


def events(db: Db, limit: int = KEEP_EVENTS) -> list[ThreadEventRow]:
    return list(reversed(rows.select(db.conn, ThreadEventRow, "1 = 1 ORDER BY id DESC LIMIT ?", (limit,))))


def save(db: Db, threads: list[tuple[Thread, dict[str, float], tuple[str, ...]]], new_events: list[ThreadEventRow],
         of_card: dict[str, str] | None = None) -> None:
    """Write every thread with its members and repos, add the new events (the oldest go after KEEP_EVENTS), and
    replace which card is in which thread (`of_card`)."""
    def run(c):
        for t, members, repos in threads:
            rows.upsert(c, t, ("id",))
            c.execute("DELETE FROM thread_members WHERE thread_id = ?", (t.id,))
            rows.insert_many(c, [ThreadMember(t.id, m, w) for m, w in members.items()])
            c.execute("DELETE FROM thread_repos WHERE thread_id = ?", (t.id,))
            rows.insert_many(c, [ThreadRepo(t.id, r) for r in dict.fromkeys(repos)])
        for e in new_events:
            rows.insert(c, e)
        c.execute("DELETE FROM thread_events WHERE id <= (SELECT max(id) FROM thread_events) - ?", (KEEP_EVENTS,))
        if of_card is not None:
            c.execute("DELETE FROM thread_cards")
            rows.insert_many(c, [ThreadCard(th, card) for card, th in of_card.items()], or_="IGNORE")
    db.write(run)


def of_cards(db: Db) -> dict[str, str]:
    """{card id: its thread now}."""
    return {card: th for th, card in db.query("SELECT thread_id, card_id FROM thread_cards")}


def last_saves(db: Db) -> dict[str, ThreadSnapshot]:
    """The newest saved card of each thread."""
    return {s.thread_id: s for s in rows.select(db.conn, ThreadSnapshot, "id IN (SELECT max(id) FROM thread_snapshots "
                                                                         "GROUP BY thread_id)")}


def add_save(db: Db, s: ThreadSnapshot, reasons) -> int:
    def run(c):
        sid = rows.insert(c, s)
        rows.insert_many(c, [ThreadSnapshotReason(sid, SaveReason(r)) for r in sorted(set(reasons))])
        return sid
    return db.write(run)


def saves(db: Db, thread_id: str) -> list[tuple[ThreadSnapshot, list[str]]]:
    """A thread's saved cards, oldest first, each with its reasons."""
    snaps = rows.select(db.conn, ThreadSnapshot, "thread_id = ?", (thread_id,), order="at, id")
    why = {}
    for sid, r in db.query("SELECT r.snapshot_id, r.reason FROM thread_snapshot_reasons r JOIN thread_snapshots s ON "
                           "s.id = r.snapshot_id WHERE s.thread_id = ?", (thread_id,)):
        why.setdefault(sid, []).append(r)
    return [(s, sorted(why.get(s.id, []))) for s in snaps]


def count(db: Db, thread_id: str) -> int:
    return db.one("SELECT count(*) FROM thread_snapshots WHERE thread_id = ?", (thread_id,))[0]


def counts(db: Db) -> dict[str, tuple[int, int, int]]:
    """{thread id: (saves, first save time, last save time)}."""
    return {t: (n, a, b) for t, n, a, b in db.query("SELECT thread_id, count(*), min(at), max(at) FROM "
                                                    "thread_snapshots GROUP BY thread_id")}


def last_cards(db: Db) -> dict[str, str]:
    """{thread id: the JSON of its newest saved card}."""
    return dict(db.query("SELECT thread_id, card FROM thread_snapshots WHERE id IN (SELECT max(id) FROM "
                         "thread_snapshots GROUP BY thread_id)"))


def prune(db: Db, keep_after: int, keep_reasons: set[str], day_of) -> int:
    """Delete the saves from before `keep_after` except the last of each day (`day_of(at)`) of each thread and the
    saves with a reason in `keep_reasons`. Returns how many went."""
    old = db.query("SELECT id, thread_id, at FROM thread_snapshots WHERE at < ? ORDER BY at, id", (keep_after,))
    if not old:
        return 0
    keep_ids = {sid for (sid,) in db.query(
        f"SELECT DISTINCT snapshot_id FROM thread_snapshot_reasons WHERE reason IN ({rows.marks(len(keep_reasons))})",
        sorted(keep_reasons))}
    last_of_day = {}
    for sid, th, at in old:
        last_of_day[(th, day_of(at))] = sid
    keep_ids |= set(last_of_day.values())
    drop = [sid for sid, _, _ in old if sid not in keep_ids]

    def run(c):
        for i in range(0, len(drop), 500):
            part = drop[i:i + 500]
            c.execute(f"DELETE FROM thread_snapshots WHERE id IN ({rows.marks(len(part))})", part)
    if drop:
        db.write(run)
    return len(drop)
