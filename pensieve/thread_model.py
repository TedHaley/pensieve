"""The model of work for threads built from sessions: Session > Scene, Area, Thread, Item.

  Session  one agent transcript. Its scenes are its children.
  Scene    one run of exchanges on one theme (segment.components). Belongs to one session.
  Thread   one goal, followed over time. Holds work scenes from one or more sessions. Sits in one area.
  Area     a space the user works in. Areas nest. Every thread sits in one area; Unfiled is the default.
  Item     a standalone question or comment. Sits in a placeholder area. No goal, no state, never closes.

A scene is a work scene, a question or a comment. Only work scenes join threads. Questions and comments become
items.

Closing without evidence: a thread with no PR, ticket or commit data can still close. When its last scene ends
with no action left (the last message asks nothing and says nothing is to do), the thread is closing. It becomes
closed after CLOSE_DAYS with no change. The rule is in `no_evidence_state`. It reads what the scene says, not
external data."""
from dataclasses import dataclass, field
from enum import Enum
import time

from pensieve.task_states import CLOSE_DAYS, FORGET_DAYS, RECENT_DAYS, State

UNFILED = "unfiled"


class SceneKind(str, Enum):
    WORK = "work"
    QUESTION = "question"
    COMMENT = "comment"


class LastMessage(str, Enum):
    """What the last message of a scene leaves: an action for someone, a question, or nothing to do."""
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
    scenes: list = field(default_factory=list)


@dataclass
class Link:
    kind: str  # pr, ticket, branch, url
    key: str


@dataclass
class Scene:
    session_id: str
    index: int
    exchanges: tuple   # (first, last), inclusive
    title: str = ""
    description: str = ""
    kind: SceneKind = SceneKind.WORK
    last: LastMessage = LastMessage.UNCLEAR
    branch: str = ""
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
    area_id: str = UNFILED
    scenes: list = field(default_factory=list)  # scene ids, in time order
    state: State = State.OPEN


@dataclass
class Item:
    id: str
    area_id: str        # a placeholder area, such as Questions or Comments
    kind: SceneKind     # question or comment
    scene_id: str


def builtin_areas() -> list[Area]:
    """The areas every install starts with: Unfiled for threads, and the two placeholder areas for items."""
    return [
        Area(UNFILED, "Unfiled", source=AreaSource.BUILTIN),
        Area("questions", "Questions", source=AreaSource.BUILTIN, kind=AreaKind.PLACEHOLDER),
        Area("comments", "Comments", source=AreaSource.BUILTIN, kind=AreaKind.PLACEHOLDER),
    ]


def item_for(scene: Scene) -> Item | None:
    """A question or comment scene becomes an item in its placeholder area. A work scene does not."""
    if scene.kind == SceneKind.QUESTION:
        return Item(scene.id, "questions", SceneKind.QUESTION, scene.id)
    if scene.kind == SceneKind.COMMENT:
        return Item(scene.id, "comments", SceneKind.COMMENT, scene.id)
    return None


def no_evidence_state(scenes: list[Scene], updated: float, now: float | None = None) -> tuple[State, str]:
    """The state of a thread that has no PR, ticket or commit data. Reads what its scenes say.

    The last scene decides. If its last message leaves nothing to do, the thread is closing, and it becomes closed
    after CLOSE_DAYS with no change. Otherwise it is open while recent, loose ends when quiet, and forgotten after
    FORGET_DAYS."""
    now = now or time.time()
    if not scenes:
        return State.FORGOTTEN, "no work scenes"
    last = scenes[-1]
    quiet = int((now - updated) // 86400)
    if last.last == LastMessage.NOTHING:
        if quiet >= CLOSE_DAYS:
            return State.CLOSED, f"nothing left for {quiet} days, with no change"
        return State.CLOSING, "the last message leaves nothing to do"
    if now - updated < RECENT_DAYS * 86400:
        return State.OPEN, f"work in the last {RECENT_DAYS} days"
    if quiet >= FORGET_DAYS:
        return State.FORGOTTEN, f"left, quiet for {quiet} days"
    return State.LOOSE_ENDS, f"left, quiet for {quiet} days"
