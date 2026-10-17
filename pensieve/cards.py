"""Context cards as files, in the context-card/1 format (context-card.schema.json).

A card has two parts. `remote` and the top-level fields hold what any reader can fetch again from a server: the git
remote and branch, PRs, tickets, messages and artifacts. `local` holds what works only on the machine that made it:
folder paths, the command that resumes a session, session ids. `share` drops `local` and every local path, so a
shared card carries URLs and commands (`handoff`) instead.

Each card has two renderings: `render_html` for people (one page with no outside files that also holds the JSON),
and `render_agent` for agents (the data as compact JSON, with the commands that refresh it). Agents in a Pensieve
session get the text card (work.compact) from the MCP tools and the prompt hook."""
import html
import json
import re
from datetime import datetime, timezone

from pensieve import work

SCHEMA = "context-card/1"
_SECRET = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----|"
    r"(?i:(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*)['\"]?[^\s'\"]{6,})")
_LOCAL_PATH = re.compile(r"(?:/Users/|/home/|~/)[^\s`'\")\]>,;]+")
_NONE = lambda s: (s or "").strip(" .").lower() in ("", "none")


def _iso(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else ""


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return f"pensieve {version('pensieve')}"
    except PackageNotFoundError:
        return "pensieve"


def _num(url: str) -> int:
    m = re.search(r"/(?:pull|merge_requests)/(\d+)", url)
    return int(m.group(1)) if m else 0


def _repo(url: str) -> str:
    m = re.search(r"https?://[^/]+/([^/]+/[^/]+)/", url)
    return m.group(1) if m else ""


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _repos(t: dict) -> list[dict]:
    """The remote of each git folder of the task: clone URL (without credentials), branch, its commit, and whether
    every commit is pushed."""
    out = []
    for p in t.get("places", []):
        if not p.get("git"):
            continue
        root = p["path"]
        git = re.sub(r"//[^@/]+@", "//", work._git(root, "remote", "get-url", "origin", ttl=3600))
        if not git:
            continue
        branch = p.get("branch") or ""
        out.append(_clean(dict(name=p.get("repo"), git=git, web=work.web_url(git), branch=branch,
                               head=work._git(root, "rev-parse", "--short=12", branch or "HEAD"),
                               pushed=bool(p.get("upstream") and not p.get("ahead")) or None)))
    return out


def build(t: dict, note: dict, hist: list[dict]) -> dict:
    """The local context-card/1 object of a task. It has the same validation as the text card: summary sentences
    that git or GitHub contradict are marked, and the last session's own question wins over the model's."""
    note, changed = work.reconcile(note, t) if note else ({}, [])
    last = max((s["updated"] for s in t.get("sessions", [])), default=0)
    prs = [(p, False) for p in t["prs"]] + [(p, True) for p in t.get("branch_prs", [])]
    since = [f"PR #{_num(p['url'])} merged {_iso(p['merged_at'])[:10]}" for p, _ in prs
             if last and (p.get("merged_at") or 0) > last]
    since += [c for c in changed if not c.startswith("PR #")]
    asked = work.last_question(t.get("last_words") or "")
    roots = [p["path"] + "/" for p in t.get("places", [])]
    files, outside, attachments = [], [], []
    for s in t["sessions"][:5]:
        for a in s.get("artifacts", []):
            if a.get("url"):
                attachments.append(dict(kind="artifact" if "/artifact/" in a["url"] else "page", title=a.get("name"), url=a["url"]))
                continue
            f = a.get("path") or ""
            rel = next((f[len(r):] for r in roots if f.startswith(r)), None)
            if rel and rel not in files:
                files.append(rel)
            elif not rel and f and "/.claude/" not in f and f not in outside:
                outside.append(f)
    repos = _repos(t)
    pulls = [_clean(dict(url=p["url"], repo=_repo(p["url"]), number=_num(p["url"]), title=p.get("title"),
                         state=p.get("state") or "", checks=p.get("checks"), review=p.get("review"), branch=p.get("head"),
                         merged_at=_iso(p.get("merged_at")), shared_branch=shared or None)) for p, shared in prs]
    left = note.get("left") if not _NONE(note.get("left")) else ""
    card = dict(
        schema=SCHEMA, id=t["id"], title=t["title"], kind=t.get("kind", "work"), locality="local",
        state=dict(value=t["state"], why=t.get("state_why", ""), set_by=t.get("state_by") or "auto"),
        made_at=_iso(datetime.now().timestamp()), last_activity=_iso(t["updated"]), last_session=_iso(last),
        origin=_clean(dict(by=_author(t), generator=_version())),
        summary=_clean(dict(recap=note.get("recap"), current=note.get("current"), left=left,
                            decisions=None if _NONE(note.get("decisions")) else note.get("decisions"), as_of=_iso(last))),
        waiting_on_you=asked or ("" if _NONE(note.get("question")) else note.get("question")),
        since_last_session=since, outdated=changed,
        last_words=work._clean_words(t.get("last_words") or ""),
        remote=_clean(dict(
            repos=repos, pull_requests=pulls,
            tickets=[_clean(dict(key=k["key"], status=k["status"], title=k["title"], url=k["url"])) for k in t["tickets"]],
            links=[_clean(dict(url=i["url"], title=i["title"], source=work._server_label(i["server"]) if i.get("server") else ""))
                   for i in t.get("items", [])[:10] if i.get("url")])),
        attachments=attachments[:10],
        files=files or [f[len(r):] for f in t["files"] for r in roots[:1] if f.startswith(r)],
        sessions=[dict(agent=s["id"].split(":", 1)[0], title=s["title"], updated=_iso(s["updated"])) for s in t["sessions"][:8]],
        timeline=[dict(at=_iso(e["ts"]), source=e["source"], title=e["title"]) for e in t["events"][:20]],
        history=[dict(at=_iso(h["at"]), state=h["state"], by=h["by"], reason=h["reason"]) for h in hist[-10:]],
        local=_clean(dict(
            places=[_clean(dict(path=p["path"], repo=p.get("repo"), branch=p.get("branch"), worktree=p.get("worktree"),
                                uncommitted=p.get("changed"), unpushed=p.get("ahead"))) for p in t.get("places", [])],
            resume=(t.get("resume") or {}).get("command", ""),
            session_ids=[s["id"] for s in t["sessions"][:8]], files=outside)))
    card["handoff"] = _handoff(card)
    return _clean(card)


def _author(t: dict) -> str:
    root = next((p["path"] for p in t.get("places", []) if p.get("git")), "")
    return work._git(root, "config", "user.name", ttl=3600) if root else ""


def _handoff(card: dict) -> dict:
    """What someone on another machine needs: the next step, the commands that get the work, and the commands that
    get the live state again."""
    r = card.get("remote", {})
    pulls, repos = r.get("pull_requests", []), r.get("repos", [])
    start, refresh = [], []
    open_pr = next((p for p in pulls if p.get("state") in ("open", "draft") and not p.get("shared_branch")), None)
    for repo in repos:
        if repo.get("branch") and repo["branch"] not in work.DEFAULT_BRANCHES:
            if open_pr:
                start.append(f"gh pr checkout {open_pr['url']}")
            elif repo.get("pushed"):
                start.append(f"git fetch {repo['git']} {repo['branch']} && git switch {repo['branch']}")
            else:
                start.append(f"# the branch {repo['branch']} is not on the remote: ask {card['origin'].get('by') or 'its author'} to push it")
            refresh.append(f"git ls-remote {repo['git']} refs/heads/{repo['branch']}")
        break  # the first repo is where the work is
    refresh += [f"gh pr view {p['url']} --json state,statusCheckRollup,reviewDecision" for p in pulls
                if p.get("state") in ("open", "draft", "")][:3]
    refresh += [f"{k['key']}: read the ticket with your tracker's tool ({k['url']})" for k in r.get("tickets", []) if k.get("url")][:2]
    refresh += [f"{x.get('source') or 'link'}: {x['url']}" for x in r.get("links", [])][:3]
    s = card.get("summary", {})
    return _clean(dict(next=card.get("waiting_on_you") or s.get("left"), start=start, refresh=refresh))


def share(card: dict) -> dict:
    """The card to send: no `local` part and no local path anywhere (a path inside the repo becomes relative to it),
    and secrets replaced by [secret]. The model's summary, the PRs, the tickets and the handoff stay."""
    c = json.loads(json.dumps(card))
    roots = sorted((p["path"].rstrip("/") + "/" for p in c.get("local", {}).get("places", [])), key=len, reverse=True)
    c.pop("local", None)
    if c["id"].startswith("b:"):  # b:<folder>@<branch> -> b:<org/repo>@<branch>, the same on every machine
        repo = next((re.sub(r"^https?://[^/]+/", "", x["web"]) for x in c.get("remote", {}).get("repos", []) if x.get("web")), "")
        c["id"] = f"b:{repo or c['id'][2:].rsplit('@', 1)[0].rsplit('/', 1)[-1]}@{c['id'].rsplit('@', 1)[-1]}"

    def scrub(x):
        if isinstance(x, str):
            for r in roots:
                x = x.replace(r, "")
            return _SECRET.sub("[secret]", _LOCAL_PATH.sub("[local path]", x))
        if isinstance(x, list):
            return [scrub(v) for v in x]
        if isinstance(x, dict):
            return {k: scrub(v) for k, v in x.items()}
        return x
    c = scrub(c)
    c["locality"] = "shared"
    return c


def local_parts(card: dict) -> list[str]:
    """Why a card is not fit to share: its `local` part, and each field that still holds a local path. [] when it is
    fit. A shared space refuses a card that this finds anything in."""
    found = ["local"] if "local" in card else []

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
    c = dict(card)
    c["timeline"] = card.get("timeline", [])[:5]
    c["sessions"] = card.get("sessions", [])[:3]
    c["history"] = card.get("history", [])[-3:]
    head = ("Context card (context-card/1). 'summary' is a model's note as of summary.as_of; a sentence marked "
            "[outdated: …] is wrong now. 'remote' and 'handoff.refresh' say where to get the live state. "
            + ("'local' works only on this machine." if "local" in card else "Shared: no local paths; start with handoff.start."))
    return head + "\n" + json.dumps(_clean(c), ensure_ascii=False, separators=(",", ":"))


STATE_LABEL = {"latent": "Defined", "conscious": "In progress", "subconscious": "Gone quiet", "etched": "Done",
               "forgotten": "Left"}
CSS = """
:root{--bg:#f5f5f7;--card:#fff;--ink:#1d1d1f;--ink-2:#6e6e73;--ink-3:#86868b;--line:rgba(0,0,0,.08);--link:#0066cc;
--good:#1f7a43;--warn:#a34700;--bad:#c4281c;--chip:rgba(0,0,0,.05);--ask:#fff7e6;--ask-line:#f2c46d;--merged:#8250df}
@media (prefers-color-scheme:dark){:root{--bg:#000;--card:#1c1c1e;--ink:#f5f5f7;--ink-2:#a1a1a6;--ink-3:#86868b;
--line:rgba(255,255,255,.12);--link:#2997ff;--good:#30d158;--warn:#ff9f0a;--bad:#ff453a;--chip:rgba(255,255,255,.08);
--ask:#2b2210;--ask-line:#8a6418;--merged:#bf5af2}}
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
.s-conscious{color:var(--link)}.s-subconscious{color:var(--warn)}.s-etched{color:var(--good)}.s-forgotten,.s-latent{color:var(--ink-2)}
.ask{margin-top:18px;background:var(--ask);border:1px solid var(--ask-line);border-radius:12px;padding:12px 14px}
.ask b{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.06em;margin-bottom:2px}
dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 14px;margin:0}dt{color:var(--ink-3)}dd{margin:0}
.note{font-size:12px;color:var(--warn);white-space:nowrap}
ul{margin:0;padding-left:18px}li{margin:2px 0}
.rows{list-style:none;padding:0}.rows li{display:flex;gap:10px;align-items:baseline;padding:7px 0;border-top:1px solid var(--line)}
.rows li:first-child{border-top:0}.grow{flex:1;min-width:0;overflow-wrap:anywhere}
.badge{font-size:12px;font-weight:600;white-space:nowrap}.b-merged{color:var(--merged)}.b-open,.b-draft{color:var(--good)}
.b-closed{color:var(--bad)}.b-failing{color:var(--bad)}.b-passing{color:var(--good)}.b-pending{color:var(--warn)}
.tl{list-style:none;padding:0;border-left:2px solid var(--line);margin-left:4px}.tl li{padding:2px 0 6px 14px;position:relative}
.tl li:before{content:"";position:absolute;left:-6px;top:9px;width:10px;height:10px;border-radius:50%;background:var(--card);
border:2px solid var(--ink-3)}.when{color:var(--ink-3);font-size:13px;margin-right:6px;font-variant-numeric:tabular-nums}
blockquote{margin:0;padding:10px 14px;border-left:3px solid var(--line);color:var(--ink-2);white-space:pre-wrap;font-size:14px}
.cmd{display:flex;gap:8px;align-items:center;background:var(--chip);border-radius:10px;padding:8px 10px;margin:4px 0}
.cmd code{flex:1;overflow-wrap:anywhere}button{font:inherit;font-size:12px;border:1px solid var(--line);background:var(--card);
color:var(--ink);border-radius:8px;padding:3px 9px;cursor:pointer}
.local{border:1px dashed var(--line);border-radius:12px;padding:4px 14px 12px;margin-top:22px}
.local h2{margin-top:12px}
footer{margin-top:26px;padding-top:12px;border-top:1px solid var(--line);font-size:12px;color:var(--ink-3)}
details summary{cursor:pointer;color:var(--ink-2)}
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


def render_html(card: dict) -> str:
    """The card for people: one page with no outside files, light and dark, that leads with what needs a decision.
    The page holds the card's JSON in <script type="application/json" id="context-card">, so `from_html` and any
    tool can read it back."""
    e = lambda s: html.escape(str(s or ""))
    day = lambda s: (s or "")[:10]
    st, r, loc = card.get("state", {}), card.get("remote", {}), card.get("local", {})
    out = [f'<span class="pill s-{e(st.get("value"))}">{e(STATE_LABEL.get(st.get("value"), st.get("value")))}</span>'
           + ('<span class="pill">Shared</span>' if card.get("locality") == "shared" else ""),
           f"<h1>{e(card['title'])}</h1>",
           '<div class="meta">' + "".join(f"<span>{x}</span>" for x in [
               e(st.get("why")), f"last session {e(day(card.get('last_session')))}" if card.get("last_session") else "",
               e((card.get("origin") or {}).get("by")),
               ", ".join(e(k["key"]) for k in r.get("tickets", []))] if x) + "</div>"]
    if card.get("waiting_on_you"):
        out.append(f'<div class="ask"><b>Waiting on you</b>{e(card["waiting_on_you"])}</div>')
    sm = card.get("summary") or {}
    if sm:
        rows = [f"<dt>{label}</dt><dd>{_marked(sm[k])}</dd>" for k, label in (
            ("recap", "What it is"), ("current", "Done so far"), ("left", "What's left"), ("decisions", "Decided")) if sm.get(k)]
        out += [f"<h2>Where it stands · as of {e(day(sm.get('as_of')))}</h2>", f"<dl>{''.join(rows)}</dl>"]
    if card.get("since_last_session"):
        out += ["<h2>Since the last session</h2>", "<ul>" + "".join(f"<li>{e(x)}</li>" for x in card["since_last_session"]) + "</ul>"]
    if r.get("pull_requests"):
        out += ["<h2>Pull requests</h2>", '<ul class="rows">' + "".join(
            f'<li><span class="badge b-{e(p.get("state"))}">{e(p.get("state") or "unknown")}</span>'
            f'<span class="grow"><a href="{e(p["url"])}">{e(p.get("repo"))}#{e(p.get("number"))}</a> {e(p.get("title"))}'
            + (' <span class="meta">(shared branch)</span>' if p.get("shared_branch") else "") + "</span>"
            + (f'<span class="badge b-{e(p["checks"])}">checks {e(p["checks"])}</span>' if p.get("checks") else "")
            + "</li>" for p in r["pull_requests"]) + "</ul>"]
    if r.get("repos"):
        out += ["<h2>Where the work is</h2>", '<ul class="rows">' + "".join(
            f'<li><span class="grow"><a href="{e(x.get("web") or x["git"])}">{e(x.get("name") or x["git"])}</a>'
            + (f' on <code>{e(x["branch"])}</code>' if x.get("branch") else "")
            + (f' at <code>{e(x["head"][:8])}</code>' if x.get("head") else "") + "</span>"
            + f'<span class="meta">{"pushed" if x.get("pushed") else "not all pushed"}</span></li>' for x in r["repos"]) + "</ul>"]
    if r.get("tickets"):
        out += ["<h2>Tickets</h2>", '<ul class="rows">' + "".join(
            f'<li><span class="badge">{e(k.get("status"))}</span><span class="grow">'
            + (f'<a href="{e(k["url"])}">{e(k["key"])}</a>' if k.get("url") else e(k["key"])) + f" {e(k.get('title'))}</span></li>"
            for k in r["tickets"]) + "</ul>"]
    links = [(x.get("source") or "Link", x) for x in r.get("links", [])] + [
        ("Artifact" if a.get("kind") == "artifact" else "Page", a) for a in card.get("attachments", [])]
    if links:
        out += ["<h2>Links and artifacts</h2>", '<ul class="rows">' + "".join(
            f'<li><span class="badge">{e(src)}</span><span class="grow"><a href="{e(x["url"])}">{e(x.get("title") or x["url"])}</a></span></li>'
            for src, x in links) + "</ul>"]
    h = card.get("handoff", {})
    cmds = ([loc["resume"]] if loc.get("resume") else []) + h.get("start", [])
    if cmds:
        out += ["<h2>Pick it up</h2>"] + [_cmd(c, i) for i, c in enumerate(cmds)]
    if card.get("last_words"):
        out += ["<h2>Where the last session stopped</h2>", f"<blockquote>{_md(card['last_words'])}</blockquote>"]
    if card.get("files"):
        out += ["<h2>Files</h2>", f"<details><summary>{len(card['files'])} files</summary><ul>"
                + "".join(f"<li><code>{e(f)}</code></li>" for f in card["files"][:30]) + "</ul></details>"]
    if card.get("timeline"):
        out += ["<h2>Timeline</h2>", '<ul class="tl">' + "".join(
            f'<li><span class="when">{e(day(x["at"]))}</span>{e(x["source"])}: {e(x["title"])}</li>' for x in card["timeline"]) + "</ul>"]
    if loc.get("places"):
        out += ['<section class="local"><h2>On this Mac only</h2><ul class="rows">' + "".join(
            f'<li><span class="grow"><code>{e(p["path"])}</code>' + (f' on <code>{e(p["branch"])}</code>' if p.get("branch") else "")
            + '</span><span class="meta">' + "".join(f"<span>{x}</span>" for x in [
                "worktree" if p.get("worktree") else "", f"{p['uncommitted']} uncommitted" if p.get("uncommitted") else "",
                f"{p['unpushed']} not pushed" if p.get("unpushed") else ""] if x) + "</span></li>"
            for p in loc["places"]) + "</ul></section>"]
    th = card.get("thread") or {}
    foot = (f"Context card · {e(card.get('schema'))} · made {e(day(card.get('made_at')))} by "
            f"{e((card.get('origin') or {}).get('generator'))}"
            + (f' · <a href="{e(th["url"])}">thread</a> revision {e(th.get("revision"))}' if th.get("url") else "")
            + " · this page holds the card as JSON")
    data = json.dumps(card, ensure_ascii=False).replace("<", "\\u003c")
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            '<meta name=viewport content="width=device-width,initial-scale=1">'
            f"<title>{e(card['title'])}</title><style>{CSS}</style></head><body><main>"
            + "".join(out) + f"<footer>{foot}</footer></main>"
            f'<script type="application/json" id="context-card">{data}</script></body></html>')


def from_html(page: str) -> dict:
    """The card that an HTML card page carries."""
    m = re.search(r'<script type="application/json" id="context-card">(.*?)</script>', page, re.S)
    if not m:
        raise ValueError("not a context card page")
    return json.loads(m.group(1))
