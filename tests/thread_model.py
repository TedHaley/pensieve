"""Tests of the work model (pensieve/thread_model.py): scenes, items, areas and the no-evidence closing rule.
Run: python tests/thread_model.py"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import thread_model as tm  # noqa: E402
from pensieve.task_states import State  # noqa: E402

DAY = 86400
NOW = time.time()


def scene(index, last=tm.LastMessage.ACTION, kind=tm.SceneKind.WORK, session="claude:s1"):
    return tm.Scene(session_id=session, index=index, exchanges=(index, index), last=last, kind=kind)


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))
    return ok


def run():
    results = []
    results.append(check("scene id is session#index", scene(3).id, "claude:s1#3"))
    results.append(check("question scene becomes an item in Questions",
                         tm.item_for(scene(1, kind=tm.SceneKind.QUESTION)).area_id, "questions"))
    results.append(check("comment scene becomes an item in Comments",
                         tm.item_for(scene(1, kind=tm.SceneKind.COMMENT)).area_id, "comments"))
    results.append(check("work scene is not an item", tm.item_for(scene(1)), None))

    areas = {a.id: a for a in tm.builtin_areas()}
    results.append(check("builtin areas: Unfiled, Questions, Comments", sorted(areas),
                         ["comments", "questions", "unfiled"]))
    results.append(check("placeholder areas are placeholders", areas["questions"].kind, tm.AreaKind.PLACEHOLDER))

    t = tm.Thread(id="t1", goal="Settings")
    results.append(check("a thread sits in Unfiled by default", t.area_id, tm.UNFILED))

    results.append(check("closing: last message leaves nothing, quiet 0",
                         tm.no_evidence_state([scene(0), scene(1, tm.LastMessage.NOTHING)], NOW, NOW)[0],
                         State.CLOSING))
    results.append(check("closing holds at 6 days",
                         tm.no_evidence_state([scene(1, tm.LastMessage.NOTHING)], NOW - 6 * DAY, NOW)[0],
                         State.CLOSING))
    results.append(check("closed after 7 days with no change",
                         tm.no_evidence_state([scene(1, tm.LastMessage.NOTHING)], NOW - 7 * DAY, NOW)[0],
                         State.CLOSED))
    results.append(check("an action left: open while recent",
                         tm.no_evidence_state([scene(1, tm.LastMessage.ACTION)], NOW, NOW)[0], State.OPEN))
    results.append(check("an action left, quiet 10 days: loose ends",
                         tm.no_evidence_state([scene(1, tm.LastMessage.ACTION)], NOW - 10 * DAY, NOW)[0],
                         State.LOOSE_ENDS))
    results.append(check("an action left, quiet 21 days: forgotten",
                         tm.no_evidence_state([scene(1, tm.LastMessage.ACTION)], NOW - 21 * DAY, NOW)[0],
                         State.FORGOTTEN))
    results.append(check("only the last scene decides",
                         tm.no_evidence_state([scene(1, tm.LastMessage.NOTHING), scene(2, tm.LastMessage.ACTION)],
                                              NOW, NOW)[0], State.OPEN))

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
