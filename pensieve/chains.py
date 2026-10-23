"""The chains of a thread: each pair in one direction, branch -> PR -> ticket, with the state of each end.

  a branch pairs with its PRs (a PR's head is the branch), or with its tickets (keys in its name) when it has no PR,
  or stands alone
  a PR pairs with its tickets (keys in its title or head branch), or stands alone when its branch is not listed
  a ticket with no pair stands alone

A pair is open when one of its ends is verified open (thread_model.sink_complete). An unknown end counts as closed.
Each end's state word comes from the status of its sink (sink_status.status_of_task)."""
import re

from pensieve import thread_model as tm
from pensieve.sink_status import pr_key

WORDS = {"EXISTS": "open", "OPEN": "open", "MERGED": "merged", "CLOSED": "closed", "DELETED": "deleted", "Done": "done"}


def _mentions(text: str, key: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(key)}(?![0-9])", text or "", re.I) is not None


def _end(kind: tm.SinkKind, name: str, short: str, label: str, status: dict, title: str = "", url: str = "",
         path: str = "") -> dict:
    value = status.get(label, "")
    done = tm.sink_complete(tm.Sink(kind, name), status)
    return {"kind": kind.value, "name": name, "short": short, "title": title, "url": url, "path": path,
            "word": WORDS.get(value, value.lower() if value else "unknown"),
            "open": done == tm.Completion.OPEN}


def build(task: dict, status: dict) -> list[dict]:
    """The pairs of a task: [{a: end, b: end or None, open: bool}]. `status` is sink_status.status_of_task's result."""
    prs = [p for p in task.get("prs", []) if pr_key(p.get("url", ""))]
    tickets = [k for k in task.get("tickets", []) if k.get("key")]
    pairs, seen, used = [], set(), set()

    def add(a: dict, b: dict | None = None) -> None:
        if b and b["kind"] == "ticket":
            used.add(b["name"])
        if a["kind"] == "ticket":
            used.add(a["name"])
        key = (a["kind"], a["name"], b and (b["kind"], b["name"]))
        if key not in seen:
            seen.add(key)
            pairs.append({"a": a, "b": b, "open": a["open"] or bool(b and b["open"])})

    def pr_end(p: dict) -> dict:
        key = pr_key(p["url"])
        return _end(tm.SinkKind.PR, key, "#" + key.rsplit("#", 1)[-1], f"pr {key}", status, p.get("title", ""),
                    url=p.get("url", ""))

    def ticket_end(k: dict) -> dict:
        return _end(tm.SinkKind.TICKET, k["key"], k["key"], f"ticket {k['key']}", status, k.get("title", ""),
                    url=k.get("url", ""))

    branch_names = set()
    for b in task.get("branches", []):
        name = b.get("branch")
        if not name:
            continue
        sink = f"{b.get('repo', '')}@{name}"
        place = next((pl for pl in task.get("places", []) if pl.get("branch") == name), {})
        branch_end = _end(tm.SinkKind.BRANCH, sink, name.split("/")[-1], f"branch {sink}", status, name,
                          url=place.get("remote_url", ""), path=place.get("path", ""))
        branch_names.add(name)
        own = [p for p in prs if p.get("head") == name]
        if own:
            for p in own:
                add(branch_end, pr_end(p))
        else:
            named = [k for k in tickets if _mentions(name, k["key"])]
            if named:
                for k in named:
                    add(branch_end, ticket_end(k))
            else:
                add(branch_end)

    for p in prs:
        end = pr_end(p)
        named = [k for k in tickets if _mentions(f"{p.get('title', '')} {p.get('head', '')}", k["key"])]
        if named:
            for k in named:
                add(end, ticket_end(k))
        elif p.get("head") not in branch_names:
            add(end)

    for k in tickets:
        if k["key"] not in used:
            add(ticket_end(k))
    return pairs
