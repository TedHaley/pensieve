"""Read-only queries for the stats of the Insights tab (pensieve/insights_stats.py): commits, tickets, cards, items
closed and sessions, by time. Each returns plain tuples."""
from pensieve.store.db import Db
from pensieve.store.rows import marks


def commits(idx: Db, since: int) -> list[tuple[str, str, int]]:
    """(author email in lower case, repo root, time) of the commits from `since` on."""
    return idx.query("SELECT lower(author), repo, ts FROM commits WHERE ts >= ?", (since,))


def repos(idx: Db) -> list[tuple[str, str, str]]:
    """(root, name, remote) of each indexed repo."""
    return idx.query("SELECT root, name, coalesce(remote, '') FROM repos")


def repo_heads(idx: Db) -> list[tuple]:
    """(root, head, indexed) of each repo: changes when a repo is indexed again."""
    return idx.query("SELECT root, coalesce(head, ''), coalesce(indexed, 0) FROM repos ORDER BY root")


def sessions(idx: Db) -> list[tuple[str, str, str, str]]:
    """(id, project folder, started, updated) of each session; times are ISO text as the transcripts give them."""
    return idx.query("SELECT id, coalesce(project, ''), coalesce(started, ''), coalesce(updated, '') FROM sessions")


def tickets_done(store: Db, keys: list[str], since: int) -> list[tuple[str, str, int]]:
    """(key, status, time) of the tickets assigned to one of `keys` whose last change is from `since` on."""
    if not keys:
        return []
    return store.query(
        "SELECT DISTINCT d.key, coalesce(d.status, ''), coalesce(d.updated_at, d.seen_at, 0) FROM documents d "
        f"JOIN document_people p ON p.doc_id = d.id WHERE d.kind = 'ticket' AND p.role = 'assignee' "
        f"AND p.key IN ({marks(len(keys))}) AND coalesce(d.updated_at, d.seen_at, 0) >= ?", (*keys, since))


def cards(store: Db) -> list[tuple[str, str, int, str]]:
    """(id, state, updated_at, view JSON) of every card (the 30-day window of the Threads tab)."""
    return store.query("SELECT id, state, updated_at, view FROM cards")


def items_closed(store: Db, since: int) -> list[tuple[str, int]]:
    """(card id, time) of the items of a card that a later commit did."""
    return store.query("SELECT card_id, at FROM card_done WHERE at >= ?", (since,))
