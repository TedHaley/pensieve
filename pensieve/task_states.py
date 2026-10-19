"""The five states of a thread on the Threads tab, how each one is set, and where changes are kept.

A thread gets a state automatically from its evidence (`derive`). The user can set a state by hand on the Threads
tab. A state set by hand holds until the thread has new activity after it; then the automatic state applies again.
Every change by hand is a row in `card_states` (in store.db), so a thread keeps its history."""
import time
from dataclasses import asdict, dataclass
from enum import Enum


class State(str, Enum):
    TODO = "todo"
    OPEN = "open"
    LOOSE_ENDS = "loose_ends"
    COMPLETE = "complete"
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
    StateModel(State.COMPLETE, "Complete", "Done.",
               "A PR is merged and nothing happened after the merge, with no PR open. Or its own PR (from its branch) "
               "is merged and no PR is open. Or its PRs are merged or closed, or its ticket is done, or its work is in "
               "the default branch (its branch is merged, or its commits are there and its note says nothing is left), "
               "and nothing is left uncommitted.",
               "#8e8e93"),
    StateModel(State.FORGOTTEN, "Forgotten", "Not finished, and it will likely stay that way.",
               "Material work that is not done and quiet for 21 days, with no open PR and no uncommitted work. Or a "
               "conversation with no ticket and no edits, once its session has ended.",
               "#ff453a"),
]}
ORDER = [State.OPEN, State.LOOSE_ENDS, State.TODO, State.COMPLETE, State.FORGOTTEN]
RECENT_DAYS = 3
FORGET_DAYS = 21  # the Forgotten rule above says this number
MERGE_GRACE = 15 * 60  # activity this soon after a merge is the wrap-up of the same work
RENAMED = {"discarded": "forgotten", "latent": "todo", "conscious": "open", "subconscious": "loose_ends",
           "etched": "complete"}  # states saved under an earlier name
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
        return State.COMPLETE, "its PR is merged, and nothing happened after"
    own_merged = any(p.get("own") and p.get("state") == "merged" for p in prs)
    if not open_prs and not open_tickets and own_merged:  # files left uncommitted after a merge are leftovers
        return State.COMPLETE, "its PR is merged"
    if not open_prs and not open_tickets and not uncommitted and task.get("integrated"):  # see work._integrated
        return State.COMPLETE, task["integrated"]["why"]
    if not open_prs and not open_tickets and not uncommitted and (merged or done_tickets or (material and _nothing_left(note))):
        return State.COMPLETE, "merged" if merged else "ticket done" if done_tickets else "nothing left"
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
    return {k: StateChange(k, s.state.value, s.by.value, s.reason or "", float(s.at))
            for k, s in cards.latest_states(v2.store_db()).items()}


def history(store, task_id: str) -> list[dict]:
    from pensieve import store as v2
    from pensieve.store import cards
    return [dict(state=s.state.value, by=s.by.value, reason=s.reason or "", at=float(s.at))
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
    if manual and manual.state != "auto" and manual.at >= task.get("updated", 0):
        task.update(state=State(manual.state).value, state_by=manual.by, state_why=manual.reason or f"set by {manual.by}")
    else:
        task.update(state=auto.value, state_by="auto", state_why=why)


def models() -> list[dict]:
    return [{**asdict(MODELS[s]), "state": s.value} for s in ORDER]
