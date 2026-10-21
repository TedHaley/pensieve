"""Cards data: each session's sinks, with the PRs and tickets that go with them.

  .venv/bin/python bench/cards_build.py     # writes bench/out/graph/cards.json

For each PR sink, GitHub gives its head branch, title and body (cached in pr_details.json). Ticket keys come from a PR's
title, body and head branch, and from a branch's name. So:
  a branch's PRs are the PRs whose head branch is that branch
  a branch's tickets are the keys in its name
  a PR's tickets are the keys in its title, body and head branch
  a ticket's state comes from ticket_states.json (Jira statuses cached by hand), or unknown
Each card has the session's sinks, open ones first, then its source: the session, its folder and its times."""
import json
import re
import subprocess
from pathlib import Path

from pensieve.digest import NOT_TICKETS

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "bench" / "out" / "graph"
TICKET = re.compile(r"(?<![\w-])([A-Z][A-Z0-9]{1,9})-(\d{1,6})\b")
ORDER = {"open": 0, "unknown": 1, "stale": 2, "closed": 3}


def token(user: str) -> str:
    r = subprocess.run(["gh", "auth", "token", "--user", user], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


TOKENS = {}


def gh_json(args: list[str]):
    """gh output as JSON, from the active account, then from TedHaley's token."""
    import os
    for user in (None, "TedHaley"):
        env = dict(os.environ)
        if user:
            if user not in TOKENS:
                TOKENS[user] = token(user)
            env["GH_TOKEN"] = TOKENS[user]
        r = subprocess.run(["gh", *args], capture_output=True, text=True, env=env, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    return None


def keys_in(text: str) -> list[str]:
    return sorted({f"{a}-{b}" for a, b in TICKET.findall(text or "") if a not in NOT_TICKETS})


def pr_details(key: str, cache: dict) -> dict:
    if key in cache:
        return cache[key]
    repo, num = key.rsplit("#", 1)
    d = gh_json(["pr", "view", num, "-R", repo, "--json", "headRefName,title,body,state"]) or {}
    cache[key] = d
    return d


def main():
    rel = json.loads((OUT / "relations.json").read_text())
    threads = json.loads((OUT / "threads.json").read_text())
    tstate_path = OUT.parent / "board" / "ticket_states.json"
    tstates = json.loads(tstate_path.read_text()) if tstate_path.exists() else {}
    cache_path = OUT / "pr_details.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    nodes = {n["id"]: n for n in rel["nodes"]}

    prs_needed = {t["target"].split(":", 1)[1] for th in threads for t in th["sinks"] if t["target"].startswith("pr:")}
    for key in sorted(prs_needed):
        pr_details(key, cache)
    cache_path.write_text(json.dumps(cache, indent=1))

    pr_by_branch = {}
    for key, d in cache.items():
        if d.get("headRefName"):
            repo = key.rsplit("#", 1)[0]
            pr_by_branch.setdefault(f"{repo}@{d['headRefName']}", []).append(key)

    def ticket_state(k):
        return tstates.get(f"ticket {k}", "unknown")

    cards = []
    for th in threads:
        sinks = []
        for t in th["sinks"]:
            target = t["target"]
            kind, name = target.split(":", 1)
            item = dict(kind=kind, name=name, state=t["state"], prs=[], tickets=[])
            if kind == "pr":
                d = cache.get(name, {})
                item["title"] = d.get("title", "")
                item["branch"] = f"{name.rsplit('#', 1)[0]}@{d['headRefName']}" if d.get("headRefName") else ""
                item["tickets"] = [dict(key=k, state=ticket_state(k)) for k in keys_in(
                    d.get("title", "") + " " + (d.get("body") or "") + " " + d.get("headRefName", ""))]
            elif kind == "branch":
                item["prs"] = [dict(key=k, state=cache.get(k, {}).get("state", "UNKNOWN"),
                                    title=cache.get(k, {}).get("title", "")) for k in pr_by_branch.get(name, [])]
                item["tickets"] = [dict(key=k, state=ticket_state(k)) for k in keys_in(name.split("@", 1)[-1])]
            sinks.append(item)
        sinks.sort(key=lambda s: (ORDER.get(s["state"], 9), s["kind"], s["name"]))
        s = th["sessions"][0]
        node = nodes.get(s, {})
        cards.append(dict(id=th["id"], state=th["state"], quiet_days=th["quiet_days"], sinks=sinks,
                          source=dict(session=s, title=node.get("label", ""), folder=node.get("project", ""),
                                      written=node.get("written", 0))))
    cards.sort(key=lambda c: (ORDER.get(c["state"], 9), c["quiet_days"]))
    (OUT / "cards.json").write_text(json.dumps(cards, indent=1))
    print(f"cards {len(cards)}; sinks {sum(len(c['sinks']) for c in cards)}; "
          f"sinks with a PR {sum(1 for c in cards for s in c['sinks'] if s['prs'] or s.get('title'))}; "
          f"with a ticket {sum(1 for c in cards for s in c['sinks'] if s['tickets'])}")


if __name__ == "__main__":
    main()
