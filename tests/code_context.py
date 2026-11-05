"""Tests of the context that search and read add to a code hit (pensieve/codectx.py). Run: python tests/code_context.py

The files, the bundle and the databricks CLI are made up. The fake CLI is a script on PATH."""
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pensieve import codectx  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="pensieve-codectx-test-"))


def write(rel, text):
    p = TMP / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return str(p)


def test_about_python_docstring():
    """The first sentence of a module docstring, over several lines."""
    f = write("a/main.py", '#!/usr/bin/env python\n"""Streaming matcher for the new\nbuild. It reads Kafka."""\nx = 1\n')
    assert codectx.about(f) == "Streaming matcher for the new build."


def test_about_comments():
    """A leading run of line comments, a block comment, and nothing for a file that starts with code."""
    assert codectx.about(write("b/x.ts", "// Sends the digest.\n// Daily.\nexport const a = 1\n")) == "Sends the digest."
    assert codectx.about(write("b/y.go", "/**\n * Package y parses dates.\n */\npackage y\n")) == "Package y parses dates."
    assert codectx.about(write("b/z.py", "import os\n")) is None


def test_about_skips_pragmas():
    """Lint and notebook markers are not a summary: the docstring after them is."""
    f = write("b/p.py", '# flake8: noqa: F401\n# Databricks notebook source\n"""Matches people."""\n')
    assert codectx.about(f) == "Matches people."
    assert codectx.about(write("b/q.py", "# flake8: noqa\nimport os\n")) is None


def test_code_only_scope():
    """A plain query searches code, in one indexed repo and in the scope of everything. A kind or project filter,
    an unindexed repo, or a scope with no repos keeps the search wide."""
    from pensieve import mcp_server, scopes
    repo = scopes.Scope(name="r", sources=[], repos={"/r"})
    assert mcp_server._code_only(repo, "retry webhook", None)
    assert mcp_server._code_only(None, "retry webhook", None)
    assert mcp_server._code_only(scopes.Scope(name="a", sources=[], repos=None), "retry webhook", None)
    assert not mcp_server._code_only(repo, "retry webhook", "session")
    assert not mcp_server._code_only(None, "retry kind:session", None)
    assert not mcp_server._code_only(None, "kind:doc streaming", None)
    assert not mcp_server._code_only(repo, "project:api decisions", None)
    assert not mcp_server._code_only(scopes.Scope(name="u", sources=[], repos={"/r"}, unindexed=True), "x", None)
    assert not mcp_server._code_only(scopes.Scope(name="d", sources=[], repos=set()), "x", None)


def test_siblings_order():
    """Test folders first, then config and doc files, then other folders and code. The file itself is left out."""
    f = write("c/main.py", "")
    for rel in ("c/job.yml", "c/README.md", "c/util.py", "c/__init__.py"):
        write(rel, "")
    (TMP / "c/tests").mkdir()
    (TMP / "c/sub").mkdir()
    assert codectx.siblings(f) == ["tests/", "README.md", "job.yml", "sub/", "util.py"]
    assert codectx.siblings(f, limit=2) == ["tests/", "README.md", "+3 more"]


def test_enclosing_innermost():
    """A line inside a nested function gives the nested function, with its decorator."""
    f = write("d/m.py", "class A:\n    @staticmethod\n    def f():\n        x = 1\n        return x\n\n\ny = 2\n")
    d = codectx.enclosing(f, 4)
    assert (d["name"], d["start"], d["end"]) == ("f", 2, 5) and not d["truncated"]
    assert codectx.enclosing(f, 8) is None
    assert codectx.enclosing(write("d/m.js", "function f() {}\n"), 1) is None


def test_enclosing_long():
    """A definition longer than the limit keeps its head and the lines around the hit."""
    body = "".join(f"    x{i} = {i}\n" for i in range(400))
    d = codectx.enclosing(write("e/long.py", "def big():\n" + body), 300)
    assert d["truncated"] and d["text"].startswith("def big():")
    assert "x298 = 298" in d["text"] and "x50 = 50" not in d["text"]
    assert len(d["text"].split("\n")) <= codectx.MAX_DEFINITION_LINES + 1


BUNDLE = """bundle:
  name: pipes
targets:
  dev:
    workspace:
      host: https://dev.example.com
      profile: dev
  prod_ci:
    workspace:
      host: https://prod.example.com/
  orphan:
    workspace:
      host: https://nobody.example.com
"""

JOB = """resources:
  jobs:
    my_stream:
      name: "my_stream_${bundle.target}"
      tasks:
        - task_key: run
      continuous:
        pause_status: "PAUSED"
targets:
  prod_ci:
    resources:
      jobs:
        my_stream:
          tasks:
            - task_key: run
"""

FAKE_CLI = """#!/bin/sh
# answers like `databricks ... -o json` for profile prod; fails login for profile dev
case "$*" in
  *"--profile dev"*) echo "Error: refresh token is invalid" >&2; exit 1 ;;
  "jobs list --name my_stream_prod_ci --profile prod -o json") echo '[{"job_id": 7}]' ;;
  "jobs list "*) echo '[]' ;;
  "jobs get 7 --profile prod -o json") echo '{"settings": {"continuous": {"pause_status": "PAUSED"}}}' ;;
  "jobs list-runs --job-id 7 --limit 1 --profile prod -o json")
    echo '[{"run_id": 9, "start_time": 1790000000000, "state": {"result_state": "SUCCESS"}}]' ;;
  *) echo "unexpected: $*" >&2; exit 2 ;;
esac
"""


def setup_bundle():
    write("bundle/databricks.yml", BUNDLE)
    code = write("bundle/pipes/stream/main.py", '"""Stream."""\n')
    write("bundle/pipes/stream/job.yml", JOB)
    cfg = write("databrickscfg", "[dev]\nhost = https://dev.example.com\n[prod]\nhost = https://prod.example.com\n"
                                 "[acct]\nhost = https://accounts.cloud.databricks.com\n")
    os.environ["DATABRICKS_CONFIG_FILE"] = cfg
    cli = write("bin/databricks", FAKE_CLI)
    os.chmod(cli, os.stat(cli).st_mode | stat.S_IEXEC)
    os.environ["PATH"] = f"{TMP / 'bin'}:{os.environ['PATH']}"
    return code


def test_bundle_jobs_and_targets():
    """A job file next to the code names its jobs. Targets map to a profile by name or by host. A target whose
    host has no profile is left out."""
    code = setup_bundle()
    jobs = codectx.bundle_jobs(code)
    assert [(j["key"], j["name"]) for j in jobs] == [("my_stream", "my_stream_${bundle.target}")]
    assert codectx.targets(jobs[0]["file"]) == [dict(target="dev", profile="dev"),
                                                dict(target="prod_ci", profile="prod")]


def test_job_state():
    """Deployed in prod: paused, with its last run. The dev login failed: unknown, not "not deployed"."""
    code = setup_bundle()
    codectx._cache.clear()
    r = codectx.job_state(code)
    (job,) = r["jobs"]
    (dep,) = job["deployed"]
    assert (dep["target"], dep["job_id"], dep["trigger"], dep["paused"]) == ("prod_ci", 7, "continuous", True)
    assert dep["last_run"]["run_id"] == 9 and dep["last_run"]["state"] == "SUCCESS"
    assert job["not_deployed"] == [] and job["unknown"] == ["dev"]
    assert "databricks auth login --profile dev" in r["cannot_read"]["dev"]


def test_no_jobs():
    assert codectx.job_state(write("f/plain.py", "x = 1\n"))["jobs"] == []


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t0 = time.time()
        t()
        print(f"ok   {t.__name__}  {round((time.time() - t0) * 1000)} ms")
    print(f"{len(tests)} passed")
