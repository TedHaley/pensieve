"""What a context card says about its thread: its anchor (the title), where the work left off, and what is left to
close it. A card recalls one thread of work, so its title comes from what the thread is for (a ticket, else the
branch's pull request, else the branch, else the session's main goal), never from the newest message: a long session
holds side questions and other ideas.

The functions take plain card dicts (the shape of work.tasks) and transcript turns [(role, text)], and do not read
files, call a model or touch a database, so tests can run them on made-up data."""
import re
from dataclasses import dataclass, field

# Text in a user turn that the user did not write: slash commands and their output, hook and system notes,
# interruptions, background task notices.
NOISE = re.compile(r"^\s*(<command-|<local-command|<system-reminder|<task-notification|<bash-|\[Request interrupted"
                   r"|Caveat: )", re.I)
PASTE = re.compile(r"<pasted_content[^>]*>.*?</pasted_content[^>]*>", re.S)
TAG_BLOCK = re.compile(r"<(system-reminder|task-notification)[^>]*>.*?</\1>", re.S)
# A reply that only says go on: it carries no topic of its own.
TRIVIAL = re.compile(r"^\W*(ok(ay)?|k|yes|yep|yeah|y|no|nope|sure|go|go ahead|continue|proceed|do it|do that|do the steps"
                     r"|thanks?( you)?|ty|great|perfect|nice|cool|lgtm|commit( and push)?|push( it)?|merge( it)?|run it"
                     r"|try again|again|retry|stop|wait|done|next|keep going|sounds good|looks good|ship it)\W*$", re.I)
SHORT_REPLY = 40          # characters: an agent reply shorter than this says little ("Done.", "On it.")
MSG_HEAD, MSG_TAIL = 400, 200  # a long message keeps its start and its end
BRANCH_PREFIX = re.compile(r"^(feature|feat|fix|bugfix|hotfix|chore|refactor|docs|test|wip|[a-z][\w.-]{1,20})/", re.I)
CONVENTIONAL = re.compile(r"^\s*(\w+)(\([^)]*\))?!?:\s*")  # "fix(scope): " at the start of a PR title
TAGS = re.compile(r"^\s*(\[[^\]]{1,40}\]\s*)+")  # "[on-call] [Data] " at the start of a ticket title: a queue
KEY_SUFFIX = re.compile(r"\s*[(\[]?\b[A-Z][A-Z0-9]+-\d+\b[)\]]?\s*$")  # "(ABC-12)" at the end of a title


@dataclass(frozen=True)
class Anchor:
    """What the thread is for. kind: ticket, pr, branch, goal or session. `theme` is True when the title is only a
    starting point (a branch name, a session title) that the note writer can name better from the thread's data."""
    kind: str
    title: str
    ref: str = ""
    theme: bool = False


@dataclass(frozen=True)
class Reply:
    """Where the agent left off in a session: its last substantive reply after the user's last message, the question
    it asked (if any), and whether it had not replied yet (the session is running, or was stopped)."""
    text: str = ""
    question: str = ""
    waiting: bool = False


@dataclass(frozen=True)
class Pending:
    """One thing left to close the thread. Pensieve only reads, so each item is a hint of who can act and where:
    'agent' (the user's agent can do it in a session), 'you' (only the user can: a review, a merge, an answer), or
    'parent' (a step in the system the item lives in, such as closing a ticket in its tracker)."""
    text: str
    how: str
    ref: str = ""
    why: list = field(default_factory=list)


def _cap(t: str) -> str:
    return t[:1].upper() + t[1:] if t else t


def _clean_title(t: str) -> str:
    return _cap(KEY_SUFFIX.sub("", CONVENTIONAL.sub("", TAGS.sub("", t or ""))).strip(" -:"))


def branch_theme(branch: str) -> str:
    """A branch name as words: 'feature/sam/retry-card-payments' -> 'Retry card payments'."""
    b = branch or ""
    while BRANCH_PREFIX.match(b):
        b = BRANCH_PREFIX.sub("", b, count=1)
    return _cap(re.sub(r"[-_.]+", " ", b).strip())


def _key_rank(card: dict, key: str) -> tuple:
    """How strongly a ticket is the card's own: named by its branch, then by its own PR, then by being the newest."""
    branches = " ".join(b.get("branch", "") for b in card.get("branches", [])).lower()  # branch names: often lower case
    prs = " ".join(f"{p.get('title', '')} {p.get('head', '')}" for p in card.get("prs", [])).lower()
    return (key.lower() not in branches, key.lower() not in prs)


def _num(s: str) -> int:
    m = re.search(r"(\d+)/?$", s or "")
    return int(m.group(1)) if m else 1 << 30


def anchor(card: dict) -> Anchor:
    """The thread's anchor, which names it. The title must stay the same while the thread goes on, so it comes from
    the thread's own records, never from a note or the newest message, and each pick is one that does not change:
    1. a goal split from another card keeps its own goal title (its sessions are the parent's);
    2. else the thread's own pull request: the first one opened (the lowest number), whatever its state now;
    3. else the ticket it solves: the one its branch names, then the one its PR names, then the lowest key;
    4. else the feature branch, as words;
    5. else the session's own title."""
    if card.get("goal_of"):
        return Anchor("goal", card.get("title", ""), card.get("id", ""))
    own = [p for p in card.get("prs", []) if p.get("own") or p.get("head") in {b.get("branch") for b in card.get("branches", [])}]
    named = [p for p in own if (p.get("title") or "").strip()]
    if named:
        p = min(named, key=lambda p: _num(p["url"]))
        return Anchor("pr", _clean_title(p["title"]), p["url"])
    tickets = [k for k in card.get("tickets", []) if k.get("title") and not k.get("ref")]
    if tickets:
        k = min(tickets, key=lambda k: (*_key_rank(card, k["key"]), k["key"].split("-")[0], _num(k["key"])))
        return Anchor("ticket", _clean_title(k["title"]) or k["key"], k["key"])
    branches = sorted(b["branch"] for b in card.get("branches", []) if b.get("branch"))
    if branches:
        return Anchor("branch", branch_theme(branches[0]), branches[0])
    s = pick_session(card)
    return Anchor("session", _cap((s or {}).get("title", "") or card.get("title", "")), (s or {}).get("id", ""), theme=True)


def card_title(card: dict, note: dict | None = None) -> str:
    """The title a card shows: its anchor's title, which stays the same while the thread goes on. Only a thread with
    no PR, ticket or branch, whose session has no title, takes the note writer's title."""
    a = card.get("anchor") or {}
    if a.get("title"):
        return a["title"]
    return _cap(((note or {}).get("title") or "").strip()) or card.get("title", "")


def pick_session(card: dict) -> dict | None:
    """The session that says where the thread left off: the card's own session with the newest activity. Sessions
    that are only shared with the card (they worked on its branch for a while) do not count."""
    own = card.get("sessions") or []
    return max(own, key=lambda s: s.get("updated") or 0) if own else None


def _words_of(text: str) -> str:
    """A user turn without what the user did not type: system blocks and pasted content."""
    return PASTE.sub(" ", TAG_BLOCK.sub(" ", text)).strip()


def _trim(text: str) -> str:
    return text if len(text) <= MSG_HEAD + MSG_TAIL + 5 else f"{text[:MSG_HEAD]} … {text[-MSG_TAIL:]}"


def my_messages(turns: list, n: int = 2) -> list[str]:
    """The user's last n substantive messages, oldest first: no commands or system text, no pasted blocks (the user's
    own words around a paste stay), no bare 'ok' / 'continue', and a long message cut to its start and end."""
    out = []
    for role, text in reversed(turns):
        if role != "user" or NOISE.match(text):
            continue
        words = _words_of(text)
        if not words or TRIVIAL.match(words):
            continue
        out.append(_trim(words))
        if len(out) == n:
            break
    return out[::-1]


def _last_question(text: str) -> str:
    """The question at the end of a reply: the last sentence that ends with '?' in its last paragraph."""
    last = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()][-1:] or [""]
    qs = re.findall(r"[^.!?\n]*\?", last[0])
    return qs[-1].strip() if qs else ""


def agent_reply(turns: list) -> Reply:
    """Where the agent left off after the user's last substantive message: its last reply of some length and the
    question it ends with. When the agent has not replied since (the session is running, or was stopped), `waiting`
    is True and the text is its last substantive reply before that message."""
    last_user = max((i for i, (r, t) in enumerate(turns) if r == "user" and not NOISE.match(t)
                     and _words_of(t) and not TRIVIAL.match(_words_of(t))), default=-1)
    after = [t for r, t in turns[last_user + 1:] if r == "assistant" and len(t.strip()) >= SHORT_REPLY]
    if after:
        return Reply(after[-1].strip(), _last_question(after[-1]))
    before = [t for r, t in turns[:max(last_user, 0)] if r == "assistant" and len(t.strip()) >= SHORT_REPLY]
    return Reply(before[-1].strip() if before else "", "", waiting=True)


DONE_WORDS = re.compile(r"\b(done|closed|resolved|complete|completed|won'?t do|cancel+ed)\b", re.I)


def pending(card: dict, reply: Reply | None = None, resolved: bool = False) -> list[Pending]:
    """What is left to close the thread, most urgent first, each with who can act:
    - a question the agent asked: only the user can answer it;
    - each open pull request, by where it stands (failing checks, changes requested, approved, in review, draft);
    - unpushed commits and uncommitted files on the card's own branch;
    - a ticket that is not done although the thread's work is: close it in its tracker. The work is done when the
      card's pull request merged, or when the note writer, which reads every source of the card, judged the thread
      `resolved`;
    - a ticket that is not done, with no pull request and not resolved: the work itself."""
    out = []
    if reply and reply.question:
        out.append(Pending(f"Answer the agent: {reply.question}", "you"))
    for p in card.get("prs", []):
        n = "#" + p["url"].rstrip("/").rsplit("/", 1)[-1]
        st, checks, review = p.get("state"), p.get("checks"), p.get("review")
        if st not in ("open", "draft"):
            continue
        if checks == "failing":
            out.append(Pending(f"Fix the failing checks on {n}", "agent", p["url"]))
        if review == "changes requested":
            out.append(Pending(f"Address the review on {n}", "agent", p["url"]))
        if st == "draft":
            out.append(Pending(f"Mark {n} ready for review", "you", p["url"]))
        elif review == "approved" and checks != "failing":
            out.append(Pending(f"Merge {n}", "you", p["url"]))
        elif review != "changes requested":
            out.append(Pending(f"Get {n} reviewed", "you", p["url"]))
    own = {b.get("branch") for b in card.get("branches", [])}
    for pl in card.get("places", []):
        if pl.get("branch") not in own or pl.get("head"):
            continue
        if pl.get("changed"):
            out.append(Pending(f"Commit {pl['changed']} changed files on {pl['branch']}", "agent", pl["branch"]))
        if pl.get("ahead"):
            out.append(Pending(f"Push {pl['ahead']} commits on {pl['branch']}", "agent", pl["branch"]))
    merged = any(p.get("state") == "merged" for p in card.get("prs", []))
    seen = set()
    for k in card.get("tickets", []):  # one ticket can come from two sources (its tracker, a session that read it)
        if k.get("ref") or k["key"] in seen:
            continue
        seen.add(k["key"])
        if DONE_WORDS.search(k.get("status") or ""):
            continue
        if merged or resolved:
            why = "its pull request merged" if merged else "the work in its sessions is done"
            out.append(Pending(f"Close {k['key']}: {why}", "parent", k["key"]))
        elif not card.get("prs"):
            out.append(Pending(f"Work on {k['key']}", "agent", k["key"]))
    return out


_ROLE = re.compile(r"^(User|Assistant|Memory): ", re.M)


def turns_from_chunks(chunks: list[str]) -> list:
    """Transcript turns [(role, text)] from a session's stored chunks ("User: …" / "Assistant: …" exchanges, cut on
    paragraphs). A chunk that starts inside a turn continues the turn before it."""
    out = []
    for c in chunks:
        parts = _ROLE.split(c)
        if parts[0].strip() and out:
            out[-1] = (out[-1][0], f"{out[-1][1]}\n\n{parts[0].strip()}")
        for role, text in zip(parts[1::2], parts[2::2]):
            if role != "Memory":
                out.append((role.lower(), text.strip()))
    return out
