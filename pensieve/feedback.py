"""Bug reports and ideas about Pensieve, from agents (the report_feedback MCP tool) and from the user (the app).
They stay in the `feedback` table of store.db on this Mac. Nothing is sent: the app shows each report, and the user
can open it as a pre-filled GitHub issue to send it, or dismiss it. An agent's report can hold code or paths from the
user's work, so a person reads it before it leaves the Mac."""
import platform
import time
import uuid
from urllib.parse import urlencode

from pensieve import store
from pensieve.models import FeedbackStatus
from pensieve.store import misc

KINDS = ("bug", "idea")
STATUSES = ("new", "sent", "dismissed")
REPO = "https://github.com/TedHaley/pensieve"
MAX_DETAILS = 4000  # characters: a report, not a log dump


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("pensieve")
    except PackageNotFoundError:
        return "dev"


def add(kind: str, summary: str, details: str = "", source: str = "user", context: dict | None = None) -> dict:
    """Record a report. `source`: the agent that filed it (its MCP client name), or "user"."""
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    summary = " ".join((summary or "").split())
    if not summary:
        raise ValueError("summary is empty: say in one line what went wrong or what would be better")
    item = dict(id=uuid.uuid4().hex[:10], ts=int(time.time()), kind=kind, summary=summary[:200],
                details=(details or "").strip()[:MAX_DETAILS], source=source or "user", status="new",
                version=_version(), context={k: v for k, v in (context or {}).items() if v})
    return misc.add_feedback(store.store_db(), item)


def items(status: str | None = None) -> list[dict]:
    """Newest first, each with the GitHub link that sends it."""
    out = [dict(x, issue_url=issue_url(x)) for x in misc.feedback(store.store_db())
           if status is None or x.get("status") == status]
    return sorted(out, key=lambda x: -x.get("ts", 0))


def set_status(id: str, status: str) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
    hit = misc.set_feedback_status(store.store_db(), id, FeedbackStatus(status))
    if not hit:
        raise KeyError(id)
    return hit


def issue_url(x: dict) -> str:
    """A new GitHub issue, filled in: the user reads it there before it is posted."""
    who = "an agent (" + x["source"] + ")" if x.get("source") not in (None, "", "user") else "the user"
    body = "\n".join([
        x.get("details") or "_No details._",
        "",
        "---",
        f"Reported by {who} with Pensieve {x.get('version', '?')} on macOS {platform.mac_ver()[0] or '?'} "
        f"({platform.machine()}).",
    ])
    labels = "bug" if x.get("kind") == "bug" else "enhancement"
    return f"{REPO}/issues/new?" + urlencode({"title": x.get("summary", ""), "body": body[:6000], "labels": labels})
