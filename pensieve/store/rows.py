"""Model <-> row helpers that the repository modules share. A model's fields are its table's columns, in order."""
import sqlite3

from pensieve import models


def _auto_id(cls, obj) -> bool:
    """An `id` field that is None: SQLite gives the row its id."""
    cols = models.columns(cls)
    return cols[0] == "id" and getattr(obj, "id") is None


def insert(c: sqlite3.Connection, obj, or_: str = "") -> int:
    """Insert a model's row (`or_`: 'REPLACE' or 'IGNORE'). Returns the row id."""
    cls = type(obj)
    cols, vals = models.columns(cls), models.values(obj)
    if _auto_id(cls, obj):
        cols, vals = cols[1:], vals[1:]
    sql = f"INSERT {'OR ' + or_ if or_ else ''} INTO {cls.__table__} ({', '.join(cols)}) " \
          f"VALUES ({', '.join('?' * len(cols))})"
    return c.execute(sql, vals).lastrowid


def insert_many(c: sqlite3.Connection, objs, or_: str = ""):
    objs = list(objs)
    if not objs:
        return
    cls = type(objs[0])
    cols = models.columns(cls)
    c.executemany(f"INSERT {'OR ' + or_ if or_ else ''} INTO {cls.__table__} ({', '.join(cols)}) "
                  f"VALUES ({', '.join('?' * len(cols))})", [models.values(o) for o in objs])


def upsert(c: sqlite3.Connection, obj, key: tuple[str, ...], keep: tuple[str, ...] = ()) -> None:
    """Insert a row, or update the row with the same `key` columns. Columns in `keep` keep their stored value."""
    cls = type(obj)
    cols, vals = models.columns(cls), models.values(obj)
    if _auto_id(cls, obj):
        cols, vals = cols[1:], vals[1:]
    sets = [f"{k} = excluded.{k}" for k in cols if k not in key and k not in keep]
    c.execute(f"INSERT INTO {cls.__table__} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
              f"ON CONFLICT ({', '.join(key)}) DO " + (f"UPDATE SET {', '.join(sets)}" if sets else "NOTHING"), vals)


def select(c: sqlite3.Connection, cls, where: str = "", args=(), order: str = "") -> list:
    sql = f"SELECT {', '.join(models.columns(cls))} FROM {cls.__table__}"
    if where:
        sql += f" WHERE {where}"
    if order:
        sql += f" ORDER BY {order}"
    return [models.from_row(cls, r) for r in c.execute(sql, args)]


def select_one(c: sqlite3.Connection, cls, where: str, args=()):
    r = select(c, cls, where, args)
    return r[0] if r else None


def marks(n: int) -> str:
    return ", ".join("?" * n) or "NULL"
