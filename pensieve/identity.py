"""Entity resolution for the People tab: which identities (emails, GitHub logins, Slack ids, names) are one person.

The steps:
1. Facts. The keys of one record join at once: a commit's email and its GitHub login, a Slack profile's id and its
   email, a noreply email and the login that it names.
2. Evidence. Each pair of groups that can be one person gets a score from 0 to 1. Pairs at JOIN or more join, best
   first. The evidence: the same full name; the same email name at another domain; a login, an email name or a
   one-word git name that spells a full name ('slee', 'sam.lee', 'samlee' for 'Sam Lee'); the same handle (a git
   name, a Slack display name and a login); a first name only, when no one else in the same repos has that first
   name; commits under the user's own git name in the user's repos. Support adds a little: commits in the same repo,
   in the same months, and the same task. A review of each other's pull requests takes a lot away.
3. The user's own merges and the user's own keys join last, whatever the evidence.

Two groups with conflicting strong keys never join by evidence: two Slack ids, or two work emails at the same domain
that both have commits. A key that the user split joins nothing, except by the user's own merge.

Each join keeps its reason, score and evidence (joined_because), so that the card can show why. Pairs with some
evidence that did not join are kept as 'maybe' (the card offers a merge) and as 'rejected' (a conflict)."""
import re
import unicodedata
from collections import Counter, defaultdict

JOIN = 0.8        # the score at which two groups join
MAYBE = 0.55      # the score at which the card offers a merge
PUBLIC_MAIL = {"gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com", "msn.com", "yahoo.com",
               "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com", "aol.com", "gmx.com", "gmx.de",
               "hey.com", "fastmail.com", "pm.me", "yandex.ru", "mail.com", "qq.com", "163.com"}
NOREPLY_DOMAIN = "users.noreply.github.com"
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "localhost", "none", "(none)", "test.com",
                       "email.com", "domain.com"}
GENERIC = {"admin", "dev", "developer", "info", "me", "mail", "hello", "contact", "git", "github", "noreply", "root",
           "user", "test", "work", "code", "bot", "team", "support", "office", "hi", "email", "personal", "private"}
FACT_REASON = {
    "github": "GitHub lists these together (a pull request's author or a commit's author)",
    "slack": "the Slack profile lists these together",
    "git": "one git commit author",
    "task": "one task record",
    "directory": "Find a person found these together",
}


def fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def handle(s: str, stems=()) -> str:
    """A login, an email name or a one-word name in one form: lower case, no separators, no digits at the end, and
    no company name at either end ('Sam-Lee-example42' -> 'samlee' when 'example' is a stem)."""
    h = re.sub(r"[^a-z0-9]+", "", fold(s)).rstrip("0123456789")
    for st in stems:
        if len(h) > len(st) + 2:
            if h.endswith(st):
                h = h[: -len(st)]
            elif h.startswith(st):
                h = h[len(st):]
    return h.rstrip("0123456789")


def _plain(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", fold(s.split("@")[0]))


def placeholder(email: str) -> bool:
    """An email that git tools fill in when none is set ('you@example.com'): many people share it."""
    d = domain(email.lower())
    return not d or d in PLACEHOLDER_DOMAINS or d.endswith((".local", ".localdomain", ".invalid", ".example"))


def domain(email: str) -> str:
    return email.rsplit("@", 1)[-1] if "@" in email else ""


def is_work_mail(email: str) -> bool:
    d = domain(email)
    return bool(d) and d not in PUBLIC_MAIL and d != NOREPLY_DOMAIN


def mail_kind(email: str) -> str:
    d = domain(email)
    return "noreply" if d == NOREPLY_DOMAIN else "personal" if d in PUBLIC_MAIL else "work"


def name_spellings(full: str) -> dict:
    """The ways a login or an email name spells a full name, each with its score:
    'sam lee' -> samlee 0.9, leesam 0.85, slee 0.75, saml 0.6."""
    w = full.split()
    if len(w) < 2:
        return {}
    first, last = w[0], w[-1]
    out = {"".join(w): 0.9, first + last: 0.9, last + first: 0.85}
    if len(last) >= 4:
        out[first[0] + last] = 0.75
        if len(w) > 2:
            out[first[0] + "".join(x[0] for x in w[1:-1]) + last] = 0.75
    if len(first) >= 4:
        out[first + last[0]] = 0.6
    return {k: v for k, v in out.items() if len(k) >= 4}


def _subseq(small: str, big: str) -> bool:
    it = iter(big)
    return all(c in it for c in small)


class Union:
    """Union-find over identity keys, with each group's features, so a join can be checked for conflicts."""

    def __init__(self):
        self.p = {}
        self.f = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def feats(self, x):
        r = self.find(x)
        if r not in self.f:
            self.f[r] = dict(slack=set(), commit_mail=set(), split=False, obs=0)
        return self.f[r]

    def conflict(self, a, b) -> str:
        fa, fb = self.feats(a), self.feats(b)
        if fa["split"] or fb["split"]:
            return "you split one of these identities"
        if fa["slack"] and fb["slack"] and fa["slack"] != fb["slack"]:
            return "two different Slack accounts"
        da = defaultdict(set)
        for e in fa["commit_mail"]:
            da[domain(e)].add(e)
        for e in fb["commit_mail"]:
            d = domain(e)
            if da.get(d) and e not in da[d]:
                return f"two different work emails at {d}, both with commits"
        return ""

    def join(self, a, b) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        fa, fb = self.feats(ra), self.feats(rb)
        self.p[ra] = rb
        fb["slack"] |= fa["slack"]
        fb["commit_mail"] |= fa["commit_mail"]
        fb["split"] = fb["split"] or fa["split"]
        fb["obs"] += fa["obs"]
        self.f.pop(ra, None)
        return True


def resolve_keys(raw: dict, ovr: dict, stems=()) -> dict:
    """Group the identity keys of `raw` into people. Returns {uf, joins, maybe, rejected, mine, anchors}.
    joins: [{a, b, kind: fact|evidence|user|me, reason, score, evidence}], one for each join of two groups."""
    uf = Union()
    split = set(ovr.get("split", []))
    work = raw.get("work", {})
    joins, rejected = [], []

    def anchor(ob):
        strong = [k for k in ob["keys"] if not k.startswith("name:") and k not in split]
        names = [k for k in ob["keys"] if k.startswith("name:") and k not in split]
        return (strong or names or ob["keys"])[0], strong, names

    anchors = []
    for ob in raw["ids"]:
        for k in ob["keys"]:
            uf.find(k)
        anchors.append(anchor(ob)[0])
    for k in split:
        if k in uf.p:
            uf.feats(k)["split"] = True
    for k in list(uf.p):
        if k.startswith("slack:") and k not in split:
            uf.feats(k)["slack"].add(k)
        if k.startswith("email:") and k not in split and is_work_mail(k[6:]) and (work.get(k) or {}).get("repos"):
            uf.feats(k)["commit_mail"].add(k[6:])

    def record(a, b, kind, reason, score=1.0, evidence=()):
        joins.append(dict(a=a, b=b, kind=kind, reason=reason, score=round(score, 2), evidence=list(evidence)))

    for ob, a in zip(raw["ids"], anchors):
        _, strong, _ = anchor(ob)
        uf.feats(a)["obs"] += 1
        for k in strong[1:]:
            if uf.find(k) != uf.find(strong[0]):
                # a join is worth showing only when both sides were seen on their own: not a record's own new key
                both = uf.feats(k)["obs"] and uf.feats(strong[0])["obs"]
                uf.join(strong[0], k)
                if both:
                    nr = k.startswith("gh:") and ob.get("email", "").endswith("@" + NOREPLY_DOMAIN)
                    record(strong[0], k, "fact", "the GitHub noreply email names this login" if nr
                           else FACT_REASON.get(ob["src"], "one record lists these together"))
    for ob, a in zip(raw["ids"], anchors):
        _, strong, names = anchor(ob)
        for nk in names:
            if not strong or uf.find(nk) == uf.find(strong[0]):
                continue
            seen = uf.feats(nk)["obs"] > 0 and uf.feats(strong[0])["obs"] > 0
            why = uf.conflict(strong[0], nk)
            if why:
                rejected.append(dict(a=strong[0], b=nk, reason="the same full name", why=why))
                continue
            uf.join(strong[0], nk)
            if seen:
                record(strong[0], nk, "evidence", f"the same full name ({nk[5:]})", 0.9)

    groups = defaultdict(set)
    for k in list(uf.p):
        groups[uf.find(k)].add(k)
    obs_of = defaultdict(list)
    for ob, a in zip(raw["ids"], anchors):
        obs_of[uf.find(a)].append(ob)
    feat = {r: _features(keys, obs_of.get(r, []), work, stems, split) for r, keys in groups.items()}
    me_keys = set(raw.get("me", [])) | set(ovr.get("me", []))
    me_names = {fold(n).strip() for n in raw.get("me_names", []) if n}
    maybe = []
    for score, ra, rb, reason, ev in sorted(_candidates(feat, uf, raw, me_keys, me_names, stems), key=lambda c: -c[0]):
        if uf.find(ra) == uf.find(rb):
            continue
        if score < JOIN:
            if score >= MAYBE:
                maybe.append(dict(a=ra, b=rb, reason=reason, score=round(score, 2), evidence=ev))
            continue
        why = uf.conflict(ra, rb)
        if why:
            rejected.append(dict(a=ra, b=rb, reason=reason, why=why, score=round(score, 2)))
            continue
        uf.join(ra, rb)
        record(ra, rb, "evidence", reason, score, ev)

    for a, b in ovr.get("merge", []):
        if uf.find(a) != uf.find(b):
            uf.join(a, b)
            record(a, b, "user", "you merged them")
    mine = [k for k in sorted(me_keys) if k in uf.p and k not in split]
    for k in mine[1:]:
        if uf.find(k) != uf.find(mine[0]):
            uf.join(mine[0], k)
            how = raw.get("me_sources", {}).get(k) or ["you marked it: This is me"]
            record(mine[0], k, "me", "both are yours (" + "; ".join(how) + ")")
    maybe = [m for m in maybe if uf.find(m["a"]) != uf.find(m["b"]) and not uf.conflict(m["a"], m["b"])]
    return dict(uf=uf, joins=joins, maybe=maybe, rejected=rejected, mine=mine, anchors=anchors)


def _features(keys, obs, work, stems, split) -> dict:
    full, handles, firsts, aliases = set(), {}, set(), set()
    for k in keys:
        if k in split:
            continue
        kind, v = k.split(":", 1)
        if kind == "name":
            full.add(v)
        elif kind == "gh":
            handles[handle(v, stems)] = ("login", v)
        elif kind == "email" and domain(v) != NOREPLY_DOMAIN:
            local = v.split("@")[0]
            if fold(local) not in GENERIC:
                handles.setdefault(handle(local, stems), ("email", v))
    for ob in obs:
        for n in [ob.get("name", "")] + list(ob.get("aliases") or []):
            n = (n or "").strip()
            if not n:
                continue
            if len(n.split()) == 1:
                h = handle(n, stems)
                if h and fold(n) not in GENERIC:
                    handles.setdefault(h, ("Slack name" if ob["src"] == "slack" else "git name", n))
                    if n.isalpha():
                        firsts.add(fold(n))
            else:
                aliases.add(n)
    repos, months, tasks = Counter(), defaultdict(set), set()
    for k in keys:
        w = work.get(k) or {}
        for r, v in (w.get("repos") or {}).items():
            repos[r] += v.get("commits", 0)
            months[r] |= set(v.get("months") or [])
        tasks |= set(w.get("tasks") or [])
    handles = {h: v for h, v in handles.items() if len(h) >= 3}
    return dict(full=full, handles=handles, firsts=firsts, aliases=aliases, repos=repos, months=months, tasks=tasks,
                keys=keys)


def _candidates(feat, uf, raw, me_keys, me_names, stems):
    """(score, root a, root b, reason, evidence) for the pairs of groups that can be one person."""
    by_spelling = defaultdict(list)   # a spelling of a full name -> (root, full name, base score)
    by_handle = defaultdict(list)     # a handle -> (root, what it is)
    by_first = defaultdict(set)       # a first name -> roots with a full name that starts with it
    for r, f in feat.items():
        for n in f["full"]:
            for s, sc in name_spellings(n).items():
                by_spelling[s].append((r, n, sc))
            by_first[n.split()[0]].add(r)
        for h, what in f["handles"].items():
            by_handle[h].append((r, what))
    reviewed = set()  # keys that reviewed each other's pull requests: two people, not one
    for a in raw.get("acts", []):
        if a["kind"] == "pr":
            reviewed.add((a["a"], a["b"]))
            reviewed.add((a["b"], a["a"]))
    out = {}

    def add(ra, rb, base, reason, ev):
        if ra == rb:
            return
        ra, rb = sorted((ra, rb))
        fa, fb = feat[ra], feat[rb]
        ev, score = list(ev), base
        both = sorted(set(fa["repos"]) & set(fb["repos"]), key=lambda r: -(fa["repos"][r] + fb["repos"][r]))
        if both:
            score += 0.08
            ev.append("both commit in " + ", ".join(both[:2]))
            if any(fa["months"][r] & fb["months"][r] for r in both):
                score += 0.04
                ev.append("active in the same months")
        if fa["tasks"] & fb["tasks"]:
            score += 0.08
            ev.append("in the same task")
        if any((x, y) in reviewed for x in fa["keys"] for y in fb["keys"]):
            score -= 0.4
            ev.append("they reviewed each other's pull requests")
        score = min(score, 0.99)
        if score > out.get((ra, rb), (0,))[0]:
            out[(ra, rb)] = (score, ra, rb, reason, ev)

    for r, f in feat.items():
        for h, (kind, val) in f["handles"].items():
            for o, n, sc in by_spelling.get(h, []):
                add(r, o, sc, f"the {kind} '{val}' spells the name {n}", [f"{kind} {val} = {n}"])
            if len(h) >= 4:
                for o, (k2, v2) in by_handle.get(h, []):
                    if o != r and not (kind == "email" and k2 == "email"):
                        # equal only once digits or a company name are taken off ('sam42' and 'Sam'): a first name
                        # that many people share, so not enough alone
                        exact = _plain(val) == _plain(v2)
                        strong = (len(h) >= 5 or "login" in (kind, k2)) and exact
                        add(r, o, 0.85 if strong else 0.7 if len(h) >= 5 else 0.6,
                            f"the same handle '{h}' ({kind} {val}, {k2} {v2})", [f"{kind} {val}", f"{k2} {v2}"])
        for k in f["keys"]:
            if not k.startswith("email:") or domain(k) == NOREPLY_DOMAIN:
                continue
            h = handle(k[6:].split("@")[0], stems)
            for o, (k2, v2) in by_handle.get(h, []):
                if o != r and k2 == "email" and domain(v2) != domain(k):
                    add(r, o, 0.85 if len(h) >= 5 else 0.5, f"the same email name at another domain ({k[6:]}, {v2})",
                        [k[6:], v2])
    # an abbreviated login ('alexmrtn' for 'Alex Martin'): the first name, then letters of the last name in order
    for h, lst in by_handle.items():
        for r, (kind, val) in lst:
            if kind != "login":
                continue
            for first, roots in by_first.items():
                if len(first) < 3 or not h.startswith(first) or len(h) - len(first) < 3:
                    continue
                rest = h[len(first):]
                for o in roots:
                    for n in feat[o]["full"]:
                        last = n.split()[-1]
                        if n.split()[0] == first and rest[0] == last[0] and rest != last and _subseq(rest, last):
                            add(r, o, 0.6, f"the login '{val}' abbreviates the name {n}", [f"login {val} ~ {n}"])
    for r, f in feat.items():
        if f["full"]:
            continue
        for first in f["firsts"]:
            if len(first) < 3:
                continue
            near = [o for o in by_first.get(first, ()) if o != r and set(feat[o]["repos"]) & set(f["repos"])]
            others = [o for o, g in feat.items() if o != r and first in g["firsts"] and set(g["repos"]) & set(f["repos"])]
            if len(near) == 1 and not others:
                o = near[0]
                both = sorted(set(feat[o]["repos"]) & set(f["repos"]))
                add(r, o, 0.72, f"the first name '{first}' is the only one of its kind in {', '.join(both[:2])}",
                    [f"one-word git name {first}", f"full name {sorted(feat[o]['full'])[0]}"])
    me_roots = {uf.find(k) for k in me_keys if k in uf.p}
    me_repos = set().union(*[set(feat[m]["repos"]) for m in me_roots if m in feat]) if me_roots else set()
    if me_names and me_roots:
        m = sorted(me_roots)[0]
        for r, f in feat.items():
            if r in me_roots:
                continue
            names = {fold(a).strip() for a in f["aliases"]} | f["full"] | {
                fold(v).strip() for k2, v in f["handles"].values() if k2 == "git name"}
            hit, both = names & me_names, set(f["repos"]) & me_repos
            if hit and both:
                add(r, m, 0.8, f"commits under your git name '{sorted(hit)[0]}' in {', '.join(sorted(both)[:2])}", [])
    return list(out.values())
