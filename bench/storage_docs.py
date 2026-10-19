"""Storage v2, documents: load N synthetic items through connections.store, store them again unchanged (a re-sync),
and read the items of the last 30 days the way the Context tab does (work._items). Runs in a new temporary
PENSIEVE_DATA. Usage: python bench/storage_docs.py 10000 100000"""
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

os.environ["PENSIEVE_DATA"] = tempfile.mkdtemp(prefix="pensieve-bench-docs-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import connections, store, work  # noqa: E402
from pensieve.store import documents  # noqa: E402

DAY = 86400
SOURCES = ["jira_a", "linear_b", "github_c", "notion_d", "hubspot_e"]
WORDS = ("index search card session ticket build deploy cache retry queue worker model vector token people org "
         "project branch commit review merge test alert metric owner team release").split()


NOW = time.time()


def items(n: int, seed: int = 1):
    rnd, now = random.Random(seed), NOW
    for i in range(n):
        upd = now - rnd.random() * 180 * DAY
        yield dict(key=f"ABC-{i + 1}", summary=" ".join(rnd.choices(WORDS, k=8)).capitalize(),
                   status=rnd.choice(["To Do", "In Progress", "Done"]),
                   assignee={"displayName": f"Person {rnd.randrange(200)}", "emailAddress": f"p{rnd.randrange(200)}@example.com"},
                   updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(upd)),
                   created=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(upd - 3 * DAY)),
                   description=" ".join(rnd.choices(WORDS, k=80)))


def load(n: int) -> float:
    per = n // len(SOURCES)
    t = time.perf_counter()
    for s, sid in enumerate(SOURCES):
        batch = []
        for it in items(per, seed=s):
            batch.append(it)
            if len(batch) == 100:
                connections.store(sid, sid, "search_issues", {}, json.dumps({"issues": batch}))
                batch = []
        if batch:
            connections.store(sid, sid, "search_issues", {}, json.dumps({"issues": batch}))
    return time.perf_counter() - t


def run(n: int):
    store.close_all()
    for p in store.root().glob("*.db*"):
        p.unlink()
    connections._sources_seen.clear()
    first = load(n)
    again = load(n)
    db = store.store_db()
    assert documents.count(db) == n // len(SOURCES) * len(SOURCES), documents.count(db)
    since = time.time() - 30 * DAY
    times = []
    for _ in range(5):
        t = time.perf_counter()
        got = work._items(since)
        times.append(time.perf_counter() - t)
    t = time.perf_counter()
    documents.window(db, int(since))
    rows_only = time.perf_counter() - t
    size = sum(p.stat().st_size for p in store.root().glob("store.db*")) / 1e6
    print(f"{n:>7} docs: first load {first:6.1f} s ({n / first:,.0f}/s), re-sync unchanged {again:6.2f} s, "
          f"30-day scan {min(times):.3f} s for {len(got)} items (rows only {rows_only:.3f} s), store.db {size:.0f} MB",
          flush=True)


if __name__ == "__main__":
    for arg in sys.argv[1:] or ["10000"]:
        run(int(arg))
    store.close_all()
