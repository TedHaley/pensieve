"""Session digests: the part of a session that a thread can use without its transcript.

A digest holds the session's id, its times, its links and (later) its summary and steps. It holds no transcript
text. `build` makes one from a parsed session (parsers.Session). The local app keeps digests. The cloud service
stores the same JSON. Schema: pensieve/session_digest.schema.json."""
import re
from urllib.parse import urlsplit, urlunsplit

SCHEMA = "session_digest/0.1"

# GitHub pull request URL and the org/repo#n form. Both give the key "org/repo#n".
_PR_URL = re.compile(r"https?://(?:www\.)?github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_PR_REF = re.compile(r"(?<![\w/.@-])([\w.-]+/[\w.-]+)#(\d+)\b")
_TICKET = re.compile(r"(?<![\w-])([A-Z][A-Z0-9]{1,9})-(\d{1,6})\b")
_URL = re.compile(r"https?://[^\s`'\"<>()\[\]{}]+")
_BRANCH = re.compile(r"\b(?:checkout -b|switch -c|switch -C|checkout -B|push -u origin|push origin) ([\w./-]+)")

# Words that look like ticket keys but are not. Standards, formats and tools.
NOT_TICKETS = {"UTF", "ISO", "SHA", "HTTP", "HTTPS", "TLS", "SSL", "RFC", "ES", "MD", "MP", "PDF", "CSV", "JSON",
               "API", "UTC", "GMT", "ASCII", "ARM", "X", "IPV", "IPV6", "AES", "RSA", "SHA256", "OAUTH", "CVE",
               "COVID", "H", "ISO8601", "PEP", "CPU", "GPU", "RAM", "SSH", "DNS", "TCP", "UDP", "XML", "HTML", "CSS"}

TRAILING = ".,;:!?"


def canonical_url(url: str) -> str:
    """A URL without trailing punctuation, fragment or query. The same page gives the same key."""
    url = url.rstrip(TRAILING)
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def extract_links(text: str) -> list[dict]:
    """Every link in `text`, once per key, with how often it appears. Each record has kind, key and ref."""
    found: dict[tuple[str, str], dict] = {}

    def add(kind: str, key: str, ref: str):
        rec = found.setdefault((kind, key), dict(kind=kind, key=key, ref=ref, count=0))
        rec["count"] += 1

    pr_spans = []
    for m in _PR_URL.finditer(text):
        pr_spans.append(m.span())
        add("pr", f"{m.group(1)}#{m.group(2)}", m.group(0).rstrip(TRAILING))
    for m in _PR_REF.finditer(text):
        if any(a <= m.start() < b for a, b in pr_spans):
            continue
        add("pr", f"{m.group(1)}#{m.group(2)}", f"{m.group(1)}#{m.group(2)}")
    for m in _TICKET.finditer(text):
        if m.group(1) in NOT_TICKETS:
            continue
        key = f"{m.group(1)}-{m.group(2)}"
        add("ticket", key, key)
    for m in _BRANCH.finditer(text):
        name = m.group(1).rstrip(TRAILING)
        if name:
            add("branch", name, name)
    for m in _URL.finditer(text):
        if _PR_URL.match(m.group(0)):
            continue  # a pull request URL is already a pr link
        url = canonical_url(m.group(0))
        if url:
            add("url", url, url)
    return sorted(found.values(), key=lambda r: (r["kind"], -r["count"], r["key"]))


def build(session) -> dict:
    """A digest of a parsed session. `summary`, `steps_done`, `steps_left` and `closing_signal` are empty here;
    the summarizer fills them. The links come from the text of the user's and the agent's turns."""
    text = "\n".join(t for _, t in session.turns if t)
    return {
        "schema": SCHEMA,
        "session_id": session.id,
        "source": session.source,
        "started_at": session.started or "",
        "ended_at": session.updated or "",
        "summary": "",
        "steps_done": [],
        "steps_left": [],
        "closing_signal": None,
        "links": extract_links(text),
    }
