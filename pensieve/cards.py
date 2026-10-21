"""Threads as files, in the pensieve-thread/2 format (thread.schema.json).

A local thread and a shared thread have the same core: the problem, the goal, the state and what is next. They
differ in their supporting context, the resources (pensieve/resources.py). A local thread points to everything,
paths included. A shared thread keeps what its reader can open (PRs, the branch, tickets, messages, public pages) and
replaces what they can't (transcripts, memory files, uncommitted changes, private artifacts) with summaries that
were written from the whole document when the thread was shared.

Each thread has two renderings: `render_html` for people (one page with no outside files that also holds the JSON),
and `render_agent` for agents (the data as compact JSON). Agents in a Pensieve session get the text view
(work.compact) from the MCP tools and the prompt hook. Pages in the older context-card/1 format still read."""
import html
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from pensieve import cardtext, resources as res
from pensieve import task_states, work

SCHEMA = "pensieve-thread/2"
PAGE_ID = "pensieve-thread"  # the id of the <script> that holds the JSON; pages before version 2 used "context-card"
_SECRET = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----|"
    r"(?i:(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*)['\"]?[^\s'\"]{6,})")
_LOCAL_PATH = re.compile(r"(?:/Users/|/home/|~/)[^\s`'\")\]>,;]+|\.claude/worktrees/[^\s`'\")\]>,;]*")
# a sentence about the owner's own machine (a file to delete on the Desktop, a memory note): kept on the local card,
# dropped from the shared one
_OWN_MACHINE = re.compile(r"\b(Desktop|Downloads|Documents folder|MEMORY\.md|memory files?|on (this|my|your|the) "
                          r"(Mac|machine|laptop)|local (file|folder)s?)\b", re.I)
SUMMARIES = 6  # local and restricted resources that get a summary when a card is shared, the most useful first


def _iso(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else ""


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return f"pensieve {version('pensieve')}"
    except PackageNotFoundError:
        return "pensieve"


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _num(url: str) -> int:
    m = re.search(r"/(?:pull|merge_requests)/(\d+)", url)
    return int(m.group(1)) if m else 0


def build(t: dict, note: dict, hist: list[dict]) -> dict:
    """The local context-card/1 object of a task. Its core has the same validation as the text card: sentences
    that git or GitHub contradict are marked, and the last session's own question wins over the model's."""
    core, changed = work.reconcile(note, t) if note else ({}, [])
    last = max((s["updated"] for s in t.get("sessions", [])), default=0)
    since = [f"PR #{_num(p['url'])} merged {_iso(p['merged_at'])[:10]}" for p in t["prs"] + t.get("branch_prs", [])
             if last and (p.get("merged_at") or 0) > last]
    since += [c for c in changed if not c.startswith("PR #")]
    asked = work.asked_question(t)
    if asked:
        core["question"] = asked
    if core:
        core["as_of"] = _iso(last)
    root = next((p["path"] for p in t.get("places", []) if p.get("git")), "")
    card = dict(
        schema=SCHEMA, id=t["id"], title=cardtext.card_title(t, note), label=t["title"],
        kind=t.get("kind", "work"), locality="local",
        state=dict(value=t["state"], why=t.get("state_why", ""), set_by=t.get("state_by") or "auto"),
        made_at=_iso(datetime.now().timestamp()), last_activity=_iso(t["updated"]), last_session=_iso(last),
        origin=_clean(dict(by=work._git(root, "config", "user.name", ttl=3600) if root else "", generator=_version())),
        core=core, since_last_session=since, outdated=changed,
        left=[dict(step=x["text"], who=x["how"], ref=x.get("ref") or None) for x in t.get("left") or []],
        resources=[r.to_dict() for r in res.collect(t)],
        last_words=work._clean_words(t.get("last_words") or ""),
        timeline=[_clean(dict(at=_iso(e["ts"]), source=e["source"], title=e["title"], private=e.get("private") or None))
                  for e in t["events"][:20]],
        history=[dict(at=_iso(h["at"]), state=h["state"], by=h["by"], reason=h["reason"]) for h in hist[-10:]],
        local=_clean(dict(resume=(t.get("resume") or {}).get("command", ""))))
    card["handoff"] = handoff(card)
    return _clean(card)


def handoff(card: dict) -> dict:
    """How someone on another machine picks the work up: the next step, the commands that get the work, and the
    commands that get its live state again."""
    rs = card.get("resources", [])
    branch = next((r for r in rs if r["kind"] == "branch"), None)
    change = next((r for r in rs if r["kind"] == "change"), None)
    open_pr = next((r for r in rs if r["kind"] == "pr" and r.get("ref", {}).get("state") in ("open", "draft")), None)
    by = (card.get("origin") or {}).get("by") or "its author"
    start, refresh = [], []
    if open_pr:
        start.append(f"gh pr checkout {open_pr['ref']['url']}")
    elif branch:
        b = branch["ref"]
        start.append(f"git fetch {b['git']} {b['branch']} && git switch {b['branch']}")
    if change:  # uncommitted files or unpushed commits: only on the owner's machine
        start.append(f"# {change['title']}: only on {by}'s machine; ask {by} to commit and push them")
    elif change is None and not branch and any(r["kind"] == "folder" for r in rs) and card.get("kind") == "branch":
        start.append(f"# the branch is not on the remote: ask {by} to push it")
    if branch:
        refresh.append(f"git ls-remote {branch['ref']['git']} refs/heads/{branch['ref']['branch']}")
    refresh += [f"gh pr view {r['ref']['url']} --json state,statusCheckRollup,reviewDecision" for r in rs
                if r["kind"] == "pr" and r.get("ref", {}).get("state") in ("open", "draft", "")][:3]
    refresh += [f"{r['ref'].get('source') or 'Tracker'}: {r['ref']['url']}" for r in rs
                if r["kind"] in ("ticket", "message", "page") and r.get("ref", {}).get("url")][:4]
    core = card.get("core", {})
    left = card.get("left") or []
    return _clean(dict(next=core.get("question") or core.get("next") or (left[0]["step"] if left else None),
                       start=start, refresh=refresh))


def draft_share(card: dict, store, include: list[int] | None = None) -> dict:
    """The shared card to preview: the local card's resources (those in `include`, by position; default all), and
    a summary of each local or restricted one, written from the whole document now. Nothing leaves the Mac; `share`
    then makes the card fit to send."""
    rs = [dict(r) for i, r in enumerate(card.get("resources", [])) if include is None or i in include]
    todo = sorted((r for r in rs if r["reach"] in (res.LOCAL, res.RESTRICTED) and r["kind"] != "folder"),
                  key=lambda r: -r.get("relevance", 0))[:SUMMARIES]

    def one(r):
        obj = res.Resource(r["kind"], r["title"], r.get("ref", {}), relevance=r.get("relevance", 0.5), reach=r["reach"])
        r["summary"] = res.summarize(obj, res.read_full(obj, store), card)
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(one, todo))
    return share(dict(card, resources=rs))


def share(card: dict, ask: str = "") -> dict:
    """The card to send. No `local` part and no local path anywhere: a local resource keeps its summary and loses its
    path, or is left out when it has no summary. A private resource (a direct or group message, a private channel)
    and its timeline points are left out whole; another message keeps its link and loses its text (its title is the
    message's first line) and any summary. Secrets become [secret], a branch card's id becomes the same on
    every machine (b:org/repo@branch), and `ask` (what the sender needs from the reader) leads the card."""
    c = json.loads(json.dumps(card))
    c.pop("local", None)
    c.pop("last_words", None)  # a transcript's words: the session's summary says what matters
    c["core"] = {k: v if k == "as_of" else " ".join(s for s in re.split(r"(?<=[.;!?])\s+", v) if not _OWN_MACHINE.search(s))
                 for k, v in c.get("core", {}).items()}
    c["timeline"] = [dict(x, title="A message") if x.get("source") == "message" else x
                     for x in c.get("timeline", []) if not x.get("private")]
    kept = []
    for r in c.get("resources", []):
        if r["reach"] == res.PRIVATE:  # a direct or group message, or a private channel: not even its link
            continue
        if r["kind"] == "message":  # a message's title is its own text: the reader gets the link, not the words
            src = (r.get("ref") or {}).get("source") or ""
            r["title"] = f"{src} message" if src else "A message"
            r.pop("summary", None)
        if r["reach"] == res.LOCAL:
            if not r.get("summary") or r["kind"] == "folder":
                continue
            r.pop("ref", None)
        elif r.get("ref"):
            r["ref"] = {k: v for k, v in r["ref"].items() if k not in ("path", "session", "root")}
        kept.append(r)
    c["resources"] = kept
    if c["id"].startswith("b:"):
        repo = next((re.sub(r"^https?://[^/]+/", "", r["ref"]["web"]).split("/tree/")[0] for r in kept
                     if r["kind"] == "branch" and r.get("ref", {}).get("web")), "")
        c["id"] = f"b:{repo or c['id'][2:].rsplit('@', 1)[0].rsplit('/', 1)[-1]}@{c['id'].rsplit('@', 1)[-1]}"
    if ask.strip():
        c["ask"] = ask.strip()
    c["handoff"] = handoff(c)

    def scrub(x):
        if isinstance(x, str):
            return _SECRET.sub("[secret]", _LOCAL_PATH.sub("[local path]", x))
        if isinstance(x, list):
            return [scrub(v) for v in x]
        if isinstance(x, dict):
            return {k: scrub(v) for k, v in x.items()}
        return x
    c = scrub(c)
    c["locality"] = "shared"
    return _clean(c)


def local_parts(card: dict) -> list[str]:
    """Why a card is not fit to share: its `local` part, a local resource with a path, and each field that still holds a
    local path. [] when it is fit. A shared space refuses a card that this finds anything in."""
    found = ["local"] if "local" in card else []
    found += [f"resources[{i}]" for i, r in enumerate(card.get("resources", []))
              if r.get("reach") == res.PRIVATE or (r.get("reach") == res.LOCAL and r.get("ref"))]
    found += [f"timeline[{i}]" for i, x in enumerate(card.get("timeline", [])) if x.get("private")]

    def walk(x, at):
        if isinstance(x, str) and _LOCAL_PATH.search(x):
            found.append(at)
        elif isinstance(x, list):
            for i, v in enumerate(x):
                walk(v, f"{at}[{i}]")
        elif isinstance(x, dict):
            for k, v in x.items():
                walk(v, f"{at}.{k}" if at else k)
    walk({k: v for k, v in card.items() if k != "local"}, "")
    return found


def render_agent(card: dict) -> str:
    """The card for an agent: its data as compact JSON, newest timeline first and cut short, with a line on how to
    read it. Agents follow ids, URLs and commands; they don't need layout."""
    c = dict(card, timeline=card.get("timeline", [])[:5], history=card.get("history", [])[-3:])
    c["resources"] = [{k: v for k, v in r.items() if k != "relevance"} for r in card.get("resources", [])]
    head = ("Pensieve thread (pensieve-thread/2). 'core' (problem, goal, state, next, question) is a model's note as of "
            "core.as_of; a sentence marked [outdated: …] is wrong now. Each resource has a reach: git, mcp and public "
            "ones can be opened with their ref; local and restricted ones are summarized. 'handoff' says how to start "
            "and how to get the live state. "
            + ("'local' works only on this machine." if card.get("locality") == "local" else "Shared: start with 'ask' and handoff.start."))
    return head + "\n" + json.dumps(_clean(c), ensure_ascii=False, separators=(",", ":"))


STATE_LABEL = {"open": "Open", "loose_ends": "Loose ends", "closed": "Closed", "forgotten": "Forgotten"}
NOTE_LABEL = {"session": "Session", "memory": "Memory", "change": "Changes", "file": "File", "artifact": "Artifact",
              "message": "Message", "page": "Page", "ticket": "Ticket"}
REACH_LABEL = {res.LOCAL: "summary", res.GIT: "git", res.MCP: "via MCP", res.RESTRICTED: "may need access", res.PUBLIC: "public",
               res.PRIVATE: "private, never shared"}
CSS = """
:root{--bg:#f5f5f7;--card:#fff;--ink:#1d1d1f;--ink-2:#6e6e73;--ink-3:#86868b;--line:rgba(0,0,0,.08);--link:#0066cc;
--good:#1f7a43;--warn:#a34700;--bad:#c4281c;--chip:rgba(0,0,0,.05);--ask:#fff7e6;--ask-line:#f2c46d;--merged:#8250df;
--req:#eef4ff;--req-line:#9ec1ff}
@media (prefers-color-scheme:dark){:root{--bg:#000;--card:#1c1c1e;--ink:#f5f5f7;--ink-2:#a1a1a6;--ink-3:#86868b;
--line:rgba(255,255,255,.12);--link:#2997ff;--good:#30d158;--warn:#ff9f0a;--bad:#ff453a;--chip:rgba(255,255,255,.08);
--ask:#2b2210;--ask-line:#8a6418;--merged:#bf5af2;--req:#0e1d33;--req-line:#2f5a99}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",
"Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased;padding:32px 16px}
main{max-width:760px;margin:0 auto;background:var(--card);border:1px solid var(--line);border-radius:18px;padding:28px 28px 20px}
h1{font:600 26px/1.2 -apple-system,BlinkMacSystemFont,"SF Pro Display",sans-serif;margin:6px 0 4px;letter-spacing:-.01em}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--ink-3);margin:26px 0 8px;font-weight:600}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
code,pre{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:13px}
.meta{color:var(--ink-2);font-size:13px}.meta span+span:before{content:"·";margin:0 6px;color:var(--ink-3)}
.pill{display:inline-block;font-size:12px;font-weight:600;padding:3px 10px;border-radius:99px;background:var(--chip);margin-right:6px}
.s-open{color:var(--link)}.s-loose_ends{color:var(--warn)}.s-complete{color:var(--good)}.s-forgotten,.s-todo{color:var(--ink-2)}
.box{margin-top:18px;border-radius:12px;padding:12px 14px}.box b{display:block;font-size:12px;text-transform:uppercase;
letter-spacing:.06em;margin-bottom:2px}.ask{background:var(--ask);border:1px solid var(--ask-line)}
.req{background:var(--req);border:1px solid var(--req-line)}
dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 14px;margin:0}dt{color:var(--ink-3)}dd{margin:0}
.note{font-size:12px;color:var(--warn);white-space:nowrap}
ul{margin:0;padding-left:18px}li{margin:2px 0}
.rows{list-style:none;padding:0}.rows li{display:flex;gap:10px;align-items:baseline;padding:7px 0;border-top:1px solid var(--line)}
.rows li:first-child{border-top:0}.grow{flex:1;min-width:0;overflow-wrap:anywhere}.grow p{margin:3px 0 0;color:var(--ink-2);font-size:14px}
.badge{font-size:12px;font-weight:600;white-space:nowrap;min-width:64px}.b-merged{color:var(--merged)}.b-open,.b-draft{color:var(--good)}
.b-closed{color:var(--bad)}.b-failing{color:var(--bad)}.b-passing{color:var(--good)}.b-pending{color:var(--warn)}
.reach{font-size:11px;color:var(--ink-3);white-space:nowrap}
.tl{list-style:none;padding:0;border-left:2px solid var(--line);margin-left:4px}.tl li{padding:2px 0 6px 14px;position:relative}
.tl li:before{content:"";position:absolute;left:-6px;top:9px;width:10px;height:10px;border-radius:50%;background:var(--card);
border:2px solid var(--ink-3)}.when{color:var(--ink-3);font-size:13px;margin-right:6px;font-variant-numeric:tabular-nums}
blockquote{margin:0;padding:10px 14px;border-left:3px solid var(--line);color:var(--ink-2);white-space:pre-wrap;font-size:14px}
.cmd{display:flex;gap:8px;align-items:center;background:var(--chip);border-radius:10px;padding:8px 10px;margin:4px 0}
.cmd code{flex:1;overflow-wrap:anywhere}button{font:inherit;font-size:12px;border:1px solid var(--line);background:var(--card);
color:var(--ink);border-radius:8px;padding:3px 9px;cursor:pointer}
.local{border:1px dashed var(--line);border-radius:12px;padding:4px 14px 12px;margin-top:22px}.local h2{margin-top:12px}
footer{margin-top:26px;padding-top:12px;border-top:1px solid var(--line);font-size:12px;color:var(--ink-3)}
@media (max-width:560px){main{padding:20px 16px}dl{grid-template-columns:1fr}dt{margin-top:6px}}
"""


def _marked(text: str) -> str:
    """Note text with its [outdated: …] marks shown as what is true now. The sentence stays readable: often only
    part of it is outdated ("push, deploy, rerun" after the push)."""
    return re.sub(r"\s*\[outdated: ([^\]]*)\]", lambda m: f' <span class="note">now: {m.group(1)}</span>', html.escape(text))


def _md(text: str) -> str:
    """The Markdown that agents write most (bold, inline code), as HTML; everything else stays plain text."""
    t = html.escape(text)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    return re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", t)


def _cmd(text: str, i: int) -> str:
    return (f'<div class="cmd"><code id="c{i}">{html.escape(text)}</code><button onclick="navigator.clipboard.writeText('
            f"document.getElementById('c{i}').textContent)\">Copy</button></div>")


def _url(r: dict) -> str:
    ref = r.get("ref", {})
    return ref.get("url") or ref.get("web") or ""


def render_html(card: dict) -> str:
    """The thread for people: one page with no outside files, light and dark, that leads with the ask and the core.
    The page holds the thread's JSON in <script type="application/json" id="pensieve-thread">, so `from_html` and
    any tool can read it back."""
    e = lambda s: html.escape(str(s or ""))
    day = lambda s: (s or "")[:10]
    st, core, rs = card.get("state", {}), card.get("core", {}), card.get("resources", [])
    sv = task_states.RENAMED.get(st.get("value"), st.get("value"))  # a page saved before the states were renamed
    shared = card.get("locality") == "shared"
    by = (card.get("origin") or {}).get("by")
    out = [f'<span class="pill s-{e(sv)}">{e(STATE_LABEL.get(sv, sv))}</span>'
           + ('<span class="pill">Shared</span>' if shared else ""),
           f"<h1>{e(card['title'])}</h1>",
           '<div class="meta">' + "".join(f"<span>{x}</span>" for x in [
               e(st.get("why")), f"last session {e(day(card.get('last_session')))}" if card.get("last_session") else "",
               e(by), ", ".join(e(r["ref"].get("key")) for r in rs if r["kind"] == "ticket" and r.get("ref", {}).get("key"))] if x)
           + "</div>"]
    if card.get("ask"):
        out.append(f'<div class="box req"><b>{e(by or "The sender")} asks you</b>{e(card["ask"])}</div>')
    if core.get("question"):
        out.append(f'<div class="box ask"><b>Waiting on {e(by) if shared and by else "you"}</b>{e(core["question"])}</div>')
    rows = [f"<dt>{label}</dt><dd>{_marked(core[k])}</dd>" for k, label in work.CORE_LABELS[:4] if core.get(k)]
    if rows:
        out += [f"<h2>The work · as of {e(day(core.get('as_of')))}</h2>", f"<dl>{''.join(rows)}</dl>"]
    if card.get("since_last_session"):
        out += ["<h2>Since then</h2>", "<ul>" + "".join(f"<li>{e(x)}</li>" for x in card["since_last_session"]) + "</ul>"]
    h, loc = card.get("handoff", {}), card.get("local", {})
    cmds = ([loc["resume"]] if loc.get("resume") else []) + h.get("start", [])
    if cmds:
        out += ["<h2>Pick it up</h2>"] + [_cmd(c, i) for i, c in enumerate(cmds)]
    linked = [r for r in rs if r["reach"] != res.LOCAL and _url(r)]
    if linked:
        out += ["<h2>Links</h2>", '<ul class="rows">' + "".join(_link_row(r, e) for r in linked) + "</ul>"]
    notes = [r for r in rs if r.get("summary") and (r["reach"] == res.LOCAL or not _url(r))]
    if notes:
        out += ["<h2>From " + ("the owner's notes" if shared else "your sessions and files") + "</h2>",
                '<ul class="rows">' + "".join(
                    f'<li><span class="badge">{e(NOTE_LABEL.get(r["kind"], r["kind"]))}</span><span class="grow">'
                    f'{e(r["title"])}<p>{e(r["summary"])}</p></span></li>' for r in notes) + "</ul>"]
    if card.get("last_words"):
        out += ["<h2>Where the last session stopped</h2>", f"<blockquote>{_md(card['last_words'])}</blockquote>"]
    if card.get("timeline"):
        out += ["<h2>Timeline</h2>", '<ul class="tl">' + "".join(
            f'<li><span class="when">{e(day(x["at"]))}</span>{e(x["source"])}: {e(x["title"])}</li>' for x in card["timeline"][:12]) + "</ul>"]
    mine = [r for r in rs if r["reach"] == res.LOCAL and r.get("ref", {}).get("path")]
    if not shared and mine:
        out += ['<section class="local"><h2>On this Mac only</h2><ul class="rows">' + "".join(
            f'<li><span class="badge">{e(r["kind"])}</span><span class="grow">{e(r["title"])}<p><code>{e(r["ref"]["path"])}</code></p></span></li>'
            for r in mine) + "</ul></section>"]
    th = card.get("thread") or {}
    foot = (f"Pensieve thread · {e(card.get('schema'))} · made {e(day(card.get('made_at')))} by "
            f"{e((card.get('origin') or {}).get('generator'))}"
            + (f' · <a href="{e(th["url"])}">thread</a> revision {e(th.get("revision"))}' if th.get("url") else "")
            + " · this page holds the thread as JSON")
    data = json.dumps(card, ensure_ascii=False).replace("<", "\\u003c")
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            '<meta name=viewport content="width=device-width,initial-scale=1">'
            f"<title>{e(card['title'])}</title><style>{CSS}</style></head><body><main>"
            + "".join(out) + f"<footer>{foot}</footer></main>"
            f'<script type="application/json" id="{PAGE_ID}">{data}</script></body></html>')


def _link_row(r: dict, e) -> str:
    ref = r.get("ref", {})
    state = ref.get("state") or ref.get("status") or ""
    label = {"pr": state or "PR", "branch": "branch", "code": "file", "ticket": state or "ticket",
             "message": ref.get("source") or "message", "page": ref.get("source") or "page", "artifact": "artifact",
             "link": "link"}.get(r["kind"], r["kind"])
    return (f'<li><span class="badge b-{e(state)}">{e(label)}</span><span class="grow"><a href="{e(_url(r))}">{e(r["title"])}</a>'
            + (f"<p>{e(r['summary'])}</p>" if r.get("summary") else "") + f'</span><span class="reach">{e(REACH_LABEL[r["reach"]])}</span></li>')


REVIVE_PROMPT = ("Pick up this thread: \"{title}\" (Pensieve thread {id}). Read it first with Pensieve's `thread` tool "
                 "(id {id}): it gives the thread as it is now, with what is left to close it. If you do not have "
                 "Pensieve's tools, read {copy}. Then check the live state of its branch, pull requests and tickets, "
                 "and review what has been done. What is left says who can do each step: the agent's steps are yours, "
                 "the user's steps are mine, and the rest happen in the system the item lives in (close a ticket in "
                 "its tracker, for example). Tell me where the work stands and how you will close the remaining "
                 "items with your own tools, and ask me before you change anything, here or in another system.")
REVIVE_KEEP_DAYS = 7  # copies for agents without Pensieve's tools are deleted after this


def revive_prompt(card: dict) -> str:
    """REVIVE_PROMPT with the card in it, not in a file: for any agent, also one that can't read this Mac's files."""
    return REVIVE_PROMPT.format(title=card["title"], id=card["id"], copy="its copy below") + "\n\n" + render_agent(card)


def revive_command(card: dict, folder, agent: str = "") -> str:
    """The shell command that starts a new agent session on a thread: the agent reads the live thread with Pensieve's
    `task` tool, or a copy in a file when it has no Pensieve tools, and starts in the work's folder. `agent`: the
    agent to start (agents.AGENTS); by default the user's default agent, else one that worked on the thread (this Mac's checkout, or the home folder when it is
    gone; the card's handoff.start gets the work then). The agent is the one of the card's last session."""
    import shlex
    from pathlib import Path
    import time
    folder = Path(folder).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.md"):  # the copies are for one start each: old ones only fill the folder
        if time.time() - old.stat().st_mtime > REVIVE_KEEP_DAYS * 86400:
            old.unlink(missing_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", card["title"].lower()).strip("-")[:50] or "thread"
    path = folder / f"{slug}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(render_agent(card))
    where = next((r["ref"]["path"] for r in card.get("resources", []) if r["kind"] == "folder"
                  and Path(r.get("ref", {}).get("path", "")).is_dir()), str(Path.home()))
    from pensieve import agents, settings
    worked = [r.get("ref", {}).get("session", "").split(":", 1)[0] for r in card.get("resources", []) if r["kind"] == "session"]
    agent = agent if agent in agents.AGENTS else agents.choose(settings.get("default_agent"), worked)
    prompt = REVIVE_PROMPT.format(title=card["title"], id=card["id"], copy=f"its copy in {path}")
    return f"cd {shlex.quote(where)} && {agents.start(agent, prompt)}"


def save(card: dict, folder) -> str:
    """Write the thread's HTML page into folder, named after its title; returns the path."""
    from pathlib import Path
    slug = re.sub(r"[^a-z0-9]+", "-", card["title"].lower()).strip("-")[:60] or "thread"
    path = Path(folder).expanduser() / f"{slug}.thread.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(card))
    return str(path)


def from_html(page: str) -> dict:
    """The thread that an HTML thread page carries (or a context-card/1 page, from before version 2)."""
    m = re.search(r'<script type="application/json" id="(?:pensieve-thread|context-card)">(.*?)</script>', page, re.S)
    if not m:
        raise ValueError("not a Pensieve thread page")
    t = json.loads(m.group(1))
    st = t.get("state") or {}
    if st.get("value") in task_states.RENAMED:
        t["state"] = {**st, "value": task_states.RENAMED[st["value"]]}
    return t
