"""The search gate for Claude Code (the claude-search-gate add-on, integrations.HOOK_SPEC): in each turn, Claude can't
search code with Grep, Glob or a grep-like shell command until it has called a Pensieve tool.

Three hooks post here. A user prompt starts a turn with the gate closed (reset); a call to one of Pensieve's MCP tools
opens it (mark); a code search asks first (check). The gate only closes in a repo that Pensieve indexes, so an agent
is never stuck where Pensieve can't help, and a backend that isn't running blocks nothing (the hook's curl fails).

For the field test, one session in `search_gate_holdout` (a setting) runs as the control: the gate never blocks there,
and logs what it would have done. Every decision is a row in the usage log (source "gate"), with the session id in
`conn`, so a report can compare the two arms."""
import hashlib
import re
import threading
import time

from pensieve import settings

# a code search that starts a command line; filtering output (`... | grep x`) is not one
SEARCH_CMD = re.compile(r"^\s*(cd [^;&]*(&&|;)\s*)?(grep|rg|ag|find|git grep)(\s|$)")
SEARCH_TOOLS = {"Grep", "Glob"}
DENY = ("Search with Pensieve first: call Pensieve's search tool with what the code does and the names you would "
        "guess (in:<folder>, kind:code and \"exact text\" narrow it). After one Pensieve call this turn, Grep, Glob, "
        "grep, rg and find are allowed.")
TURN_HOURS = 12  # a session's open gate is forgotten after this long without a new prompt
PROMPT_RACE = 10  # seconds: the reset and the prompt hook run at the same time; a fresh prompt-hook opening wins
SHORT = 3  # words: a shorter message ("ok, go on") continues the task and keeps the gate as it was

_open: dict[str, tuple[float, str]] = {}  # session id -> (when the gate opened this turn, "tool" or "prompt")
_lock = threading.Lock()


def arm(session: str) -> str:
    """'control' for one session in search_gate_holdout (0: none), picked by the session id, else 'gate'."""
    n = settings.get("search_gate_holdout")
    if not n or not session:
        return "gate"
    return "control" if int(hashlib.sha1(session.encode()).hexdigest()[:8], 16) % n == 0 else "gate"


def reset(session: str, prompt: str = ""):
    """A user prompt starts a turn with the gate closed, unless the message is short (it continues the task) or the
    prompt hook already searched Pensieve for it."""
    now = time.time()
    with _lock:
        at, by = _open.get(session, (0, ""))
        if len(prompt.split()) >= SHORT and not (by == "prompt" and now - at < PROMPT_RACE):
            _open.pop(session, None)
        for s in [s for s, (t, _) in _open.items() if t < now - TURN_HOURS * 3600]:
            del _open[s]


def mark(session: str, by: str = "tool"):
    """A Pensieve call opens the gate for the turn: one of its MCP tools ("tool"), or the prompt hook's search that
    put Pensieve's results in the prompt ("prompt")."""
    if session:
        with _lock:
            _open[session] = (time.time(), by)


def is_search(tool: str, tool_input: dict) -> bool:
    if tool in SEARCH_TOOLS:
        return True
    return tool == "Bash" and any(SEARCH_CMD.match(line) for line in str(tool_input.get("command") or "").splitlines())


def check(session: str, tool: str, tool_input: dict, indexed) -> str:
    """The decision for one tool call: not_search, open (Pensieve was called this turn), unindexed (`indexed()` is
    false: the repo isn't Pensieve's), would_deny (the control arm), or deny."""
    if not is_search(tool, tool_input):
        return "not_search"
    with _lock:
        if session in _open:
            return "open"
    if not indexed():
        return "unindexed"
    return "would_deny" if arm(session) == "control" else "deny"
