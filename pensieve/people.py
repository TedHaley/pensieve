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

from pensieve import config, connections, identity, org, orgstore, repos as repos_mod, settings, work

DIR = config.DATA_DIR / "people"
RAW = DIR / "raw.json"            # identities and interactions, by identity key (the slow part)
GRAPH = DIR / "graph.json"        # people, edges and profiles (resolved from RAW and the overrides)
OVERRIDES = DIR / "overrides.json"
ORG_EVIDENCE = DIR / "org_evidence.json"  # org evidence over all builds (orgstore.py)
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
    if email and "@" in email and not identity.placeholder(email):
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
        self.work = defaultdict(_new_work)
        self.me = set()
        self.me_names = set()
        self.me_sources = defaultdict(list)   # key -> how Pensieve knows that it is the user's
        self.extra = {}                       # GitHub teams, code owners and repo owners, for the org chart
        self.stats = Counter()

    def ident(self, src, name="", email="", login="", slack="", avatar="", title="", aliases=()):
        ks = _keys(name, email, login, slack)
        if ks:
            al = sorted({a.strip() for a in aliases if a and a.strip() and a.strip() != (name or "").strip()})
            self.ids.append(dict(keys=ks, name=name or "", email=(email or "").lower(), login=login or "",
                                 slack=slack or "", avatar=avatar or "", title=title or "", src=src,
                                 **({"aliases": al} if al else {})))
        return ks[0] if ks else None

    def mine(self, key, how):
        self.me.add(key)
        if how not in self.me_sources[key]:
            self.me_sources[key].append(how)

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
                    me_names=sorted(self.me_names), me_sources=dict(self.me_sources), **self.extra,
                    acts=[dict(kind=k, a=a, b=b, n=v["n"], last=v["last"],
                               refs=sorted(v["refs"], key=lambda r: -(r.get("ts") or 0))[:6])
                          for (k, a, b), v in self.acts.items()],
                    work={k: {**w, "exts": dict(w["exts"]), "channels": dict(w["channels"]),
                              "tasks": sorted(w["tasks"])[:40], "approved": dict(w["approved"]),
                              "approved_by": dict(w["approved_by"]),
                              "subjects": sorted(w["subjects"], key=lambda s: -s[0])[:12],
                              "prs": sorted(w["prs"], key=lambda p: -p["ts"])[:10],
                              "reviews": sorted(w["reviews"], key=lambda p: -p["ts"])[:10]}
                          for k, w in self.work.items()})


def _new_work():
    return {"repos": {}, "exts": Counter(), "subjects": [], "prs": [], "reviews": [], "channels": Counter(),
            "slack_last": 0, "messages": 0, "tasks": set(), "n_prs": 0, "n_reviews": 0, "approved": Counter(),
            "approved_by": Counter(), "owns": [], "blame": {}}


def _me_git(roots) -> tuple[dict, dict]:
    """The user's git emails and names, each with where it was found: the global config and each repo's own."""
    emails, names = defaultdict(list), defaultdict(list)
    for where, args in [("global", ["config", "--global"])] + [(Path(r).name, ["-C", r, "config"]) for r in roots]:
        for k, into in (("user.email", emails), ("user.name", names)):
            try:
                v = subprocess.run(["git", *args, k], capture_output=True, text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                v = ""
            if v and where not in into[v]:
                into[v].append(where)
    return emails, names


def _gh_accounts() -> list[dict]:
    """Every GitHub account that `gh` is logged in to (gh auth status, read only): [{login, host, active}]."""
    hit = _read(CACHE / "gh-accounts.json", {})
    if hit.get("at", 0) > time.time() - 86400:
        return hit.get("accounts", [])
    out = []
    if shutil.which("gh"):
        try:
            r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=20)
            text = r.stdout + "\n" + r.stderr
        except (OSError, subprocess.TimeoutExpired):
            text = ""
        cur = None
        for ln in text.splitlines():
            m = re.search(r"Logged in to (\S+) (?:account|as) ([\w-]+)", ln)
            if m:
                cur = dict(host=m.group(1), login=m.group(2), active=False)
                out.append(cur)
            elif cur and re.search(r"Active account:\s*true", ln):
                cur["active"] = True
    _write(CACHE / "gh-accounts.json", {"at": time.time(), "accounts": out})
    return out


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
    if hit.get("head") == head and hit.get("v") == 2:
        return hit
    out = repos_mod.git(root, "log", f"--since={GIT_DAYS}.days", "--no-merges", "-n", "5000", "--name-only",
                        "--format=%x1e%at%x1f%an%x1f%ae%x1f%(trailers:key=Co-authored-by,valueonly=true,"
                        "separator=%x1d)%x1f%s")
    authors, files = {}, defaultdict(Counter)
    file_last = defaultdict(dict)
    coauthors = {}
    for rec in out.split("\x1e")[1:]:
        head_line, _, names = rec.partition("\n")
        bits = head_line.split("\x1f", 4)
        if len(bits) < 5:
            continue
        ts, an, ae, trailers, subj = int(bits[0]), bits[1], bits[2].lower(), bits[3], bits[4]
        if is_bot(an, ae):
            continue
        a = authors.setdefault(ae, {"names": Counter(), "commits": 0, "last": 0, "areas": Counter(), "exts": Counter(),
                                    "subjects": [], "months": set()})
        a["names"][an] += 1
        a["commits"] += 1
        a["last"] = max(a["last"], ts)
        a["months"].add(time.strftime("%Y%m", time.gmtime(ts)))
        if len(a["subjects"]) < 8:
            a["subjects"].append([ts, subj])
        for t in trailers.split("\x1d"):
            m = re.match(r"\s*(.*?)\s*<([^<>\s]+@[^<>\s]+)>", t)
            if m and not is_bot(m.group(1), m.group(2)) and m.group(2).lower() != ae:
                c = coauthors.setdefault(m.group(2).lower(), {"name": m.group(1), "with": Counter(), "last": 0})
                c["with"][ae] += 1
                c["last"] = max(c["last"], ts)
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
    first = _first_commits(root, head, hit)
    res = dict(v=2, head=head, name=name, root=root, first=first,
               authors={e: {"name": a["names"].most_common(1)[0][0], "commits": a["commits"], "last": a["last"],
                            "first": first.get(e, 0), "months": sorted(a["months"]),
                            "names": [n for n, _ in a["names"].most_common(3)],
                            "areas": dict(a["areas"].most_common(8)), "exts": dict(a["exts"]), "subjects": a["subjects"]}
                        for e, a in authors.items()},
               coauthors={e: {"name": c["name"], "with": dict(c["with"]), "last": c["last"]} for e, c in coauthors.items()},
               pairs=[[x, y, p[0], p[1], [a for a, _ in p[2].most_common(3)]] for (x, y), p in pairs.items()])
    _write(cf, res)
    return res


def _first_commits(root: str, head: str, hit: dict) -> dict:
    """email -> the time of their first commit in the repo's whole history (tenure). After the first run, only the
    commits since the last HEAD are read."""
    first = dict(hit.get("first") or {})
    rng = [f"{hit['head']}..{head}"] if first and hit.get("head") else []
    out = repos_mod.git(root, "log", "--no-merges", "--format=%ae%x1f%at", *rng)
    for ln in out.splitlines():
        e, _, ts = ln.partition("\x1f")
        if ts.isdigit():
            e = e.lower()
            first[e] = min(first.get(e) or int(ts), int(ts))
    return first


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
            k = raw.ident("git", name=a["name"], email=email, aliases=a.get("names", []))
            if not k:
                continue
            w = raw.work[k]
            w["repos"][name] = {"commits": a["commits"], "last": a["last"], "areas": a["areas"],
                                "first": a.get("first") or 0, "months": a.get("months", [])}
            w["exts"].update(a["exts"])
            w["subjects"] += [[ts, name, s] for ts, s in a["subjects"]]
        for x, y, n, last, areas in g["pairs"]:
            raw.act("code", "email:" + x, "email:" + y, last, n=n,
                    ref={"t": f"{name}: {', '.join(areas)}", "ts": last, "repo": name, "areas": areas})
        # a Co-authored-by trailer ties a name to an email (an identity), and the co-author to the commit's author
        for email, c in g.get("coauthors", {}).items():
            k = raw.ident("git", name=c["name"], email=email)
            for other, n in c["with"].items():
                raw.act("code", "email:" + other, k, c["last"], n=n,
                        ref={"t": f"{name}: co-authored commits", "ts": c["last"], "repo": name, "areas": []})


def _gh_slug(remote: str) -> str:
    """owner/name of a GitHub remote: the repos table keeps it as github.com/owner/name; a git remote URL works too."""
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", remote or "")
    return m.group(1) if m else ""


def _gh_repo(slug: str) -> list[dict]:
    """The last PR_LIMIT pull requests of a GitHub repo through `gh` (read only): author, reviewers and commit
    authors. Cached for GH_TTL."""
    cf = CACHE / f"gh-{_hash(slug)}.json"
    hit = _read(cf, {})
    if hit.get("at", 0) > time.time() - GH_TTL and hit.get("v") == 2:
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
                approvers=sorted({(rv.get("author") or {}).get("login", "") for rv in p.get("reviews") or []
                                  if rv.get("state") == "APPROVED"} - {""}),
                commit_authors=list({(c.get("login") or c.get("email") or ""): dict(
                    login=c.get("login") or "", email=c.get("email") or "", name=c.get("name") or "")
                    for cm in authors.get(p.get("number"), []) for c in cm.get("authors") or []}.values())))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        prs = hit.get("prs", [])  # kept, and not asked again before GH_TTL
    _write(cf, {"at": time.time(), "prs": prs, "v": 2})
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
    log = raw.extra.setdefault("pr_log", [])  # each pull request's people, for the org evidence store (orgstore.py)
    for name, prs in results:
        for p in prs:
            raw.stats["prs"] += 1
            a = p["author"]
            if not a["login"] or a["bot"] or is_bot(a["name"], login=a["login"]):
                continue
            if p.get("url"):
                log.append(dict(url=p["url"], repo=name, ts=p["ts"], author=a["login"].lower(),
                                reviewers=[x.lower() for x in p["reviewers"] if x != a["login"] and not is_bot(login=x)],
                                approvers=[x.lower() for x in p.get("approvers") or []
                                           if x != a["login"] and not is_bot(login=x)]))
            ak = raw.ident("github", name=a["name"], login=a["login"],
                           avatar=f"https://github.com/{a['login']}.png?size=128")
            for c in p["commit_authors"]:
                if c["login"] and not is_bot(c["name"], c["email"], c["login"]):
                    raw.ident("github", name=c["name"] if c["name"] != c["login"] else "", email=c["email"],
                              login=c["login"])
            ref = {"t": p["title"], "url": p["url"], "ts": p["ts"], "state": p["state"], "repo": name}
            raw.work[ak]["prs"].append(dict(ref))
            raw.work[ak]["n_prs"] += 1
            approvers = set(p.get("approvers") or [])
            for rv in p["reviewers"]:
                if rv == a["login"] or is_bot(login=rv):
                    continue
                rk = raw.ident("github", login=rv, avatar=f"https://github.com/{rv}.png?size=128")
                raw.work[rk]["reviews"].append({**ref, "author": a["login"]})
                raw.work[rk]["n_reviews"] += 1
                if rv in approvers:
                    raw.work[rk]["approved"][a["login"].lower()] += 1
                    raw.work[ak]["approved_by"][rv.lower()] += 1
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
        name = f.get("Real Name") or f.get("Name") or f.get("Display Name") or ""
        disp = f.get("Display Name", "")
        out.append(dict(id=uid, name=name, title=f.get("Title", ""), email=f.get("Email", "").lower(),
                        avatar=pic.group(1) if pic else "", bot=f.get("Bot", "").lower() == "yes",
                        display=disp if disp != name else ""))
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
                       avatar=prof.get("image_192") or prof.get("image_72") or "", bot=bool(x.get("is_bot")),
                       display=prof.get("display_name") or "")
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
    """(doc:<id>, raw item, is private) for each collected message or thread of a source that is switched on. The
    `documents` table gives them by kind; no file is read."""
    from pensieve import store as v2
    from pensieve.store import documents
    for d in documents.of_kinds(v2.store_db(), ("message", "thread"), connections.indexed(settings.enabled)):
        try:
            raw = json.loads(d.raw)
        except ValueError:
            continue
        if not connections.CHAT.search(f"{d.source_id} {raw.get('_tool', '')}"):
            continue
        yield f"doc:{d.id}", raw, d.private


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
        for k in ("name", "title", "email", "avatar", "display"):
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
    key_of = {uid: raw.ident("slack", name=u["name"], email=u["email"], slack=uid, avatar=u["avatar"], title=u["title"],
                             aliases=[u.get("display", "")])
              for uid, u in users.items() if not u.get("bot")}
    if prof.get("me"):
        raw.mine("slack:" + prof["me"], "your own Slack profile")
        if users.get(prof["me"], {}).get("email"):
            raw.mine("email:" + users[prof["me"]]["email"].lower(), "the email on your Slack profile")
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
        def mine(p):
            """When this person took part in the task: their newest commit in it, else the newest ticket they are
            assigned, else 0 (no time). Not the task's own update time: that is anyone's, and collecting new items
            moves it to now."""
            return max([c["ts"] for c in t.get("commits", []) if c.get("author") == p] +
                       [x.get("updated") or 0 for x in t.get("tickets", []) if x.get("assignee") == p] + [0])
        for p in t.get("people", []):
            if is_bot(p, p if "@" in p else ""):
                continue
            k = raw.ident("task", email=p) if "@" in p else raw.ident("task", name=p)
            if k and t.get("id"):
                raw.work[k]["tasks"].add(t["id"])
            when = mine(p)
            raw.act("task", "me", k, when, ref={"t": t.get("title", ""), "id": t.get("id"), "ts": when})
        for it in t.get("items", []):
            for k in slack_by_item.get(it.get("path"), ()):
                when = it.get("updated") or 0  # the message's own time
                raw.act("task", "me", k, when, ref={"t": t.get("title", ""), "id": t.get("id"), "ts": when})
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
    for e, where in emails.items():
        raw.mine("email:" + e.lower(), "git config user.email (" + ", ".join(where[:3]) + ")")
    for n, where in names.items():
        raw.me_names.add(n)
        if norm_name(n):
            raw.mine("name:" + norm_name(n), "git config user.name (" + ", ".join(where[:3]) + ")")
    login = _gh_login()
    if login:
        raw.mine("gh:" + login.lower(), "the active gh account")
    for acc in _gh_accounts():
        if acc["host"] == "github.com":
            raw.mine("gh:" + acc["login"].lower(), "gh auth status" + (" (active account)" if acc["active"] else ""))
    raw.extra["repo_owner"] = {name: _gh_slug(remote).split("/")[0] for _root, name, remote in repo_rows
                               if _gh_slug(remote)}
    _collect_git(raw, repo_rows)
    _collect_github(raw, repo_rows, {e.lower() for e in emails})
    by_item = _collect_slack(raw)
    _collect_tasks(raw, store, by_item)
    _collect_directory(raw)
    _collect_owners(raw, repo_rows)
    _collect_blame(raw, repo_rows)
    d = raw.dump()
    _write(RAW, d)
    return d


blame_fn = None   # the server sets it: repo name -> [{author: 'Name <email>', share, lines, dirs}] (git blame)


def _collect_blame(raw: _Raw, repo_rows):
    """Each author's share of the lines of each repo (git blame, from the index): ownership, for the org chart."""
    if not blame_fn:
        return
    for _root, name, _remote in repo_rows:
        try:
            rows = blame_fn(name) or []
        except Exception:
            continue
        for r in rows:
            m = re.search(r"<([^<>\s]+@[^<>\s]+)>", r.get("author", ""))
            if m and r.get("share", 0) >= 0.01:
                raw.work["email:" + m.group(1).lower()]["blame"][name] = round(r["share"], 4)


def _collect_directory(raw: _Raw):
    """The people that Find a person found and kept (directory.json): more evidence of which identities are one
    person (a Slack profile's emails, a GitHub login). Records that join no one are not people of the graph."""
    for r in _read(DIR / "directory.json", {}).get("people", []):
        common = dict(name=r.get("name", ""), login=r.get("login", ""), slack=r.get("slack", ""),
                      avatar=r.get("avatar_url", ""), title=r.get("title", ""))
        for e in (r.get("emails") or [""])[:4]:
            raw.ident("directory", email=e, **common)


_OWNER_FILES = ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")
MAX_TEAMS = 40


def _collect_owners(raw: _Raw, repo_rows):
    """Code owners: the CODEOWNERS file of each repo (people, emails and GitHub teams), and the members of those
    teams through `gh` (GET only, cached for a day)."""
    owners, teams = {}, Counter()
    team_repos = defaultdict(Counter)
    for root, name, _remote in repo_rows:
        f = next((Path(root) / p for p in _OWNER_FILES if (Path(root) / p).is_file()), None)
        if not f:
            continue
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        cnt = Counter()
        for ln in text.splitlines():
            ln = ln.split("#", 1)[0].split()
            for o in ln[1:]:
                cnt[o.lower()] += 1
        owners[name] = dict(cnt.most_common(200))
        for o, n in cnt.items():
            if o.startswith("@") and "/" in o:
                teams[o[1:]] += n
                team_repos[o[1:]][name] += n
            elif o.startswith("@"):
                raw.work["gh:" + o[1:]]["owns"].append([name, n])
            elif "@" in o:
                raw.work["email:" + o]["owns"].append([name, n])
    raw.extra["codeowners"] = owners
    got = {}
    if teams and shutil.which("gh"):
        with ThreadPoolExecutor(4) as pool:
            for slug, t in zip([s for s, _ in teams.most_common(MAX_TEAMS)],
                               pool.map(_gh_team, [s for s, _ in teams.most_common(MAX_TEAMS)])):
                if t:
                    got[slug] = {**t, "paths": teams[slug], "repos": dict(team_repos[slug])}
    raw.extra["teams"] = got
    raw.stats["gh_teams"] = len(got)


def _gh_team(slug: str) -> dict | None:
    """A GitHub team ('org/team'): its name, parent team, members and maintainers. Read only; cached for a day."""
    cf = CACHE / f"gh-team-{_hash(slug)}.json"
    hit = _read(cf, {})
    if hit.get("at", 0) > time.time() - 86400:
        return hit.get("team")
    org_, _, team = slug.partition("/")

    def get(path):
        r = subprocess.run(["gh", "api", "--method", "GET", path], capture_output=True, text=True, timeout=30)
        if r.returncode:
            raise OSError(r.stderr[:200])
        return json.loads(r.stdout or "null")
    try:
        info = get(f"orgs/{org_}/teams/{team}")
        members = get(f"orgs/{org_}/teams/{team}/members?per_page=100")
        maint = get(f"orgs/{org_}/teams/{team}/members?role=maintainer&per_page=100")
        res = dict(name=info.get("name") or team, parent=(info.get("parent") or {}).get("slug", ""),
                   members=sorted({m["login"].lower() for m in members or [] if m.get("login")}),
                   maintainers=sorted({m["login"].lower() for m in maint or [] if m.get("login")}))
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError, TypeError):
        res = hit.get("team")
    _write(cf, {"at": time.time(), "team": res})
    return res


def overrides() -> dict:
    o = _read(OVERRIDES, {})
    return {"merge": o.get("merge", []), "split": o.get("split", []), "me": o.get("me", [])}


def _set_overrides(fn):
    with _lock:
        o = overrides()
        fn(o)
        _write(OVERRIDES, o)


def _stems(raw: dict) -> list[str]:
    """Company names that logins and email names carry ('sam-example'): the stems of the work email domains that
    three or more people use, and the GitHub owners of the repos."""
    doms = Counter(identity.domain(ob["email"]) for ob in raw["ids"] if identity.is_work_mail(ob.get("email", "")))
    st = {d.split(".")[0] for d, n in doms.items() if n >= 3} | set((raw.get("repo_owner") or {}).values())
    return sorted({identity.fold(s) for s in st if len(s) >= 3}, key=len, reverse=True)


def _stable_ids(groups: dict, prev: dict | None, now: float) -> dict:
    """root -> (person id, aliases, since). A person keeps the id that they had in the last build: when two people
    join, the older id stays and the other becomes an alias; when one splits, the part with most of the old keys
    keeps it. A new person's id is the hash of their best key."""
    prev_people = (prev or {}).get("people", {})
    prev_built = (prev or {}).get("built", now)
    prev_of = {}
    for pid, p in prev_people.items():
        for k in p["keys"]:
            prev_of[k] = pid
    overlap = defaultdict(Counter)
    for root, keys in groups.items():
        for k in keys:
            if k in prev_of:
                overlap[prev_of[k]][root] += 1
    owned = defaultdict(list)
    for pid, roots in overlap.items():
        owned[roots.most_common(1)[0][0]].append(pid)
    out, used = {}, set()
    for root, keys in groups.items():
        olds = sorted(owned.get(root, []), key=lambda q: (prev_people[q].get("since") or prev_built, q))
        if olds:
            pid = olds[0]
            aliases = sorted(set(olds[1:]) | {a for q in olds for a in prev_people[q].get("aliases", [])} - {pid})
            since = prev_people[pid].get("since") or prev_built
        else:
            ranked = sorted(keys, key=_key_rank)
            pid = next(("p" + _hash(k) for k in ranked if "p" + _hash(k) not in prev_people), "p" + _hash(ranked[0]))
            aliases, since = [], now
        while pid in used:
            pid = "p" + _hash(pid)
        used.add(pid)
        out[root] = (pid, aliases, since)
    return out


def _best_name(obs, first: str) -> str:
    names = Counter()
    for src, n in {(ob["src"], (ob["name"] or "").strip()) for ob in obs}:  # one vote per source, not per repo
        if n:
            # a one-word name is often a login or a git shorthand; people know each other by the Slack name
            names[n] += {"slack": 3, "github": 2, "directory": 2}.get(src, 1) * (3 if len(n.split()) > 1 else 1)
    return names.most_common(1)[0][0] if names else first.split(":", 1)[-1].split("@")[0]


def resolve(raw: dict, ovr: dict, prev: dict | None = None) -> dict:
    """People from the raw identities (see identity.py: facts, then scored evidence, then the user's own merges and
    keys). `prev` is the last graph, so that ids stay the same from one build to the next."""
    now = time.time()
    me_keys = set(raw.get("me", [])) | set(ovr["me"])
    ids = []
    for ob in raw["ids"]:  # a raw file from an older build can hold placeholder emails
        ks = [k for k in ob["keys"] if not (k.startswith("email:") and identity.placeholder(k[6:]))]
        if ks:
            ids.append({**ob, "keys": ks} if len(ks) < len(ob["keys"]) else ob)
    raw = {**raw, "ids": ids}
    R = identity.resolve_keys(raw, ovr, _stems(raw))
    uf, mine = R["uf"], R["mine"]
    groups = defaultdict(set)
    for k in list(uf.p):
        groups[uf.find(k)].add(k)
    if mine:
        me_root = uf.find(mine[0])
    else:  # nothing seen of the user yet: a node of their own
        me_root = sorted(me_keys, key=_key_rank)[0] if me_keys else "me:you"
        groups[me_root] = set(me_keys) or {me_root}
    obs_of = defaultdict(list)
    for ob, a in zip(raw["ids"], R["anchors"]):
        obs_of[uf.find(a)].append(ob)
    for root in [r for r in groups if r != me_root]:
        obs = obs_of.get(root, [])
        if obs and all(ob["src"] == "directory" for ob in obs):
            del groups[root]  # found by a search only: no work with anyone yet
    ids = _stable_ids(groups, prev, now)
    joins_of = defaultdict(list)
    for j in R["joins"]:
        joins_of[uf.find(j["a"])].append(j)

    people, pid_of = {}, {}
    for root, keys in groups.items():
        obs = obs_of.get(root, [])
        first = sorted(keys, key=_key_rank)[0]
        pid, aliases, since = ids[root]
        avatar = next((ob["avatar"] for ob in obs if ob["src"] == "slack" and ob["avatar"]), "") or \
            next((ob["avatar"] for ob in obs if ob["avatar"]), "")
        people[pid] = dict(id=pid, name=_best_name(obs, first), me=root == me_root, keys=sorted(keys, key=_key_rank),
                           title=next((ob["title"] for ob in obs if ob["title"]), ""), avatar_url=avatar,
                           sources=sorted({ob["src"] for ob in obs} - {"directory"}) or sorted({ob["src"] for ob in obs}),
                           aliases=aliases, since=since, joins=joins_of.get(root, []))
        for k in keys:
            pid_of[k] = pid
    me_id = next(p["id"] for p in people.values() if p["me"])
    pid_of["me"] = me_id
    maybe = defaultdict(list)
    for m in R["maybe"]:
        pa, pb = pid_of.get(m["a"]), pid_of.get(m["b"])
        if pa and pb and pa != pb:
            maybe[pa].append({**m, "id": pb})
            maybe[pb].append({**m, "id": pa})

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

    work_of = {pid: _merge_work(p["keys"], raw["work"]) for pid, p in people.items()}
    via = _my_channels(raw, people[me_id], pid_of)
    profiles = {pid: _profile(p, work_of, edges, me_id, people) for pid, p in people.items()}
    for pid, p in people.items():
        p["last"] = profiles[pid]["last"]
        p["function"] = profiles[pid]["function"]
    ev = orgstore.load(ORG_EVIDENCE)
    new_build = orgstore.merge(ev, raw, now, org.is_manager_title)
    chart = org.infer(people, profiles, work_of, edges, raw, ev)
    orgstore.add_teams(ev, chart["teams"], lambda q: people[q]["keys"][0] if people[q]["keys"] else "", now, new_build)
    orgstore.save(ORG_EVIDENCE, ev)
    chart["evidence"] = orgstore.summary(ev)
    for pid, pr in profiles.items():
        pr["joined_because"] = [_join_view(j) for j in people[pid]["joins"]]
        pr["maybe"] = [dict(id=m["id"], name=people[m["id"]]["name"], reason=m["reason"], score=m["score"],
                            evidence=m["evidence"]) for m in sorted(maybe.get(pid, []), key=lambda m: -m["score"])[:3]]
        pr.update(org.card(chart, pid))
        if pid == me_id:
            pr["profiles"] = _my_profiles(people[me_id], raw, via, people)
        elif pr.get("relation") is not None:
            pr["relation"]["via"] = _via_for(pid, via, people[me_id], raw)
    joins = [j for p in people.values() for j in p["joins"]]
    stats = {**raw.get("stats", {}), "identities": len(raw["ids"]), "people": len(people),
             "joins": dict(Counter(j["kind"] for j in joins)), "rejected": len(R["rejected"]), "maybe": len(R["maybe"])}
    return dict(me=me_id, built=raw.get("built", now), resolved=now, stats=stats,
                people=people, profiles=profiles, org=chart, rejected=R["rejected"][:200],
                edges=[{"a": e["a"], "b": e["b"], "w": e["w"], "kinds": dict(e["kinds"]), "last": e["last"]}
                       for e in sorted(edges.values(), key=lambda e: -e["w"])[:5000]],
                me_edges={(e["b"] if e["a"] == me_id else e["a"]): {k: v[:8] for k, v in e["refs"].items()}
                          for e in edges.values() if me_id in (e["a"], e["b"])})


def _merge_work(keys, work_by_key) -> dict:
    out = {"repos": {}, "exts": Counter(), "subjects": [], "prs": [], "reviews": [], "channels": Counter(),
           "slack_last": 0, "messages": 0, "n_prs": 0, "n_reviews": 0, "approved": Counter(), "approved_by": Counter(),
           "owns": Counter(), "blame": {}, "first": 0, "tasks": set()}
    for k in keys:
        w = work_by_key.get(k)
        if not w:
            continue
        for r, v in w["repos"].items():
            cur = out["repos"].setdefault(r, {"commits": 0, "last": 0, "areas": Counter(), "first": 0})
            cur["commits"] += v["commits"]
            cur["last"] = max(cur["last"], v["last"])
            cur["areas"].update(v["areas"])
            if v.get("first"):
                cur["first"] = min(cur["first"] or v["first"], v["first"])
                out["first"] = min(out["first"] or v["first"], v["first"])
        out["exts"].update(w["exts"])
        out["subjects"] += w["subjects"]
        out["prs"] += w["prs"]
        out["reviews"] += w["reviews"]
        out["channels"].update(w["channels"])
        out["slack_last"] = max(out["slack_last"], w["slack_last"])
        out["messages"] += w["messages"]
        out["n_prs"] += w.get("n_prs", 0)
        out["n_reviews"] += w.get("n_reviews", 0)
        out["approved"].update(w.get("approved") or {})
        out["approved_by"].update(w.get("approved_by") or {})
        for repo, n in w.get("owns") or []:
            out["owns"][repo] += n
        for repo, s in (w.get("blame") or {}).items():
            out["blame"][repo] = round(out["blame"].get(repo, 0) + s, 4)
        out["tasks"] |= set(w.get("tasks") or [])
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


def _profile(p, work_of, edges, me_id, people) -> dict:
    w = work_of[p["id"]]
    fn, inferred = _function(p["title"], w["exts"])
    repos = sorted(({"repo": r, "commits": v["commits"], "last": v["last"],
                     "areas": [a for a, _ in v["areas"].most_common(4)]} for r, v in w["repos"].items()),
                   key=lambda r: -r["commits"])
    peers = sorted(((e["w"], e["b"] if e["a"] == p["id"] else e["a"]) for e in edges.values()
                    if p["id"] in (e["a"], e["b"]) and me_id not in (e["a"], e["b"])), reverse=True)
    last = max([r["last"] for r in repos] + [x["ts"] for x in w["prs"] + w["reviews"]] + [w["slack_last"], 0])
    rel = None
    if p["id"] != me_id:
        mine = work_of[me_id]
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


def _key_view(k: str) -> str:
    kind, _, v = k.partition(":")
    return {"email": "email ", "gh": "GitHub @", "slack": "Slack ", "name": "name "}.get(kind, kind + " ") + v


def _join_view(j: dict) -> dict:
    return dict(a=j["a"], b=j["b"], a_label=_key_view(j["a"]), b_label=_key_view(j["b"]), kind=j["kind"],
                reason=j["reason"], score=j["score"], evidence=j["evidence"])


def _my_channels(raw: dict, me: dict, pid_of: dict) -> dict:
    """The user's profile key -> kind of edge -> other person -> count: through which of the user's identities each
    edge to another person runs. Direct messages run through the user's Slack, tasks through the Context tab."""
    mine = set(me["keys"])
    slack = next((k for k in me["keys"] if k.startswith("slack:")), "slack:")
    via = defaultdict(lambda: defaultdict(Counter))
    for a in raw["acts"]:
        for mk, ok in ((a["a"], a["b"]), (a["b"], a["a"])):
            if mk != "me" and mk not in mine:
                continue
            po = pid_of.get(ok)
            if not po or po == me["id"]:
                continue
            pk = mk if mk != "me" else (slack if a["kind"] == "dm" else "tasks:")
            via[pk][a["kind"]][po] += a["n"]
    return via


def _profile_label(k: str, raw: dict) -> tuple[str, str]:
    """(label, kind) of one of the user's identities: 'work GitHub @sam', 'personal email', 'Slack'."""
    kind, _, v = k.partition(":")
    if kind == "gh":
        w = raw.get("work", {}).get(k) or {}
        owners = raw.get("repo_owner") or {}
        repos = {x.get("repo") for x in (w.get("prs") or []) + (w.get("reviews") or [])}
        orgs_ = {owners.get(r, "").lower() for r in repos if owners.get(r)}
        if orgs_ - {v.lower()}:
            return f"work GitHub @{v}", "github"
        if v.lower() in {o.lower() for o in owners.values()} or not orgs_:
            return f"personal GitHub @{v}", "github"
        return f"GitHub @{v}", "github"
    if kind == "email":
        return {"work": "work email", "personal": "personal email", "noreply": "GitHub noreply email"}[
            identity.mail_kind(v)] + " " + v, "email"
    if kind == "slack":
        return "Slack", "slack"
    if kind == "tasks":
        return "your tasks (Context tab)", "tasks"
    return _key_view(k), kind


def _my_profiles(me: dict, raw: dict, via: dict, people: dict) -> list[dict]:
    """The user's card: each of the user's identities, how Pensieve knows it is theirs, and through which channels it
    connects them to which people."""
    joined = {}
    for j in me["joins"]:
        joined.setdefault(j["b"], j["reason"])
        joined.setdefault(j["a"], j["reason"])
    out = []
    for k in [k for k in me["keys"] if not k.startswith("name:")] + ["tasks:"]:
        label, kind = _profile_label(k, raw)
        chans = []
        for ek, ppl in sorted(via.get(k, {}).items(), key=lambda kv: -sum(kv[1].values())):
            top = sorted(ppl.items(), key=lambda kv: -kv[1])
            chans.append(dict(kind=ek, label=KIND_LABEL.get(ek, ek), n=sum(ppl.values()), people=len(ppl),
                              top=[dict(id=o, name=people[o]["name"]) for o, _ in top[:4] if o in people]))
        w = raw.get("work", {}).get(k) or {}
        if k == "tasks:" and not chans:
            continue
        out.append(dict(key=k, label=label, kind=kind, channels=chans,
                        how=list(raw.get("me_sources", {}).get(k) or ([joined[k]] if k in joined else [])),
                        commits=sum(v.get("commits", 0) for v in (w.get("repos") or {}).values()),
                        prs=w.get("n_prs", 0), reviews=w.get("n_reviews", 0)))
    return sorted(out, key=lambda x: (-sum(c["n"] for c in x["channels"]), x["label"]))


def _via_for(pid: str, via: dict, me: dict, raw: dict) -> list[dict]:
    """Another person's card: which of the user's identities connect the user to them, and how."""
    out = []
    for k, kinds in via.items():
        ks = [dict(kind=ek, label=KIND_LABEL.get(ek, ek), n=ppl[pid]) for ek, ppl in kinds.items() if ppl.get(pid)]
        if ks:
            out.append(dict(key=k, label=_profile_label(k, raw)[0], kinds=sorted(ks, key=lambda x: -x["n"])))
    return sorted(out, key=lambda x: -sum(y["n"] for y in x["kinds"]))


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
            g = resolve(collect(store, repo_rows_fn()), overrides(), graph())
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
        _publish(resolve(raw, overrides(), graph()))


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
    if g and pid not in g["profiles"]:
        pid = current_id(pid) or pid
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


REL_PEERS = 8
REL_COLLABS = 6


def relations(pid: str) -> dict | None:
    """GET /api/people/<id>/relations: the person's nearest org around them, each link with its inferred relation,
    confidence and evidence. Managers and reports come from the org chart's reporting lines, peers from the same
    inferred team, and collaborators are the strongest graph edges outside those."""
    g = graph()
    if g and pid not in g["people"]:
        pid = current_id(pid) or pid
    if not g or pid not in g["people"]:
        return None
    ppl, ch, me = g["people"], g.get("org") or {}, g["me"]
    ops = ch.get("people", {})
    op = ops.get(pid, {})
    teams = {t["id"]: t for t in ch.get("teams", [])}
    team = teams.get(op.get("team"))
    first = "your" if pid == me else ppl[pid]["name"].split()[0] + "'s"
    who = "you" if pid == me else ppl[pid]["name"].split()[0]

    def node(q, relation, label, conf, evidence, **extra):
        p, o = ppl[q], ops.get(q, {})
        return dict(id=q, name=p["name"], me=p["me"], function=p.get("function", ""), title=p["title"],
                    avatar=bool(p["avatar_url"] and _avatar_file(p["avatar_url"])), level=o.get("level"),
                    level_confidence=o.get("level_confidence", 0), relation=relation, label=label,
                    confidence=round(conf or 0, 2), evidence=evidence, **extra)

    edges = {}   # the other person -> the graph edge between them
    for e in g["edges"]:
        if pid in (e["a"], e["b"]):
            o = e["b"] if e["a"] == pid else e["a"]
            if o in ppl:
                edges[o] = e
    wmax = max((e["w"] for e in edges.values()), default=0) or 1
    rv = ch.get("reviews") or {}

    def why_edge(q):
        e = edges.get(q)
        if not e:
            return []
        out = [f"{KIND_LABEL.get(k, k)}: {n:,}" for k, n in sorted(e["kinds"].items(), key=lambda kv: -kv[1])]
        if e.get("last"):
            out.append("last together " + time.strftime("%Y-%m-%d", time.localtime(e["last"])))
        return out

    def link_label(q):
        """The strongest kind of work between them, said from the selected person's side."""
        e = edges.get(q)
        theirs, mine = rv.get(q, {}).get(pid, 0), rv.get(pid, {}).get(q, 0)
        if theirs or mine:
            if theirs >= mine:
                return f"reviews {first} PRs" + (f" · {theirs}" if theirs > 1 else ""), theirs + mine
            return f"{who} review{'' if pid == me else 's'} their PRs" + (f" · {mine}" if mine > 1 else ""), theirs + mine
        if not e:
            return "", 0
        k = max(e["kinds"], key=lambda k: KIND_WEIGHT.get(k, 1) * math.log1p(e["kinds"][k]))
        return {"code": "works on the same files", "pr": "pull request reviews", "task": "shares tasks",
                "thread": "talks in Slack threads", "dm": "direct messages", "channel": "same Slack channels"}.get(k, k), 0

    up = [x for x in ch.get("edges", []) if x["report"] == pid and x["manager"] in ppl][:3]
    down = [x for x in ch.get("edges", []) if x["manager"] == pid and x["report"] in ppl][:12]
    managers = [node(x["manager"], "manager", f"manager · inferred {x['confidence']:.0%}", x["confidence"],
                     x["evidence"]) for x in up]
    reports = [node(x["report"], "report", f"report · inferred {x['confidence']:.0%}", x["confidence"],
                    x["evidence"]) for x in down]
    taken = {pid} | {x["manager"] for x in up} | {x["report"] for x in down}
    peers = []
    if team:
        mates = [q for q in team["members"] if q not in taken and q in ppl]
        mates.sort(key=lambda q: (-(edges[q]["w"] if q in edges else 0), ppl[q].get("rank", 1e9)))
        for q in mates[:REL_PEERS]:
            conf = min(op.get("team_confidence", 0), ops.get(q, {}).get("team_confidence", 0))
            lab, _ = link_label(q)
            peers.append(node(q, "peer", "peer · same team", conf,
                              [f"both in the inferred team {team['name']}"] + team.get("evidence", [])[:2] + why_edge(q),
                              also=lab))
        taken |= set(mates[:REL_PEERS]) | set(team["members"])
    others = sorted((q for q in edges if q not in taken), key=lambda q: -edges[q]["w"])[:REL_COLLABS]
    collabs = []
    for q in others:
        lab, _ = link_label(q)
        collabs.append(node(q, "collaborator", lab or "works together", edges[q]["w"] / wmax, why_edge(q)))
    p = ppl[pid]
    center = dict(id=pid, name=p["name"], me=p["me"], function=p.get("function", ""), title=p["title"],
                  avatar=bool(p["avatar_url"] and _avatar_file(p["avatar_url"])), level=op.get("level"),
                  level_confidence=op.get("level_confidence", 0), level_evidence=op.get("level_evidence", []))
    return dict(id=pid, center=center, managers=managers, peers=peers, reports=reports, collaborators=collabs,
                team=dict(id=team["id"], name=team["name"], size=team["size"], confidence=op.get("team_confidence", 0),
                          evidence=op.get("team_evidence", [])) if team else None,
                evidence=ch.get("evidence") or {}, note="Inferred from the work Pensieve can see. Not an HR record.")


def org_chart() -> dict:
    """GET /api/people/org: inferred teams, each person's function, seniority and team with confidence and
    evidence, and inferred manager -> report edges. Each node has what the UI needs to draw it."""
    g = graph()
    if not g or not g.get("org"):
        return dict(teams=[], people={}, edges=[], leaders=[], building=_state["building"], me=g and g.get("me"))
    ch = g["org"]
    ppl = {}
    for pid, op in ch["people"].items():
        p = g["people"].get(pid)
        if not p:
            continue
        ppl[pid] = {**op, "me": p["me"], "rank": p.get("rank"), "closeness": p.get("closeness", 0), "title": p["title"],
                    "avatar": bool(p["avatar_url"] and _avatar_file(p["avatar_url"]))}
    return dict(me=g["me"], built=g["built"], note=ch.get("note", ""), teams=ch["teams"], people=ppl,
                edges=ch["edges"], leaders=ch.get("leaders", []), evidence=ch.get("evidence") or {},
                building=_state["building"])


def _uniq(refs, key):
    seen, out = set(), []
    for r in sorted(refs, key=lambda r: -(r.get("ts") or 0)):
        k = r.get(key) or r.get("t")
        if k and k not in seen:
            seen.add(k)
            out.append(r)
    return out


def current_id(pid: str) -> str | None:
    """The id that a person has now, for an id from an older build. An id is the hash of the person's first key, so
    it changes when a better key joins them (a Slack profile with their email): the old id is the hash of one of
    their keys now."""
    g = graph()
    if not g:
        return None
    if pid in g["people"]:
        return pid
    return next((p["id"] for p in g["people"].values() if pid in p.get("aliases", [])), None) or \
        next((p["id"] for p in g["people"].values() if any("p" + _hash(k) == pid for k in p["keys"])), None)


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
