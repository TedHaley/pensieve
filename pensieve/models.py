"""The typed models of storage v2 (docs/design/storage.md).

Each table of store.db and index.db has a frozen dataclass here, with the same fields, in the same order, and the same
types. A closed set of values is a StrEnum with the same values as the table's CHECK. Times are int Unix seconds,
UTC. Only pensieve/store/ turns rows into these models and back (`from_row`, `values`)."""
import time
from dataclasses import dataclass, fields
from enum import StrEnum
from functools import cache
from typing import NewType, get_type_hints

SourceId = NewType("SourceId", str)
DocId = NewType("DocId", int)
IdentityKey = NewType("IdentityKey", str)
PersonId = NewType("PersonId", str)
CardId = NewType("CardId", str)
ChunkId = NewType("ChunkId", int)


def now() -> int:
    """The time now, as the tables keep it: int Unix seconds, UTC."""
    return int(time.time())


class SourceKind(StrEnum):
    MCP = "mcp"
    AGENT_SESSIONS = "agent_sessions"
    GH_CLI = "gh_cli"
    APPS = "apps"
    API = "api"  # a service's own API, which Pensieve reads with the user's token (connections.SLACK)


class SourceOrigin(StrEnum):
    ENGINE = "engine"
    AGENT = "agent"
    OWN = "own"
    CLI = "cli"
    BUILTIN = "builtin"


class SourceStatus(StrEnum):
    CONNECTED = "connected"
    NEEDS_AUTH = "needs_auth"
    FAILED = "failed"
    UNKNOWN = "unknown"


class Switched(StrEnum):
    ON = "on"
    OFF = "off"


class DocKind(StrEnum):
    TICKET = "ticket"
    PULL_REQUEST = "pull_request"
    ISSUE = "issue"
    MESSAGE = "message"
    THREAD = "thread"
    PAGE = "page"
    MEETING = "meeting"
    FILE = "file"
    RECORD = "record"
    OTHER = "other"


class PersonRole(StrEnum):
    AUTHOR = "author"
    ASSIGNEE = "assignee"
    REVIEWER = "reviewer"
    COMMENTER = "commenter"
    ATTENDEE = "attendee"
    OWNER = "owner"
    MENTIONED = "mentioned"


class IdentityKind(StrEnum):
    EMAIL = "email"
    GH = "gh"
    SLACK = "slack"
    NAME = "name"


class KeyReason(StrEnum):
    FACT = "fact"
    EVIDENCE = "evidence"
    USER = "user"
    ME = "me"


class OverrideAction(StrEnum):
    MERGE = "merge"
    SPLIT = "split"
    ME = "me"


class ReviewState(StrEnum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    COMMENTED = "commented"


class CardKind(StrEnum):
    TICKET = "ticket"
    BRANCH = "branch"
    WORK = "work"
    GOAL = "goal"


class State(StrEnum):
    """A thread's state as the store saves it (pensieve/task_states.py says what each one means). Closed is saved
    as complete, the name the store's CHECK allows (task_states.stored)."""
    OPEN = "open"
    TODO = "todo"
    THOUGHTS = "thoughts"
    LOOSE_ENDS = "loose_ends"
    CLOSED = "complete"
    FORGOTTEN = "forgotten"

    @classmethod
    def _missing_(cls, value):  # rows saved under an older name (todo, closing, closed) still load
        from pensieve.task_states import RENAMED
        new = "closed" if value == "closed" else RENAMED.get(value)
        if new == "closed":
            return cls.CLOSED
        return cls(new) if new in cls._value2member_map_ else None


class StateSet(StrEnum):
    """What a row of card_states sets: a state, or `auto` (the automatic state applies again). The old names stay
    so that saved rows still read; task_states.RENAMED maps them to the four states."""
    OPEN = "open"
    LOOSE_ENDS = "loose_ends"
    CLOSED = "closed"
    FORGOTTEN = "forgotten"
    AUTO = "auto"
    TODO = "todo"
    CLOSING = "closing"
    COMPLETE = "complete"
    DISCARDED = "discarded"
    LATENT = "latent"
    CONSCIOUS = "conscious"
    SUBCONSCIOUS = "subconscious"
    ETCHED = "etched"


class SetBy(StrEnum):
    USER = "user"
    AGENT = "agent"
    AUTO = "auto"


class MemberKind(StrEnum):
    SESSION = "session"
    DOCUMENT = "document"
    PULL_REQUEST = "pull_request"
    BRANCH = "branch"
    COMMIT = "commit"
    FILE = "file"
    TICKET_KEY = "ticket_key"
    WORKTREE = "worktree"


class ThreadEvent(StrEnum):
    START = "start"
    CONTINUE = "continue"
    FORK = "fork"
    SPLIT = "split"
    MERGE = "merge"
    REVIVE = "revive"
    SUGGEST = "suggest"


class SaveReason(StrEnum):
    """Why a thread's card was saved, from the versions that kept a thread history. Kept for the table."""
    START = "start"
    FORK = "fork"
    SPLIT = "split"
    MERGE = "merge"
    REVIVE = "revive"
    STATE = "state"
    MEMBERS = "members"
    PUSH = "push"
    GOAL = "goal"


class FeedbackBy(StrEnum):
    USER = "user"
    AGENT = "agent"


class FeedbackKind(StrEnum):
    BUG = "bug"
    IDEA = "idea"


class FeedbackStatus(StrEnum):
    NEW = "new"
    SENT = "sent"
    DISMISSED = "dismissed"


class StepStatus(StrEnum):
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"  # its v2 table or function is not in this build yet: tried again at the next start


class EdgeKind(StrEnum):
    CODE = "code"
    PR = "pr"
    TASK = "task"
    THREAD = "thread"
    DM = "dm"
    CHANNEL = "channel"


@dataclass(frozen=True)
class Source:
    __table__ = "sources"
    id: str
    kind: SourceKind
    connector: str | None
    name: str
    origin: SourceOrigin
    status: SourceStatus
    switched: Switched | None = None
    collected_at: int | None = None
    failed_at: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class Document:
    __table__ = "documents"
    id: int | None  # None before the first insert
    source_id: str
    key: str
    kind: DocKind
    title: str
    url: str | None
    status: str | None
    branch: str | None
    created_at: int | None
    updated_at: int | None
    seen_at: int | None
    private: bool
    text: str
    raw: str  # JSON, the service's item as it came
    hash: str

    @property
    def time(self) -> int | None:
        """The document's time: the service's update time, else when an agent session last saw it."""
        return self.updated_at if self.updated_at is not None else self.seen_at


@dataclass(frozen=True)
class DocumentPerson:
    __table__ = "document_people"
    doc_id: int
    key: str
    role: PersonRole
    name: str | None = None


@dataclass(frozen=True)
class DocumentChange:
    __table__ = "document_changes"
    doc_id: int
    at: int
    field: str
    old: str | None
    new: str | None


@dataclass(frozen=True)
class DocumentSession:
    __table__ = "document_sessions"
    doc_id: int
    session_id: str
    seen_at: int


@dataclass(frozen=True)
class Identity:
    __table__ = "identities"
    key: str
    kind: IdentityKind
    name: str | None
    title: str | None
    avatar_url: str | None
    source: str
    first_seen: int
    last_seen: int


@dataclass(frozen=True)
class Person:
    __table__ = "persons"
    id: str
    name: str
    is_me: bool
    created_at: int


@dataclass(frozen=True)
class PersonKey:
    __table__ = "person_keys"
    key: str
    person_id: str
    reason: KeyReason
    score: float | None = None
    evidence: str | None = None  # JSON


@dataclass(frozen=True)
class PersonAlias:
    __table__ = "person_aliases"
    old_id: str
    person_id: str


@dataclass(frozen=True)
class PersonOverride:
    __table__ = "person_overrides"
    id: int | None
    action: OverrideAction
    a: str
    b: str | None
    at: int


@dataclass(frozen=True)
class OrgPullRequest:
    __table__ = "org_pull_requests"
    url: str
    repo: str
    author_key: str | None
    created_at: int | None
    updated_at: int | None


@dataclass(frozen=True)
class OrgReview:
    __table__ = "org_reviews"
    url: str
    reviewer_key: str
    state: ReviewState
    at: int | None


@dataclass(frozen=True)
class OrgTitle:
    __table__ = "org_titles"
    key: str
    title: str
    first_seen: int
    last_seen: int
    builds: int


@dataclass(frozen=True)
class OrgFirstCommit:
    __table__ = "org_first_commits"
    key: str
    at: int


@dataclass(frozen=True)
class Card:
    __table__ = "cards"
    id: str
    kind: CardKind
    goal_of: str | None
    title: str
    state: State
    first_at: int
    updated_at: int
    built_at: int
    signature: str
    view: str  # JSON: the whole card as the page and agents read it (work.py's task)


@dataclass(frozen=True)
class CardMember:
    __table__ = "card_members"
    card_id: str
    kind: MemberKind
    ref: str
    at: int | None = None


@dataclass(frozen=True)
class CardNote:
    __table__ = "card_notes"
    card_id: str
    version: int
    title: str | None
    problem: str | None
    goal: str | None
    state: str | None
    next: str | None
    question: str | None
    signature: str
    model: str
    written_at: int
    extra: str = "{}"  # JSON: the rest of the model's answer (evidence, goals) and the goals split from it


@dataclass(frozen=True)
class CardStateSet:
    __table__ = "card_states"
    id: int | None
    card_id: str
    state: StateSet
    by: SetBy
    reason: str | None
    at: int


@dataclass(frozen=True)
class CardDone:
    __table__ = "card_done"
    card_id: str
    item: str
    at: int
    why: str = ""


@dataclass(frozen=True)
class CardDoneCheck:
    """The last done check of a card, from the versions that had model-written notes: its key held the note's time,
    its items and the commits it saw. Kept for the table; nothing writes it now."""
    __table__ = "card_done_checks"
    card_id: str
    key: str
    at: int


@dataclass(frozen=True)
class Thread:
    __table__ = "threads"
    id: str
    goal: str
    goal_vec: bytes | None
    created_at: int
    ended_at: int | None
    updated_at: int
    parent: str | None = None
    merged_into: str | None = None
    snapshot: str | None = None  # the last snapshot that the linker linked to it


@dataclass(frozen=True)
class ThreadMember:
    __table__ = "thread_members"
    thread_id: str
    member: str
    weight: float


@dataclass(frozen=True)
class ThreadRepo:
    __table__ = "thread_repos"
    thread_id: str
    repo: str


@dataclass(frozen=True)
class ThreadCard:
    __table__ = "thread_cards"
    thread_id: str
    card_id: str


@dataclass(frozen=True)
class ThreadEventRow:
    __table__ = "thread_events"
    id: int | None
    at: int
    event: ThreadEvent
    thread_id: str
    other: str | None = None
    sim: float | None = None


@dataclass(frozen=True)
class ThreadSnapshot:
    __table__ = "thread_snapshots"
    id: int | None
    thread_id: str
    at: int
    snapshot: str
    state: State
    structure: str
    pushed: str
    goal_vec: bytes | None
    card: str  # JSON, context-card/1


@dataclass(frozen=True)
class ThreadSnapshotReason:
    __table__ = "thread_snapshot_reasons"
    snapshot_id: int
    reason: SaveReason


@dataclass(frozen=True)
class Feedback:
    __table__ = "feedback"
    id: str  # 10 hex characters: the app and the MCP tool name a report by it
    at: int
    by: FeedbackBy
    kind: FeedbackKind
    text: str  # the one-line summary
    status: FeedbackStatus
    details: str
    source: str  # "user", or the MCP client name of the agent that filed it
    version: str  # Pensieve's version when it was filed
    context: str  # JSON object: where the agent was (cwd)


@dataclass(frozen=True)
class MigrationStep:
    __table__ = "migration_steps"
    step: str          # one import from v1 (pensieve/migrate.py), or "delete_v1"
    status: StepStatus
    count: int         # rows imported (or paths removed)
    error: str | None
    tries: int
    at: int


@dataclass(frozen=True)
class V1DocPath:
    __table__ = "v1_doc_paths"
    path: str          # <source id>/<file name> of a v1 connection item (~/.pensieve/connections/<sid>/<name>.md)
    source_id: str
    key: str           # documents.key of the same item


@dataclass(frozen=True)
class DocChunk:
    __table__ = "doc_chunks"
    id: int | None
    doc_id: int
    idx: int
    text: str
    vec: bytes  # float16
    hash: str


@dataclass(frozen=True)
class DocIndexState:
    __table__ = "doc_index_state"
    doc_id: int
    hash: str  # the document's hash when its chunks were made
    at: int


@dataclass(frozen=True)
class EmbedCache:
    __table__ = "embed_cache"
    hash: str
    model: str
    vec: bytes


@dataclass(frozen=True)
class PersonEdge:
    __table__ = "person_edges"
    a: str
    b: str
    kind: EdgeKind
    n: int
    last_at: int | None
    weight: float


@dataclass(frozen=True)
class CacheEntry:
    __table__ = "cache"
    key: str
    value: bytes
    expires_at: int


@dataclass(frozen=True)
class Meta:
    __table__ = "meta"
    key: str
    value: str


@dataclass(frozen=True)
class SlackUser:
    """A Slack user that a profile lookup or a collected message showed."""
    __table__ = "slack_users"
    id: str  # U012ABC
    name: str
    title: str
    email: str
    avatar_url: str
    display: str
    bot: bool
    is_me: bool
    seen_at: int


@dataclass(frozen=True)
class DirectoryPerson:
    """A person that Find a person found in the user's services and kept."""
    __table__ = "directory_people"
    id: str  # "d" + 10 hex characters
    name: str
    login: str
    slack: str
    title: str
    avatar_url: str
    found_at: int
    seen_at: int


@dataclass(frozen=True)
class DirectoryEmail:
    __table__ = "directory_emails"
    person_id: str
    email: str


@dataclass(frozen=True)
class DirectorySource:
    __table__ = "directory_sources"
    person_id: str
    idx: int
    source: str  # the connector that found them: slack, github, ...


@dataclass(frozen=True)
class OrgTeamPair:
    """How often two people (by an identity key, a < b) were in the same inferred team, decayed over builds."""
    __table__ = "org_team_pairs"
    a_key: str
    b_key: str
    weight: float
    updated_at: int  # when the build that last changed the weight ran


@dataclass(frozen=True)
class OrgManagerTitle:
    """A title with a manager word, as org_titles counts it (manager-title sightings)."""
    __table__ = "org_manager_titles"
    key: str
    title: str
    first_seen: int
    last_seen: int
    builds: int


@dataclass(frozen=True)
class OrgState:
    """The one row of org evidence counts: the first and last build, and when the team counts last decayed."""
    __table__ = "org_state"
    id: int  # always 1
    since: int
    updated: int
    builds: int
    last_build: int
    teams_at: int


@dataclass(frozen=True)
class UsageCall:
    """One MCP tool call (v1: usage.db)."""
    __table__ = "usage_calls"
    id: int | None
    at: int
    source: str | None  # mcp or hook
    tool: str
    client: str | None
    conn: str | None
    cwd: str | None
    scope: str | None
    args: str | None  # JSON
    ms: float | None
    ok: bool | None
    error: str | None
    n_results: int | None
    results: str | None  # JSON: [[id, path], ...]
    laps: str | None  # JSON: per-phase times
    out_chars: int | None


@dataclass(frozen=True)
class AgentJob:
    """A completion that waits for the user's agent (v1: agent_jobs.db)."""
    __table__ = "agent_jobs"
    id: str  # sha1 of system, prompt and schema
    kind: str
    system: str
    prompt: str
    schema: str | None  # JSON schema of the answer
    max_tokens: int
    asked_at: int
    answer: str | None
    answered_at: int | None
    claimed_at: int | None
    claimer: str | None


@dataclass(frozen=True)
class AgentSession:
    """An agent session that answered jobs."""
    __table__ = "agent_sessions"
    conn: str
    client: str | None
    at: int
    answers: int


@dataclass(frozen=True)
class Avatar:
    """A picture in v2/avatars: <hash>.<ext>. No ext: the download failed at `at`."""
    __table__ = "avatars"
    hash: str  # sha1 of the picture's address, 16 hex characters
    ext: str | None
    at: int


@dataclass(frozen=True)
class SessionUsage:
    """The tokens of an agent session in one hour, with one model (pensieve/tokens.py). `cache_write_1h` is the part
    of `cache_write` that went to the 1-hour cache."""
    __table__ = "session_usage"
    session_id: str
    hour: int  # the start of the hour, Unix seconds, UTC
    model: str
    calls: int
    input: int
    output: int
    cache_write: int
    cache_read: int
    cache_write_1h: int


STORE_MODELS = (Source, Document, DocumentPerson, DocumentChange, DocumentSession, Identity, Person, PersonKey,
                PersonAlias, PersonOverride, OrgPullRequest, OrgReview, OrgTitle, OrgFirstCommit, Card, CardMember,
                CardNote, CardStateSet, CardDone, CardDoneCheck, Thread, ThreadMember, ThreadRepo, ThreadCard, ThreadEventRow,
                ThreadSnapshot, ThreadSnapshotReason, Feedback, SlackUser, DirectoryPerson, DirectoryEmail,
                DirectorySource, OrgTeamPair, OrgManagerTitle, OrgState, UsageCall, AgentJob, AgentSession,
                MigrationStep, V1DocPath)
INDEX_MODELS = (DocChunk, DocIndexState, EmbedCache, PersonEdge, CacheEntry, Meta, Avatar, SessionUsage)


@cache
def _converters(cls) -> tuple[tuple[str, object], ...]:
    """For each field: the function that turns a column value into the field's type (an enum or bool), or None."""
    hints = get_type_hints(cls)
    out = []
    for f in fields(cls):
        t = hints[f.name]
        args = getattr(t, "__args__", None) or (t,)
        conv = next((a for a in args if isinstance(a, type) and issubclass(a, (StrEnum, bool))), None)
        out.append((f.name, conv))
    return tuple(out)


def from_row(cls, row):
    """A model from a row whose columns are the model's fields in order (a tuple or sqlite3.Row)."""
    kw = {}
    for (name, conv), v in zip(_converters(cls), row):
        kw[name] = conv(v) if conv is not None and v is not None else v
    return cls(**kw)


def columns(cls) -> tuple[str, ...]:
    return tuple(f.name for f in fields(cls))


def values(obj) -> tuple:
    """The row of a model: enums as their values, bools as 0 or 1."""
    out = []
    for f in fields(obj):
        v = getattr(obj, f.name)
        out.append(int(v) if isinstance(v, bool) else v.value if isinstance(v, StrEnum) else v)
    return tuple(out)
