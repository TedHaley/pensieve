"""The coding agents that Pensieve can start in Terminal: how each one resumes one of its own sessions, how it starts
a new session with a first prompt, and whether it is installed. The user's harness (settings default_agent: one of
these, or "custom" for a command of their own in custom_agent) runs every session Pensieve starts. A session resumes
in the agent that wrote it when that is the harness, or when no harness is chosen; any other harness takes it over
with a first prompt that reads its transcript."""
import os
import shlex
import subprocess
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Agent:
    id: str             # the session source in Pensieve ("claude:<id>"), and the settings value
    name: str
    binary: str
    resume: tuple       # arguments that resume a session: "{sid}" is the session id; () when it can't
    start: tuple        # arguments that start an interactive session with a first prompt: "{prompt}"
    mcp: str = ""       # the integrations item that adds Pensieve's MCP server to the agent ("" when none)


AGENTS = {a.id: a for a in (
    Agent("claude", "Claude Code", "claude", ("--resume", "{sid}"), ("{prompt}",), "claude-mcp"),
    Agent("codex", "Codex", "codex", ("resume", "{sid}"), ("{prompt}",), "codex-mcp"),
    Agent("qwen", "Qwen Code", "qwen", ("--resume", "{sid}"), ("-i", "{prompt}"), "qwen-mcp"),
    Agent("gemini", "Gemini CLI", "gemini", ("--resume", "{sid}"), ("-i", "{prompt}"), "gemini-mcp"),
    Agent("cursor", "Cursor CLI", "cursor-agent", ("--resume", "{sid}"), ("{prompt}",), "cursor-mcp"),
)}
CUSTOM = "custom"
HANDOFF_PROMPT = ("Continue the {agent} session \"{title}\" (session {id}). It ran in another agent, so you can't "
                  "resume it directly: read its conversation first with `pensieve transcript {id}`. Then check the "
                  "live state of this folder's branch, pull requests and tickets, tell me where the work stands and "
                  "what you would do next, and ask me before you change anything.")
_found = (0.0, frozenset())


def custom() -> Agent | None:
    """The user's own harness (settings custom_agent: name, start and an optional resume command), or None."""
    from pensieve import settings
    c = settings.get("custom_agent") or {}
    try:
        start, resume = shlex.split(c.get("start") or ""), shlex.split(c.get("resume") or "")
    except ValueError:
        return None
    if not start:
        return None
    return Agent(CUSTOM, c.get("name") or start[0], start[0], tuple(resume[1:]) if resume else (), tuple(start[1:]))


def known() -> dict[str, Agent]:
    """Every agent Pensieve can start: the built-in ones, and the custom harness when it is set."""
    c = custom()
    return {**AGENTS, **({CUSTOM: c} if c else {})}


def get(agent_id: str) -> Agent | None:
    return known().get(agent_id)


def installed() -> frozenset[str]:
    """The ids of the agents whose command the user's shell finds. The commands run in Terminal, in the user's
    login shell, whose PATH can differ from this process's (agents installed with npm, mise or asdf), so the shell is
    asked. Kept for 10 minutes, per set of commands."""
    global _found
    every = known()
    names = " ".join(sorted({shlex.quote(a.binary) for a in every.values()}))
    if time.time() - _found[0] < 600 and getattr(installed, "names", None) == names:
        return _found[1]
    shell = os.environ.get("SHELL") or "/bin/zsh"
    try:
        out = subprocess.run([shell, "-ilc", f"for c in {names}; do command -v \"$c\" >/dev/null 2>&1 && echo \"$c\"; done"],
                             capture_output=True, text=True, timeout=15).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        out = []
    _found = (time.time(), frozenset(a.id for a in every.values() if a.binary in out))
    installed.names = names
    return _found[1]


def _cmd(agent: Agent, args: tuple, **values) -> str:
    return " ".join([shlex.quote(agent.binary)] + [shlex.quote(a.format(**values)) for a in args])


def harness() -> str:
    """The agent the user chose to run their sessions in (settings default_agent), or "" for automatic."""
    from pensieve import settings
    h = settings.get("default_agent") or ""
    return h if get(h) else ""


def resume(agent_id: str, sid: str) -> str | None:
    """The command that resumes session `sid` in the agent that wrote it, or None for an agent Pensieve can't start."""
    a = get(agent_id)
    return _cmd(a, a.resume, sid=sid) if a and a.resume else None


def start(agent_id: str, prompt: str) -> str:
    """The command that starts a new interactive session of the agent, with a first prompt."""
    a = get(agent_id) or AGENTS["claude"]
    return _cmd(a, a.start, prompt=prompt)


RESUME_WITH_PROMPT = {"claude"}  # agents whose resume takes a next prompt: `claude --resume <id> "<prompt>"`


def resume_with(agent_id: str, sid: str, prompt: str) -> str | None:
    """The command that resumes session `sid` and sends it `prompt` as its next message, or None for an agent whose
    resume can't take one."""
    a = get(agent_id)
    return _cmd(a, a.resume + ("{prompt}",), sid=sid, prompt=prompt) if a and agent_id in RESUME_WITH_PROMPT else None


def open_session(agent_id: str) -> str:
    """The command that starts the agent's interactive session with no first prompt: its start arguments without
    {prompt}, and without the option that takes it (qwen -i {prompt} is qwen)."""
    a = get(agent_id) or AGENTS["claude"]
    args = list(a.start)
    if "{prompt}" in args:
        i = args.index("{prompt}")
        del args[i - 1 if i and args[i - 1].startswith("-") else i:i + 1]
    return _cmd(a, tuple(args))


def continue_session(session_id: str, title: str = "") -> dict:
    """How to pick up session `session_id` ("claude:<id>"): {command, agent, name, handoff}. In the agent that wrote
    it when that is the harness or no harness is chosen; else the harness starts with HANDOFF_PROMPT."""
    source, _, sid = session_id.partition(":")
    h = harness()
    if (not h or h == source) and resume(source, sid):
        return dict(command=resume(source, sid), agent=source, name=get(source).name, handoff=False)
    to = h or choose()
    wrote = get(source).name if get(source) else source
    prompt = HANDOFF_PROMPT.format(agent=wrote, title=" ".join((title or "untitled").split())[:120], id=session_id)
    return dict(command=start(to, prompt), agent=to, name=get(to).name, handoff=True)


def choose(default: str = "", preferred=()) -> str:
    """The agent for a new session: the user's default when it is installed, else the first installed agent of
    `preferred` (the ones that worked on the thread), else the first installed one; Claude Code when none is found."""
    have, every = installed(), known()
    for a in (default, *preferred, *every):
        if a in every and a in have:
            return a
    return "claude"


def listing(default: str = "") -> list[dict]:
    """The agents, each with whether it is installed and whether Pensieve's MCP server is in its config already."""
    from pensieve import integrations
    have = installed()
    on = lambda a: bool(a.mcp) and integrations.mcp_state(a.mcp) in ("on", "theirs")
    return [dict(id=a.id, name=a.name, installed=a.id in have, default=a.id == default, resumes=bool(a.resume),
                 mcp=a.mcp, connect=f"pensieve integrate {a.mcp}" if a.mcp else "", connected=on(a))
            for a in known().values()]
