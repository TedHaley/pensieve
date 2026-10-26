"""Chat messages on threads (ROADMAP D11): a collected Slack message or thread joins each thread that it names.

A message names a thread by an exact reference in its text:
- one of the thread's ticket keys (ABC-123);
- the link of one of its pull requests, or `repo#123` for it;
- the name of its feature branch, when the name is specific (it has a dash, a slash, an underscore or a digit, and
  at least 6 characters).

A message never merges threads, as a ticket or a pull request does (work.group): many messages name two pieces of
work at once. A direct message or a private channel stays `private`: the card on this Mac shows it, and a shared card
leaves it out."""
import re

from pensieve import cardtext

LIMIT = 10  # messages on one thread, newest first
_KEY = re.compile(r"(?<![A-Za-z0-9-])([A-Z][A-Z0-9]{1,9}-\d{1,6})(?![0-9])")
_PR_URL = re.compile(r"github\.com/[^/\s]+/([^/\s]+)/pull/(\d+)", re.I)
_PR_SHORT = re.compile(r"(?<![\w/-])(?:[\w.-]+/)?([A-Za-z][\w.-]*)#(\d+)\b")  # app#7 or org/app#7
_WORD = re.compile(r"[\w][\w./-]*[\w]")
_refs_cache: dict[str, tuple[float, set]] = {}  # item path -> (its time, its references)


def _branch_ok(name: str) -> bool:
    return len(name) >= 6 and bool(re.search(r"[-/\d_]", name)) and name not in cardtext.DEFAULT_BRANCHES


def _item_refs(it: dict) -> set:
    """The references in a message: ("key", ABC-1), ("pr", repo, n) and ("word", token). Read once per version."""
    hit = _refs_cache.get(it["path"])
    if hit and hit[0] == it["updated"]:
        return hit[1]
    text = f"{it['title']}\n{it['text']}"
    refs = {("key", k) for k in _KEY.findall(text)}
    refs |= {("pr", r.lower(), n) for r, n in _PR_URL.findall(text) + _PR_SHORT.findall(text)}
    refs |= {("word", w) for w in _WORD.findall(text) if _branch_ok(w)}
    _refs_cache[it["path"]] = (it["updated"], refs)
    return refs


def _thread_refs(t: dict) -> dict:
    """{reference: why} of thread `t`, in the form of _item_refs."""
    out = {}
    for k in t.get("keys") or [x["key"] for x in t.get("tickets", []) if x.get("key")]:
        out[("key", k)] = f"names {k}"
    for p in t.get("prs", []):
        m = _PR_URL.search(p.get("url", ""))
        if m:
            out[("pr", m.group(1).lower(), m.group(2))] = f"links PR #{m.group(2)}"
    for b in {b["branch"] for b in t.get("branches", [])}:
        if _branch_ok(b):
            out[("word", b)] = f"names {b}"
    return out


def attach(threads: list[dict], items: list[dict]) -> None:
    """Set each thread's `chat`: the messages of `items` (work._item rows of chat documents) that name it, newest
    first, each {title, url, updated, private, why, path}, and `chat_more`, the count left out."""
    by_ref: dict[tuple, list[tuple[dict, str]]] = {}
    for t in threads:
        t["chat"], t["chat_more"] = [], 0
        for ref, why in _thread_refs(t).items():
            by_ref.setdefault(ref, []).append((t, why))
    if not by_ref:
        return
    for it in sorted(items, key=lambda i: -i["updated"]):
        done = set()
        for ref in _item_refs(it) & by_ref.keys():
            for t, why in by_ref[ref]:
                if id(t) in done:
                    continue
                done.add(id(t))
                if len(t["chat"]) < LIMIT:
                    t["chat"].append(dict(title=it["title"], url=it["url"], updated=it["updated"],
                                          private=it["private"], why=why, path=it["path"]))
                else:
                    t["chat_more"] += 1
