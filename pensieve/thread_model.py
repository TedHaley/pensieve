"""The model of tracked work: sources, sinks, sessions, components, threads and notes.

  Source     where work is read from. A session is a source.
  Sink       where work is written to: a branch, a pull request, a ticket, an email. A sink has a completion that can
             be verified (a PR merged or closed, a branch deleted or merged, a ticket done). A sink with no verifiable
             completion cannot close a thread.
  Session    one agent transcript. Its components are its children.
  Component  one run of exchanges on one theme. It reads from its session and may write to one or more sinks.
  Thread     the work that writes to one sink, or to a set of sinks that the same components write to. It has a
             label (its title) and a state. A thread never holds a question or an answer.
  Note       a question or an answer (or any component that asks or states without writing). It sits outside
             threads. It has no state and never closes.

Rules are in `thread_state`. The state of a thread comes from its sinks and its last activity. A closed thread does
not reopen. New work on a closed sink starts a new thread.

Sinks are kinds: `pr`, `branch`, `ticket`, `email`. Each kind has a completion check (`sink_complete`). Adding a kind
means adding one check."""
from dataclasses import dataclass, field
from enum import Enum
import time

RECENT_DAYS = 3  # an open thread had work in this many days
FORGET_DAYS = 21  # a loose thread quiet for this many days is forgotten


class SinkKind(str, Enum):
    PR = "pr"
    BRANCH = "branch"
    TICKET = "ticket"
    EMAIL = "email"


class Completion(str, Enum):
    DONE = "done"  # verified complete
    OPEN = "open"  # verified not complete
    UNKNOWN = "unknown"  # no check, or the check failed: the sink cannot close a thread


class ThreadState(str, Enum):
    OPEN = "open"
    LOOSE_ENDS = "loose_ends"
    CLOSED = "closed"
    FORGOTTEN = "forgotten"


class ComponentKind(str, Enum):
    WORK = "work"
    QUESTION = "question"
    ANSWER = "answer"
    COMMENT = "comment"


@dataclass(frozen=True)
class Sink:
    kind: SinkKind
    key: str  # org/repo#n for a PR, a branch name, KEY-n for a ticket, an address for email

    @property
    def label(self) -> str:
        return f"{self.kind.value} {self.key}"


@dataclass
class Session:
    id: str
    source: str  # claude, codex, qwen
    started: float = 0.0
    written: float = 0.0  # last write: the time rules use it
    title: str = ""
    components: list = field(default_factory=list)


@dataclass
class Component:
    session_id: str
    index: int
    exchanges: tuple  # (first, last), inclusive
    kind: ComponentKind = ComponentKind.WORK
    title: str = ""
    description: str = ""
    writes: list = field(default_factory=list)  # Sink objects this component writes to

    @property
    def id(self) -> str:
        return f"{self.session_id}#{self.exchanges[0]}-{self.exchanges[1]}"


@dataclass
class Thread:
    id: str
    label: str
    sinks: list = field(default_factory=list)  # Sink objects
    components: list = field(default_factory=list)  # component ids, in time order
    last_active: float = 0.0
    state: ThreadState = ThreadState.OPEN


def sink_complete(sink: Sink, status: dict) -> Completion:
    """The completion of one sink. `status` maps a sink label to what its system reported: a PR state (OPEN, MERGED,
    CLOSED) for a pr sink, or a ticket status for a ticket sink. Kinds with no check return UNKNOWN."""
    value = status.get(sink.label)
    if sink.kind == SinkKind.PR:
        if value in ("MERGED", "CLOSED"):
            return Completion.DONE
        if value == "OPEN":
            return Completion.OPEN
        return Completion.UNKNOWN
    if sink.kind == SinkKind.TICKET:
        if value in ("Done", "Closed", "Resolved"):
            return Completion.DONE
        if value:
            return Completion.OPEN
        return Completion.UNKNOWN
    if sink.kind == SinkKind.BRANCH:
        if value in ("MERGED", "DELETED"):
            return Completion.DONE
        if value in ("EXISTS", "OPEN"):
            return Completion.OPEN
        return Completion.UNKNOWN
    return Completion.UNKNOWN  # email: no completion check yet


def thread_state(sinks: list[Sink], status: dict, last_active: float, now: float | None = None) -> tuple[ThreadState, str]:
    """The state of a thread from its sinks and its last activity.

    closed: every sink is verified DONE. A thread with one sink that cannot be verified cannot close.
    open: work in the last RECENT_DAYS, and some sink is verified open. A sink that cannot be verified does not open
          a thread.
    loose ends: quiet for RECENT_DAYS or longer, and not closed.
    forgotten: quiet for FORGET_DAYS or longer, and not closed."""
    now = now or time.time()
    if not sinks:
        return ThreadState.LOOSE_ENDS, "no sink: the work cannot be verified"
    done = [s for s in sinks if sink_complete(s, status) == Completion.DONE]
    if len(done) == len(sinks):
        return ThreadState.CLOSED, "every sink is verified done: " + ", ".join(s.label for s in sinks)
    quiet = (now - last_active) / 86400
    if quiet >= FORGET_DAYS:
        return ThreadState.FORGOTTEN, f"quiet for {int(quiet)} days"
    if quiet < RECENT_DAYS and any(sink_complete(s, status) == Completion.OPEN for s in sinks):
        return ThreadState.OPEN, f"work in the last {RECENT_DAYS} days, and a sink is verified open"
    not_done = [s.label for s in sinks if sink_complete(s, status) != Completion.DONE]
    return ThreadState.LOOSE_ENDS, f"not verified done: {', '.join(not_done)}; quiet for {int(quiet)} days"
