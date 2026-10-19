"""The card tables: `cards` and `card_members` (written by the card builder), `card_notes` (the AI notes),
`card_states` (states set by the user or an agent) and `card_done` (note items that a later commit did)."""
from pensieve.models import Card, CardDone, CardDoneCheck, CardMember, CardNote, CardStateSet, MemberKind
from pensieve.store import rows
from pensieve.store.db import Db


def save(db: Db, cards: list[tuple[Card, list[CardMember]]], delete=(), wait: bool = True):
    """Write cards with their members (a card's old members go), and delete the cards in `delete`. One job."""
    delete = list(delete)

    def run(c):
        if delete:
            for i in range(0, len(delete), 900):
                part = delete[i:i + 900]
                c.execute(f"DELETE FROM cards WHERE goal_of IN ({rows.marks(len(part))})", part)
                c.execute(f"DELETE FROM cards WHERE id IN ({rows.marks(len(part))})", part)
        for card, members in sorted(cards, key=lambda x: x[0].goal_of is not None):  # a goal card after its main
            rows.upsert(c, card, ("id",))
            c.execute("DELETE FROM card_members WHERE card_id = ?", (card.id,))
            rows.insert_many(c, members, or_="IGNORE")
        return [card.id for card, _ in cards]
    return db.write(run, wait=wait)


def get(db: Db, id: str) -> Card | None:
    return rows.select_one(db.conn, Card, "id = ?", (id,))


def all(db: Db) -> list[Card]:
    return rows.select(db.conn, Card, order="updated_at DESC")


def ids(db: Db) -> set[str]:
    return {r[0] for r in db.query("SELECT id FROM cards")}


def signatures(db: Db) -> dict[str, str]:
    return dict(db.query("SELECT id, signature FROM cards"))


def views(db: Db, ids=None) -> dict[str, str]:
    """{card id: view JSON}, of every card or of `ids`."""
    if ids is None:
        return dict(db.query("SELECT id, view FROM cards"))
    ids = list(ids)
    return dict(db.query(f"SELECT id, view FROM cards WHERE id IN ({rows.marks(len(ids))})", ids)) if ids else {}


def members(db: Db, card_id: str) -> list[CardMember]:
    return rows.select(db.conn, CardMember, "card_id = ?", (card_id,))


def with_members(db: Db, kind: MemberKind, refs) -> set[str]:
    """The cards that have any of these members (the `card_members_ref` index)."""
    refs, out = list(refs), set()
    for i in range(0, len(refs), 900):
        part = refs[i:i + 900]
        out |= {r[0] for r in db.query(f"SELECT card_id FROM card_members WHERE kind = ? AND ref IN "
                                       f"({rows.marks(len(part))})", [kind.value, *part])}
    return out


def note(db: Db, card_id: str) -> CardNote | None:
    return rows.select_one(db.conn, CardNote, "card_id = ?", (card_id,))


def notes(db: Db) -> dict[str, CardNote]:
    return {n.card_id: n for n in rows.select(db.conn, CardNote)}


def put_notes(db: Db, put: list[CardNote], delete=()) -> None:
    """Write notes, and delete the notes of `delete` first (the goals a note split off before)."""
    delete = list(delete)

    def run(c):
        if delete:
            c.execute(f"DELETE FROM card_notes WHERE card_id IN ({rows.marks(len(delete))})", delete)
        for n in put:
            rows.upsert(c, n, ("card_id",))
    db.write(run)


def put_note_tree(db: Db, main: CardNote, goals: list[CardNote]) -> None:
    """Write a card's note and the notes of the goals split from it, in one job. The goal notes that the card's
    earlier note split off go first."""
    def run(c):
        c.execute("DELETE FROM card_notes WHERE json_extract(extra, '$.goal_of') = ?", (main.card_id,))
        for n in [main, *goals]:
            rows.upsert(c, n, ("card_id",))
    db.write(run)


def add_state(db: Db, s: CardStateSet) -> int:
    return db.write(lambda c: rows.insert(c, s))


def latest_states(db: Db) -> dict[str, CardStateSet]:
    """The newest row of each card in card_states."""
    return {s.card_id: s for s in rows.select(db.conn, CardStateSet, "id IN (SELECT max(id) FROM card_states "
                                                                       "GROUP BY card_id)")}


def state_history(db: Db, card_id: str) -> list[CardStateSet]:
    return rows.select(db.conn, CardStateSet, "card_id = ?", (card_id,), order="at, id")


def done(db: Db) -> dict[str, dict[str, str]]:
    """{card id: {item: why}}."""
    out = {}
    for d in rows.select(db.conn, CardDone):
        out.setdefault(d.card_id, {})[d.item] = d.why
    return out


def put_done(db: Db, card_id: str, items: list[CardDone], check: CardDoneCheck | None = None) -> None:
    """Replace a card's done items, and record the check that found them."""
    def run(c):
        c.execute("DELETE FROM card_done WHERE card_id = ?", (card_id,))
        rows.insert_many(c, items, or_="REPLACE")
        if check is not None:
            rows.insert(c, check, or_="REPLACE")
    db.write(run)


def done_checks(db: Db) -> dict[str, str]:
    """{card id: key of its last done check}."""
    return dict(db.query("SELECT card_id, key FROM card_done_checks"))
