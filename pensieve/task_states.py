"""The six states of a thread on the Threads tab, how each one is set, and where changes are kept.

A thread gets its state automatically from its sinks (thread_model.thread_state, through sink_status.task_state).
Unknown counts as closed. A thread with no sink of its own takes the state of the sinks it contributed to, else it is a
thought (thread_model.thought_state). A ticket of the user's with no work on it is a to-do. Work with a sink is never
forgotten automatically: the user sets it aside by hand. A state set by hand holds until the thread has new activity
after it; then the automatic state applies again. Every change by hand is a row in `card_states` (in store.db)."""
import time
from dataclasses import asdict, dataclass
from enum import Enum


class State(str, Enum):
    OPEN = "open"
    TODO = "todo"
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
    StateModel(State.TODO, "To-do", "A ticket of yours that no session, branch or PR has picked up.",
               "A ticket assigned to you, or yours, that is not done, with no session, branch or PR of yours on it. "
               "Open once one starts.", "#bf5af2"),
    StateModel(State.THOUGHTS, "Thoughts", "A session with no data source: no PR, branch or ticket it wrote to.",
               "A session with no data source, active in the last 2 days. Forgotten after 2 days quiet.", "#5ac8fa"),
    StateModel(State.LOOSE_ENDS, "Loose ends","Work that is not done and has gone quiet: close it out.",
               "Work that is not done (an open PR, a ticket that is not done, uncommitted files, or work left), with "
               "no activity in the last 3 days.",
               "#ff9f0a"),
    StateModel(State.CLOSED, "Closed", "Done. A closed thread does not reopen: new work starts a new thread.",
               "Every sink it writes to is verified done: its PR is merged or closed, its ticket is done, or its branch "
               "is merged or deleted.",
               "#8e8e93"),
    StateModel(State.FORGOTTEN, "Forgotten", "Not finished, and it will likely stay that way.",
               "A thought quiet for 2 days, or a thread you set aside by hand. Work with a data source that is not done "
               "stays a loose end.",
               "#ff453a"),
]}
ORDER = [State.OPEN, State.TODO, State.THOUGHTS, State.LOOSE_ENDS, State.CLOSED, State.FORGOTTEN]
RENAMED = {"discarded": "forgotten", "latent": "loose_ends", "conscious": "open", "subconscious": "loose_ends",
           "etched": "closed", "complete": "closed", "closing": "loose_ends"}  # earlier names
STORED = {"closed": "complete", "closing": "loose_ends"}  # the names the store's CHECK accepts


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


def _todo(task: dict) -> bool:
    """A to-do: a ticket that is not done, and no session, branch, PR or commit of the user on it."""
    return bool(task.get("tickets")) and not task.get("sessions") and not task.get("branches") \
        and not task.get("prs") and not task.get("commits")


def apply(task: dict, manual: StateChange | None):
    """Set task["state"] and why: by hand while no activity is newer than that change, else automatically."""
    from pensieve import chains, sink_status, thread_model as tm
    by_sinks, reason, status = sink_status.task_state(task)
    if by_sinks is None and task.get("contributed"):  # it wrote to sinks another thread holds: their state is its state
        c = task["contributed"]
        by_sinks, reason, status = sink_status.task_state({**task, "branches": c["branches"], "prs": c["prs"], "tickets": c["tickets"]})
        reason = f"contributed to {', '.join(b['branch'] for b in c['branches']) or 'its sinks'}: {reason}"
    if by_sinks is None:  # no data source: a thought, by its last activity
        by_sinks, why = tm.thought_state(task.get("updated", 0))
    else:
        why = reason
    auto = State(by_sinks.value)
    if auto in (State.OPEN, State.LOOSE_ENDS) and _todo(task):
        auto, why = State.TODO, "a ticket of yours with no session, branch or PR on it yet"
    task["chains"] = chains.build(task, status)
    if manual and manual.state != "auto" and manual.at >= task.get("updated", 0):
        state = RENAMED.get(manual.state, manual.state)
        task.update(state=State(state).value, state_by=manual.by, state_why=manual.reason or f"set by {manual.by}",
                    next=None)
    else:
        task.update(state=auto.value, state_by="auto", state_why=why, next=next_state(auto))


def next_state(state: State) -> dict | None:
    """The state that a thread's quiet time moves it to, and after how many seconds of quiet (counted from its
    `updated`), or None when time alone never changes it: open goes to loose ends, a thought is forgotten. Loose ends
    are never forgotten by time: work with a data source that is not done stays in view."""
    from pensieve import thread_model as tm
    after = {State.OPEN: (State.LOOSE_ENDS, tm.RECENT_DAYS), State.THOUGHTS: (State.FORGOTTEN, tm.THOUGHT_DAYS)}
    if state not in after:
        return None
    to, days = after[state]
    return dict(state=to.value, after=days * 86400)


def models() -> list[dict]:
    return [{**asdict(MODELS[s]), "state": s.value} for s in ORDER]
