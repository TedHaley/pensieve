"""index.db: the `cache` table (answers of gh and git, kept until `expires_at`), the `meta` table (derived views that
a build writes as one piece), and the `avatars` table (which pictures are in the avatars folder).

A cache row stays after it expires: a caller that cannot reach gh uses the old answer, as v1 did with its cache
files. `purge` deletes the rows that expired long ago."""
import json
import time

from pensieve.models import Avatar, CacheEntry, Meta, now
from pensieve.store import rows
from pensieve.store.db import Db

KEEP_EXPIRED = 30 * 86400  # an expired row stays this long, for use when gh fails


def get(db: Db, key: str) -> bytes | None:
    """The value of a key, expired or not."""
    r = db.one("SELECT value FROM cache WHERE key = ?", (key,))
    return r[0] if r else None


def entry(db: Db, key: str) -> CacheEntry | None:
    return rows.select_one(db.conn, CacheEntry, "key = ?", (key,))


def put(db: Db, key: str, value: bytes, expires_at: int, wait: bool = True):
    e = CacheEntry(key, value, int(expires_at))
    return db.write(lambda c: rows.insert(c, e, or_="REPLACE"), wait=wait)


def get_json(db: Db, key: str, default=None):
    v = get(db, key)
    if v is None:
        return default
    try:
        return json.loads(v)
    except ValueError:
        return default


def put_json(db: Db, key: str, obj, ttl: float, wait: bool = True):
    """Keep a JSON value for `ttl` seconds."""
    return put(db, key, json.dumps(obj).encode(), int(time.time() + ttl), wait=wait)


def delete(db: Db, key: str):
    db.write(lambda c: c.execute("DELETE FROM cache WHERE key = ?", (key,)))


def purge(db: Db, keep: float = KEEP_EXPIRED) -> int:
    """Delete the rows that expired more than `keep` seconds ago. Returns how many."""
    return db.write(lambda c: c.execute("DELETE FROM cache WHERE expires_at < ?", (now() - int(keep),)).rowcount)


def meta(db: Db, key: str) -> str | None:
    """A value of the `meta` table: a derived view that a build writes whole."""
    r = db.one("SELECT value FROM meta WHERE key = ?", (key,))
    return r[0] if r else None


def set_meta(db: Db, key: str, value: str, wait: bool = True):
    m = Meta(key, value)
    return db.write(lambda c: rows.insert(c, m, or_="REPLACE"), wait=wait)


def meta_json(db: Db, key: str, default=None):
    v = meta(db, key)
    if v is None:
        return default
    try:
        return json.loads(v)
    except ValueError:
        return default


def set_meta_json(db: Db, key: str, obj, wait: bool = True):
    return set_meta(db, key, json.dumps(obj), wait=wait)


def avatars(db: Db) -> dict[str, Avatar]:
    """Hash of a picture's address -> its row. A row with no `ext` is a download that failed at `at`."""
    return {a.hash: a for a in rows.select(db.conn, Avatar)}


def put_avatar(db: Db, a: Avatar, wait: bool = True):
    return db.write(lambda c: rows.insert(c, a, or_="REPLACE"), wait=wait)


def put_avatars(db: Db, avs: list[Avatar]):
    db.write(lambda c: rows.insert_many(c, avs, or_="REPLACE"))
