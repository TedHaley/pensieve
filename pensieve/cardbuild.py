"""The incremental card builder (docs/design/storage.md, "Cards within 2 seconds").

A write that can change a card sends an event: a session indexed, a document stored, commits loaded, a card's state
set by hand. The builder thread takes the events of the last 200 ms together:

1. It loads again only what the events name: those sessions (their rows, text facts and edited files), those
   documents, or the commits and worktrees.
2. It groups every session, document and commit of the window again, in memory (work.group). This is fast, because
   the text of each session is read once and kept as its facts.
3. It runs git and gh (work._pickup) only for the cards whose members changed, and for the cards that the event names
   through `card_members_ref` (a PR or a card). The other cards keep their folders and PRs from the last build.
4. It writes the rows of `cards` and `card_members` that changed, deletes the cards that went, and calls `listeners`
   with the ids. The server sends them to the page as a `card` event.

A full build runs at the start and every FULL_SECONDS: it moves the window, and it asks gh for the PR states."""
import copy
import hashlib
import json
import os
from pensieve import config, task_states
import queue
import threading
import time
import traceback

from pensieve import store as v2
from pensieve import work
from pensieve.models import Card, CardKind, CardMember, MemberKind, State
from pensieve.store import cards as store_cards

DAYS = 30            # the window of the built cards; /api/work filters it for a shorter one
FULL_SECONDS = 600   # a full build: the window moves, and gh gives the PR states (cached 10 minutes anyway)
COALESCE = 0.2       # events this close together make one build

SESSION, DOCUMENT, COMMIT, PR, STATE, REFRESH = "session", "document", "commit", "pr", "state", "refresh"

current: "Builder | None" = None


def emit(kind: str, refs, wait: float = 0) -> bool:
    """Send an event to the running builder (none runs in the MCP process: then nothing happens). With `wait`
    (seconds), returns when the build that took it has written its cards."""
    b = current
    if b is None:
        return False
    return b.emit(kind, refs, wait)


def _plain(x):
    return sorted(x) if isinstance(x, (set, frozenset)) else str(x)


def _fingerprint(t: dict) -> str:
    """Of a card before its folders and PRs: what it is made of. The same fingerprint keeps the last pickup."""
    return hashlib.sha1(json.dumps(t, sort_keys=True, default=_plain).encode()).hexdigest()


class Builder:
    def __init__(self, store, days: int = DAYS):
        self.store, self.days = store, days
        self.inp: work.Inputs | None = None
        self.picked: dict[str, tuple] = {}   # card id -> (fingerprint, places, prs, resume) of its last pickup
        self.views: dict[str, str] = store_cards.views(v2.store_db())  # card id -> view JSON, as written
        self.sigs: dict[str, str] = store_cards.signatures(v2.store_db())
        self.tasks: dict[str, dict] = {}
        self.listeners = []                  # fn(changed ids, removed ids), on the builder thread
        self.q: queue.Queue = queue.Queue()
        self.last_full = 0.0
        self.builds: list[dict] = []         # the last builds: {at, kind, ms, changed}
        self._thread = None

    def start(self):
        global current
        current = self
        self._thread = threading.Thread(target=self._loop, daemon=True, name="card-builder")
        self._thread.start()

    def emit(self, kind: str, refs, wait: float = 0) -> bool:
        done = threading.Event() if wait else None
        self.q.put((kind, list(refs), done))
        return done.wait(wait) if done else True

    def _loop(self):
        try:
            self.full(net=False)  # cards from the caches at once; the PR states follow
            self.full(net=True)
        except Exception:
            traceback.print_exc()
        while True:
            try:
                first = self.q.get(timeout=1.0)
            except queue.Empty:
                first = None
            events = [first] if first else []
            end = time.monotonic() + COALESCE
            while first and time.monotonic() < end:
                try:
                    events.append(self.q.get(timeout=max(0.0, end - time.monotonic())))
                except queue.Empty:
                    break
            try:
                if time.time() - self.last_full > FULL_SECONDS:
                    self.full(net=True)
                elif events:
                    self.update(events)
            except Exception:
                traceback.print_exc()
            finally:
                for _, _, done in events:
                    if done:
                        done.set()

    def full(self, net: bool = True):
        t0 = time.perf_counter()
        inp = work.Inputs(self.days)
        inp.load(self.store)
        self.inp = inp
        changed = self._profiled(lambda: self._build(force=None, net=net))
        self.last_full = time.time()
        self._log("full", t0, changed)

    def update(self, events):
        """Load again what the events name, and build."""
        t0 = time.perf_counter()
        kinds: dict[str, set] = {}
        for kind, refs, _ in events:
            kinds.setdefault(kind, set()).update(refs)
        inp, db = self.inp, v2.store_db()
        inp.window()  # the end of the window is now
        if kinds.get(SESSION):
            inp.load_sessions(self.store, kinds[SESSION])
        if kinds.get(DOCUMENT):
            inp.load_items(kinds[DOCUMENT])
        if COMMIT in kinds:
            inp.load_repos(self.store)
        force = set(kinds.get(STATE, ())) | set(kinds.get(REFRESH, ()))
        if kinds.get(PR):
            force |= store_cards.with_members(db, MemberKind.PULL_REQUEST, kinds[PR])
        net = bool(kinds.get(REFRESH) or kinds.get(PR))
        changed = self._profiled(lambda: self._build(force=force, net=net))
        self._log("+".join(sorted(kinds)), t0, changed)
        # a changed card with an open PR, or a PR of unknown state: ask gh at once, in a build of its own
        ask = [i for i in changed if i in self.tasks and not net and any(
            "github.com" in p["url"] and p.get("state") in ("", "open", "draft") for p in self.tasks[i]["prs"])]
        if ask:
            self.q.put((REFRESH, ask, None))

    def _build(self, force: set | None, net: bool) -> list[str]:
        """Group, pick up and finish every card; write the ones whose view changed. `force`: the cards whose folders
        and PRs are read again even when their members did not change (None: every card)."""
        inp = self.inp
        picked = {}

        def pick(t):
            fp, old = fps[t["id"]], self.picked.get(t["id"])
            if force is not None and old and old[0] == fp and t["id"] not in force:
                t["places"], t["prs"] = copy.deepcopy(old[1]), copy.deepcopy(old[2])
                if old[3]:
                    t["resume"] = dict(old[3])
            else:
                work._pickup(t, inp.wts)
            picked[t["id"]] = (fp, copy.deepcopy(t["places"]), copy.deepcopy(t["prs"]), t.get("resume"))
        was = work._net["off"]
        work._net["off"] = not net  # grouping asks gh about the PRs the sessions name: from the cache when offline
        try:
            out = work.group(inp)
            fps = {t["id"]: _fingerprint(t) for t in out}
            final = work.finish(out, inp, self.store, pickup=pick)
        finally:
            work._net["off"] = was
        self.picked = picked
        views = {t["id"]: json.dumps(t, sort_keys=True, default=_plain) for t in final}
        changed = [i for i, v in views.items() if self.views.get(i) != v]
        removed = [i for i in self.views if i not in views]
        by_id = {t["id"]: t for t in final}
        if changed or removed:
            now = int(time.time())
            rows = [(self._card(by_id[i], views[i], now), _members(by_id[i])) for i in changed]
            store_cards.save(v2.store_db(), rows, delete=removed)
            for card, _ in rows:
                self.sigs[card.id] = card.signature
        self.views, self.tasks = views, by_id
        if changed or removed:
            self._tell(changed, removed)
        return changed + removed

    def _tell(self, changed, removed):
        for fn in self.listeners:
            try:
                fn(changed, removed)
            except Exception:
                traceback.print_exc()

    @staticmethod
    def _card(t: dict, view: str, now: int) -> Card:
        kind = CardKind.GOAL if t.get("goal_of") else CardKind(t["kind"])
        times = [e["ts"] for e in t.get("events", []) if e.get("ts")] + [t["updated"]]
        return Card(t["id"], kind, t.get("goal_of"), t["title"] or t["id"],
                    State(task_states.stored(t["state"])), int(min(times) or 0),
                    int(t["updated"] or 0), now, work._signature(t), view)

    def _log(self, kind, t0, changed):
        ms = round((time.perf_counter() - t0) * 1000)
        self.builds = (self.builds + [dict(at=time.time(), kind=kind, ms=ms, changed=len(changed))])[-50:]
        if kind == "full" or ms >= 2000:  # a full build, or a slow update: worth a line in the log
            print(f"threads: {kind} build in {ms} ms, {len(changed)} cards changed, {len(self.tasks)} threads", flush=True)

    def _profiled(self, fn):
        """Run one build step; with PENSIEVE_PROFILE_BUILDS set, write where its time went to DATA_DIR/threads/profile.txt
        (the last build only), for a look at a slow build on a live server."""
        if not os.environ.get("PENSIEVE_PROFILE_BUILDS"):
            return fn()
        import cProfile
        import io
        import pstats
        pr = cProfile.Profile()
        pr.enable()
        try:
            return fn()
        finally:
            pr.disable()
            s = io.StringIO()
            pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(40)
            p = config.DATA_DIR / "threads" / "profile.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(s.getvalue())


def _members(t: dict) -> list[CardMember]:
    """What a card is made of, for `card_members` and its `card_members_ref` index."""
    cid, out = t["id"], {}

    def add(kind, ref, at=None):
        if ref:
            out.setdefault((kind, str(ref)), CardMember(cid, kind, str(ref), int(at) if at else None))
    for s in t.get("sessions", []):
        add(MemberKind.SESSION, s["id"], s.get("updated"))
    for x in t.get("tickets", []) + t.get("items", []):
        if str(x.get("path", "")).startswith("doc:"):
            add(MemberKind.DOCUMENT, x["path"][4:], x.get("updated"))
    for p in t.get("prs", []) + t.get("branch_prs", []):
        add(MemberKind.PULL_REQUEST, p["url"])
    for b in t.get("branches", []):
        add(MemberKind.BRANCH, f"{b['repo']}@{b['branch']}")
    for c in t.get("commits", []):
        add(MemberKind.COMMIT, c["sha"], c.get("ts"))
    for k in t.get("keys", []):
        add(MemberKind.TICKET_KEY, k)
    for f in t.get("files", []):
        add(MemberKind.FILE, f)
    for p in t.get("places", []):
        add(MemberKind.WORKTREE, p.get("path"))
    return list(out.values())
