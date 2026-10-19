"""The coding agents that Pensieve can start in Terminal: how each one resumes one of its own sessions, how it starts
a new session with a first prompt, and whether it is installed. A session resumes only in the agent that wrote it; a
new session on a thread starts in the user's default agent."""
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
    resume: tuple       # arguments that resume a session: "{sid}" is the session id
    start: tuple        # arguments that start an interactive session with a first prompt: "{prompt}"


AGENTS = {a.id: a for a in (
    Agent("claude", "Claude Code", "claude", ("--resume", "{sid}"), ("{prompt}",)),
    Agent("codex", "Codex", "codex", ("resume", "{sid}"), ("{prompt}",)),
    Agent("qwen", "Qwen Code", "qwen", ("--resume", "{sid}"), ("-i", "{prompt}")),
    Agent("gemini", "Gemini CLI", "gemini", ("--resume", "{sid}"), ("-i", "{prompt}")),
    Agent("cursor", "Cursor CLI", "cursor-agent", ("--resume", "{sid}"), ("{prompt}",)),
)}
_found = (0.0, frozenset())


def installed() -> frozenset[str]:
    """The ids of the agents whose command the user's shell finds. The commands run in Terminal, in the user's
    login shell, whose PATH can differ from this process's (agents installed with npm, mise or asdf), so the shell is
    asked. Kept for 10 minutes."""
    global _found
    if time.time() - _found[0] < 600:
        return _found[1]
    shell = os.environ.get("SHELL") or "/bin/zsh"
    names = " ".join(a.binary for a in AGENTS.values())
    try:
        out = subprocess.run([shell, "-ilc", f"for c in {names}; do command -v $c >/dev/null 2>&1 && echo $c; done"],
                             capture_output=True, text=True, timeout=15).stdout.split()
    except (OSError, subprocess.SubprocessError):
        out = []
    _found = (time.time(), frozenset(a.id for a in AGENTS.values() if a.binary in out))
    return _found[1]


def _cmd(agent: Agent, args: tuple, **values) -> str:
    return " ".join([agent.binary] + [shlex.quote(a.format(**values)) for a in args])


def resume(agent_id: str, sid: str) -> str | None:
    """The command that resumes session `sid` in the agent that wrote it, or None for an agent Pensieve can't start."""
    a = AGENTS.get(agent_id)
    return _cmd(a, a.resume, sid=sid) if a else None


def start(agent_id: str, prompt: str) -> str:
    """The command that starts a new interactive session of the agent, with a first prompt."""
    a = AGENTS[agent_id]
    return _cmd(a, a.start, prompt=prompt)


def choose(default: str = "", preferred=()) -> str:
    """The agent for a new session: the user's default when it is installed, else the first installed agent of
    `preferred` (the ones that worked on the thread), else the first installed one; Claude Code when none is found."""
    have = installed()
    for a in (default, *preferred, *AGENTS):
        if a in AGENTS and a in have:
            return a
    return "claude"


def listing(default: str = "") -> list[dict]:
    have = installed()
    return [dict(id=a.id, name=a.name, installed=a.id in have, default=a.id == default) for a in AGENTS.values()]
