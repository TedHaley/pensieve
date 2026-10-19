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
    """One thing left to close the thread. how: 'session' (the agent can do it in a session), 'you' (only the user
    can: a review, a merge, a reply), or 'mark' (it may already be done elsewhere: mark it resolved)."""
    text: str
    how: str
    ref: str = ""
    why: list = field(default_factory=list)


def _clean_title(t: str) -> str:
    return KEY_SUFFIX.sub("", CONVENTIONAL.sub("", t or "")).strip(" -:")


def branch_theme(branch: str) -> str:
    """A branch name as words: 'feature/sam/retry-card-payments' -> 'Retry card payments'."""
    b = branch or ""
    while BRANCH_PREFIX.match(b):
        b = BRANCH_PREFIX.sub("", b, count=1)
    words = re.sub(r"[-_.]+", " ", b).strip()
    return words[:1].upper() + words[1:] if words else ""


def _key_rank(card: dict, key: str) -> tuple:
    """How strongly a ticket is the card's own: named by its branch, then by its own PR, then by being the newest."""
    branches = " ".join(b.get("branch", "") for b in card.get("branches", [])).lower()  # branch names: often lower case
    prs = " ".join(f"{p.get('title', '')} {p.get('head', '')}" for p in card.get("prs", [])).lower()
    return (key.lower() not in branches, key.lower() not in prs)


def anchor(card: dict) -> Anchor:
    """The thread's anchor, which names the card:
    1. a goal split from another card keeps its own goal title (its sessions are the parent's);
    2. else the ticket the thread solves: the one its branch names, then its PR, then the newest;
    3. else the card's own pull request (the open one first);
    4. else the feature branch, as words;
    5. else the session's own title, as a starting point."""
    if card.get("goal_of"):
        return Anchor("goal", card.get("title", ""), card.get("id", ""))
    tickets = [k for k in card.get("tickets", []) if k.get("title") and not k.get("ref")]
    if tickets:
        k = sorted(tickets, key=lambda k: (*_key_rank(card, k["key"]), -(k.get("updated") or 0)))[0]
        return Anchor("ticket", _clean_title(k["title"]) or k["key"], k["key"])
    own = [p for p in card.get("prs", []) if p.get("own") or p.get("head") in {b.get("branch") for b in card.get("branches", [])}]
    if own:
        p = sorted(own, key=lambda p: p.get("state") not in ("open", "draft"))[0]
        return Anchor("pr", _clean_title(p.get("title", "")) or p["url"], p["url"])
    branches = [b["branch"] for b in card.get("branches", []) if b.get("branch")]
    if branches:
        return Anchor("branch", branch_theme(branches[0]), branches[0], theme=True)
    s = pick_session(card)
    return Anchor("session", (s or {}).get("title", "") or card.get("title", ""), (s or {}).get("id", ""), theme=True)


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


def pending(card: dict, reply: Reply | None = None) -> list[Pending]:
    """What is left to close the thread, most urgent first, each with who can do it:
    - a question the agent asked: only the user can answer it;
    - each open pull request, by where it stands (failing checks, changes requested, approved, in review, draft);
    - unpushed commits and uncommitted files on the card's own branch;
    - a ticket that is not done although its pull request merged: probably done, so mark it;
    - a ticket that is not done and has no pull request yet: the work itself."""
    out = []
    if reply and reply.question:
        out.append(Pending(f"Answer the agent: {reply.question}", "you"))
    for p in card.get("prs", []):
        n = "#" + p["url"].rstrip("/").rsplit("/", 1)[-1]
        st, checks, review = p.get("state"), p.get("checks"), p.get("review")
        if st not in ("open", "draft"):
            continue
        if checks == "failing":
            out.append(Pending(f"Fix the failing checks on {n}", "session", p["url"]))
        if review == "changes requested":
            out.append(Pending(f"Address the review on {n}", "session", p["url"]))
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
            out.append(Pending(f"Commit {pl['changed']} changed files on {pl['branch']}", "session", pl["branch"]))
        if pl.get("ahead"):
            out.append(Pending(f"Push {pl['ahead']} commits on {pl['branch']}", "session", pl["branch"]))
    merged = any(p.get("state") == "merged" for p in card.get("prs", []))
    for k in card.get("tickets", []):
        if k.get("ref") or DONE_WORDS.search(k.get("status") or ""):
            continue
        if merged:
            out.append(Pending(f"Move {k['key']} to done: its pull request merged", "mark", k["key"]))
        elif not card.get("prs"):
            out.append(Pending(f"Work on {k['key']}", "session", k["key"]))
    return out
