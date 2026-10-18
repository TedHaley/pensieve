"""Fill a copy of the index with 30 days of card history, as the keeper (pensieve/history.py) would have saved it:
the tasks as of every `step` hours (work.tasks_at), a note for each new snapshot (work.make_note, with the copy's
insights engine), and the keeper's rules for what to save. For benchmarks of agents that read a thread's history.

  PENSIEVE_DATA=<a copy of ~/.pensieve> .venv/bin/python bench/history_backfill.py --days 30 --step 12

It moves the copy's <data>/threads aside first, and leaves the linker's state behind, so a server on the copy goes on
from the backfill with the same thread ids. Never run it on the real ~/.pensieve."""
import argparse
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    from pensieve import config, history, threads, work
    from threads_replay import _Store
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--step", type=float, default=12)
    ap.add_argument("--jobs", type=int, default=6)
    a = ap.parse_args()
    if config.DATA_DIR.resolve() == (Path.home() / ".pensieve").resolve():
        sys.exit("refusing to backfill the real index: set PENSIEVE_DATA to a copy")
    if history._dir().exists():
        shutil.move(str(history._dir()), str(history._dir()) + f".before-backfill-{int(time.time())}")
    store = _Store(config.DATA_DIR / "pensieve.db")
    keeper, notes, now = history.Keeper(store), {}, time.time()  # notes: snapshot id -> note
    for k in range(int(a.days * 24 / a.step), -1, -1):
        until = now - k * a.step * 3600
        ts = [t for t in work.tasks_at(store, until, 30)
              if t["files"] or t["branches"] or t["prs"] or t["keys"] or t["commits"]]
        ids = {t["id"]: threads.snapshot_id(threads.members(t)) for t in ts}
        todo = [t for t in ts if ids[t["id"]] not in notes]

        def one(t):
            try:
                return ids[t["id"]], work.make_note(t)
            except Exception as e:  # the engine is off: the card goes without a note
                print(f"no note for {t['id']}: {e}", file=sys.stderr)
                return ids[t["id"]], {}
        with ThreadPoolExecutor(a.jobs) as ex:
            notes.update(dict(ex.map(one, todo)))
        saved = keeper.run_once(now=until, tasks=ts, notes={t["id"]: notes.get(ids[t["id"]], {}) for t in ts})
        print(f"{time.strftime('%m-%d %H:%M', time.localtime(until))}: {len(ts)} tasks, {len(todo)} notes written, "
              f"{len(saved)} cards saved", file=sys.stderr)
    print(f"{len(history.listing())} threads with history in {history._dir()}", file=sys.stderr)


if __name__ == "__main__":
    main()
