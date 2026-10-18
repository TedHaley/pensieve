"""Connector profiles: what Pensieve knows about the cloud services that people connect most (Slack, Notion, Google
Drive, HubSpot, GitHub, Granola, Linear).

The collection itself is the generic one in connections.py. A profile adds what the generic code can not guess:
- the read tools of the service's MCP servers, also the ones whose names fail the verb rule (`API-post-search`);
- a fixed collection plan (`recipe`) and the follow-up call for each item found (`follow`), so a server that Pensieve
  connects to itself needs no model to plan the calls;
- a hint for the model when Claude Code collects;
- how to read an item: its key, title, times and people (author, assignee, owner, attendees, commenters);
- for Slack, how to split its text results into one item per message or thread;
- how a user without Claude Code adds the service: the official remote MCP URL with OAuth sign-in, or a token.

People are kept as `_people` in the raw item and in the front matter of the Markdown, each with the service and its
id there (a Slack user id, a GitHub login, an email), so a people graph can later tie one person's identities."""
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def _iso(v) -> str:
    """A time as ISO 8601 UTC: from ISO text, Unix seconds or milliseconds, or a Slack ts."""
    if v in (None, ""):
        return ""
    try:
        f = float(v)
        if f > 1e11:
            f /= 1000
        return datetime.fromtimestamp(f, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    try:
        return datetime.fromisoformat(str(v).strip().replace("Z", "+00:00")).astimezone(timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return ""


_PERSON_TEXT = re.compile(r"^\s*(?P<name>[^<(]*?)\s*(?:<(?P<email>[^<>@\s]+@[^<>\s]+)>)?\s*"
                          r"(?:\((?:ID:\s*)?(?P<id>[\w.-]+)\))?\s*$")


def person(v, service: str) -> dict | None:
    """One person as {name, email, id, service}, from a user object or from text like 'Name <a@b.c> (ID: U123)'."""
    if isinstance(v, dict):
        if isinstance(v.get("user"), dict) and not (v.get("name") or v.get("email")):  # {"user": {...}} wrappers
            return person(v["user"], service)
        name = next((v[k] for k in ("displayName", "display_name", "real_name", "name", "full_name", "fullName",
                                    "login", "username") if isinstance(v.get(k), str) and v[k].strip()), "")
        email = next((v[k] for k in ("email", "emailAddress", "email_address") if isinstance(v.get(k), str)), "")
        pid = next((str(v[k]) for k in ("login", "accountId", "account_id", "user_id", "userId", "id")
                    if isinstance(v.get(k), (str, int)) and str(v[k])), "")
        if not (name or email or pid):
            return None
        return {k: x for k, x in dict(name=name, email=email, id=pid, service=service).items() if x}
    if isinstance(v, (str, int)) and str(v).strip():
        m = _PERSON_TEXT.match(str(v))
        if m and (m["email"] or m["id"]):
            return {k: x for k, x in dict(name=m["name"], email=m["email"], id=m["id"], service=service).items() if x}
        s = str(v).strip()
        if s.isdigit() or re.fullmatch(r"[UW][A-Z0-9]{8,}", s):  # a HubSpot owner id, a Slack user id
            return dict(id=s, service=service)
        if "@" in s and " " not in s:
            return dict(email=s, service=service)
        return dict(name=s[:100], service=service)
    return None


# The fields that name people, by role. A field may hold one person or a list.
ROLES = {
    "author": ("author", "creator", "created_by", "createdBy", "reporter", "user", "from", "sender", "organizer",
               "lastModifyingUser", "last_edited_by"),
    "assignee": ("assignee", "assignees", "assigned_to", "assignedTo", "requested_reviewers", "reviewers"),
    "owner": ("owner", "owners", "hubspot_owner_id", "ownerId", "owner_id", "lead"),
    "attendee": ("attendees", "participants", "invitees", "people", "members", "guests"),
}


def people(d: dict, service: str) -> list[dict]:
    """Everyone an item names, each once per role: [{role, name, email, id, service}]."""
    out, seen = [], set()
    if isinstance(d.get("properties"), dict):  # HubSpot keeps the owner there
        d = {**{k: v for k, v in d["properties"].items() if isinstance(v, (str, int))}, **d}

    def add(role, v):
        p = person(v, service)
        if not p:
            return
        k = (role, p.get("id") or p.get("email") or p.get("name"))
        if k not in seen:
            seen.add(k)
            out.append({"role": role, **p})
    for role, keys in ROLES.items():
        for k in keys:
            v = d.get(k)
            for x in (v if isinstance(v, list) else [v] if v not in (None, "", {}) else [])[:50]:
                add(role, x)
    for c in d["comments"][:100] if isinstance(d.get("comments"), list) else []:
        if isinstance(c, dict):
            add("commenter", c.get("author") or c.get("user"))
    for i, m in enumerate(d["messages"][:200] if isinstance(d.get("messages"), list) else []):
        if isinstance(m, dict):
            add("author" if i == 0 else "commenter", m.get("from"))
    return out


# How the Slack MCP server (also the claude.ai connector) writes its results: Markdown inside JSON. Search results
# are "### Result 1 of 2" blocks; channel and thread history are "=== Message from … ===" blocks.
_RESULT = re.compile(r"^### Result \d+ of \d+\s*$", re.M)
_MESSAGE = re.compile(r"^=== Message from (?P<from>.+?) at (?P<time>.+?) ===\s*$", re.M)
_CHANNEL = re.compile(r"^Channel:\s*(?P<name>.+?)\s*\((?:ID:\s*)?(?P<id>[A-Z0-9]+)\)\s*$", re.M)


def _slack_field(block: str, name: str) -> str:
    m = re.search(rf"^{name}:[ \t]*(.*)$", block, re.M)
    return m.group(1).strip() if m else ""


def _private_channel(label: str, cid: str, block: str) -> bool:
    return bool(re.match(r"(DM|Group|mpdm-)", label or "") or re.fullmatch(r"[DG][A-Z0-9]{8,}", cid or "")
                or re.search(r"^\W*Participants:", block, re.M))


_THREAD_PART = re.compile(r"^(?:=== THREAD PARENT MESSAGE ===|--- Reply \d+ of \d+ ---)\s*$", re.M)


def _slack_messages(text: str) -> list[dict]:
    head = _CHANNEL.search(text.split("===", 1)[0])
    out = []
    if _THREAD_PART.search(text):  # a thread: the parent message, then "--- Reply 1 of 3 ---" blocks
        for block in _THREAD_PART.split(text)[1:]:
            block = re.sub(r"^=== THREAD REPLIES.*$", "", block, flags=re.M)
            body = re.sub(r"^(From|Time|Message TS):.*\n?", "", block.strip(), flags=re.M).strip()
            out.append({"from": _slack_field(block, "From"), "time": _slack_field(block, "Time"),
                        "ts": _slack_field(block, "Message TS"), "text": body[:20000]})
        return [dict(m, channel=head["name"] if head else "", channel_id=head["id"] if head else "") for m in out]
    parts = _MESSAGE.split(text)  # [before, from, time, body, from, time, body, ...]
    for i in range(1, len(parts) - 2, 3):
        frm, when, body = parts[i], parts[i + 1], parts[i + 2]
        ts = re.search(r"^Message TS:\s*(\S+)", body, re.M)
        body = re.sub(r"^Message TS:.*\n?", "", body, flags=re.M).strip()
        out.append({"from": frm.strip(), "time": when.strip(), "ts": ts.group(1) if ts else "", "text": body[:20000]})
    return [dict(m, channel=head["name"] if head else "", channel_id=head["id"] if head else "") for m in out]


def _one_line(s: str, n: int = 100) -> str:
    s = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2", s or "")
    return re.sub(r"\s+", " ", s).strip()[:n]


def slack_parse(tool: str, args: dict, data) -> list[dict] | None:
    """Slack's text results as items: one per message found by a search or read from a channel, and one per
    thread (with all its messages) read with a thread tool. None when the result is not in this form."""
    if not isinstance(data, dict):
        return None
    text = next((data[k] for k in ("results", "messages", "thread", "result") if isinstance(data.get(k), str)), None)
    if text is None:
        return None
    text = text.replace("\\/", "/")
    if _RESULT.search(text):
        items = []
        for block in _RESULT.split(text)[1:]:
            block = block.split("\n---", 1)[0]
            ch = _CHANNEL.search(block)
            cid, label = (ch["id"], ch["name"]) if ch else ("", "")
            ts = _slack_field(block, "Message_ts")
            link = re.search(r"\((https?://[^)\s]+)\)", _slack_field(block, "Permalink"))
            link = link.group(1) if link else ""
            thread = re.search(r"thread_ts=([\d.]+)", link)
            if not (ts or link):
                continue
            body = block.split("\nText:", 1)[1] if "\nText:" in block else ""
            m = re.search(r"\nContext (?:before|after):", body)  # the messages around it, when asked for
            body, context = (body[:m.start()], body[m.start():]) if m else (body, "")
            items.append({"id": f"{cid}-{ts}" if cid and ts else link, "ts": ts, "channel": label, "channel_id": cid,
                          "from": _slack_field(block, "From"), "time": _slack_field(block, "Time"),
                          "permalink": link, "thread_ts": thread.group(1) if thread else "",
                          "text": body.strip()[:20000], **({"context": context.strip()[:5000]} if context else {}),
                          **({"channel_type": "im", "is_private": True} if _private_channel(label, cid, block) else {})})
        return items
    msgs = _slack_messages(text)
    if not msgs:
        return None
    cid = args.get("channel_id") or msgs[0]["channel_id"]
    private = {"channel_type": "im", "is_private": True} if _private_channel(msgs[0]["channel"], cid, text) else {}
    if "thread" in tool and args.get("message_ts"):
        first = next((m for m in msgs if m["ts"] == args["message_ts"]), msgs[0])
        ordered = [first] + sorted((m for m in msgs if m is not first), key=lambda m: m["ts"])
        return [{"id": f"{cid}-{args['message_ts']}", "ts": args["message_ts"], "channel": msgs[0]["channel"],
                 "channel_id": cid, "title": _one_line(first["text"]), "from": first["from"], "time": first["time"],
                 "text": first["text"], "reply_count": len(msgs) - 1,
                 "messages": [{k: m[k] for k in ("from", "time", "ts", "text")} for m in ordered], **private}]
    return [dict(m, id=f"{cid}-{m['ts']}", channel_id=cid, **private) for m in msgs if m["ts"]]


def slack_norm(d: dict) -> dict:
    title = d.get("title") or _one_line(d.get("text", ""))
    where = d.get("channel") or ""
    last = d["messages"][-1].get("ts") if isinstance(d.get("messages"), list) and d["messages"] else d.get("ts")
    return dict(title=f"{where}: {title}" if where and title else title or where, created=_iso(d.get("ts")),
                key=d["id"] if "-" in str(d.get("id") or "") else "",  # channel-ts: a ts alone is not unique
                updated=_iso(max(str(m.get("ts") or "") for m in d["messages"]) if d.get("messages") else last))


def slack_follow(d: dict, names: set) -> tuple | None:
    """A message that is part of a thread: read the whole thread. Never in a direct message."""
    ts = d.get("thread_ts") or (d.get("ts") if d.get("reply_count") else "")
    if ts and d.get("channel_id") and "slack_read_thread" in names and not d.get("messages") and not d.get("is_private"):
        return "slack_read_thread", {"channel_id": d["channel_id"], "message_ts": ts, "response_format": "concise"}
    return None


_GH_URL = re.compile(r"https://(?P<host>[^/]+)/(?:repos/)?(?P<repo>[^/]+/[^/]+)/(?:issues|pull|pulls)/(?P<n>\d+)")


def github_norm(d: dict) -> dict:
    url = d.get("html_url") or d.get("url") or ""
    m = _GH_URL.match(url) if isinstance(url, str) else None
    repo = d["repository"].get("nameWithOwner") if isinstance(d.get("repository"), dict) else None
    repo = repo or (m["repo"] if m else "")
    n = d.get("number") or (m["n"] if m else "")
    out = dict(created=_iso(d.get("createdAt") or d.get("created_at")),
               updated=_iso(d.get("updatedAt") or d.get("updated_at")))
    if repo and n:
        out["key"] = f"{repo}#{n}"
        out["title"] = f"{repo}#{n} {d.get('title') or ''}".strip()
    return out


def github_follow(d: dict, names: set) -> tuple | None:
    m = _GH_URL.match(str(d.get("html_url") or d.get("url") or ""))
    if not m or isinstance(d.get("comments"), list) or not (d.get("comments") or d.get("commentsCount")):
        return None
    owner, repo = m["repo"].split("/")
    if "issue_read" in names:
        return "issue_read", {"method": "get_comments", "owner": owner, "repo": repo, "issue_number": int(m["n"])}
    if "get_issue_comments" in names:
        return "get_issue_comments", {"owner": owner, "repo": repo, "issue_number": int(m["n"])}
    return None


def _gh(*args, timeout=30) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[-300:] or f"gh exited {r.returncode}")
    return r.stdout


def gh_ready() -> tuple[bool, str]:
    """Whether the gh command line is on this Mac and signed in."""
    if not shutil.which("gh"):
        return False, "gh is not installed"
    try:
        r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)[:200]
    return r.returncode == 0, "" if r.returncode == 0 else "gh is not signed in: run gh auth login"


GH_FIELDS = "assignees,author,body,closedAt,commentsCount,createdAt,isPullRequest,labels,number,repository,state," \
            "title,updatedAt,url"


def collect_gh(since: str, limit: int = 60, discussions: int = 15) -> list[tuple]:
    """Issues and pull requests that involve the user (author, assignee, mentioned, commenter) and changed since
    `since`, through gh, with the discussion of the most recent ones: issue comments, and reviews of pull requests.
    Only read commands: `gh search` and GET calls of `gh api` (never with -f, -F or -X, which would write)."""
    day = since[:10]
    found = json.loads(_gh("search", "issues", "--involves", "@me", "--updated", f">={day}", "--include-prs",
                           "--sort", "updated", "--limit", str(limit), "--json", GH_FIELDS, timeout=60) or "[]")
    for it in found[:discussions]:
        m = _GH_URL.match(it.get("url", ""))
        if not m or m["host"] != "github.com" or not (it.get("commentsCount") or it.get("isPullRequest")):
            continue
        base, comments = f"repos/{m['repo']}", []
        try:
            if it.get("commentsCount"):
                comments += [dict(author=c.get("user"), created=c.get("created_at"), body=(c.get("body") or "")[:4000],
                                  url=c.get("html_url"))
                             for c in json.loads(_gh("api", f"{base}/issues/{m['n']}/comments?per_page=50") or "[]")]
            if it.get("isPullRequest"):
                comments += [dict(author=c.get("user"), created=c.get("submitted_at"), state=c.get("state"),
                                  body=(c.get("body") or "")[:4000], url=c.get("html_url"))
                             for c in json.loads(_gh("api", f"{base}/pulls/{m['n']}/reviews?per_page=50") or "[]")
                             if c.get("body") or c.get("state") in ("APPROVED", "CHANGES_REQUESTED")]
        except (RuntimeError, ValueError, subprocess.TimeoutExpired):
            continue
        if comments:
            it["comments"] = sorted(comments, key=lambda c: c.get("created") or "")
    return [("gh search issues", {"involves": "@me", "updated": f">={day}"}, json.dumps(found))]


def linear_norm(d: dict) -> dict:
    return dict(title=f"{d['identifier']} {d.get('title') or ''}".strip() if d.get("identifier") else "",
                created=_iso(d.get("createdAt")), updated=_iso(d.get("updatedAt")))


def linear_follow(d: dict, names: set) -> tuple | None:
    ident = d.get("identifier")
    if ident and "get_issue" in names and not d.get("description"):
        return "get_issue", {"id": ident}
    return None


def notion_norm(d: dict) -> dict:
    title = d.get("title")
    props = d.get("properties") if isinstance(d.get("properties"), dict) else {}
    if not isinstance(title, str):
        for v in props.values():
            if isinstance(v, dict) and v.get("type") == "title":
                title = "".join(t.get("plain_text", "") for t in v.get("title") or [] if isinstance(t, dict))
    return dict(title=title if isinstance(title, str) else "",
                created=_iso(d.get("created_time") or d.get("createdTime") or d.get("timestamp")),
                updated=_iso(d.get("last_edited_time") or d.get("lastEditedTime") or d.get("timestamp")))


def notion_follow(d: dict, names: set) -> tuple | None:
    if d.get("text") or d.get("content") or d.get("object") == "page":
        return None
    ref = d.get("url") or d.get("id")
    if ref and "notion-fetch" in names:
        return "notion-fetch", {"id": ref}
    return None


def hubspot_norm(d: dict) -> dict:
    props = d.get("properties") if isinstance(d.get("properties"), dict) else {}
    p = {**{k: v for k, v in props.items() if isinstance(v, (str, int, float))},
         **{k: v for k, v in d.items() if isinstance(v, (str, int, float))}}
    name = p.get("dealname") or p.get("subject") or p.get("hs_ticket_subject") or p.get("name") or \
        " ".join(x for x in (p.get("firstname"), p.get("lastname")) if isinstance(x, str)).strip() or p.get("email")
    kind = d.get("objectType") or d.get("type") or ""
    return dict(title=f"{kind} {name}".strip() if name else "",
                key=f"{kind}-{d['id']}" if kind and d.get("id") else "",
                created=_iso(p.get("createdate") or d.get("createdAt")),
                updated=_iso(p.get("hs_lastmodifieddate") or p.get("lastmodifieddate") or d.get("updatedAt")))


def granola_norm(d: dict) -> dict:
    return dict(title=d.get("title") or d.get("name") or "",
                created=_iso(d.get("created_at") or d.get("createdAt") or d.get("date") or d.get("start")),
                updated=_iso(d.get("updated_at") or d.get("updatedAt")))


def drive_norm(d: dict) -> dict:
    return dict(title=d.get("name") or d.get("title") or "", created=_iso(d.get("createdTime")),
                updated=_iso(d.get("modifiedTime")))


# recipe: the calls made in order when the server has the tool. Their arguments may hold {day} (YYYY-MM-DD), {iso}
# (ISO 8601), {unix} (seconds) and {ms} (milliseconds) of the time the collection starts from.
# add: how a user adds the service to Pensieve's own servers. oauth: sign in through the browser (the server has
# dynamic client registration); header: a token sent in a header; env: a token in a local server's environment.
PROFILES = {
    "notion": dict(
        label="Notion", match=r"notion",
        reads={"notion-search", "notion-fetch", "notion-get-users", "notion-get-comments", "notion-get-self",
               "notion-get-teams", "API-post-search", "API-post-database-query", "API-retrieve-a-page",
               "API-get-block-children", "API-retrieve-a-comment", "API-get-users", "API-get-self"},
        recipe=[("API-post-search", {"sort": {"direction": "descending", "timestamp": "last_edited_time"},
                                     "page_size": 50})],
        hint="Use notion-search for the pages the user created or edited since {day}, then notion-fetch for each "
             "page to get its content.",
        norm=notion_norm, follow=notion_follow,
        add=[dict(kind="oauth", label="Sign in to Notion", url="https://mcp.notion.com/mcp",
                  help="Notion's own MCP server. You sign in with your Notion account in the browser."),
             dict(kind="env", label="Notion integration token", command="npx",
                  args=["-y", "@notionhq/notion-mcp-server"], env="NOTION_TOKEN", placeholder="ntn_…",
                  help="A local server with an internal integration token. It sees only the pages you share with "
                       "the integration.", docs="https://www.notion.so/profile/integrations")]),
    "slack": dict(
        label="Slack", match=r"slack", chat=True,
        reads={"slack_search_public", "slack_search_public_and_private", "slack_read_channel", "slack_read_thread",
               "slack_read_canvas", "slack_read_user_profile", "slack_search_channels", "slack_search_users",
               "slack_list_channels", "slack_get_channel_history", "slack_get_thread_replies", "slack_get_users",
               "slack_get_user_profile"},
        recipe=[("slack_search_public", {"filters": "from:me after:{day}", "sort": "timestamp", "limit": 20,
                                         "include_context": False, "natural_language_query": ""})],
        hint="Use slack_search_public with filters like 'from:me after:{day}' and sort 'timestamp', then "
             "slack_read_thread for each thread that one of the messages is in. Do not read direct messages.",
        parse=slack_parse, norm=slack_norm, follow=slack_follow,
        add=[dict(kind="env", label="Slack token", command="npx", args=["-y", "@modelcontextprotocol/server-slack"],
                  env="SLACK_BOT_TOKEN", env_more="SLACK_TEAM_ID", placeholder="xoxp-…",
                  help="Slack's own MCP server (mcp.slack.com) has no open sign-up for apps, so Pensieve can't sign "
                       "in to it yet. Use the Slack connector of Claude Code, or a token of a Slack app that you "
                       "create, with channels:history, channels:read and users:read.",
                  docs="https://api.slack.com/apps")]),
    "drive": dict(
        label="Google Drive", match=r"google.?drive|gdrive",
        reads={"search", "google_drive_search", "google_drive_fetch", "read_file", "get_file", "list_files",
               "search_files", "read_file_content", "get_file_metadata"},
        recipe=[],
        hint="Search for the files that the user owns or edited since {day}, then read the most useful ones.",
        norm=drive_norm,
        add=[dict(kind="folder", label="Google Drive for desktop folder",
                  help="Google has no remote MCP server for Drive that Pensieve can sign in to. Use the Google Drive "
                       "connector of Claude Code, or add your Drive for desktop folder in Files & folders. Google "
                       "Docs there are only links; Word, PDF and text files are read.",
                  docs="https://www.google.com/drive/download/")]),
    "hubspot": dict(
        label="HubSpot", match=r"hubspot",
        reads={"hubspot-search-objects", "hubspot-list-objects", "hubspot-batch-read-objects",
               "hubspot-get-user-details", "hubspot-list-properties", "hubspot-get-property",
               "hubspot-list-associations", "hubspot-get-engagement", "hubspot-get-schemas"},
        recipe=[("hubspot-search-objects", {"objectType": t, "limit": 50,
                                            "filterGroups": [{"filters": [{"propertyName": "hs_lastmodifieddate",
                                                                           "operator": "GTE", "value": "{ms}"}]}]})
                for t in ("deals", "tickets", "companies", "contacts")],
        hint="Search the deals, tickets, companies and contacts that changed since {day} and that the user owns.",
        norm=hubspot_norm,
        add=[dict(kind="env", label="HubSpot private app token", command="npx", args=["-y", "@hubspot/mcp-server"],
                  env="PRIVATE_APP_ACCESS_TOKEN", placeholder="pat-…",
                  help="HubSpot's remote MCP server needs an app registered in HubSpot, so Pensieve can't sign in to "
                       "it yet. Use a private app token with read scopes for CRM objects.",
                  docs="https://developers.hubspot.com/docs/apps/legacy-apps/private-apps/overview")]),
    "github": dict(
        label="GitHub", match=r"github",
        reads={"search_issues", "search_pull_requests", "list_issues", "list_pull_requests", "get_issue",
               "get_pull_request", "get_issue_comments", "get_pull_request_comments", "get_pull_request_reviews",
               "issue_read", "pull_request_read", "get_me"},
        recipe=[("search_issues", {"query": "involves:@me updated:>={day}", "sort": "updated", "perPage": 50}),
                ("search_pull_requests", {"query": "involves:@me updated:>={day}", "sort": "updated", "perPage": 50})],
        hint="Search issues and pull requests with 'involves:@me updated:>={day}', then read the comments of the "
             "most recent ones.",
        norm=github_norm, follow=github_follow,
        add=[dict(kind="gh", label="The gh command line",
                  help="Pensieve reads the issues and pull requests that involve you, with their discussions, "
                       "through gh. Run gh auth login once in a terminal."),
             dict(kind="header", label="GitHub token", url="https://api.githubcopilot.com/mcp/",
                  header="Authorization", prefix="Bearer ", extra={"X-MCP-Readonly": "true"},
                  placeholder="github_pat_…",
                  help="GitHub's remote MCP server, in read-only mode, with a fine-grained token that can read "
                       "issues and pull requests.", docs="https://github.com/settings/personal-access-tokens")]),
    "granola": dict(
        label="Granola", match=r"granola",
        reads={"list_meetings", "get_meetings", "get_meeting_transcript", "query_granola_meetings", "search_meetings",
               "get_meeting"},
        recipe=[],
        hint="List the meetings since {day}, then get the notes of each one with its attendees.",
        norm=granola_norm,
        add=[dict(kind="oauth", label="Sign in to Granola", url="https://mcp.granola.ai/mcp",
                  help="Granola's own MCP server. You sign in with your Granola account in the browser.")]),
    "linear": dict(
        label="Linear", match=r"linear",
        reads={"list_issues", "list_my_issues", "get_issue", "list_comments", "list_projects", "get_project",
               "list_teams", "get_team", "list_users", "get_user", "list_documents", "get_document",
               "list_issue_statuses", "list_cycles"},
        recipe=[("list_issues", {"assignee": "me", "updatedAt": "{iso}", "limit": 50}),
                ("list_issues", {"createdBy": "me", "updatedAt": "{iso}", "limit": 50})],
        hint="List the issues assigned to or created by the user that changed since {iso}, then get each issue "
             "with its comments.",
        norm=linear_norm, follow=linear_follow,
        add=[dict(kind="oauth", label="Sign in to Linear", url="https://mcp.linear.app/mcp", scope="read",
                  help="Linear's own MCP server, with read access only. You sign in in the browser."),
             dict(kind="header", label="Linear API key", url="https://mcp.linear.app/mcp", header="Authorization",
                  prefix="Bearer ", placeholder="lin_api_…",
                  help="Linear's own MCP server with a personal API key.",
                  docs="https://linear.app/settings/account/security")]),
}
ORDER = list(PROFILES)


def match(*names: str) -> str | None:
    """The profile id for a server, by its name (or the name of one of its tools)."""
    text = " ".join(n for n in names if n)
    return next((pid for pid in ORDER if re.search(PROFILES[pid]["match"], text, re.I)), None)


def known_read(pid: str | None, tool: str) -> bool:
    return bool(pid) and tool in PROFILES[pid]["reads"]


def fill(args, when: datetime):
    """A recipe's arguments with the start time put in."""
    if isinstance(args, dict):
        return {k: fill(v, when) for k, v in args.items()}
    if isinstance(args, list):
        return [fill(v, when) for v in args]
    if isinstance(args, str):
        vals = dict(day=when.strftime("%Y-%m-%d"), iso=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    unix=str(int(when.timestamp())), ms=str(int(when.timestamp() * 1000)))
        return re.sub(r"\{(day|iso|unix|ms)\}", lambda m: vals[m.group(1)], args)
    return args


def hint(pid: str | None, when: datetime) -> str:
    return fill(PROFILES[pid]["hint"], when) if pid else ""


def parse(pid: str | None, tool: str, args: dict, data) -> list[dict] | None:
    fn = PROFILES[pid].get("parse") if pid else None
    return fn(tool.rsplit(".", 1)[-1], args or {}, data) if fn else None


def follow(pid: str | None, item: dict, names: set) -> tuple | None:
    fn = PROFILES[pid].get("follow") if pid else None
    return fn(item, names) if fn else None


def normalize(pid: str | None, d: dict) -> dict:
    """What Pensieve keeps of an item besides its own fields: `_title`, `_created`, `_updated`, `_people`, and
    `_key` when the item's own id is not unique (a GitHub number is unique only in its repo)."""
    n = PROFILES[pid]["norm"](d) if pid and PROFILES[pid].get("norm") else {}
    out = {"_people": people(d, pid or "mcp")}
    if pid:
        out["_connector"] = pid
    for k in ("title", "created", "updated", "key"):
        if n.get(k):
            out[f"_{k}"] = str(n[k])
    for k, fields in (("_created", ("created_at", "createdAt", "created", "createdTime", "created_time")),
                      ("_updated", ("updated_at", "updatedAt", "updated", "modifiedTime", "last_edited_time"))):
        if k not in out:
            t = _iso(next((d[f] for f in fields if d.get(f)), None))
            if t:
                out[k] = t
    return out


def drive_folders() -> list[Path]:
    """Google Drive for desktop's folders on this Mac."""
    try:
        return sorted(p for p in (Path.home() / "Library" / "CloudStorage").iterdir() if p.name.startswith("GoogleDrive-"))
    except OSError:
        return []
