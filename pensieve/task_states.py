"""The six states of a thread on the Threads tab, how each one is set, and where changes are kept.

A thread gets a state automatically from its evidence (`derive`). The user can set a state by hand on the Threads
tab. A state set by hand holds until the thread has new activity after it; then the automatic state applies again.
Every change by hand is a row in `card_states` (in store.db), so a thread keeps its history.

Two paths lead out of work. Loose ends -> forgotten: work is left and has gone quiet for FORGET_DAYS. Closing ->
closed: nothing is left, and no outside evidence says so; after CLOSE_DAYS with no change it is closed. A closed
thread does not reopen. New work starts a new thread."""
import time
from dataclasses import asdict, dataclass
from enum import Enum


class State(str, Enum):
    TODO = "todo"
    OPEN = "open"
    LOOSE_ENDS = "loose_ends"
    CLOSING = "closing"
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
    StateModel(State.LOOSE_ENDS, "Loose ends", "Work that is not done and has gone quiet: close it out.",
               "Material work that is not done (an open PR, a ticket that is not done, uncommitted files, or work "
               "left), with no activity in the last 3 days.", "#ff9f0a"),
    StateModel(State.TODO, "To do", "A ticket you have not started.",
               "A ticket that is not done (or whose status is not known), with no edits, commits, branch or PR yet.",
               "#5e5ce6"),
    StateModel(State.CLOSING, "Closing", "Nothing is left to do. No outside evidence says it is done yet.",
               "Material work with nothing left in its note, no open PR, no open ticket, nothing uncommitted, and no "
               "merged PR or done ticket. It becomes Closed after 7 days with no change.",
               "#64d2ff"),
    StateModel(State.CLOSED, "Closed", "Done. A closed thread does not reopen: new work starts a new thread.",
               "Outside evidence says it is done: its PR is merged or closed, its ticket is done, or its work is in the "
               "default branch, with nothing left uncommitted. Or it has been closing for 7 days with no change.",
               "#8e8e93"),
    StateModel(State.FORGOTTEN, "Forgotten", "Not finished, and it will likely stay that way.",
               "Material work that is not done and quiet for 21 days, with no open PR and no uncommitted work. Or a "
               "conversation with no ticket and no edits, once its session has ended.",
               "#ff453a"),
]}
ORDER = [State.OPEN, State.LOOSE_ENDS, State.TODO, State.CLOSING, State.CLOSED, State.FORGOTTEN]
RECENT_DAYS = 3
FORGET_DAYS = 21  # the Forgotten rule above says this number: loose ends left this long become forgotten
CLOSE_DAYS = 7  # the Closed rule above says this number: closing for this long becomes closed
MERGE_GRACE = 15 * 60  # activity this soon after a merge is the wrap-up of the same work
RENAMED = {"discarded": "forgotten", "latent": "todo", "conscious": "open", "subconscious": "loose_ends",
           "etched": "closed", "complete": "closed"}  # states saved under an earlier name
DONE = {"done", "closed", "resolved", "won't do", "wont do", "cancelled", "canceled", "merged", "complete", "completed",
        "fixed", "invalid", "duplicate", "released", "shipped"}


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


def derive(task: dict, note: dict, now: float | None = None) -> tuple[State, str]:
    """The automatic state of a task, and why."""
    now = now or time.time()
    prs = task.get("prs", [])
    open_prs = [p for p in prs if p.get("state") in ("open", "draft")]
    merged = [p for p in prs if p.get("state") in ("merged", "closed")]
    tickets = task.get("tickets", [])
    open_tickets = [k for k in tickets if k.get("status") and k["status"].lower() not in DONE]
    done_tickets = [k for k in tickets if k.get("status") and k["status"].lower() in DONE]
    feature = {b["branch"] for b in task.get("branches", [])}
    uncommitted = any(p.get("changed") and p.get("branch") in feature and not p.get("head")
                      for p in task.get("places", []))
    material = bool(task.get("files") or task.get("commits") or prs or feature)
    recent = now - task.get("updated", 0) < RECENT_DAYS * 86400
    quiet = int((now - task.get("updated", now)) // 86400)
    if task.get("active"):
        return State.OPEN, "a session is running now"
    merged_at = max([p.get("merged_at") or 0 for p in prs if p.get("state") == "merged"] + [0])
    if merged_at and not open_prs and not open_tickets and task.get("updated", 0) <= merged_at + MERGE_GRACE:
        return State.CLOSED, "its PR is merged, and nothing happened after"
    own_merged = any(p.get("own") and p.get("state") == "merged" for p in prs)
    if not open_prs and not open_tickets and own_merged:  # files left uncommitted after a merge are leftovers
        return State.CLOSED, "its PR is merged"
    if not open_prs and not open_tickets and not uncommitted and task.get("integrated"):  # see work._integrated
        return State.CLOSED, task["integrated"]["why"]
    if not open_prs and not open_tickets and not uncommitted and (merged or done_tickets):
        return State.CLOSED, "merged" if merged else "ticket done"
    if not open_prs and not open_tickets and not uncommitted and material and _nothing_left(note):
        if quiet >= CLOSE_DAYS:  # no outside evidence, and no change for the whole closing period
            return State.CLOSED, f"nothing left for {quiet} days, with no change"
        return State.CLOSING, "nothing left, and no outside evidence yet"
    if not material:  # to do is a ticket not started yet (status unknown counts); a conversation alone is not work
        if any(not (k.get("status") or "").lower() in DONE for k in tickets):
            return State.TODO, "a ticket with no edits, commits, branch or PR yet"
        return State.FORGOTTEN, "a conversation with no ticket and no work to pick up"
    if not open_prs and not uncommitted and quiet >= FORGET_DAYS:
        return State.FORGOTTEN, f"not done, quiet for {quiet} days, nothing waiting"
    if recent:
        return State.OPEN, f"work in the last {RECENT_DAYS} days"
    return State.LOOSE_ENDS, f"not done, quiet for {quiet} days"


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
    auto, why = derive(task, note)
    from pensieve import sink_status
    done, reason = sink_status.task_closed_by_sinks(task)
    if done:  # every sink the task writes to is verified done (sink_status.py)
        auto, why = State.CLOSED, reason
    if manual and manual.state != "auto" and manual.at >= task.get("updated", 0):
        state = RENAMED.get(manual.state, manual.state)
        task.update(state=State(state).value, state_by=manual.by, state_why=manual.reason or f"set by {manual.by}")
    else:
        task.update(state=auto.value, state_by="auto", state_why=why)


def models() -> list[dict]:
    return [{**asdict(MODELS[s]), "state": s.value} for s in ORDER]
