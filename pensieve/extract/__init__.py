"""Extractors: each one reads one kind of evidence from a session and returns the edges it finds.

An edge says that a session relates to a branch, PR or ticket. It has a strength:
  write    the session changed the target (a push, a commit, a PR opened or merged, a Jira update)
  mention  the session named the target in text only

Register an extractor with @extractor(name). The graph builder runs every registered extractor on every session.
Add one for a tool you use by writing a function that takes the session's raw transcript lines and yields Edge."""
from dataclasses import dataclass
from typing import Callable, Iterable

WRITE, MENTION = "write", "mention"


@dataclass(frozen=True)
class Edge:
    session: str     # the session id, e.g. claude:<uuid>
    kind: str        # branch, pr or ticket
    key: str         # branch name, org/repo#n, or KEY-n
    strength: str    # write or mention
    evidence: str    # what showed it, in a few words
    extractor: str   # the extractor that found it


EXTRACTORS: dict[str, Callable[["Session"], Iterable[Edge]]] = {}


def extractor(name: str):
    def wrap(fn):
        EXTRACTORS[name] = fn
        return fn
    return wrap


class Session:
    """What an extractor gets: the session id, its parsed turns, and its raw transcript records."""

    def __init__(self, sid: str, turns: list, records: list):
        self.id = sid
        self.turns = turns
        self.records = records  # the transcript's JSON records, in order (empty for sources without them)


def run_all(session: Session) -> list[Edge]:
    out = []
    for name, fn in EXTRACTORS.items():
        out.extend(fn(session))
    return out
