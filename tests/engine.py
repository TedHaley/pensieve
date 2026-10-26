"""Tests of the text engine (pensieve/engine.py): a command runs once, is killed with what it started when it takes
too long, and only one job runs at a time. Run: python tests/engine.py"""
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ["PENSIEVE_DATA"] = tempfile.mkdtemp(prefix="pensieve-engine-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import config, engine, settings  # noqa: E402


def test_command_answers():
    assert engine.run_command("cat", "hello there") == "hello there"
    assert engine.run_command("echo {prompt}", "from the argument") == "from the argument"


def test_command_is_killed_with_its_children():
    marker = f"pensieve-engine-test-{os.getpid()}"
    t = time.time()
    try:
        engine.run_command(f"sh -c 'sleep 30 & sleep 31 # {marker}'", "", timeout=1)
    except RuntimeError as e:
        assert "took more than" in str(e)
    else:
        raise AssertionError("no timeout")
    assert time.time() - t < 5
    time.sleep(0.3)
    left = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.strip()
    assert not left, f"processes left behind: {left}"


def test_missing_command():
    try:
        engine.run_command("no-such-agent-cli -p", "x")
    except RuntimeError as e:
        assert "can't find no-such-agent-cli" in str(e)
    else:
        raise AssertionError("no error")


def test_one_job_at_a_time():
    """A waiting job is not answered while another process holds the lock file, and is once the lock is free."""
    from pensieve import agent_jobs
    assert agent_jobs.take("say hi", "", None, "test", 20) is None  # queued
    settings.update({"text_engine": "cli", "cli_command": "cat"})
    with open(config.DATA_DIR / "engine.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        assert engine.run_once() == 0
    assert engine.run_once() == 1
    answer = agent_jobs.take("say hi", "", None, "test", 20)  # cat echoes the prompt, with the engine's mark
    assert answer.startswith(engine.MARK) and answer.endswith("say hi")


def test_engine_sessions_are_never_indexed():
    """A session that the engine ran (its mark, or its folder) is never the user's work, whatever the settings."""
    from pensieve import indexer, parsers
    s = parsers.Session("x", "claude", "/p", project="/Users/me/code/app",
                        turns=[("user", engine.MARK + "\nYou write the status line of one thread")])
    assert indexer.engine_session(s)
    settings.update({"skip_temp_sessions": False})
    assert indexer.engine_session(parsers.Session("y", "codex", "/p", project="/private/var/folders/x/T/pensieve-ai-123"))
    assert not indexer.engine_session(parsers.Session("z", "claude", "/p", project="/Users/me/code/app",
                                                      turns=[("user", "where do we retry payments")]))


def test_default_engine():
    """Someone who never chose an engine (or chose "agent" or "" before) gets Claude Code with Haiku when it is
    installed, else the built-in model when the Mac can run it, else a model server. Never no engine at all."""
    s = {**settings.DEFAULTS, "text_engine": "agent"}
    d = settings._default_engine(s)
    if config.find_exe("claude"):
        assert d["text_engine"] == "cli" and d["cli_command"].startswith("claude -p --model haiku")
    else:
        assert d == {"text_engine": "server" if engine.builtin_unsupported() else "builtin"}
    assert "" not in settings.ENGINES
    real_find, real_unsupported = config.find_exe, engine.builtin_unsupported
    try:
        for claude in (True, False):
            for why in (None, "no"):
                config.find_exe = lambda name, c=claude: "/bin/claude" if c and name == "claude" else real_find(name)
                engine.builtin_unsupported = lambda w=why: w
                assert settings._default_engine(s)["text_engine"] in settings.ENGINES
    finally:
        config.find_exe, engine.builtin_unsupported = real_find, real_unsupported


def test_sweep_stops_only_its_own_server():
    """A process whose engine is not the built-in one leaves alone a built-in server that another Pensieve process
    started, and stops the one that it started itself. With the built-in engine, an idle server stops."""
    real_pids, real_stop = engine.builtin_pids, engine.stop_builtin
    stopped = []
    engine.builtin_pids = lambda: [4242]
    engine.stop_builtin = lambda: stopped.append(1)
    try:
        settings.update({"text_engine": "server"})
        engine.builtin["pid"] = None
        engine.sweep()
        assert not stopped  # another process's server
        engine.builtin["pid"] = 4242
        engine.sweep()
        assert stopped == [1]  # its own server
        settings.update({"text_engine": "builtin"})
        engine.builtin["pid"], engine.builtin["last_used"] = None, time.time()
        engine.sweep()
        assert stopped == [1]  # in use
        engine.builtin["last_used"] = time.time() - engine.IDLE_SECONDS - 1
        engine.sweep()
        assert stopped == [1, 1]  # idle
    finally:
        engine.builtin_pids, engine.stop_builtin = real_pids, real_stop
        engine.builtin["pid"] = None


FAKE_AGENT = """import json, sys
text = sys.stdin.read()
assert text.startswith("[pensieve text job]"), "no mark"
print(json.dumps(dict(summary="A session that retried payments.", tags=["payments", "retries"])))
"""


def test_the_worker_drains_the_job_queue():
    """The essential path: summaries that wait in the queue are answered in the background by the worker that the
    app starts, with no agent session, and the next ask gets the answer."""
    import asyncio
    import inspect
    from pensieve import agent_jobs, llm, server, summarize
    assert "engine_worker()" in inspect.getsource(server.lifespan), "the app no longer starts the text engine"
    agent = Path(os.environ["PENSIEVE_DATA"]) / "fake_agent.py"
    agent.write_text(FAKE_AGENT)
    settings.update({"setup_done": True, "text_engine": "cli", "cli_command": f"{sys.executable} {agent}",
                     "engine_rate": 600})
    asks = [dict(prompt=f"Summarize session {i}", schema=summarize.SESSION_SCHEMA, kind="session summary") for i in range(4)]
    asks.append(dict(prompt="Summarize this session", schema=None, kind="session summary"))
    for a in asks:
        try:
            llm.complete(a["prompt"], max_tokens=700, schema=a["schema"], kind=a["kind"])
        except llm.Queued:
            pass
        else:
            raise AssertionError("answered before the engine ran")
    assert sum(agent_jobs.counts().values()) >= len(asks)

    async def drain():
        engine.builtin_pids = lambda: []  # the worker's sweep must not stop a real built-in server on this Mac
        task = asyncio.create_task(server.engine_worker())
        for _ in range(300):
            if not agent_jobs.counts():
                break
            await asyncio.sleep(0.1)
        task.cancel()
    asyncio.run(drain())
    assert not agent_jobs.counts(), f"jobs left: {agent_jobs.counts()}"
    for a in asks:
        got = llm.complete(a["prompt"], max_tokens=700, schema=a["schema"], kind=a["kind"])
        assert (got["summary"] if a["schema"] else json.loads(got)["summary"]) == "A session that retried payments."


def test_one_builtin_server():
    """The built-in server is started only when none runs: one that answers (a backend from before a restart left
    it) is used, one that still loads is waited for, extra ones are stopped, and two callers at once start one."""
    import threading as th
    real = {k: getattr(engine, k) for k in ("_builtin_up", "builtin_pids", "builtin_unsupported", "_have_weights")}
    real_popen, real_kill, real_sleep = engine.subprocess.Popen, engine.os.kill, engine.time.sleep
    world = dict(pids=[], up=False, started=0, killed=[])

    def popen(argv, **kw):
        assert "mlx_lm" in argv and str(engine.BUILTIN_PORT) in argv
        world["started"] += 1
        world["pids"].append(1000 + world["started"])
        th.Timer(0.3, lambda: world.update(up=True)).start()
        return type("Proc", (), {"pid": world["pids"][-1]})()
    try:
        engine._builtin_up = lambda: world["up"]
        engine.builtin_pids = lambda: list(world["pids"])
        engine.builtin_unsupported = lambda: None
        engine._have_weights = lambda: True
        engine.subprocess.Popen = popen
        engine.os.kill = lambda p, sig: world["killed"].append(p)
        engine.time.sleep = lambda s: real_sleep(min(s, 0.05))

        world.update(up=True, pids=[500])  # reuse after a restart
        assert engine.ensure_builtin() == engine.BUILTIN_URL and world["started"] == 0

        world.update(up=False, pids=[500, 501])  # one still loads, one extra
        th.Timer(0.3, lambda: world.update(up=True)).start()
        engine.ensure_builtin()
        assert world["started"] == 0 and world["killed"] == [501]

        world.update(up=False, pids=[], killed=[])  # none runs: two callers at once start one
        callers = [th.Thread(target=engine.ensure_builtin) for _ in range(2)]
        for c in callers:
            c.start()
        for c in callers:
            c.join()
        assert world["started"] == 1, f"started {world['started']} servers"
    finally:
        for k, v in real.items():
            setattr(engine, k, v)
        engine.subprocess.Popen, engine.os.kill, engine.time.sleep = real_popen, real_kill, real_sleep


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t0 = time.time()
        t()
        print(f"ok   {t.__name__}  {round((time.time() - t0) * 1000)} ms")
    print(f"{len(tests)} passed")
