"""The People tab: the people the user works with, seen from the user's side, and how close each one is to the user.

Each person is one record built from several identities:
- git authors (name and email) of the indexed repos;
- GitHub logins, from the pull requests that `gh` can read, and from noreply emails (123+login@users.noreply.github.com);
- Slack users, from the items that Pensieve collected from a Slack MCP server (connections/<server>/.raw/).
Identities join on the same email, then on a GitHub login, then on the same full name (two words or more). The user can
merge or split people by hand; these overrides stay in ~/.pensieve/people/overrides.json.

Edges between people come from what this Mac can see: files edited by both (git), pull requests that one wrote and the
other reviewed, the user's tasks (the Context tab), and Slack threads, channels and direct messages. Closeness to the
user is the weight of the user's own edge to a person, with a small share through the people between them.

The build runs in a background thread. Git results are cached per repo until its HEAD moves, pull requests for six
hours, and the whole graph for half an hour, so it does not slow indexing or search."""
import hashlib
import json
import math
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pensieve import config, connections, repos as repos_mod, settings, work

DIR = config.DATA_DIR / "people"
RAW = DIR / "raw.json"            # identities and interactions, by identity key (the slow part)
GRAPH = DIR / "graph.json"        # people, edges and profiles (resolved from RAW and the overrides)
OVERRIDES = DIR / "overrides.json"
PROFILES = DIR / "slack_profiles.json"
AVATARS = DIR / "avatars"
CACHE = DIR / "cache"

REBUILD_SECONDS = 1800
GIT_DAYS = 365
GH_TTL = 6 * 3600
PR_LIMIT = 60
COMMIT_PRS = 20
MAX_PR_REPOS = 15
SHARED_FILE_MAX = 12   # a file with more authors than this is shared ground: no edges from it
THREAD_MAX = 15        # a thread with more people than this gives no pair edges
CHANNEL_MAX = 30
HALF_LIFE_DAYS = 120
AVATAR_TOP = 150       # pictures are fetched for this many people, nearest first
KIND_WEIGHT = {"code": 1.0, "pr": 2.0, "task": 2.0, "thread": 1.5, "dm": 3.0, "channel": 0.3}
KIND_LABEL = {"code": "edited the same files", "pr": "pull request reviews", "task": "shared tasks",
              "thread": "Slack threads", "dm": "direct messages", "channel": "Slack channels"}

_lock = threading.RLock()
_state = {"graph": None, "building": False, "error": None, "started": 0.0}
listeners = []   # called (from a worker thread) when a new graph is ready

# AI co-authors of commits, and service accounts
_BOTS = re.compile(r"noreply@anthropic\.com|^claude$|copilot|cursor ?agent|^codex$|openai|^github-|-bot$|^bot-", re.I)
NOREPLY = re.compile(r"^(?:\d+\+)?([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)@users\.noreply\.github\.com$", re.I)


def norm_name(name: str) -> str:
    """A full name in one form ('José  Díaz-Lee' -> 'jose diaz lee'), or '' for a name of one word ('sam',
    'sam-lee': a login more often than a name)."""
    if len((name or "").split()) < 2:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z ]+", " ", s.replace("-", " "))
    words = s.split()
    return " ".join(words) if len(words) >= 2 else ""


def is_bot(name: str = "", email: str = "", login: str = "") -> bool:
    for x in (name, email, login):
        if x and (repos_mod.is_bot(x) or _BOTS.search(x)):
            return True
    return email.lower() == "noreply@github.com"


def _keys(name="", email="", login="", slack="") -> list[str]:
    email = (email or "").strip().lower()
    out = []
    if email and "@" in email:
        out.append("email:" + email)
        m = NOREPLY.match(email)
        if m:
            out.append("gh:" + m.group(1).lower())
    if login:
        out.append("gh:" + login.lower())
    if slack:
        out.append("slack:" + slack)
    n = norm_name(name)
    if n:
        out.append("name:" + n)
    return out


def _key_rank(k: str):
    return ({"email": 0, "gh": 1, "slack": 2, "name": 3}.get(k.split(":", 1)[0], 4), k)


def _hash(s: str, n=10) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:n]


def _decay(ts: float, now: float) -> float:
    return 0.5 ** (max(0.0, now - ts) / 86400 / HALF_LIFE_DAYS) if ts else 0.25


def _read(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def _write(p: Path, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(p)


class _Raw:
    """What the collectors find: identity observations, interactions between identity keys, and per-key work."""

    def __init__(self):
        self.ids = []                      # {keys, name, email, login, slack, avatar, title, src}
        self.acts = {}                     # (kind, a, b) -> {n, last, refs}
        self.work = defaultdict(lambda: {"repos": {}, "exts": Counter(), "subjects": [], "prs": [], "reviews": [],
                                         "channels": Counter(), "slack_last": 0, "messages": 0})
        self.me = set()
        self.stats = Counter()

    def ident(self, src, name="", email="", login="", slack="", avatar="", title=""):
        ks = _keys(name, email, login, slack)
        if ks:
            self.ids.append(dict(keys=ks, name=name or "", email=(email or "").lower(), login=login or "",
                                 slack=slack or "", avatar=avatar or "", title=title or "", src=src))
        return ks[0] if ks else None

    def act(self, kind, a, b, ts=0, ref=None, n=1):
        if not a or not b or a == b:
            return
        a, b = ("me", a if b == "me" else b) if "me" in (a, b) else tuple(sorted((a, b)))
        r = self.acts.setdefault((kind, a, b), {"n": 0, "last": 0, "refs": []})
        r["n"] += n
        r["last"] = max(r["last"], ts or 0)
        if ref and len(r["refs"]) < 40 and ref not in r["refs"]:
            r["refs"].append(ref)

    def dump(self) -> dict:
        return dict(built=time.time(), ids=self.ids, me=sorted(self.me), stats=dict(self.stats),
                    acts=[dict(kind=k, a=a, b=b, n=v["n"], last=v["last"],
                               refs=sorted(v["refs"], key=lambda r: -(r.get("ts") or 0))[:6])
                          for (k, a, b), v in self.acts.items()],
                    work={k: {**w, "exts": dict(w["exts"]), "channels": dict(w["channels"]),
                              "subjects": sorted(w["subjects"], key=lambda s: -s[0])[:12],
                              "prs": sorted(w["prs"], key=lambda p: -p["ts"])[:10],
                              "reviews": sorted(w["reviews"], key=lambda p: -p["ts"])[:10]}
                          for k, w in self.work.items()})


def _me_git(roots) -> tuple[set[str], set[str]]:
    """The user's git emails and names: the global config and each repo's own."""
    emails, names = set(), set()
    for args in [["config", "--global"]] + [["-C", r, "config"] for r in roots]:
        for k, into in (("user.email", emails), ("user.name", names)):
            try:
                v = subprocess.run(["git", *args, k], capture_output=True, text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                v = ""
            if v:
                into.add(v)
    return emails, names


def _gh_login() -> str:
    hit = _read(CACHE / "gh-user.json", {})
    if hit.get("at", 0) > time.time() - 86400:
        return hit.get("login", "")
    login = ""
    if shutil.which("gh"):
        try:
            login = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True,
                                   timeout=15).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    _write(CACHE / "gh-user.json", {"at": time.time(), "login": login})
    return login


_FUNCTIONS = [
    ("frontend", {".js", ".jsx", ".ts", ".tsx", ".css", ".scss", ".less", ".html", ".vue", ".svelte", ".mjs"}),
    ("backend", {".py", ".go", ".java", ".rb", ".rs", ".cs", ".php", ".scala", ".ex", ".exs", ".kt", ".c", ".cc",
                 ".cpp", ".h"}),
    ("mobile", {".swift", ".m", ".mm", ".dart"}),
    ("data", {".sql", ".ipynb", ".r", ".dbt"}),
    ("infrastructure", {".tf", ".hcl", ".yaml", ".yml", ".sh", "dockerfile", ".toml", ".nix", "makefile"}),
    ("docs", {".md", ".mdx", ".rst", ".txt", ".adoc"}),
]


def _ext_kind(path: str) -> str:
    name = path.lower().rsplit("/", 1)[-1]
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else name
    return next((kind for kind, exts in _FUNCTIONS if ext in exts), "")


def _git_repo(root: str, name: str) -> dict | None:
    """One repo's authors over GIT_DAYS: commits, areas, file kinds, recent subjects, and pairs of authors who edited
    the same files. Cached until the repo's HEAD moves."""
    head = repos_mod.git(root, "rev-parse", "HEAD").strip()
    if not head:
        return None
    cf = CACHE / f"git-{_hash(root)}.json"
    hit = _read(cf, {})
    if hit.get("head") == head and hit.get("v") == 1:
        return hit
    out = repos_mod.git(root, "log", f"--since={GIT_DAYS}.days", "--no-merges", "-n", "5000", "--name-only",
                        "--format=%x1e%at%x1f%an%x1f%ae%x1f%s")
    authors, files = {}, defaultdict(Counter)
    file_last = defaultdict(dict)
    for rec in out.split("\x1e")[1:]:
        head_line, _, names = rec.partition("\n")
        bits = head_line.split("\x1f", 3)
        if len(bits) < 4:
            continue
        ts, an, ae, subj = int(bits[0]), bits[1], bits[2].lower(), bits[3]
        if is_bot(an, ae):
            continue
        a = authors.setdefault(ae, {"names": Counter(), "commits": 0, "last": 0, "areas": Counter(), "exts": Counter(),
                                    "subjects": []})
        a["names"][an] += 1
        a["commits"] += 1
        a["last"] = max(a["last"], ts)
        if len(a["subjects"]) < 8:
            a["subjects"].append([ts, subj])
        touched = set()
        for f in (n for n in names.split("\n") if n):
            files[f][ae] += 1
            file_last[f][ae] = max(file_last[f].get(ae, 0), ts)
            touched.add(repos_mod.area_of(f))
            k = _ext_kind(f)
            if k:
                a["exts"][k] += 1
        for t in touched:
            a["areas"][t] += 1
    pairs = defaultdict(lambda: [0, 0, Counter()])
    for f, c in files.items():
        if not 2 <= len(c) <= SHARED_FILE_MAX:
            continue
        es = sorted(c)
        for i, x in enumerate(es):
            for y in es[i + 1:]:
                p = pairs[(x, y)]
                p[0] += min(c[x], c[y])
                p[1] = max(p[1], min(file_last[f][x], file_last[f][y]))
                p[2][repos_mod.area_of(f)] += 1
    res = dict(v=1, head=head, name=name, root=root,
               authors={e: {"name": a["names"].most_common(1)[0][0], "commits": a["commits"], "last": a["last"],
                            "areas": dict(a["areas"].most_common(8)), "exts": dict(a["exts"]), "subjects": a["subjects"]}
                        for e, a in authors.items()},
               pairs=[[x, y, p[0], p[1], [a for a, _ in p[2].most_common(3)]] for (x, y), p in pairs.items()])
    _write(cf, res)
    return res


def _collect_git(raw: _Raw, repo_rows):
    for root, name, _remote in repo_rows:
        try:
            g = _git_repo(root, name)
        except Exception:
            g = None
        if not g:
            continue
        raw.stats["repos"] += 1
        for email, a in g["authors"].items():
            k = raw.ident("git", name=a["name"], email=email)
            if not k:
                continue
            w = raw.work[k]
            w["repos"][name] = {"commits": a["commits"], "last": a["last"], "areas": a["areas"]}
            w["exts"].update(a["exts"])
            w["subjects"] += [[ts, name, s] for ts, s in a["subjects"]]
        for x, y, n, last, areas in g["pairs"]:
            raw.act("code", "email:" + x, "email:" + y, last, n=n,
                    ref={"t": f"{name}: {', '.join(areas)}", "ts": last, "repo": name, "areas": areas})


def _gh_slug(remote: str) -> str:
    """owner/name of a GitHub remote: the repos table keeps it as github.com/owner/name; a git remote URL works too."""
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", remote or "")
    return m.group(1) if m else ""


def _gh_repo(slug: str) -> list[dict]:
    """The last PR_LIMIT pull requests of a GitHub repo through `gh` (read only): author, reviewers and commit
    authors. Cached for GH_TTL."""
    cf = CACHE / f"gh-{_hash(slug)}.json"
    hit = _read(cf, {})
    if hit.get("at", 0) > time.time() - GH_TTL:
        return hit.get("prs", [])
    def gh(limit, fields):
        r = subprocess.run(["gh", "pr", "list", "--repo", slug, "--state", "all", "--limit", str(limit), "--json",
                            fields], capture_output=True, text=True, timeout=90)
        if r.returncode:
            raise OSError(r.stderr[:200])
        return json.loads(r.stdout or "[]")
    prs = []
    try:
        # commit authors tie logins to emails; GitHub caps that query's size, so only the newest pull requests
        authors = {p.get("number"): p.get("commits") or [] for p in gh(COMMIT_PRS, "number,commits")}
        for p in gh(PR_LIMIT, "number,title,url,state,updatedAt,author,reviews"):
            au = p.get("author") or {}
            prs.append(dict(
                number=p.get("number"), title=p.get("title", ""), url=p.get("url", ""),
                state=(p.get("state") or "").lower(), ts=work._ts(p.get("updatedAt") or ""),
                author=dict(login=au.get("login", ""), name=au.get("name", ""), bot=bool(au.get("is_bot"))),
                reviewers=sorted({(rv.get("author") or {}).get("login", "") for rv in p.get("reviews") or []} - {""}),
                commit_authors=list({(c.get("login") or c.get("email") or ""): dict(
                    login=c.get("login") or "", email=c.get("email") or "", name=c.get("name") or "")
                    for cm in authors.get(p.get("number"), []) for c in cm.get("authors") or []}.values())))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        prs = hit.get("prs", [])  # kept, and not asked again before GH_TTL
    _write(cf, {"at": time.time(), "prs": prs})
    return prs


def _collect_github(raw: _Raw, repo_rows, my_emails: set[str]):
    if not shutil.which("gh"):
        return
    since = time.time() - 180 * 86400

    def mine(root):  # the user's commits in a repo over the last 180 days: the repos they work in come first
        g = _read(CACHE / f"git-{_hash(root)}.json", {})
        return sum(a["commits"] for e, a in g.get("authors", {}).items() if e in my_emails and a["last"] > since)
    rows = [(root, name, _gh_slug(remote)) for root, name, remote in repo_rows]
    rows = sorted([r for r in rows if r[2]], key=lambda r: -mine(r[0]))[:MAX_PR_REPOS]
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda r: (r[1], _gh_repo(r[2])), rows))
    for name, prs in results:
        for p in prs:
            raw.stats["prs"] += 1
            a = p["author"]
            if not a["login"] or a["bot"] or is_bot(a["name"], login=a["login"]):
                continue
            ak = raw.ident("github", name=a["name"], login=a["login"],
                           avatar=f"https://github.com/{a['login']}.png?size=128")
            for c in p["commit_authors"]:
                if c["login"] and not is_bot(c["name"], c["email"], c["login"]):
                    raw.ident("github", name=c["name"] if c["name"] != c["login"] else "", email=c["email"],
                              login=c["login"])
            ref = {"t": p["title"], "url": p["url"], "ts": p["ts"], "state": p["state"], "repo": name}
            raw.work[ak]["prs"].append(dict(ref))
            for rv in p["reviewers"]:
                if rv == a["login"] or is_bot(login=rv):
                    continue
                rk = raw.ident("github", login=rv, avatar=f"https://github.com/{rv}.png?size=128")
                raw.work[rk]["reviews"].append({**ref, "author": a["login"]})
                raw.act("pr", ak, rk, p["ts"], ref={**ref, "by": a["login"], "reviewer": rv})


# Slack's MCP server answers in text. A user: "Name: … / User ID: U… / Title: … / Email: … / Profile Pic: [Photo](url)"
# (search) or "User ID: … / Real Name: … / Title: …" (profile). A message: "Channel: #name (ID: C…)", "From: Name
# <email> (ID: U…)", "Message_ts: …" and a permalink that holds thread_ts for a reply.
_FIELD = re.compile(r"^[ \t]*(Name|Real Name|Display Name|Title|Email|User ID|Profile Pic|Bot)[ \t]*:[ \t]*(.*)$", re.M)
_FROM = re.compile(r"^\W*From:\s*(.+?)\s*(?:<([^<>\s]+@[^<>\s]+)>)?\s*\(ID:\s*(U[A-Z0-9]{6,})\)", re.M)
_CHANNEL = re.compile(r"^\W*Channel:\s*(.*?)\s*\(ID:\s*([CDG][A-Z0-9]{6,})\)", re.M)
_MSG_TS = re.compile(r"Message_ts:\s*(\d+\.\d+)")
_THREAD_TS = re.compile(r"thread_ts=(\d+\.\d+)")
_PERMALINK = re.compile(r"Permalink:\s*\[[^\]]*\]\((https?://[^)\s]+)\)")
_URL_IN = re.compile(r"\((https?://[^)\s]+)\)")
_SEGMENT = re.compile(r"\n(?:-{3,}|#{2,4} Result \d+ of \d+)[ \t]*(?=\n)")
_UID = re.compile(r"U[A-Z0-9]{6,}")


def parse_slack_users(text: str) -> list[dict]:
    """The Slack users in a tool result's text: {id, name, title, email, avatar, bot}."""
    out = []
    for seg in _SEGMENT.split(text):
        if "User ID:" not in seg:
            continue
        f = {}
        for k, v in _FIELD.findall(seg):
            f.setdefault(k, v.strip())
        uid = f.get("User ID", "")
        if not _UID.fullmatch(uid):
            continue
        pic = _URL_IN.search(f.get("Profile Pic", ""))
        out.append(dict(id=uid, name=f.get("Real Name") or f.get("Name") or f.get("Display Name") or "",
                        title=f.get("Title", ""), email=f.get("Email", "").lower(), avatar=pic.group(1) if pic else "",
                        bot=f.get("Bot", "").lower() == "yes"))
    return out


def _json_users(x, depth=0):
    """Slack user objects in JSON results (other Slack MCP servers): {id: U…, real_name, profile: {…}}."""
    if depth > 6:
        return
    if isinstance(x, dict):
        uid = x.get("id")
        prof = x.get("profile") if isinstance(x.get("profile"), dict) else {}
        if isinstance(uid, str) and _UID.fullmatch(uid) and (x.get("real_name") or prof):
            yield dict(id=uid, name=x.get("real_name") or prof.get("real_name") or x.get("name") or "",
                       title=prof.get("title") or x.get("title") or "", email=(prof.get("email") or "").lower(),
                       avatar=prof.get("image_192") or prof.get("image_72") or "", bot=bool(x.get("is_bot")))
        for v in x.values():
            yield from _json_users(v, depth + 1)
    elif isinstance(x, list):
        for v in x[:500]:
            yield from _json_users(v, depth + 1)


def _json_messages(x, depth=0):
    """Slack messages in JSON results: (channel id, thread key, user id, ts)."""
    if depth > 6:
        return
    if isinstance(x, dict):
        u, ts = x.get("user"), x.get("ts")
        if isinstance(u, str) and _UID.fullmatch(u) and isinstance(ts, str):
            ch = x.get("channel")
            cid = ch.get("id") if isinstance(ch, dict) else ch if isinstance(ch, str) else x.get("channel_id") or ""
            try:
                t = float(ts)
            except ValueError:
                t = 0.0
            yield str(cid or ""), str(x.get("thread_ts") or ts), u, t
        for v in x.values():
            yield from _json_messages(v, depth + 1)
    elif isinstance(x, list):
        for v in x[:500]:
            yield from _json_messages(v, depth + 1)


def _slack_items():
    """(path of the .md, raw item, is private) for each collected item from a chat server that is switched on."""
    for d in connections.roots(settings.enabled):
        for md in d.glob("*.md"):
            try:
                text = md.read_text()
                raw = json.loads((d / ".raw" / f"{md.stem}.json").read_text())
            except (OSError, ValueError):
                continue
            tool = re.search(r"^- Source: .*\(([^()\n]+)\)$", text, re.M)
            tool = tool.group(1) if tool else ""
            if not connections.CHAT.search(f"{d.name} {tool}"):
                continue
            private = bool(raw.get("_private")) or connections.private_tool(tool) or connections.private_item(raw, True)
            yield str(md), raw, private


def _collect_slack(raw: _Raw) -> dict:
    """Slack people, and who talked with whom in the threads, channels and direct messages that the user can see.
    Returns item path -> the identity keys of the people in it (for tasks that hold Slack items)."""
    prof = _read(PROFILES, {})
    users = {u["id"]: dict(u) for u in prof.get("users", {}).values() if u.get("id")}
    threads = defaultdict(lambda: {"people": set(), "last": 0, "channel": "", "url": "", "dm": False})
    channels = defaultdict(lambda: {"people": Counter(), "last": 0, "name": ""})
    by_item = defaultdict(set)

    def seen_user(u):
        cur = users.setdefault(u["id"], dict(id=u["id"], name="", title="", email="", avatar="", bot=False))
        for k in ("name", "title", "email", "avatar"):
            cur[k] = cur.get(k) or u.get(k) or ""
        cur["bot"] = cur.get("bot") or u.get("bot", False)

    for path, item, private in _slack_items():
        raw.stats["slack_items"] += 1
        for u in _json_users(item):
            seen_user(u)
        for cid, tk, uid, ts in _json_messages(item):
            seen_user({"id": uid})
            t = threads[f"{cid}:{tk}" if cid else f"{path}:{tk}"]
            t["people"].add(uid)
            t["last"] = max(t["last"], ts)
            t["dm"] = t["dm"] or (private and cid.startswith("D"))
            if cid and not t["dm"]:
                channels[cid]["people"][uid] += 1
                channels[cid]["last"] = max(channels[cid]["last"], ts)
            by_item[path].add(uid)
        for s in connections._strings(item):
            for u in parse_slack_users(s):
                seen_user(u)
            if "From:" not in s:
                continue
            for seg in _SEGMENT.split(s):
                froms = _FROM.findall(seg)
                if not froms:
                    continue
                ch = _CHANNEL.search(seg)
                cname, cid = (ch.group(1), ch.group(2)) if ch else ("", "")
                ts_m, th = _MSG_TS.search(seg), _THREAD_TS.search(seg)
                ts = float(ts_m.group(1)) if ts_m else 0.0
                tk = th.group(1) if th else ts_m.group(1) if ts_m else ""
                link = _PERMALINK.search(seg)
                t = threads[f"{cid}:{tk}" if cid and tk else f"{path}:{cid}:{tk}"]
                dm = private and (cid.startswith("D") or cname.upper().startswith("DM"))
                t["dm"] = t["dm"] or dm
                t["channel"] = t["channel"] or ("Direct message" if dm else cname)
                t["url"] = t["url"] or (link.group(1) if link else "")
                t["last"] = max(t["last"], ts)
                for name, email, uid in froms:
                    seen_user({"id": uid, "name": name, "email": email.lower()})
                    t["people"].add(uid)
                    by_item[path].add(uid)
                    if cid and not dm:
                        channels[cid]["people"][uid] += 1
                        channels[cid]["last"] = max(channels[cid]["last"], ts)
                        channels[cid]["name"] = channels[cid]["name"] or cname
    key_of = {uid: raw.ident("slack", name=u["name"], email=u["email"], slack=uid, avatar=u["avatar"], title=u["title"])
              for uid, u in users.items() if not u.get("bot")}
    if prof.get("me"):
        raw.me.add("slack:" + prof["me"])
    for t in threads.values():
        ppl = [key_of[u] for u in t["people"] if key_of.get(u)]
        raw.stats["slack_threads"] += 1
        for k in ppl:
            w = raw.work[k]
            w["messages"] += 1
            w["slack_last"] = max(w["slack_last"], t["last"])
            if t["channel"] and not t["dm"]:
                w["channels"][t["channel"]] += 1
        ref = {"t": t["channel"] or "Slack thread", "url": t["url"], "ts": t["last"]}
        if t["dm"]:
            for k in ppl:  # the user is in every direct message they can read
                raw.act("dm", "me", k, t["last"], ref=ref)
        if 2 <= len(ppl) <= THREAD_MAX:
            for i, x in enumerate(ppl):
                for y in ppl[i + 1:]:
                    raw.act("dm" if t["dm"] else "thread", x, y, t["last"], ref=ref)
    for c in channels.values():
        ppl = [key_of[u] for u in c["people"] if key_of.get(u)]
        if 2 <= len(ppl) <= CHANNEL_MAX:
            for i, x in enumerate(ppl):
                for y in ppl[i + 1:]:
                    raw.act("channel", x, y, c["last"], ref={"t": c["name"], "ts": c["last"]})
    return {p: {key_of[u] for u in us if key_of.get(u)} for p, us in by_item.items()}


def _collect_tasks(raw: _Raw, store, slack_by_item: dict):
    """The user's own tasks (Context tab): the people in their commits, tickets and Slack items, and the coworkers
    who committed in the same folders. Each is a 'task' edge to the user."""
    try:
        ts = work.tasks(store, 30) if store is not None else []
    except Exception:
        ts = []
    for t in ts:
        ref = {"t": t.get("title", ""), "id": t.get("id"), "ts": t.get("updated", 0)}
        for p in t.get("people", []):
            if is_bot(p, p if "@" in p else ""):
                continue
            k = raw.ident("task", email=p) if "@" in p else raw.ident("task", name=p)
            raw.act("task", "me", k, t.get("updated", 0), ref=ref)
        for it in t.get("items", []):
            for k in slack_by_item.get(it.get("path"), ()):
                raw.act("task", "me", k, t.get("updated", 0), ref=ref)
    recent = [t for t in ts if t.get("files")][:8]

    def cw(t):
        try:
            return t, work.coworkers(t)
        except Exception:
            return t, []
    with ThreadPoolExecutor(4) as pool:
        for t, ppl in pool.map(cw, recent):
            for p in ppl:
                if is_bot(p["name"], p["email"]):
                    continue
                k = raw.ident("git", name=p["name"], email=p["email"])
                raw.act("task", "me", k, p["last"], ref={"t": t.get("title", ""), "id": t.get("id"), "ts": p["last"]})
    raw.stats["tasks"] = len(ts)


def collect(store, repo_rows) -> dict:
    """Run every collector and save the raw identities and interactions. `repo_rows`: (root, name, remote)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    raw = _Raw()
    emails, names = _me_git([r[0] for r in repo_rows])
    raw.me |= {"email:" + e.lower() for e in emails} | {"name:" + n for n in map(norm_name, names) if n}
    login = _gh_login()
    if login:
        raw.me.add("gh:" + login.lower())
    _collect_git(raw, repo_rows)
    _collect_github(raw, repo_rows, {e.lower() for e in emails})
    by_item = _collect_slack(raw)
    _collect_tasks(raw, store, by_item)
    d = raw.dump()
    _write(RAW, d)
    return d


def overrides() -> dict:
    o = _read(OVERRIDES, {})
    return {"merge": o.get("merge", []), "split": o.get("split", []), "me": o.get("me", [])}


def _set_overrides(fn):
    with _lock:
        o = overrides()
        fn(o)
        _write(OVERRIDES, o)


def resolve(raw: dict, ovr: dict) -> dict:
    """People from the raw identities: union on strong keys (email, GitHub login, Slack id), then on full names, then
    the user's own merges. A split key joins nothing but the user's own merges."""
    uf = work._Union()
    split = set(ovr["split"])
    me_keys = set(raw.get("me", [])) | set(ovr["me"])
    strong_of = []
    for ob in raw["ids"]:
        strong = [k for k in ob["keys"] if not k.startswith("name:") and k not in split]
        strong_of.append(strong)
        for k in ob["keys"]:
            uf.find(k)
        for k in strong[1:]:
            uf.join(strong[0], k)
    logins = {k for k in uf.p if k.startswith("gh:")}
    for ob, strong in zip(raw["ids"], strong_of):
        for nk in ob["keys"]:
            if nk.startswith("name:") and nk not in split and strong:
                uf.join(strong[0], nk)
        # a git author whose name is one word that is also a GitHub login seen in the pull requests ("sam-lee")
        lk = "gh:" + ob["name"].strip().lower()
        if strong and ob["src"] != "github" and len(ob["name"].split()) == 1 and len(lk) >= 7 and lk in logins \
                and lk not in split:
            uf.join(strong[0], lk)
    for a, b in ovr["merge"]:
        uf.join(a, b)
    mine = [k for k in sorted(me_keys) if k in uf.p and k not in split]
    for k in mine[1:]:
        uf.join(mine[0], k)
    groups = defaultdict(set)
    for k in list(uf.p):
        groups[uf.find(k)].add(k)
    if mine:
        me_root = uf.find(mine[0])
    else:  # nothing seen of the user yet: a node of their own
        me_root = sorted(me_keys, key=_key_rank)[0] if me_keys else "me:you"
        groups[me_root] = set(me_keys) or {me_root}
    obs_of = defaultdict(list)
    for ob in raw["ids"]:
        obs_of[uf.find(ob["keys"][0])].append(ob)

    people, pid_of = {}, {}
    for root, keys in groups.items():
        obs = obs_of.get(root, [])
        first = sorted(keys, key=_key_rank)[0]
        pid = "p" + _hash(first)
        names = Counter()
        for ob in obs:
            if ob["name"]:
                names[ob["name"]] += {"slack": 3, "github": 2}.get(ob["src"], 1)
        name = names.most_common(1)[0][0] if names else first.split(":", 1)[-1].split("@")[0]
        avatar = next((ob["avatar"] for ob in obs if ob["src"] == "slack" and ob["avatar"]), "") or \
            next((ob["avatar"] for ob in obs if ob["avatar"]), "")
        people[pid] = dict(id=pid, name=name, me=root == me_root, keys=sorted(keys, key=_key_rank),
                           title=next((ob["title"] for ob in obs if ob["title"]), ""), avatar_url=avatar,
                           sources=sorted({ob["src"] for ob in obs}))
        for k in keys:
            pid_of[k] = pid
    me_id = next(p["id"] for p in people.values() if p["me"])
    pid_of["me"] = me_id

    now = time.time()
    edges = {}
    for a in raw["acts"]:
        pa, pb = pid_of.get(a["a"]), pid_of.get(a["b"])
        if not pa or not pb or pa == pb:
            continue
        x, y = sorted((pa, pb))
        e = edges.setdefault((x, y), {"a": x, "b": y, "kinds": Counter(), "last": 0, "refs": defaultdict(list)})
        e["kinds"][a["kind"]] += a["n"]
        e["last"] = max(e["last"], a["last"])
        e["refs"][a["kind"]] += a["refs"]
    for e in edges.values():
        e["w"] = round(sum(KIND_WEIGHT[k] * math.log1p(n) for k, n in e["kinds"].items()) * _decay(e["last"], now), 4)

    # closeness: the user's own edge, plus a quarter of the best path through one person in between
    direct = {(e["b"] if e["a"] == me_id else e["a"]): e["w"] for e in edges.values() if me_id in (e["a"], e["b"])}
    nbr = defaultdict(dict)
    for e in edges.values():
        nbr[e["a"]][e["b"]] = e["w"]
        nbr[e["b"]][e["a"]] = e["w"]
    top = max(direct.values(), default=1) or 1
    close = {}
    for pid in people:
        if pid != me_id:
            via = max((direct[m] * w / top for m, w in nbr[pid].items() if m in direct), default=0)
            close[pid] = direct.get(pid, 0) + 0.25 * via
    cmax = max(close.values(), default=1) or 1
    for pid, p in people.items():
        p["closeness"] = 1.0 if pid == me_id else round(close.get(pid, 0) / cmax, 4)
    ranked = sorted((p for p in people.values() if not p["me"]),
                    key=lambda p: (-p["closeness"], -sum(nbr[p["id"]].values()), p["name"]))
    for i, p in enumerate(ranked):
        p["rank"] = i + 1
    people[me_id]["rank"] = 0

    profiles = {pid: _profile(p, raw["work"], edges, me_id, people) for pid, p in people.items()}
    for pid, p in people.items():
        p["last"] = profiles[pid]["last"]
        p["function"] = profiles[pid]["function"]
    return dict(me=me_id, built=raw.get("built", now), resolved=now, stats=raw.get("stats", {}),
                people=people, profiles=profiles,
                edges=[{"a": e["a"], "b": e["b"], "w": e["w"], "kinds": dict(e["kinds"]), "last": e["last"]}
                       for e in sorted(edges.values(), key=lambda e: -e["w"])[:5000]],
                me_edges={(e["b"] if e["a"] == me_id else e["a"]): {k: v[:8] for k, v in e["refs"].items()}
                          for e in edges.values() if me_id in (e["a"], e["b"])})


def _merge_work(keys, work_by_key) -> dict:
    out = {"repos": {}, "exts": Counter(), "subjects": [], "prs": [], "reviews": [], "channels": Counter(),
           "slack_last": 0, "messages": 0}
    for k in keys:
        w = work_by_key.get(k)
        if not w:
            continue
        for r, v in w["repos"].items():
            cur = out["repos"].setdefault(r, {"commits": 0, "last": 0, "areas": Counter()})
            cur["commits"] += v["commits"]
            cur["last"] = max(cur["last"], v["last"])
            cur["areas"].update(v["areas"])
        out["exts"].update(w["exts"])
        out["subjects"] += w["subjects"]
        out["prs"] += w["prs"]
        out["reviews"] += w["reviews"]
        out["channels"].update(w["channels"])
        out["slack_last"] = max(out["slack_last"], w["slack_last"])
        out["messages"] += w["messages"]
    return out


def _function(title: str, exts: Counter) -> tuple[str, bool]:
    """(function, inferred): the Slack title, or the kind of files that most of their commits touch."""
    if title:
        return title, False
    code = {k: v for k, v in exts.items() if k != "docs"}
    tot = sum(code.values())
    if tot >= 5:
        k, n = max(code.items(), key=lambda kv: kv[1])
        if n / tot >= 0.45:
            return {"frontend": "Frontend engineering", "backend": "Backend engineering", "mobile": "Mobile engineering",
                    "data": "Data engineering", "infrastructure": "Infrastructure"}[k], True
        return "Engineering", True
    if exts.get("docs", 0) >= 5:
        return "Writes docs", True
    return "", True


def _profile(p, work_by_key, edges, me_id, people) -> dict:
    w = _merge_work(p["keys"], work_by_key)
    fn, inferred = _function(p["title"], w["exts"])
    repos = sorted(({"repo": r, "commits": v["commits"], "last": v["last"],
                     "areas": [a for a, _ in v["areas"].most_common(4)]} for r, v in w["repos"].items()),
                   key=lambda r: -r["commits"])
    peers = sorted(((e["w"], e["b"] if e["a"] == p["id"] else e["a"]) for e in edges.values()
                    if p["id"] in (e["a"], e["b"]) and me_id not in (e["a"], e["b"])), reverse=True)
    last = max([r["last"] for r in repos] + [x["ts"] for x in w["prs"] + w["reviews"]] + [w["slack_last"], 0])
    rel = None
    if p["id"] != me_id:
        mine = _merge_work(people[me_id]["keys"], work_by_key)
        shared = []
        for r, v in w["repos"].items():
            mv = mine["repos"].get(r)
            if mv:
                both = set(v["areas"]) & set(mv["areas"])
                shared += [f"{r}/{a}" for a in sorted(both, key=lambda a: -(v["areas"][a] + mv["areas"][a]))[:3]]
        e = edges.get(tuple(sorted((p["id"], me_id))))
        rel = dict(closeness=p["closeness"], rank=p.get("rank"), last=e["last"] if e else 0, weight=e["w"] if e else 0,
                   kinds=[{"kind": k, "label": KIND_LABEL[k], "n": n} for k, n in e["kinds"].most_common()] if e else [],
                   shared_areas=shared[:6])
    return dict(
        id=p["id"], name=p["name"], me=p["me"], title=p["title"], function=fn, function_inferred=inferred,
        identities=[{"key": k, "kind": k.split(":", 1)[0], "value": k.split(":", 1)[1]} for k in p["keys"]],
        sources=p["sources"], repos=repos[:8], last=last,
        subjects=[{"ts": s[0], "repo": s[1], "subject": s[2]} for s in sorted(w["subjects"], key=lambda s: -s[0])[:8]],
        prs=sorted(w["prs"], key=lambda x: -x["ts"])[:6], reviews=sorted(w["reviews"], key=lambda x: -x["ts"])[:6],
        channels=[c for c, _ in w["channels"].most_common(6)], messages=w["messages"],
        peers=[o for _, o in peers[:6]], relation=rel)


def _avatar_file(url: str) -> Path | None:
    return next(iter(f for f in AVATARS.glob(_hash(url, 16) + ".*") if f.suffix != ".none"), None) \
        if AVATARS.is_dir() else None


def _fetch_avatar(url: str) -> Path | None:
    """Download a picture once into the data folder. Slack's original picture is large: its 192 px copy first."""
    import httpx
    hit = _avatar_file(url)
    if hit:
        return hit
    AVATARS.mkdir(parents=True, exist_ok=True)
    miss = AVATARS / (_hash(url, 16) + ".none")
    if miss.exists() and time.time() - miss.stat().st_mtime < 7 * 86400:
        return None
    tries = [re.sub(r"_original\.(\w+)$", r"_192.\1", url), url] if "_original." in url else [url]
    for u in tries:
        try:
            r = httpx.get(u, follow_redirects=True, timeout=10)
        except Exception:
            continue
        ct = r.headers.get("content-type", "").split(";")[0]
        if r.status_code == 200 and ct.startswith("image/") and len(r.content) < 3_000_000:
            ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}.get(ct, "img")
            f = AVATARS / f"{_hash(url, 16)}.{ext}"
            f.write_bytes(r.content)
            return f
    miss.touch()
    return None


def _fetch_avatars(g: dict):
    want = sorted((p for p in g["people"].values() if p["avatar_url"]), key=lambda p: (not p["me"], -p["closeness"]))
    with ThreadPoolExecutor(6) as pool:
        list(pool.map(lambda p: _fetch_avatar(p["avatar_url"]), want[:AVATAR_TOP]))


def avatar(pid: str) -> Path | None:
    p = (graph() or {}).get("people", {}).get(pid)
    return _avatar_file(p["avatar_url"]) if p and p["avatar_url"] else None


def graph() -> dict | None:
    with _lock:
        if _state["graph"] is None:
            _state["graph"] = _read(GRAPH, None)
        return _state["graph"]


def _publish(g: dict):
    with _lock:
        _state["graph"] = g
        _write(GRAPH, g)


def rebuild(store, repo_rows_fn, force=False) -> bool:
    """Start a background build, unless one is running or the graph is new enough."""
    with _lock:
        g = graph()
        fresh = g and time.time() - g.get("built", 0) < REBUILD_SECONDS
        if _state["building"] or (fresh and not force):
            return False
        _state["building"] = True
        _state["started"] = time.time()

    def run():
        try:
            g = resolve(collect(store, repo_rows_fn()), overrides())
            _publish(g)
            _state["error"] = None
            _fetch_avatars(g)
        except Exception as e:
            import traceback
            traceback.print_exc()
            _state["error"] = str(e)[:300]
        finally:
            _state["building"] = False
        for fn in listeners:
            fn()
    threading.Thread(target=run, daemon=True, name="people").start()
    return True


def re_resolve():
    """Apply the overrides again to the saved raw data (fast: no git, gh or Slack)."""
    raw = _read(RAW, None)
    if raw:
        _publish(resolve(raw, overrides()))


def summary(limit: int = 200) -> dict:
    """GET /api/people: the nearest `limit` people (nodes), the edges between them, and closeness to the user."""
    g = graph()
    if not g:
        return dict(me=None, nodes=[], edges=[], building=_state["building"], error=_state["error"], total=0)
    ppl = sorted(g["people"].values(), key=lambda p: (not p["me"], p.get("rank", 1e9)))[:limit + 1]
    keep = {p["id"] for p in ppl}
    nodes = [dict(id=p["id"], name=p["name"], me=p["me"], closeness=p["closeness"], rank=p.get("rank"),
                  title=p["title"], function=p.get("function", ""), sources=p["sources"], last=p.get("last", 0),
                  avatar=bool(p["avatar_url"] and _avatar_file(p["avatar_url"])),
                  slack=any(k.startswith("slack:") for k in p["keys"])) for p in ppl]
    edges = [e for e in g["edges"] if e["a"] in keep and e["b"] in keep]
    return dict(me=g["me"], built=g["built"], resolved=g.get("resolved"), stats=g.get("stats", {}), nodes=nodes,
                edges=edges, total=len(g["people"]), building=_state["building"], error=_state["error"],
                overrides=sum(len(v) for v in overrides().values()))


def profile(pid: str) -> dict | None:
    """GET /api/people/<id>: who they are, what they work on, and the user's relation to them."""
    g = graph()
    if not g or pid not in g["profiles"]:
        return None
    pr = dict(g["profiles"][pid])
    people = g["people"]
    pr["peers"] = [dict(id=o, name=people[o]["name"]) for o in pr["peers"] if o in people]
    pr["avatar"] = bool(people[pid]["avatar_url"] and _avatar_file(people[pid]["avatar_url"]))
    if pr.get("relation") is not None:
        refs = g.get("me_edges", {}).get(pid, {})
        pr["relation"] = {**pr["relation"], "tasks": _uniq(refs.get("task", []), "id")[:6],
                          "prs": _uniq(refs.get("pr", []), "url")[:6],
                          "threads": _uniq(refs.get("thread", []) + refs.get("dm", []), "url")[:6],
                          "channels": _uniq(refs.get("channel", []), "t")[:6],
                          "code": _uniq(refs.get("code", []), "t")[:6]}
    return pr


def _uniq(refs, key):
    seen, out = set(), []
    for r in sorted(refs, key=lambda r: -(r.get("ts") or 0)):
        k = r.get(key) or r.get("t")
        if k and k not in seen:
            seen.add(k)
            out.append(r)
    return out


def find(who: str) -> str | None:
    """The person id for a name, an email or 'Name <email>' (the Map's author strings)."""
    g = graph()
    if not g or not who:
        return None
    m = re.match(r"(.*?)\s*<([^>]+)>", who)
    name, email = (m.group(1), m.group(2)) if m else (who, who if "@" in who else "")
    want = set(_keys(name=name, email=email, login=who if re.fullmatch(r"[\w-]+", who) else ""))
    for p in sorted(g["people"].values(), key=lambda p: p.get("rank", 1e9)):
        if want & set(p["keys"]):
            return p["id"]
    low = who.strip().lower()
    return next((p["id"] for p in g["people"].values() if p["name"].lower() == low), None)


def merge(ids: list[str]) -> str:
    """Join these people into one, and return its id."""
    g = graph()
    keys = [g["people"][i]["keys"][0] for i in ids if g and i in g["people"]]
    if len(keys) < 2:
        raise ValueError("pick two or more people")

    def apply(o):
        o["merge"] += [[keys[0], k] for k in keys[1:]]
        o["split"] = [s for s in o["split"] if s not in keys]
    _set_overrides(apply)
    re_resolve()
    return next(p["id"] for p in graph()["people"].values() if keys[0] in p["keys"])


def split(key: str):
    """Take one identity (an email, a login, a Slack id or a name) out of the person it was joined to."""
    def apply(o):
        if key not in o["split"]:
            o["split"].append(key)
        o["merge"] = [m for m in o["merge"] if key not in m]
    _set_overrides(apply)
    re_resolve()


def set_me(pid: str):
    g = graph()
    if not g or pid not in g["people"]:
        raise ValueError("unknown person")
    _set_overrides(lambda o: o.__setitem__("me", g["people"][pid]["keys"][:3]))
    re_resolve()


def reset_overrides():
    _write(OVERRIDES, {"merge": [], "split": [], "me": []})
    re_resolve()


def missing_profiles(limit: int = 25) -> list[str]:
    """Slack ids of the nearest people with no picture or no title yet."""
    g = graph()
    if not g:
        return []
    out = []
    for p in sorted(g["people"].values(), key=lambda p: (not p["me"], p.get("rank", 1e9))):
        if p["avatar_url"] and p["title"]:
            continue
        out += [k.split(":", 1)[1] for k in p["keys"] if k.startswith("slack:")][:1]
        if len(out) >= limit:
            break
    return out


def add_profiles(results: list[tuple]) -> int:
    """Keep the Slack users that a profile lookup returned. A profile call with no user id gives the user's own."""
    d = _read(PROFILES, {})
    users = d.setdefault("users", {})
    n = 0
    for tool, args, text in results:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        # Slack's MCP server wraps its text in JSON ({"result": "User ID: …"}); other servers answer in JSON objects
        found = [u for s in ([text] if data is None else connections._strings(data)) for u in parse_slack_users(s)]
        found += list(_json_users(data)) if data is not None else []
        for u in found:
            users[u["id"]] = {**users.get(u["id"], {}), **{k: v for k, v in u.items() if v not in ("", None)}}
            n += 1
        own = not any(isinstance(v, str) and _UID.fullmatch(v) for v in (args or {}).values())
        if found and own and "profile" in connections.words(tool):
            d["me"] = found[0]["id"]
    d["at"] = time.time()
    _write(PROFILES, d)
    return n


def slack_servers() -> list[dict]:
    """Chat connections that are connected and switched on, with a read tool for user profiles."""
    return [r for r in connections.servers() if connections.CHAT.search(f"{r['id']} {r.get('name', '')}")
            and r.get("status") == "connected" and settings.enabled(f"connections/{r['id']}")
            and any(t["read"] and connections.user_tool(t["name"]) for t in r.get("tools", []))]


def fetch_profiles(limit: int = 25) -> dict:
    """Look up the Slack profiles (picture, title) of the nearest people, with read-only user tools."""
    srv = slack_servers()
    if not srv:
        return {"error": "Switch on a Slack connection in Settings › Sources first.", "found": 0}
    ids = missing_profiles(limit)
    found = 0
    for r in srv:
        found += add_profiles(connections.lookup_users(r["id"], ids))
    return {"found": found, "asked": len(ids)}
