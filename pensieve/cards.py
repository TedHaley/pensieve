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
from datetime import datetime, timezone

from pensieve import cardtext, resources as res
from pensieve import task_states, work

SCHEMA = "pensieve-thread/2"
PAGE_ID = "pensieve-thread"  # the id of the <script> that holds the JSON; pages before version 2 used "context-card"


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


def build(t: dict, hist: list[dict]) -> dict:
    """The local pensieve-thread/2 object of a task: its state, the last session's open question, what changed
    since that session, what is left, its resources, last words, timeline and state history."""
    last = max((s["updated"] for s in t.get("sessions", [])), default=0)
    since = [f"PR #{_num(p['url'])} merged {_iso(p['merged_at'])[:10]}" for p in t["prs"] + t.get("branch_prs", [])
             if last and (p.get("merged_at") or 0) > last]
    asked = work.asked_question(t)
    core = dict(question=asked, as_of=_iso(last)) if asked else {}
    root = next((p["path"] for p in t.get("places", []) if p.get("git")), "")
    card = dict(
        schema=SCHEMA, id=t["id"], title=cardtext.card_title(t), label=t["title"],
        kind=t.get("kind", "work"), locality="local",
        state=dict(value=t["state"], why=t.get("state_why", ""), set_by=t.get("state_by") or "auto"),
        made_at=_iso(datetime.now().timestamp()), last_activity=_iso(t["updated"]), last_session=_iso(last),
        origin=_clean(dict(by=work._git(root, "config", "user.name", ttl=3600) if root else "", generator=_version())),
        core=core, since_last_session=since,
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


def render_agent(card: dict) -> str:
    """The card for an agent: its data as compact JSON, newest timeline first and cut short, with a line on how to
    read it. Agents follow ids, URLs and commands; they don't need layout."""
    c = dict(card, timeline=card.get("timeline", [])[:5], history=card.get("history", [])[-3:])
    c["resources"] = [{k: v for k, v in r.items() if k != "relevance"} for r in card.get("resources", [])]
    head = ("Pensieve thread (pensieve-thread/2). 'core.question' is what the last session asked and nobody answered. "
            "'left' is what closes the thread, with who acts. Each resource has a reach: git, mcp and public ones can "
            "be opened with their ref. 'handoff' says how to start and how to get the live state. "
            + ("'local' works only on this machine." if card.get("locality") == "local" else "Shared: start with 'ask' and handoff.start."))
    return head + "\n" + json.dumps(_clean(c), ensure_ascii=False, separators=(",", ":"))


STATE_LABEL = {"open": "Open", "todo": "To-do", "loose_ends": "Loose ends", "closed": "Closed", "complete": "Closed",
               "forgotten": "Forgotten"}
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
.cluster-card{margin-top:22px;padding-top:18px;border-top:1px solid var(--line)}.cluster-card h1{font-size:20px}
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
    return _page(card["title"], _card_html(card) + f"<footer>{_foot(card)}</footer>", card)


def render_cluster_html(cards: list[dict], branch: str) -> str:
    """The threads of one branch for people, newest first, on one page: each thread as on its own page. The page
    holds the threads' JSON as a list."""
    e = lambda s: html.escape(str(s or ""))
    head = (f"<h1>{e(branch)}</h1><div class=\"meta\"><span>{len(cards)} sessions wrote to this branch, newest first</span>"
            f"<span>{e(', '.join(c['id'] for c in cards))}</span></div>")
    body = "".join(f'<section class="cluster-card">{_card_html(c)}</section>' for c in cards)
    return _page(branch, head + body + f"<footer>{_foot(cards[0]) if cards else ''}</footer>", cards)


def _page(title: str, body: str, data) -> str:
    e = lambda s: html.escape(str(s or ""))
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            '<meta name=viewport content="width=device-width,initial-scale=1">'
            f"<title>{e(title)}</title><style>{CSS}</style></head><body><main>"
            + body + "</main>"
            f'<script type="application/json" id="{PAGE_ID}">{payload}</script></body></html>')


def _foot(card: dict) -> str:
    e = lambda s: html.escape(str(s or ""))
    day = lambda s: (s or "")[:10]
    th = card.get("thread") or {}
    return (f"Pensieve thread · {e(card.get('schema'))} · made {e(day(card.get('made_at')))} by "
            f"{e((card.get('origin') or {}).get('generator'))}"
            + (f' · <a href="{e(th["url"])}">thread</a> revision {e(th.get("revision"))}' if th.get("url") else "")
            + " · this page holds the thread as JSON")


def _card_html(card: dict) -> str:
    """One thread's part of a page: its state, title, core, links, last words and timeline."""
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
    left = card.get("left") or []
    if left:
        who = {"agent": "your agent", "you": "you", "parent": "the tracker"}
        out += ["<h2>Left to close it</h2>", "<ul>" + "".join(
            f"<li>{e(x['step'])} <span class=\"note\">{e(who.get(x.get('who'), x.get('who') or ''))}</span></li>" for x in left) + "</ul>"]
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
    return "".join(out)


def _link_row(r: dict, e) -> str:
    ref = r.get("ref", {})
    state = ref.get("state") or ref.get("status") or ""
    label = {"pr": state or "PR", "branch": "branch", "code": "file", "ticket": state or "ticket",
             "message": ref.get("source") or "message", "page": ref.get("source") or "page", "artifact": "artifact",
             "link": "link"}.get(r["kind"], r["kind"])
    return (f'<li><span class="badge b-{e(state)}">{e(label)}</span><span class="grow"><a href="{e(_url(r))}">{e(r["title"])}</a>'
            + (f"<p>{e(r['summary'])}</p>" if r.get("summary") else "") + f'</span><span class="reach">{e(REACH_LABEL[r["reach"]])}</span></li>')


REVIVE_TAIL = ("Then check the live state of its branch, pull requests and tickets, "
               "and review what has been done. What is left says who can do each step: the agent's steps are yours, "
               "the user's steps are mine, and the rest happen in the system the item lives in (close a ticket in "
               "its tracker, for example). Tell me where the work stands and how you will close the remaining "
               "items with your own tools, and ask me before you change anything, here or in another system.")
REVIVE_PROMPT = ("Pick up this thread: \"{title}\" (Pensieve thread {id}). Read it first with Pensieve's `thread` tool "
                 "(id {id}): it gives the thread as it is now, with what is left to close it. If you do not have "
                 "Pensieve's tools, read {copy}. " + REVIVE_TAIL)
CLUSTER_PROMPT = ("Pick up the work on branch {branch}: {n} sessions wrote to it, each a Pensieve thread ({ids}, newest "
                  "first). Read each one first with Pensieve's `thread` tool: together they give the work as it is now, "
                  "what each session did on the branch, and what is left to close it. If you do not have Pensieve's "
                  "tools, read {copy}. " + REVIVE_TAIL)
REVIVE_KEEP_DAYS = 7  # copies for agents without Pensieve's tools are deleted after this


def revive_prompt(card: dict) -> str:
    """REVIVE_PROMPT with the card in it, not in a file: for any agent, also one that can't read this Mac's files."""
    return REVIVE_PROMPT.format(title=card["title"], id=card["id"], copy="its copy below") + "\n\n" + render_agent(card)


def _cluster_text(cards: list[dict]) -> str:
    return "\n\n".join(f"# Thread {i + 1} of {len(cards)}: {c['title']}\n\n{render_agent(c)}" for i, c in enumerate(cards))


def _cluster_head(cards: list[dict], branch: str, copy: str) -> str:
    return CLUSTER_PROMPT.format(branch=branch or "(unnamed)", n=len(cards), ids=", ".join(c["id"] for c in cards), copy=copy)


def revive_cluster_prompt(cards: list[dict], branch: str) -> str:
    """CLUSTER_PROMPT with every card of the cluster in it, for any agent."""
    return _cluster_head(cards, branch, "their copies below") + "\n\n" + _cluster_text(cards)


def _revive_copy(folder, slug: str, text: str):
    """Write `text` as a dated copy in `folder` for an agent without Pensieve's tools, and drop old copies."""
    from pathlib import Path
    import time
    folder = Path(folder).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.md"):  # the copies are for one start each: old ones only fill the folder
        if time.time() - old.stat().st_mtime > REVIVE_KEEP_DAYS * 86400:
            old.unlink(missing_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")[:50] or "thread"
    path = folder / f"{slug}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(text)
    return path


def _revive_start(cards: list[dict], agent: str, prompt: str) -> str:
    """The shell command: cd to the work's folder (the first card's checkout on this Mac, else the home folder) and
    start the agent (`agent` from agents.AGENTS, else the user's default, else one that worked on the threads)."""
    import shlex
    from pathlib import Path
    from pensieve import agents, settings
    where = next((r["ref"]["path"] for c in cards for r in c.get("resources", []) if r["kind"] == "folder"
                  and Path(r.get("ref", {}).get("path", "")).is_dir()), str(Path.home()))
    worked = [r.get("ref", {}).get("session", "").split(":", 1)[0] for c in cards for r in c.get("resources", [])
              if r["kind"] == "session"]
    agent = agent if agent in agents.AGENTS else agents.choose(settings.get("default_agent"), worked)
    return f"cd {shlex.quote(where)} && {agents.start(agent, prompt)}"


def revive_cluster_command(cards: list[dict], branch: str, folder, agent: str = "") -> str:
    """The shell command that starts a new agent session on a cluster: the threads of one branch, newest first."""
    path = _revive_copy(folder, branch or "branch", _cluster_text(cards))
    return _revive_start(cards, agent, _cluster_head(cards, branch, f"their copies in {path}"))


RESOLVE_PROMPTS = {
    "ship": ("Ship the work on branch {branch}: {n} sessions wrote to it, each a Pensieve thread ({ids}, newest "
              "first). Read each one first with Pensieve's `thread` tool. If you do not have Pensieve's tools, read "
              "{copy}. The steps that take this work to merged and closed, in order:\n{steps}\n"
              "Do the agent's steps with your own tools, in this order, and stop before each step that is mine or "
              "the tracker's: tell me what it is and what you need from me. Before anything that leaves this Mac "
              "(a push, a pull request, a comment, a ticket change), show me what you will do and wait for my yes. "
              "When a step is done, say what you checked to know it is done."),
    "discard": ("Discard the work on branch {branch}: {n} sessions wrote to it, each a Pensieve thread ({ids}, newest "
                "first). Read each one first with Pensieve's `thread` tool. If you do not have Pensieve's tools, read "
                "{copy}. The steps that close this work without merging it, in order:\n{steps}\n"
                "Nothing here is reversible: before each step, show me exactly what it deletes, closes or changes "
                "and wait for my yes. Do the agent's steps with your own tools, in this order, and stop before each "
                "step that is mine or the tracker's: tell me what it is and what you need from me. When a step is "
                "done, say what you checked to know it is done."),
}


def resolve_steps(threads: list[dict], intent: str = "ship") -> list[dict]:
    """The steps left to close a cluster one way: `ship` takes each thread's closeout steps that are not done,
    `discard` its discard steps; one of each text, in order."""
    out, seen = [], set()
    for t in threads:
        for s in t.get("discard" if intent == "discard" else "closeout", []):
            key = s.get("text", "")
            if s.get("done") or key in seen:
                continue
            seen.add(key)
            out.append(dict(text=key, who=s.get("who", ""), ref=s.get("ref", ""), stage=s.get("stage", "")))
    return out


def _resolve_head(cards: list[dict], branch: str, steps: list[dict], copy: str, intent: str) -> str:
    who = {"agent": "you, the agent", "you": "me, the user", "parent": "the tracker or GitHub"}
    lines = [f"{i + 1}. {s['text']} ({who.get(s['who'], s['who'] or 'anyone')})" for i, s in enumerate(steps)] or ["(nothing left: check that the work is done)"]
    return RESOLVE_PROMPTS[intent].format(branch=branch or "(unnamed)", n=len(cards), ids=", ".join(c["id"] for c in cards),
                                          copy=copy, steps="\n".join(lines))


def resolve_prompt(cards: list[dict], branch: str, steps: list[dict], intent: str = "ship") -> str:
    return _resolve_head(cards, branch, steps, "their copies below", intent) + "\n\n" + _cluster_text(cards)


def resolve_command(cards: list[dict], branch: str, steps: list[dict], folder, agent: str = "", intent: str = "ship") -> str:
    """The shell command that starts a new agent session to merge or discard a cluster: the steps lead its prompt."""
    path = _revive_copy(folder, f"{intent}-{branch or 'branch'}", _cluster_text(cards))
    return _revive_start(cards, agent, _resolve_head(cards, branch, steps, f"their copies in {path}", intent))


def revive_command(card: dict, folder, agent: str = "") -> str:
    """The shell command that starts a new agent session on a thread: the agent reads the live thread with Pensieve's
    `task` tool, or a copy in a file when it has no Pensieve tools, and starts in the work's folder. `agent`: the
    agent to start (agents.AGENTS); by default the user's default agent, else one that worked on the thread (this Mac's checkout, or the home folder when it is
    gone; the card's handoff.start gets the work then). The agent is the one of the card's last session."""
    path = _revive_copy(folder, card["title"], render_agent(card))
    return _revive_start([card], agent, REVIVE_PROMPT.format(title=card["title"], id=card["id"], copy=f"its copy in {path}"))


def save(card: dict, folder) -> str:
    """Write the thread's HTML page into folder, named after its title; returns the path."""
    return save_page(render_html(card), card["title"], folder)


def save_page(page: str, name: str, folder) -> str:
    """Write a page into folder as <name slug>.thread.html; returns the path."""
    from pathlib import Path
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "thread"
    path = Path(folder).expanduser() / f"{slug}.thread.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page)
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
