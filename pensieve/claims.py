"""Which session holds a branch's work, when several sessions wrote to it.

A session claims the lines it wrote with its edit tools (Edit, Write, MultiEdit). A line counts when it is still on the
branch's current version of the file. A surviving line belongs to the latest session that wrote it. The session with the
most surviving lines holds the branch (on a tie, the later one). The others lose it: the branch is not active on their
threads, and each thread's other sinks still decide its state."""
import subprocess
from functools import lru_cache


def written(path: str) -> tuple:
    """The (file, line) pairs a transcript wrote with an edit tool (pensieve/transcripts.py)."""
    from pensieve import transcripts
    return transcripts.written(path)


@lru_cache(maxsize=2048)
def _at_tip(root: str, tip: str, rel: str) -> str:
    r = subprocess.run(["git", "-C", root, "show", f"{tip}:{rel}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def _at_branch(root: str, branch: str, rel: str) -> str:
    """A file's text on a branch, cached by the branch's tip: a commit on the branch reads the file again."""
    from pensieve import work
    tip = work._git(root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", ttl=30)
    return _at_tip(root, tip, rel) if tip else ""


def shares(sessions: list[dict], root: str, branch: str, lines_of=None, text_of=None) -> dict:
    """Each session's claim on a branch: {session id: {"surviving", "written", "share", "holds"}}.
    `sessions` are dicts with id, path and updated. `lines_of(session)` gives its (file, line) pairs and
    `text_of(file)` gives the branch's text of a file. By default they read the transcript and git."""
    lines_of = lines_of or (lambda s: written(s.get("path", "")))
    text_of = text_of or (lambda rel: _at_branch(root, branch, rel))
    owner, mine = {}, {}
    prefix = root.rstrip("/") + "/"
    for s in sorted(sessions, key=lambda x: x.get("updated") or 0):  # later sessions take a line over earlier ones
        pairs = [(p[len(prefix):], line) for p, line in lines_of(s) if p.startswith(prefix)]  # this checkout's files only
        mine[s["id"]] = len(pairs)
        cache = {}
        for rel, line in pairs:
            if rel not in cache:
                cache[rel] = text_of(rel)
            if line in cache[rel]:
                owner[(rel, line)] = s["id"]
    counts = {s["id"]: 0 for s in sessions}
    for sid in owner.values():
        counts[sid] += 1
    total = sum(counts.values())
    by_time = sorted(sessions, key=lambda x: x.get("updated") or 0)
    holder = max(reversed(by_time), key=lambda s: counts[s["id"]])["id"] if total else None  # a tie: the later one
    return {s["id"]: dict(surviving=counts[s["id"]], written=mine[s["id"]], share=counts[s["id"]] / total if total else 0,
                          holds=s["id"] == holder)
            for s in sessions}


def apply(threads: list[dict]) -> None:
    """Settle the branches that several threads share. A thread that does not hold the branch loses it: the branch
    leaves its sinks, so the branch does not keep it open. Its other sinks still decide its state."""
    groups = {}
    for t in threads:
        for b in t.get("branches", []):
            groups.setdefault((b.get("repo", ""), b["branch"]), []).append(t)
    for (_, branch), ts in groups.items():
        ts = [t for t in ts if t.get("_sessions")]
        if len(ts) < 2:
            continue
        root = next((p["path"] for t in ts for p in t.get("places", [])
                     if p.get("branch") == branch and p.get("git") and p.get("path")), "")
        if not root:
            continue
        sessions = [dict(id=t["_sessions"][0]["id"], path=t["_sessions"][0].get("path", ""),
                         updated=t["_sessions"][0].get("updated") or 0) for t in ts]
        res = shares(sessions, root, branch)
        if not any(r["surviving"] for r in res.values()):  # nothing of theirs is on the branch: no evidence either way
            continue
        won = {t["_sessions"][0]["id"] for t in ts if res[t["_sessions"][0]["id"]]["holds"]}
        won_tickets = {k["key"] for t in ts if t["_sessions"][0]["id"] in won for k in t.get("tickets", [])}
        # the session with the largest share of the branch's lines names the threads that share it
        lead = max(ts, key=lambda t: res[t["_sessions"][0]["id"]]["share"])["_sessions"][0]
        for t in ts:
            t["_lead_title"] = lead.get("title", "")
            r = res[t["_sessions"][0]["id"]]
            claim = dict(branch=branch, lost=not r["holds"], **r)
            t.setdefault("claims", {})[branch] = claim  # one per branch the thread shares
            if not t.get("claim") or t["claim"]["lost"]:  # `claim`: the branch it holds, else the first it lost
                t["claim"] = claim
            if not r["holds"]:  # the loser keeps only what it holds on its own: its other sinks decide its state,
                # and what it contributed to decides when it has none (task_states.apply): a session that wrote to
                # the branch is open while the branch is, not a thought
                c = t.setdefault("contributed", dict(branches=[], prs=[], tickets=[]))
                c["branches"] += [b for b in t["branches"] if b["branch"] == branch]
                c["prs"] += [p for p in t.get("prs", []) if p.get("head") == branch]
                c["tickets"] += [k for k in t.get("tickets", []) if k["key"] in won_tickets]
                t["branches"] = [b for b in t["branches"] if b["branch"] != branch]
                t["prs"] = [p for p in t.get("prs", []) if p.get("head") != branch]
                t["tickets"] = [k for k in t.get("tickets", []) if k["key"] not in won_tickets]
