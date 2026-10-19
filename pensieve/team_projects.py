"""The Projects tab: the work around the user, as clusters of related content and the project each one is.

A project is a cluster of the items that changed recently: code files (main checkouts), documents and agent sessions,
grouped by meaning like the Map's topics, but finer and only among recent work. Each cluster then collects:
- members: the git authors of its files (commits over GIT_DAYS, each commit split over the clusters of its files),
  the user for its agent sessions, and the people of the context cards (tasks) and pull requests that belong to it;
- pull requests and tickets: from the context cards whose sessions and files are in it, and otherwise the nearest
  cluster by meaning (pull requests from the People graph, tickets from the connections);
- the user's own share of its activity.

Relevance is how much the people closest to the user (People closeness) work on a project, plus the user's own work,
each weighted by recency. The nearest projects form the "near you" band; the rest are further away.

The build runs in a background thread, at most every REBUILD_SECONDS. Git logs are cached per repo until its HEAD
moves. Clusters are kept while the set of recent items changes by less than a sixth, so names stay put (new items
join the nearest cluster); AI names are cached by membership like the Map's topics (meta 'dscope:<sig>').

The build, its cluster centers and the git logs are in index.db (pensieve/store/projects.py)."""
import hashlib
import io
import json
import math
import re
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from pensieve import repos as repos_mod, store as v2, work
from pensieve.store import cache as cache_db, projects as projects_db

VERSION = 1
GIT_TTL = 30 * 86400       # a git log is checked against its HEAD; the row expires only for the purge

REBUILD_SECONDS = 1800
PARTIAL_SECONDS = 180
ACTIVE_DAYS = 120          # an item that changed within this many days is current work
GIT_DAYS = 180
HALF_LIFE_DAYS = 45
MAX_ITEMS = 15000
COMMIT_FILES_MAX = 120     # a commit that touches more files is a sweep (renames, formatting), not one project's work
RECLUSTER_SHARE = 1 / 6    # the recent set changed by more than this: cluster again
RECLUSTER_SECONDS = 7 * 86400
NEAR_MAX, NEAR_MIN_SCORE = 10, 0.15
TEAM_SHARE = 0.6           # of relevance: the closeness-weighted work of others; the rest is the user's own work
REPO_WEIGHT = 0.6          # how strongly clusters keep to one repo (a feature added to each item's vector)
PR_MIN_SIM = 0.30         # a pull request or ticket joins the nearest cluster only when this similar

_ID_WORD = re.compile(r"[0-9a-f]*\d[0-9a-f]*")  # a piece of a hash or an id in a path names nothing
_lock = threading.RLock()
_state = {"data": None, "building": False, "error": None, "started": 0.0}
listeners = []             # called (from a worker thread) when a new build is ready
_vec_cache = {}            # text -> unit vector (titles of pull requests and tickets)


def _t(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    return work._ts(v) if v else 0.0


def _decay(ts: float, now: float) -> float:
    return 0.5 ** (max(0.0, now - ts) / 86400 / HALF_LIFE_DAYS) if ts else 0.0


def _hash(s: str, n=12) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:n]



# ---- git --------------------------------------------------------------------------------------------------------
def _git_log(root: str) -> dict:
    """The commits of the last GIT_DAYS in one repo: [ts, email, name, subject, files]. Cached until HEAD moves."""
    head = repos_mod.git(root, "rev-parse", "HEAD").strip()
    if not head:
        return {"commits": []}
    cf = "projects:git:" + root
    hit = cache_db.get_json(v2.index_db(), cf, {})
    if hit.get("head") == head and hit.get("v") == VERSION:
        return hit
    from pensieve.people import is_bot
    out = repos_mod.git(root, "log", f"--since={GIT_DAYS}.days", "--no-merges", "-n", "8000", "--name-only",
                        "--format=%x1e%at%x1f%an%x1f%ae%x1f%s")
    commits = []
    for rec in out.split("\x1e")[1:]:
        head_line, _, names = rec.partition("\n")
        bits = head_line.split("\x1f", 3)
        if len(bits) < 4 or is_bot(bits[1], bits[2]):
            continue
        files = [n for n in names.split("\n") if n]
        if files and len(files) <= COMMIT_FILES_MAX:
            commits.append([int(bits[0]), bits[2].lower(), bits[1], bits[3][:200], files])
    res = {"v": VERSION, "head": head, "commits": commits}
    cache_db.put_json(v2.index_db(), cf, res, GIT_TTL)
    return res


# ---- people -----------------------------------------------------------------------------------------------------
class _People:
    """Maps git authors, logins and names to People ids, with each person's closeness to the user."""

    def __init__(self):
        from pensieve import people
        self.mod = people
        g = people.graph() or {}
        self.me = g.get("me")
        self.people = g.get("people", {})
        self.profiles = g.get("profiles", {})
        self.idx = {}
        for pid, p in sorted(self.people.items(), key=lambda kv: -kv[1].get("rank", 1e9)):  # the nearest wins a key
            for k in p.get("keys", []):
                self.idx[k] = pid

    def of(self, name="", email="", login="") -> str | None:
        email = (email or "").lower()
        if email:
            pid = self.idx.get("email:" + email)
            if pid:
                return pid
            m = self.mod.NOREPLY.match(email)
            if m and (pid := self.idx.get("gh:" + m.group(1).lower())):
                return pid
        if login and (pid := self.idx.get("gh:" + login.lower())):
            return pid
        n = self.mod.norm_name(name or "")
        if n and len(n.split()) >= 2:
            return self.idx.get("name:" + n)
        return None

    def close(self, pid) -> float:
        return 1.0 if pid == self.me else float(self.people.get(pid, {}).get("closeness", 0) or 0)

    def card(self, pid) -> dict:
        p = self.people.get(pid, {})
        return dict(id=pid, name=p.get("name", ""), me=pid == self.me, closeness=round(self.close(pid), 3),
                    rank=p.get("rank"), avatar=bool(p.get("avatar_url") and self.mod._avatar_file(p["avatar_url"])),
                    function=p.get("function", ""))


# ---- clusters ---------------------------------------------------------------------------------------------------
def _cluster(X: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    from sklearn.cluster import KMeans
    km = KMeans(n_clusters=k, n_init=3, random_state=0).fit(X)
    cents = km.cluster_centers_
    cents = cents / np.maximum(np.linalg.norm(cents, axis=1, keepdims=True), 1e-9)
    return km.labels_, cents.astype(np.float32)


def _clusters(ids: list[str], X: np.ndarray, now: float, n_repos: int):
    """Labels for the recent items, keeping the last clusters while the set (and its repos) change little."""
    prev = None
    idx = v2.index_db()
    try:
        z = np.load(io.BytesIO(projects_db.clusters(idx) or b""), allow_pickle=False)
        prev = dict(cents=z["cents"], sigs=[str(s) for s in z["sigs"]], n=int(z["n"]), at=float(z["at"]),
                    repos=int(z["repos"]), dim=int(z["cents"].shape[1]))
    except (OSError, ValueError, KeyError, EOFError):
        pass
    if prev and prev["dim"] == X.shape[1] and prev["repos"] == n_repos and \
            abs(len(ids) - prev["n"]) <= RECLUSTER_SHARE * prev["n"] and now - prev["at"] < RECLUSTER_SECONDS:
        lab = (X @ prev["cents"].T).argmax(1)
        return lab, prev["cents"], prev["sigs"]
    k = int(max(4, min(40, round(math.sqrt(len(ids)) / 2.5), len(ids) // 12)))
    lab, cents = _cluster(X, k)
    sigs = []
    for c in range(k):
        mem = sorted(ids[i] for i in np.where(lab == c)[0][:400])
        sigs.append(_hash("|".join(mem) + f"p{VERSION}", 16))
    buf = io.BytesIO()
    np.savez(buf, cents=cents, sigs=np.array(sigs), n=len(ids), at=now, repos=n_repos)
    projects_db.set_clusters(idx, buf.getvalue(), int(now + RECLUSTER_SECONDS))
    return lab, cents, sigs


def _embed(texts: list[str]) -> np.ndarray | None:
    todo = [t for t in dict.fromkeys(texts) if t not in _vec_cache]
    if todo:
        try:
            from pensieve import indexer
            V = indexer.embed(todo)
        except Exception:
            return None
        for t, v in zip(todo, V):
            _vec_cache[t] = np.asarray(v, np.float32)
    return np.stack([_vec_cache[t] for t in texts]) if texts else None


# ---- the build --------------------------------------------------------------------------------------------------
def build(store, datamap) -> dict:
    now = time.time()
    res = datamap.points()
    pts = {p["id"]: p for p in res["points"]}
    ids_all, C = datamap._cached_vectors()
    pos = {i: k for k, i in enumerate(ids_all)}
    since = now - ACTIVE_DAYS * 86400
    recent = [p for p in pts.values() if p["id"] in pos and _t(p.get("mtime")) >= since
              and not (p.get("path") or "").startswith(work._TEMP)]  # scratch files are no project's work
    # code is the bulk in a big repo: the cap applies to code only, so no session or document is left out
    code = sorted((p for p in recent if p["type"] == "code"), key=lambda p: -_t(p.get("mtime")))
    recent = [p for p in recent if p["type"] != "code"]
    recent += code[:max(0, MAX_ITEMS - len(recent))]
    if len(recent) < 12:
        return dict(v=VERSION, built=now, projects=[], items=len(recent))
    ids = [p["id"] for p in recent]
    X = C[[pos[i] for i in ids]]
    # a project mostly lives in one repo: a repo feature pulls a cluster together without forbidding a mix
    roots = sorted({p.get("repo_root") or "" for p in recent})
    R = np.zeros((len(ids), len(roots)), np.float32)
    R[np.arange(len(ids)), [roots.index(p.get("repo_root") or "") for p in recent]] = REPO_WEIGHT
    lab, cents_aug, sigs = _clusters(ids, np.hstack([X, R]) / math.sqrt(1 + REPO_WEIGHT ** 2), now, len(roots))
    cents = cents_aug[:, :X.shape[1]]
    cents = cents / np.maximum(np.linalg.norm(cents, axis=1, keepdims=True), 1e-9)
    K = len(sigs)
    of = {i: int(c) for i, c in zip(ids, lab)}
    ppl = _People()
    P = [dict(items=[], w=Counter(), n=Counter(), last=Counter(), commits=[], prs={}, tickets={}, tasks=[],
              sessions=[], prefixes=Counter(), repos=Counter()) for _ in range(K)]
    for p in recent:
        c = of[p["id"]]
        P[c]["items"].append(p)

    def credit(c, pid, w, ts, n=1.0):
        if pid:
            P[c]["w"][pid] += w
            P[c]["n"][pid] += n
            P[c]["last"][pid] = max(P[c]["last"][pid], ts)

    # code: commits, each split over the clusters of the files it touched
    by_repo = defaultdict(dict)
    for p in recent:
        if p["type"] == "code" and p.get("repo_root"):
            by_repo[p["repo_root"]][p["path"][len(p["repo_root"]) + 1:]] = of[p["id"]]
    for root, files in by_repo.items():
        try:
            log = _git_log(root)["commits"]
        except Exception:
            log = []
        rname = next((p.get("repo") for p in recent if p.get("repo_root") == root), Path(root).name)
        for ts, email, an, subj, fs in log:
            hit = Counter(files[f] for f in fs if f in files)
            if not hit:
                continue
            tot = sum(hit.values())
            pid = ppl.of(an, email)
            for c, n in hit.items():
                share = n / tot
                credit(c, pid, share * _decay(ts, now), ts, share)
                P[c]["repos"][rname] += share
                if share >= 0.5:
                    P[c]["commits"].append(dict(ts=ts, subject=subj, author=an, pid=pid, repo=rname))
                for key in work.KEY.findall(subj):
                    P[c]["prefixes"][key[0]] += share

    # sessions: the user's own work
    for p in recent:
        c = of[p["id"]]
        if p["type"] == "session":
            ts = _t(p.get("mtime"))
            credit(c, ppl.me, 2.0 * _decay(ts, now), ts)
            P[c]["sessions"].append(dict(id=p["id"].split(":", 1)[1], title=p["title"], ts=ts, repo=p.get("repo")))
        elif p["type"] == "doc":
            ts = _t(p.get("mtime"))
            pid = ppl.of(p.get("author") or "") if p.get("author") else None
            credit(c, pid, 0.5 * _decay(ts, now), ts)

    # context cards: the cluster of most of their sessions and files
    partial = False
    try:
        tasks = work.tasks(store, 30)
    except Exception:
        tasks, partial = [], True
    code_of = {p["path"]: of[p["id"]] for p in recent if p["type"] == "code"}
    in_task = set()
    for t in tasks:
        votes = Counter()
        for s in t.get("sessions", []):
            if (c := of.get("session:" + s["id"])) is not None:
                votes[c] += 2
        for f in t.get("files", []):
            if (c := code_of.get(f)) is not None:
                votes[c] += 1
        if not votes:
            continue
        c = votes.most_common(1)[0][0]
        ts = t.get("updated", 0)
        P[c]["tasks"].append(dict(id=t["id"], title=t.get("title", ""), state=t.get("state", ""), ts=ts,
                                  keys=t.get("keys", [])[:3]))
        credit(c, ppl.me, 1.0 * _decay(ts, now), ts)
        for who in t.get("people", []):
            credit(c, ppl.of(who, who if "@" in who else ""), 0.5 * _decay(ts, now), ts)
        for k in t.get("keys", []):
            P[c]["prefixes"][k.split("-")[0]] += 1
        for pr in t.get("prs", []):
            if pr.get("url"):
                P[c]["prs"].setdefault(pr["url"], dict(url=pr["url"], title=pr.get("title") or "", state=pr.get("state") or "",
                                                       ts=ts, author=None, via="card"))
                in_task.add(pr["url"])
        for tk in t.get("tickets", []):
            k = tk.get("key") or tk.get("url")
            if k:
                P[c]["tickets"].setdefault(k, {**{x: tk.get(x) for x in ("key", "title", "status", "assignee", "url", "path")},
                                              "ts": tk.get("updated") or ts})
                in_task.add(k)

    # pull requests from the People graph (authors and reviewers), and tickets from the connections: nearest cluster
    prs = {}
    for pid, pr in ppl.profiles.items():
        for x in pr.get("prs", []):
            if x.get("url"):
                prs.setdefault(x["url"], {**x, "author": pid, "reviewers": set()})
        for x in pr.get("reviews", []):
            if x.get("url"):
                prs.setdefault(x["url"], {**x, "author": None, "reviewers": set()})["reviewers"].add(pid)
    repo_clusters = defaultdict(set)
    for c in range(K):
        for r in P[c]["repos"]:
            repo_clusters[r].add(c)
    todo = [x for u, x in prs.items() if u not in in_task and x.get("ts", 0) >= since]
    try:
        items = [i for i in work._items() if i["key"] and not i.get("private") and i["key"] not in in_task
                 and i["updated"] >= since]
    except Exception:
        items = []
    V = _embed([f"{x.get('repo', '')}: {x.get('t', '')}" for x in todo] + [i["title"] for i in items])
    if V is not None and len(V):
        S = V @ cents.T
        for j, x in enumerate(todo):
            allowed = repo_clusters.get(x.get("repo")) or set(range(K))
            c = max(allowed, key=lambda c: S[j, c])
            if S[j, c] < PR_MIN_SIM:
                continue
            ts = x.get("ts", 0)
            P[c]["prs"].setdefault(x["url"], dict(url=x["url"], title=x.get("t", ""), state=x.get("state", ""), ts=ts,
                                                  author=x["author"], repo=x.get("repo"), via="meaning"))
            credit(c, x["author"], 1.5 * _decay(ts, now), ts)
            for rv in x["reviewers"]:
                credit(c, rv, 0.75 * _decay(ts, now), ts)
        for j, it in enumerate(items, start=len(todo)):
            c = int(S[j].argmax())
            if S[j, c] < PR_MIN_SIM:
                continue
            P[c]["tickets"].setdefault(it["key"], dict(key=it["key"], title=it["title"], status=it["status"],
                                                       assignee=it["assignee"], url=it["url"], path=it["path"],
                                                       ts=it["updated"]))
            P[c]["prefixes"][it["key"].split("-")[0]] += 1
            for who in (it["assignee"] or "").split(", "):
                if who:
                    credit(c, ppl.of(who), 0.75 * _decay(it["updated"], now), it["updated"])

    # a key in a commit subject is a ticket only with a project prefix that tickets or cards use (not "CVE-2024")
    known = {k.split("-")[0] for t in tasks for k in t.get("keys", [])} | {i["key"].split("-")[0] for i in items}
    # names: keywords now, AI names (cached by membership) when the engine writes them
    texts = {c: [datamap._text(p) for p in P[c]["items"]] for c in range(K) if P[c]["items"]}
    exts = {(p.get("ext") or "").lower() for p in recent} - {""}
    kws = datamap._keywords(texts, exts) if texts else {}
    out = []
    for c in range(K):
        d = P[c]
        if not d["items"]:
            continue
        X_c = X[[k for k, i in enumerate(ids) if of[i] == c]]
        reps = [d["items"][j]["id"] for j in np.argsort(-(X_c @ cents[c]))[:24]]
        w = d["w"]
        total = sum(w.values()) or 1e-9
        mine = w.get(ppl.me, 0.0) if ppl.me else 0.0
        team = sum(ppl.close(pid) * v for pid, v in w.items() if pid != ppl.me)
        last = max([_t(p.get("mtime")) for p in d["items"]] + list(d["last"].values()) + [0])
        types = Counter(p["type"] for p in d["items"])
        members = sorted(w, key=lambda pid: -(w[pid] * (0.3 + ppl.close(pid))))
        share = mine / total
        role = ("lead" if share >= 0.5 else "contributor" if share >= 0.15 else "occasional") if mine > 0.01 else "none"
        areas = Counter()
        for p in d["items"]:
            if p["type"] == "code" and p.get("repo_root"):
                areas[f"{p.get('repo')}/{repos_mod.area_of(p['path'][len(p['repo_root']) + 1:])}"] += 1
            elif p["type"] == "doc":
                areas[(p.get("display") or "").rsplit("/", 1)[0]] += 1
        topic = Counter(p.get("cluster") for p in d["items"] if p.get("cluster") is not None).most_common(1)
        kw = ", ".join(w for w in kws.get(c, "").split(", ") if w and not _ID_WORD.fullmatch(w))
        out.append(dict(
            id=sigs[c], key=f"dscope:{sigs[c]}", sig=sigs[c], keywords=kw, reps=reps,
            name=datamap._kw_name(kw, c), description="", named=False,
            team=round(team, 3), mine=round(mine, 3), me_share=round(share, 3), role=role,
            last=last, topic=topic[0][0] if topic else None,
            counts=dict(code=types["code"], docs=types["doc"], sessions=types["session"],
                        commits=round(sum(d["repos"].values())), prs=len(d["prs"]), tickets=len(d["tickets"]),
                        tasks=len(d["tasks"]), people=len([p for p in w if p != ppl.me])),
            prefixes=[k for k, _ in d["prefixes"].most_common() if k in known][:3],
            repos=[r for r, _ in d["repos"].most_common(3)],
            areas=[a for a, _ in areas.most_common(4)],
            members=[dict(**ppl.card(pid), weight=round(w[pid] / total, 3), n=round(d["n"][pid], 1),
                          last=d["last"][pid]) for pid in members[:24] if pid in ppl.people],
            prs=sorted(d["prs"].values(), key=lambda x: -x["ts"])[:12],
            tickets=sorted(d["tickets"].values(), key=lambda x: -(x.get("ts") or 0))[:12],
            tasks=sorted(d["tasks"], key=lambda x: -x["ts"])[:10],
            sessions=sorted(d["sessions"], key=lambda x: -x["ts"])[:10],
            commits=sorted(d["commits"], key=lambda x: -x["ts"])[:10],
            files=[dict(id=p["id"], open=p["open"], type=p["type"], title=p["title"], display=p.get("display", ""),
                        ts=_t(p.get("mtime"))) for p in sorted((p for p in d["items"] if p["type"] != "session"),
                                                                key=lambda p: -_t(p.get("mtime")))[:12]]))
    for e in out:
        for pr in e["prs"]:
            if pr.get("author"):
                pr["author_name"] = ppl.people.get(pr["author"], {}).get("name", "")
        for cm in e["commits"]:
            cm["author"] = ppl.people.get(cm.pop("pid") or "", {}).get("name") or cm["author"]
    # the people near the user count for more than the user's own share, so the list is not only the user's own work
    top_team = max((e["team"] for e in out), default=1) or 1
    top_mine = max((e["mine"] for e in out), default=1) or 1
    for e in out:
        e["relevance"] = round(TEAM_SHARE * e["team"] / top_team + (1 - TEAM_SHARE) * e["mine"] / top_mine, 4)
    out.sort(key=lambda e: (-e["relevance"], -e["last"]))
    for i, e in enumerate(out):
        e["rank"] = i + 1
        e["near"] = i < 3 or (i < NEAR_MAX and e["relevance"] >= NEAR_MIN_SCORE)
    return dict(v=VERSION, built=now, projects=out, items=len(recent), clusters=K, me=ppl.me, partial=partial,
                people_built=(ppl.mod.graph() or {}).get("built"), of={i: sigs[c] for i, c in of.items()})


# ---- serving ----------------------------------------------------------------------------------------------------
def _load() -> dict | None:
    """The last build from index.db, with each member's name and closeness from the People graph now."""
    idx = v2.index_db()
    st = projects_db.state(idx)
    if not st or st.get("v") != VERSION:
        return None
    ppl = _People()
    ps = projects_db.load(idx)
    for e in ps:
        e["members"] = [dict(**ppl.card(m["id"]), weight=m["weight"], n=m["n"], last=m["last"])
                        for m in e["members"] if m["id"] in ppl.people]
    return {**st, "projects": ps}


def _save(d: dict):
    st = {k: v for k, v in d.items() if k not in ("projects", "of")}
    projects_db.save(v2.index_db(), st, d["projects"], d.get("of") or {})


def data() -> dict | None:
    with _lock:
        if _state["data"] is None:
            _state["data"] = _load()
        return _state["data"]


def rebuild(store, datamap, force=False) -> bool:
    """Start a background build, unless one is running or the last one is new enough."""
    from pensieve import people
    with _lock:
        d = data()
        g = people.graph() or {}
        # a build without the context cards (their build failed) is tried again after a few minutes
        fresh = d and time.time() - d.get("built", 0) < (PARTIAL_SECONDS if d.get("partial") else REBUILD_SECONDS) \
            and d.get("people_built") == g.get("built")
        if _state["building"] or (fresh and not force):
            return False
        _state["building"] = True
        _state["started"] = time.time()

    def run():
        try:
            d = build(store, datamap)
            _save(d)
            d.pop("of", None)
            with _lock:
                _state["data"] = d
            _state["error"] = None
        except Exception as e:
            import traceback
            traceback.print_exc()
            _state["error"] = str(e)[:300]
        finally:
            _state["building"] = False
        for fn in listeners:
            fn()
    threading.Thread(target=run, daemon=True, name="projects").start()
    return True


def _named(datamap, ps):
    for e in ps:
        if not e["named"] and (v := datamap.store.meta(e["key"])):
            n = json.loads(v)
            e.update(name=n["name"], description=n["description"], named=True)
    return ps


_SUMMARY = ("id", "name", "description", "named", "keywords", "relevance", "rank", "near", "role", "me_share", "last",
            "counts", "prefixes", "repos", "areas", "topic")


def summary(datamap, q: str = "") -> dict:
    """GET /api/projects: every project, nearest first, with its top members (the cards' list)."""
    d = data()
    if not d:
        return dict(projects=[], building=_state["building"], error=_state["error"], built=None)
    ps = _named(datamap, [dict(e) for e in d["projects"]])
    if q.strip():
        ps = matching(ps, q)
    return dict(built=d["built"], items=d.get("items", 0), building=_state["building"], error=_state["error"],
                projects=[{**{k: e.get(k) for k in _SUMMARY}, "members": e["members"][:6]} for e in ps])


def matching(ps: list[dict], q: str) -> list[dict]:
    """Projects whose name, words, areas, members, pull requests or tickets have all the words of `q`."""
    words = [w for w in re.findall(r"[\w.-]+", q.lower()) if w]

    def text(e):
        return " ".join([e["name"], e["description"], e["keywords"], " ".join(e["areas"]), " ".join(e["repos"]),
                         " ".join(e["prefixes"]), " ".join(m["name"] for m in e["members"]),
                         " ".join(x.get("title", "") for x in e["prs"]), " ".join(x.get("key") or "" for x in e["tickets"]),
                         " ".join(x.get("title") or "" for x in e["tickets"]),
                         " ".join(x["title"] for x in e["tasks"])]).lower()
    return [e for e in ps if all(w in text(e) for w in words)]


def project(datamap, pid: str) -> dict | None:
    """GET /api/projects/<id>: one project with its members, pull requests, tickets, cards, sessions and files."""
    d = data()
    e = next((x for x in (d or {}).get("projects", []) if x["id"] == pid), None)
    if not e:
        return None
    e = _named(datamap, [dict(e)])[0]
    return {k: v for k, v in e.items() if k not in ("key", "sig")}


def unnamed(datamap, limit=12) -> list[dict]:
    """The nearest projects that have no AI name yet, as topics that summarize.name_data_topic can name."""
    d = data()
    if not d:
        return []
    ps = _named(datamap, [dict(e) for e in d["projects"][:limit]])
    return [{k: e[k] for k in ("id", "key", "sig", "keywords", "reps", "name", "description", "named")}
            for e in ps if not e["named"]]


def best_for(datamap, ids: list[str]) -> list[dict]:
    """The projects that hold most of these Map items (point ids): the Map topic panel's "Open as project"."""
    d = data()
    if not d or not ids:
        return []
    of = projects_db.points(v2.index_db(), ids)
    n = Counter(of.get(i) for i in ids)
    n.pop(None, None)
    by = {e["id"]: e for e in d["projects"]}
    hits = sorted((k for k in n if k in by), key=lambda k: (-n[k], by[k]["rank"]))[:3]
    named = {e["id"]: e["name"] for e in _named(datamap, [dict(by[k]) for k in hits])}
    return [dict(id=k, name=named[k], rank=by[k]["rank"], n=n[k], share=round(n[k] / len(ids), 3)) for k in hits]

