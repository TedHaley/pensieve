"""Tests of the search gate (pensieve/gate.py) and of installing it as an add-on with three hook entries
(pensieve/integrations). Run: python tests/gate.py"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["PENSIEVE_DATA"] = tempfile.mkdtemp(prefix="pensieve-gate-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import gate, integrations as ig  # noqa: E402


def test_what_is_a_search():
    yes = [("Grep", {}), ("Glob", {}), ("Bash", {"command": "grep -rn retry ."}), ("Bash", {"command": "cd src && rg x"}),
           ("Bash", {"command": "find . -name '*.py'"}), ("Bash", {"command": "git grep x"}),
           ("Bash", {"command": "ls\nrg x"})]
    no = [("Bash", {"command": "git status | grep M"}), ("Bash", {"command": "ls -la"}), ("Read", {}),
          ("Bash", {"command": "echo grep"})]
    assert all(gate.is_search(t, i) for t, i in yes)
    assert not any(gate.is_search(t, i) for t, i in no)


def test_a_turn():
    """Closed at a prompt, open after a Pensieve call, never closed where Pensieve can't help."""
    s = next(f"s{i}" for i in range(100) if gate.arm(f"s{i}") == "gate")
    prompt = "where do we retry failed payments"
    gate.reset(s, prompt)
    assert gate.check(s, "Grep", {}, lambda: True) == "deny"
    assert gate.check(s, "Grep", {}, lambda: False) == "unindexed"
    assert gate.check(s, "Bash", {"command": "ls"}, lambda: True) == "not_search"
    gate.mark(s)
    assert gate.check(s, "Grep", {}, lambda: True) == "open"
    gate.reset(s, prompt)
    assert gate.check(s, "Grep", {}, lambda: True) == "deny"


def test_prompt_hook_opens_the_gate():
    """The prompt hook's search counts as the turn's Pensieve call, whichever of the two hooks ends first; a short
    message mid-task keeps the gate as it was."""
    s = next(f"p{i}" for i in range(100) if gate.arm(f"p{i}") == "gate")
    prompt = "why do card retries stop after a decline"
    gate.reset(s, prompt)
    gate.mark(s, by="prompt")  # the hook answers after the reset
    assert gate.check(s, "Grep", {}, lambda: True) == "open"
    gate.reset(s, prompt)  # ... or before it: the fresh prompt-hook opening wins
    assert gate.check(s, "Grep", {}, lambda: True) == "open"
    gate.mark(s)  # a tool call opens it too, but a new prompt closes that
    gate.reset(s, "now look at the refund webhook instead")
    assert gate.check(s, "Grep", {}, lambda: True) == "deny"
    gate.mark(s)
    gate.reset(s, "ok continue")  # short: the task goes on
    assert gate.check(s, "Grep", {}, lambda: True) == "open"


def test_control_arm():
    ids = [f"session-{i}" for i in range(500)]
    control = [s for s in ids if gate.arm(s) == "control"]
    assert 60 < len(control) < 140, len(control)  # about 1 in 5
    assert gate.check(control[0], "Grep", {}, lambda: True) == "would_deny"
    assert gate.arm(control[0]) == gate.arm(control[0])  # the same session keeps its arm


def test_install_three_entries():
    """The add-on writes its three entries, reports "on", and its removal leaves other hooks alone."""
    ig.CLAUDE_SETTINGS = Path(tempfile.mkdtemp()) / "settings.json"
    ig.claude_plugin = lambda: False
    other = {"matcher": "Grep|Glob|Bash", "hooks": [{"type": "command", "command": "my-own-hook.sh"}]}
    ig.CLAUDE_SETTINGS.write_text(json.dumps({"hooks": {"PreToolUse": [other]}}))
    assert ig.set_hook("claude-search-gate", True, 8765) == "on"
    hooks = json.loads(ig.CLAUDE_SETTINGS.read_text())["hooks"]
    assert {e for e in hooks} == {"PreToolUse", "PostToolUse", "UserPromptSubmit"}
    assert hooks["PreToolUse"][0] == other and "/api/gate/check" in hooks["PreToolUse"][1]["hooks"][0]["command"]
    assert "mcp__plugin_pensieve_pensieve__" in hooks["PostToolUse"][0]["matcher"]
    assert ig.set_hook("claude-search-gate", False, 8765) == "off"
    assert json.loads(ig.CLAUDE_SETTINGS.read_text())["hooks"] == {"PreToolUse": [other]}


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t0 = time.time()
        t()
        print(f"ok   {t.__name__}  {round((time.time() - t0) * 1000)} ms")
    print(f"{len(tests)} passed")
