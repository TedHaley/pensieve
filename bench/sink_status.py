"""The GitHub state of each branch sink: merged, deleted, open, or exists.

  .venv/bin/python bench/sink_status.py    # reads bench/out/board/branch_sinks.json, writes branch_states.json

Each branch sink is "org/repo@branch". For each one, GitHub is asked whether the branch exists, and whether a pull
request from it is merged or open. A branch that no longer exists is DELETED. A branch with a merged pull request is
MERGED. A branch with an open pull request is OPEN. Any other existing branch is EXISTS. The result feeds
thread_model.sink_complete."""
import json
import subprocess
import sys
import urllib.parse
from pathlib import Path

OUT = Path(__file__).resolve().parent / "out" / "board"


def gh(args: list[str]) -> tuple[int, str]:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    return r.returncode, r.stdout


def status(sink: str) -> str:
    repo, branch = sink.split("@", 1)
    code, _ = gh(["api", f"repos/{repo}/branches/{urllib.parse.quote(branch, safe='')}"])
    if code != 0:
        return "DELETED"
    code, out = gh(["pr", "list", "-R", repo, "--head", branch, "--state", "all", "--json", "state"])
    prs = json.loads(out) if code == 0 and out.strip() else []
    states = {p["state"] for p in prs}
    if "MERGED" in states:
        return "MERGED"
    if "OPEN" in states:
        return "OPEN"
    return "EXISTS"


def main():
    sinks = json.loads((OUT / "branch_sinks.json").read_text())
    result = {}
    for s in sinks:
        if "@" not in s:
            result[f"branch {s}"] = "UNKNOWN"
            continue
        result[f"branch {s}"] = status(s)
    (OUT / "branch_states.json").write_text(json.dumps(result, indent=1))
    counts = {}
    for v in result.values():
        counts[v] = counts.get(v, 0) + 1
    print(f"branch sinks {len(result)}: {counts}")


if __name__ == "__main__":
    sys.exit(main())
