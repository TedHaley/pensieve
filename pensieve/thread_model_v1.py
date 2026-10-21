"""The model of work for threads built from sessions.

  Session    one agent transcript. Its components are its children.
  Component  one run of exchanges on one theme (segment.components). It belongs to one session. Some components
             carry support data (repo, PR, branch, ticket). Many carry none.
  Goal       what components work toward or achieve. Not every component has one.
  Thread     one goal, followed over time. It holds one or more components that share that goal. One thread per goal.
  Area       groups threads. A thread may have no area.
  Item       a standalone question or comment. It sits in a placeholder area. It has no goal and never closes.

A component is a work component, a question or a comment. Only work components join threads. A question or comment
becomes an item.

Closing without evidence: a thread with no PR, ticket or commit data can still close. When the last component of its
thread ends with nothing left to do, the thread is closing. It becomes closed after CLOSE_DAYS with no change. The
rule is `no_evidence_state`. It reads what the component says, not external data."""
from dataclasses import dataclass, field
from enum import Enum
import time

from pensieve.task_states import CLOSE_DAYS, FORGET_DAYS, RECENT_DAYS, State


class ComponentKind(str, Enum):
    WORK = "work"
    QUESTION = "question"
    COMMENT = "comment"


class LastMessage(str, Enum):
    """What the last message of a component leaves: an action for someone, a question, or nothing to do."""
    ACTION = "action"
    QUESTION = "question"
    NOTHING = "nothing"  # no clear action, nothing to do
    UNCLEAR = "unclear"


class AreaSource(str, Enum):
    REPO = "repo"
    FOLDER = "folder"
    TOPIC = "topic"
    USER = "user"
    BUILTIN = "builtin"


class AreaKind(str, Enum):
    WORK = "work"
    PLACEHOLDER = "placeholder"


@dataclass
class Session:
    id: str            # the agent's id, e.g. claude:<uuid>
    source: str        # claude, qwen, codex
    started: float = 0.0
    updated: float = 0.0
    title: str = ""
    project: str = ""  # the folder the session ran in
    components: list = field(default_factory=list)


@dataclass
class Link:
    kind: str  # pr, ticket, branch, url
    key: str


@dataclass
class Component:
    session_id: str
    index: int
    exchanges: tuple   # (first, last), inclusive
    title: str = ""
    description: str = ""
    kind: ComponentKind = ComponentKind.WORK
    last: LastMessage = LastMessage.UNCLEAR
    goal: str = ""     # what the component works toward; empty when it has none
    branch: str = ""   # support data: empty when the component has none
    links: list = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.session_id}#{self.index}"


@dataclass
class Area:
    id: str
    name: str
    parent_id: str = ""  # empty for a top-level area
    source: AreaSource = AreaSource.TOPIC
    kind: AreaKind = AreaKind.WORK


@dataclass
class Thread:
    id: str
    goal: str
    area_id: str = ""  # empty when the thread has no area
    components: list = field(default_factory=list)  # component ids, in time order
    state: State = State.OPEN


@dataclass
class Item:
    id: str
    area_id: str        # a placeholder area: Questions or Comments
    kind: ComponentKind # question or comment
    component_id: str


def builtin_areas() -> list[Area]:
    """The placeholder areas every install starts with. Items live in them. Work areas are made by the user or by
    the sources (repo, folder, topic)."""
    return [
        Area("questions", "Questions", source=AreaSource.BUILTIN, kind=AreaKind.PLACEHOLDER),
        Area("comments", "Comments", source=AreaSource.BUILTIN, kind=AreaKind.PLACEHOLDER),
    ]


def item_for(component: Component) -> Item | None:
    """A question or comment component becomes an item in its placeholder area. A work component does not."""
    if component.kind == ComponentKind.QUESTION:
        return Item(component.id, "questions", ComponentKind.QUESTION, component.id)
    if component.kind == ComponentKind.COMMENT:
        return Item(component.id, "comments", ComponentKind.COMMENT, component.id)
    return None


def no_evidence_state(components: list[Component], updated: float, now: float | None = None,
                      recent_days: float = RECENT_DAYS) -> tuple[State, str]:
    """The state of a thread that has no PR, ticket or commit data. Reads what its components say.

    The last component decides. If its last message leaves nothing to do, the thread is closing, and it becomes
    closed after CLOSE_DAYS with no change. Otherwise it is open while recent, loose ends when quiet, and forgotten
    after FORGET_DAYS."""
    now = now or time.time()
    if not components:
        return State.FORGOTTEN, "no work components"
    last = components[-1]
    quiet = int((now - updated) // 86400)
    if last.last == LastMessage.NOTHING:
        if quiet >= CLOSE_DAYS:
            return State.CLOSED, f"nothing left for {quiet} days, with no change"
        return State.CLOSING, "the last message leaves nothing to do"
    if now - updated < recent_days * 86400:
        return State.OPEN, f"work in the last {recent_days:g} days"
    if quiet >= FORGET_DAYS:
        return State.FORGOTTEN, f"left, quiet for {quiet} days"
    return State.LOOSE_ENDS, f"left, quiet for {quiet} days"
