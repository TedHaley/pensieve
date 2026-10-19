"""The five states of a context card (a task) on the Context tab, how each one is set, and where changes are kept.

A task gets a state automatically from its evidence (`derive`). The user, or an agent over MCP, can set a state by
hand. A state set by hand holds until the task has new activity after it; then the automatic state applies again.
Every change by hand is a row in `task_states` (in the main database), so a task keeps its history."""
import time
from dataclasses import asdict, dataclass
from enum import Enum


class State(str, Enum):
    LATENT = "latent"
    CONSCIOUS = "conscious"
    SUBCONSCIOUS = "subconscious"
    ETCHED = "etched"
    FORGOTTEN = "forgotten"


@dataclass(frozen=True)
class StateModel:
    state: State
    label: str
    meaning: str   # what the state says about the work
    rule: str      # when Pensieve sets it by itself
    color: str


MODELS = {m.state: m for m in [
    StateModel(State.CONSCIOUS, "Conscious", "You are working on it now.",
               "A session is running, or the task has material work (edits, commits, a branch or a PR) and activity in "
               "the last 3 days, and it is not done.", "#34c759"),
    StateModel(State.SUBCONSCIOUS, "Subconscious", "Work that is not done and has gone quiet: revisit it.",
               "Material work that is not done (an open PR, a ticket that is not done, uncommitted files, or work "
               "left), with no activity in the last 3 days.", "#ff9f0a"),
    StateModel(State.LATENT, "Latent", "A ticket you have not started.",
               "A ticket that is not done (or whose status is not known), with no edits, commits, branch or PR yet.",
               "#5e5ce6"),
    StateModel(State.ETCHED, "Etched", "Done.",
               "A PR is merged and nothing happened after the merge, with no PR open. Or its own PR (from its branch) "
               "is merged and no PR is open. Or its PRs are merged or closed, or its ticket is done, and nothing is "
               "left uncommitted.",
               "#8e8e93"),
    StateModel(State.FORGOTTEN, "Forgotten", "Not finished, and it will likely stay that way.",
               "Material work that is not done and quiet for 21 days, with no open PR and no uncommitted work. Or a "
               "conversation with no ticket and no edits, once its session has ended.",
               "#ff453a"),
]}
ORDER = [State.CONSCIOUS, State.SUBCONSCIOUS, State.LATENT, State.ETCHED, State.FORGOTTEN]
RECENT_DAYS = 3
FORGET_DAYS = 21  # the Forgotten rule above says this number
MERGE_GRACE = 15 * 60  # activity this soon after a merge is the wrap-up of the same work
RENAMED = {"discarded": "forgotten"}  # states saved under an earlier name
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
        return State.CONSCIOUS, "a session is running now"
    merged_at = max([p.get("merged_at") or 0 for p in prs if p.get("state") == "merged"] + [0])
    if merged_at and not open_prs and not open_tickets and task.get("updated", 0) <= merged_at + MERGE_GRACE:
        return State.ETCHED, "its PR is merged, and nothing happened after"
    own_merged = any(p.get("own") and p.get("state") == "merged" for p in prs)
    if not open_prs and not open_tickets and own_merged:  # files left uncommitted after a merge are leftovers
        return State.ETCHED, "its PR is merged"
    if not open_prs and not open_tickets and not uncommitted and (merged or done_tickets or (material and _nothing_left(note))):
        return State.ETCHED, "merged" if merged else "ticket done" if done_tickets else "nothing left"
    if not material:  # latent is a ticket not started yet (status unknown counts); a conversation alone is not work
        if any(not (k.get("status") or "").lower() in DONE for k in tickets):
            return State.LATENT, "a ticket with no edits, commits, branch or PR yet"
        return State.FORGOTTEN, "a conversation with no ticket and no work to pick up"
    if not open_prs and not uncommitted and quiet >= FORGET_DAYS:
        return State.FORGOTTEN, f"not done, quiet for {quiet} days, nothing waiting"
    if recent:
        return State.CONSCIOUS, f"work in the last {RECENT_DAYS} days"
    return State.SUBCONSCIOUS, f"not done, quiet for {quiet} days"


def _table(store):
    """Make the table once for each database connection (a reset opens a new one)."""
    if getattr(store, "_task_states_db", None) is store.db:
        return
    store.db.execute("CREATE TABLE IF NOT EXISTS task_states(task_id TEXT, state TEXT, by TEXT, reason TEXT, at REAL)")
    store.db.execute("CREATE INDEX IF NOT EXISTS task_states_id ON task_states(task_id, at)")
    store.db.commit()
    store._task_states_db = store.db


def latest(store) -> dict[str, StateChange]:
    """The last change by hand of each task."""
    with store.lock:
        _table(store)
        rows = store.db.execute("SELECT task_id, state, by, reason, at FROM task_states ORDER BY at").fetchall()
    out = {}
    for tid, st, by, reason, at in rows:
        st = RENAMED.get(st, st)
        if st == "auto" or st in State._value2member_map_:
            out[tid] = StateChange(tid, st, by, reason, at)
    return out


def history(store, task_id: str) -> list[dict]:
    with store.lock:
        _table(store)
        rows = store.db.execute("SELECT state, by, reason, at FROM task_states WHERE task_id=? ORDER BY at",
                                (task_id,)).fetchall()
    return [dict(state=RENAMED.get(s, s), by=b, reason=r, at=a) for s, b, r, a in rows]


def set_state(store, task_id: str, state: str, by: str = "user", reason: str = "") -> StateChange:
    """Set a task's state by hand. "auto" clears it: the automatic state applies again."""
    state = RENAMED.get(state, state)
    if state != "auto" and state not in State._value2member_map_:
        raise ValueError(f"state must be one of: {', '.join(s.value for s in ORDER)}, or auto")
    if by not in ("user", "agent"):
        raise ValueError("by must be user or agent")
    ch = StateChange(task_id, state, by, reason[:500], time.time())
    with store.lock:
        _table(store)
        store.db.execute("INSERT INTO task_states VALUES(?,?,?,?,?)", (task_id, state, by, ch.reason, ch.at))
        store.db.commit()
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
