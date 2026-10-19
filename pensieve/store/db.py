"""One SQLite file of storage v2: a connection for each thread that reads, and one writer thread.

- `read(fn)` runs fn(conn) on the calling thread's own connection. In WAL mode a reader never waits for the writer,
  and there is no process-wide lock. The connection is in autocommit mode and `query_only`: each statement sees the
  last commit.
- `write(fn)` sends fn(conn) to the writer thread and waits until its commit, then returns what fn returned (or
  raises what it raised). `write(fn, wait=False)` returns a Future at once.
- The writer runs each job in its own savepoint, and commits the jobs that are queued together (up to 50 ms of
  them) in one transaction. A job that fails rolls back only its own changes.
- `after_commit` callbacks get the results of each committed batch on the writer thread (the card events use this).
- `hold()` lends the writer's connection to the calling thread for a block of statements, as one job: the code that
  indexes sessions, files and code writes this way (`Conn`). Other writes wait until the block ends, and reads never
  wait. Inside the block, `conn`, `read` and `write` on this thread use the lent connection, so the block sees its own
  writes.

Another process (the MCP command) can open the same file with its own Db: WAL lets it read while this one writes,
and `busy_timeout` makes its writes wait for this one's commit."""
import queue
import sqlite3
import threading
import time
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path

BATCH_SECONDS = 0.05
BUSY_MS = 10000


class Db:
    def __init__(self, path: Path, schema: str, version: int, migrations: dict[int, list[str]] | None = None,
                 functions: dict | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.functions = functions or {}
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._q: queue.Queue = queue.Queue()
        self._writer: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._closed = False
        self.after_commit: list = []  # fn(list of job results) after each batch's commit, on the writer thread
        self._init(schema, version, migrations or {})

    def _connect(self, readonly: bool) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=BUSY_MS / 1000)
        c.execute(f"PRAGMA busy_timeout = {BUSY_MS}")
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA synchronous = NORMAL")
        for name, fn in self.functions.items():
            c.create_function(name, 1, fn, deterministic=True)
        if readonly:
            c.execute("PRAGMA query_only = ON")
        with self._conns_lock:
            self._conns.append(c)
        return c

    def _init(self, schema: str, version: int, migrations: dict[int, list[str]]):
        c = sqlite3.connect(self.path, isolation_level=None, timeout=BUSY_MS / 1000)
        try:
            c.execute(f"PRAGMA busy_timeout = {BUSY_MS}")
            c.execute("PRAGMA journal_mode = WAL")
            c.execute("BEGIN IMMEDIATE")
            try:
                for stmt in _statements(schema):
                    c.execute(stmt)
                row = c.execute("SELECT version FROM schema_version").fetchone()
                have = row[0] if row else None
                if have is None:
                    c.execute("INSERT INTO schema_version VALUES (?)", (version,))
                elif have > version:
                    raise RuntimeError(f"{self.path.name} has schema version {have}, newer than this app's {version}")
                else:
                    for v in range(have + 1, version + 1):
                        for stmt in migrations.get(v, []):
                            c.execute(stmt)
                    c.execute("UPDATE schema_version SET version = ?", (version,))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        finally:
            c.close()

    def add_function(self, name: str, fn):
        """A SQL function on every connection of this file, now and later (triggers on the writer use them)."""
        self.functions[name] = fn
        with self._conns_lock:
            for c in self._conns:
                c.create_function(name, 1, fn, deterministic=True)

    def held(self) -> sqlite3.Connection | None:
        """The writer's connection when this thread holds it (`hold`) or is the writer thread, else None."""
        if threading.current_thread() is self._writer:
            return self._wconn
        return getattr(self._local, "held", None)

    @contextmanager
    def hold(self):
        """Run the block with the writer's connection on this thread, as one write job: its statements commit
        together after the block, and roll back when the block raises. Yields the connection. Nested holds on one
        thread are one hold. Do no slow work inside: other writes to this file wait for the block."""
        c = self.held()
        if c is not None:
            yield c
            return
        if self._closed:
            raise RuntimeError(f"{self.path.name} is closed")
        self._start()
        lent, back, out = threading.Event(), threading.Event(), {}

        def job(c):
            lent.set()
            back.wait()
            if out.get("error") is not None:
                raise out["error"]

        f = Future()
        self._q.put((job, f))
        while not lent.wait(0.5):
            if f.done():  # the batch failed before the job ran (BEGIN failed)
                f.result()
        self._local.held = self._wconn
        try:
            yield self._wconn
        except BaseException as e:
            out["error"] = e
            raise
        finally:
            self._local.held = None
            back.set()
            try:
                f.result()
            except BaseException:
                if out.get("error") is None:
                    raise

    @property
    def conn(self) -> sqlite3.Connection:
        """This thread's read connection (the writer's, while this thread holds it)."""
        held = self.held()
        if held is not None:
            return held
        c = getattr(self._local, "conn", None)
        if c is None:
            if self._closed:
                raise RuntimeError(f"{self.path.name} is closed")
            c = self._local.conn = self._connect(readonly=True)
        return c

    def read(self, fn):
        """fn(conn) on this thread's connection; no lock."""
        return fn(self.conn)

    def query(self, sql: str, args=()) -> list[tuple]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        return self.conn.execute(sql, args).fetchone()

    def write(self, fn, wait: bool = True):
        """Run fn(conn) on the writer thread, inside a transaction. With `wait`, returns fn's result after the commit
        (or raises fn's error); else returns a Future of it."""
        if self._closed:
            raise RuntimeError(f"{self.path.name} is closed")
        held = self.held()
        if held is not None:  # a job (or a hold) that writes more: run it in the same batch
            return fn(held) if wait else _done(fn(held))
        self._start()
        f = Future()
        self._q.put((fn, f))
        return f.result() if wait else f

    def flush(self):
        """Wait until every write sent so far is committed."""
        self.write(lambda c: None)

    def _start(self):
        if self._writer is None:
            with self._start_lock:
                if self._writer is None:
                    self._wconn = self._connect(readonly=False)
                    self._writer = threading.Thread(target=self._loop, daemon=True, name=f"db-writer-{self.path.name}")
                    self._writer.start()

    def _loop(self):
        c = self._wconn
        while True:
            job = self._q.get()
            if job is None:
                return
            batch, end = [job], time.monotonic() + BATCH_SECONDS
            while time.monotonic() < end:  # the jobs queued now join the batch, up to 50 ms of them
                try:
                    job = self._q.get_nowait()
                except queue.Empty:
                    break
                if job is None:
                    self._q.put(None)
                    break
                batch.append(job)
            self._run(c, batch)

    def _run(self, c: sqlite3.Connection, batch: list):
        results = []
        try:
            c.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as e:
            for _, f in batch:
                f.set_exception(e)
            return
        for i, (fn, f) in enumerate(batch):
            c.execute(f"SAVEPOINT j{i}")
            try:
                results.append((f, fn(c), None))
                c.execute(f"RELEASE j{i}")
            except BaseException as e:
                c.execute(f"ROLLBACK TO j{i}")
                c.execute(f"RELEASE j{i}")
                results.append((f, None, e))
        try:
            c.execute("COMMIT")
        except sqlite3.Error as e:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            for f, _, _ in results:
                f.set_exception(e)
            return
        for f, r, e in results:
            if e is not None:
                f.set_exception(e)
            else:
                f.set_result(r)
        ok = [r for _, r, e in results if e is None and r is not None]
        for cb in list(self.after_commit):
            try:
                cb(ok)
            except Exception:
                import traceback
                traceback.print_exc()

    def close(self):
        """Commit what is queued, stop the writer and close every connection."""
        if self._closed:
            return
        if self._writer is not None:
            self.flush()
            self._q.put(None)
            self._writer.join(timeout=5)
        self._closed = True
        with self._conns_lock:
            for c in self._conns:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._conns.clear()


_READ = ("SELECT", "WITH", "EXPLAIN", "VALUES")
_IGNORED_PRAGMAS = ("JOURNAL_MODE", "SYNCHRONOUS", "BUSY_TIMEOUT", "FOREIGN_KEYS")


class _Ran:
    """The result of a statement that ran on the writer thread: its rows, rowcount and lastrowid."""

    def __init__(self, cur):
        self.rows = cur.fetchall() if cur.description else []
        self.rowcount, self.lastrowid = cur.rowcount, cur.lastrowid
        self.description = cur.description

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class Conn:
    """A sqlite3.Connection look-alike over a Db, for the code that indexes sessions, files and code (indexer.py,
    files.py, repos.py), which was written for one shared connection and a process lock.

    - In a `db.hold()` block (and on the writer thread), every statement runs on the writer's connection.
    - Outside one, a read runs on this thread's read connection and never waits; a write runs as its own job.
    - `commit` does nothing: a hold commits when its block ends, and a single write commits at once."""

    def __init__(self, db: Db):
        self.db = db

    def execute(self, sql: str, args=()):
        c = self.db.held()
        if c is not None:
            return c.execute(sql, args)
        head = sql.lstrip().split(None, 2)
        verb = head[0].upper() if head else ""
        if verb == "PRAGMA" and len(head) > 1 and head[1].split("=")[0].strip().upper() in _IGNORED_PRAGMAS:
            return _Ran(self.db.conn.execute("SELECT 1 WHERE 0"))
        if verb in _READ or (verb == "PRAGMA" and "=" not in sql):
            return self.db.conn.execute(sql, args)
        return self.db.write(lambda c: _Ran(c.execute(sql, args)))

    def executemany(self, sql: str, rows):
        c = self.db.held()
        if c is not None:
            return c.executemany(sql, rows)
        rows = list(rows)
        return self.db.write(lambda c: _Ran(c.executemany(sql, rows)))

    def executescript(self, sql: str):
        """DDL: each statement of the script, in one job."""
        def run(c):
            for stmt in _statements(sql):
                c.execute(stmt)
        self.db.write(run)

    def create_function(self, name, n, fn, deterministic=True):
        self.db.add_function(name, fn)

    def commit(self):
        pass

    def close(self):
        pass


def _done(value) -> Future:
    f = Future()
    f.set_result(value)
    return f


def _statements(script: str) -> list[str]:
    """The statements of a schema script, split where sqlite3 says a statement is complete."""
    out, cur = [], ""
    for part in script.split(";"):  # a statement ends at a ";" where sqlite3 says it is complete (not inside a trigger)
        cur += part + ";"
        if sqlite3.complete_statement(cur):
            if cur.strip():
                out.append(cur.strip())
            cur = ""
    cur = cur[:-1].strip()  # the ";" added after the last part
    if cur:
        out.append(cur)
    return out
