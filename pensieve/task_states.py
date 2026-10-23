"""The four states of a thread on the Threads tab, how each one is set, and where changes are kept.

A thread gets its state automatically from its sinks (thread_model.thread_state, through sink_status.task_state).
Unknown counts as closed. A task with no sink is closed too: nothing verifies it. The user can set a state by hand on
the Threads tab, and forgotten is only set by hand. A state set by hand holds until the thread has new activity after
it; then the automatic state applies again. Every change by hand is a row in `card_states` (in store.db)."""
import time
from dataclasses import asdict, dataclass
from enum import Enum


class State(str, Enum):
    OPEN = "open"
    THOUGHTS = "thoughts"
    LOOSE_ENDS = "loose_ends"
    CLOSED = "closed"
    FORGOTTEN = "forgotten"


@dataclass(frozen=True)
class StateModel:
    state: State
    label: str
    meaning: str   # what the state says about the work
    rule: str      # when Pensieve sets it by itself
    color: str


MODELS = {m.state: m for m in [
    StateModel(State.OPEN, "Open", "You are working on it now.",
               "A session is running, or the thread has material work (edits, commits, a branch or a PR) and activity "
               "in the last 3 days, and it is not done.", "#34c759"),
    StateModel(State.THOUGHTS, "Thoughts", "A session with no data source: no PR, branch or ticket it wrote to.",
               "A session with no data source, written in the last 3 days. Loose ends after 3 days quiet, forgotten "
               "after 21 days quiet.", "#5ac8fa"),
    StateModel(State.LOOSE_ENDS, "Loose ends","Work that is not done and has gone quiet: close it out.",
               "Work that is not done (an open PR, a ticket that is not done, uncommitted files, or work left), with "
               "no activity in the last 3 days. Also a ticket not started yet, or a thread with no sink to verify.",
               "#ff9f0a"),
    StateModel(State.CLOSED, "Closed", "Done. A closed thread does not reopen: new work starts a new thread.",
               "Every sink it writes to is verified done: its PR is merged or closed, its ticket is done, or its branch "
               "is merged or deleted.",
               "#8e8e93"),
    StateModel(State.FORGOTTEN, "Forgotten", "Not finished, and it will likely stay that way.",
               "Material work that is not done and quiet for 21 days, with no open PR and no uncommitted work. Or a "
               "conversation with no ticket and no edits, once its session has ended.",
               "#ff453a"),
]}
ORDER = [State.OPEN, State.THOUGHTS, State.LOOSE_ENDS, State.CLOSED, State.FORGOTTEN]
RENAMED = {"discarded": "forgotten", "latent": "loose_ends", "conscious": "open", "subconscious": "loose_ends",
           "etched": "closed", "complete": "closed", "todo": "loose_ends", "closing": "loose_ends"}  # earlier names
STORED = {"closed": "complete", "todo": "loose_ends", "closing": "loose_ends"}  # the names the store's CHECK accepts


def stored(value: str) -> str:
    """The name a state is saved under: closed is saved as complete, which the store's CHECK still allows."""
    return STORED.get(value, value)


@dataclass
class StateChange:
    task_id: str
    state: str     # a State value, or "auto" (back to the automatic state)
    by: str        # user | agent
    reason: str
    at: float


def _nothing_left(note: dict) -> bool:
    left = (note.get("next") or note.get("left") or "").strip().rstrip(".").lower()  # "left" in notes before version 8
    return left in ("nothing", "none", "n/a", "nothing left")


def latest(store=None) -> dict[str, StateChange]:
    """The last change by hand of each task (the card_states table of store.db; `store` is not used any more)."""
    from pensieve import store as v2
    from pensieve.store import cards
    return {k: StateChange(k, RENAMED.get(s.state.value, s.state.value), s.by.value, s.reason or "", float(s.at))
            for k, s in cards.latest_states(v2.store_db()).items()}


def history(store, task_id: str) -> list[dict]:
    from pensieve import store as v2
    from pensieve.store import cards
    return [dict(state=RENAMED.get(s.state.value, s.state.value), by=s.by.value, reason=s.reason or "", at=float(s.at))
            for s in cards.state_history(v2.store_db(), task_id)]


def set_state(store, task_id: str, state: str, by: str = "user", reason: str = "") -> StateChange:
    """Set a task's state by hand. "auto" clears it: the automatic state applies again."""
    import math

    from pensieve import store as v2
    from pensieve.models import CardStateSet, SetBy, StateSet
    from pensieve.store import cards
    state = RENAMED.get(state, state)
    if state == State.THOUGHTS.value:
        raise ValueError("thoughts is automatic: a session with no data source")
    if state != "auto" and state not in State._value2member_map_:
        raise ValueError(f"state must be one of: {', '.join(s.value for s in ORDER)}, or auto")
    if by not in ("user", "agent"):
        raise ValueError("by must be user or agent")
    at = math.ceil(time.time())  # whole seconds, rounded up: a change holds over activity of the same second
    ch = StateChange(task_id, state, by, reason[:500], float(at))
    cards.add_state(v2.store_db(), CardStateSet(None, task_id, StateSet(state), SetBy(by), ch.reason, at))
    return ch


def apply(task: dict, note: dict, manual: StateChange | None):
    """Set task["state"] and why: by hand while no activity is newer than that change, else automatically."""
    from pensieve import chains, sink_status, thread_model as tm
    by_sinks, reason, status = sink_status.task_state(task)
    if by_sinks is None:  # no data source: a thought, by its last activity
        by_sinks, why = tm.thought_state(task.get("updated", 0))
    else:
        why = reason
    auto = State(by_sinks.value)
    task["chains"] = chains.build(task, status)
    if manual and manual.state != "auto" and manual.at >= task.get("updated", 0):
        state = RENAMED.get(manual.state, manual.state)
        task.update(state=State(state).value, state_by=manual.by, state_why=manual.reason or f"set by {manual.by}")
    else:
        task.update(state=auto.value, state_by="auto", state_why=why)


def models() -> list[dict]:
    return [{**asdict(MODELS[s]), "state": s.value} for s in ORDER]
