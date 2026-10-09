"""Background LLM enrichment: session + chunk summaries, topic names, and corpus-level insights."""
import hashlib
import json
import re
import time

from . import llm

SESSION_SYS = ("You summarize transcripts between a developer and an AI coding agent. Write a 2-sentence summary of "
               "what was worked on and the outcome, plus 3-5 short lowercase topic tags.")
SESSION_SCHEMA = {"type": "object", "properties": {
    "summary": {"type": "string"},
    "tags": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 6}},
    "required": ["summary", "tags"]}
CHUNK_SYS = "Summarize this excerpt of a developer/AI-agent conversation in one sentence of at most 20 words. Output the sentence only."

TOPIC_SYS = ("You name clusters of a developer's AI-agent sessions. Give a specific 2-4 word Title Case name "
             "(no generic words like 'Development' or 'Projects' on their own) and one sentence describing what unites them.")
TOPIC_SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}},
                "required": ["name", "description"]}

INSIGHT_SYS = (
    "You are an insightful research partner reviewing a developer's history of AI-agent sessions, grouped into topics. "
    "Find patterns they may not see themselves. Be concrete: cite topic ids (T#) and session ids (S#) from the data. "
    "headline: one sentence portrait of what this person works on. themes: recurring ideas and goals they keep returning to. "
    "connections: non-obvious links between different topics that could be combined. open_threads: work that was started "
    "but appears unfinished or blocked. unexplored: adjacent directions they have not explored yet but would likely "
    "value, each with a concrete first step. If a CODEBASE section is given, ground unexplored directions in it: "
    "name the specific code area (area, exactly as written there, or empty) and the people to talk to (people, names "
    "exactly as written there), and explain how it connects to work they already did. Never invent sessions, areas or people.")

TEAM_SYS = ("You describe what each engineer on a software team has been working on, from their recent commit "
            "messages and the code areas they changed. For each person give: focus, a 3-8 word phrase; summary, "
            "1-2 plain sentences naming the concrete features, fixes or components and their apparent purpose. "
            "Be specific and never invent work that is not in the data.")
TEAM_SCHEMA = {"type": "object", "properties": {"people": {"type": "array", "items": {
    "type": "object", "properties": {"id": {"type": "string"}, "focus": {"type": "string"}, "summary": {"type": "string"}},
    "required": ["id", "focus", "summary"]}}}, "required": ["people"]}
_item = lambda extra: {"type": "object", "properties": {"title": {"type": "string"}, "insight": {"type": "string"}, **extra},
                       "required": ["title", "insight", *extra]}
_ids = {"type": "array", "items": {"type": "string"}}
INSIGHT_SCHEMA = {"type": "object", "properties": {
    "headline": {"type": "string"},
    "themes": {"type": "array", "items": _item({"topics": _ids, "sessions": _ids}), "minItems": 3, "maxItems": 5},
    "connections": {"type": "array", "items": _item({"topics": _ids}), "minItems": 2, "maxItems": 4},
    "open_threads": {"type": "array", "items": _item({"sessions": _ids}), "minItems": 2, "maxItems": 4},
    "unexplored": {"type": "array", "items": _item({"first_step": {"type": "string"}, "area": {"type": "string"},
                                                    "people": _ids}), "minItems": 3, "maxItems": 5}},
    "required": ["headline", "themes", "connections", "open_threads", "unexplored"]}


def _transcript(turns, budget=7000):
    out, n = [], 0
    for role, text in turns:
        t = (text if role == "user" else text[:500])[:900]
        out.append(f"{role.upper()}: {t}")
        n += len(t)
        if n > budget:
            break
    return "\n\n".join(out)


def summarize_session(store, sid):
    s = store.session(sid)
    if not s or not s["turns"]:
        return
    d = llm.complete(_transcript([(t["role"], t["text"]) for t in s["turns"]]), SESSION_SYS, max_tokens=300,
                     schema=SESSION_SCHEMA)
    with store.lock:
        store.db.execute("UPDATE sessions SET summary=?, tags=? WHERE id=?",
                         (d["summary"].strip(), ",".join(t.strip().lower() for t in d["tags"][:6]), sid))
        store.db.commit()


def summarize_chunk(store, cid, text):
    out = llm.complete(text[:3000], CHUNK_SYS, max_tokens=60).split("\n")[0].strip()
    with store.lock:
        store.db.execute("UPDATE chunks SET summary=? WHERE id=?", (out, cid))
        store.db.commit()


def name_topic(store, topic, key=None):
    """Name a topic. With `key`, the result is cached in meta (scoped topics) instead of the topics table."""
    with store.lock:
        rows = store.db.execute(
            f"SELECT title, summary, project_name FROM sessions WHERE id IN ({','.join('?' * len(topic['sessions']))}) "
            "ORDER BY updated DESC LIMIT 15", topic["sessions"]).fetchall()
    body = f"Keywords: {topic['keywords']}\n\nSessions:\n" + "\n".join(
        f"- [{p}] {t}: {(s or '')[:220]}" for t, s, p in rows)
    d = llm.complete(body, TOPIC_SYS, max_tokens=150, temperature=0.3, schema=TOPIC_SCHEMA)
    with store.lock:
        if key:
            store.meta(key, json.dumps(dict(name=d["name"].strip()[:48], description=d["description"].strip())))
        else:
            store.db.execute("UPDATE topics SET name=?, description=? WHERE id=?",
                             (d["name"].strip()[:48], d["description"].strip(), topic["id"]))
        store.db.commit()


DATA_TOPIC_SYS = ("You name groups of a person's documents, code files and AI-agent sessions that sit together on a map of "
                  "their work because they are about the same thing. Give a specific 2-4 word Title Case name for the subject "
                  "(not the file type, folder or repository on its own) and 1-2 plain sentences on what the group covers and "
                  "where it lives. Never invent anything that is not in the data.")


def name_data_topic(datamap, topic):
    """Name one Data-map topic from its keywords and its most central items."""
    pts = {p["id"]: p for p in datamap.points()["points"]}
    kinds = {"doc": "document", "code": "code", "session": "agent session"}
    lines = []
    for i in topic["reps"][:20]:
        p = pts.get(i)
        if not p:
            continue
        where = p.get("display") or ""
        lines.append(f"- [{kinds.get(p['type'], p['type'])}] {p['title']} ({where})"
                     + (f": {p['summary'][:200]}" if p.get("summary") else ""))
    body = f"Keywords: {topic['keywords']}\n\nMost central items:\n" + "\n".join(lines)
    d = llm.complete(body, DATA_TOPIC_SYS, max_tokens=200, temperature=0.3, schema=TOPIC_SCHEMA)
    if topic.get("key"):  # a topic within a filtered view
        datamap.set_scoped_name(topic, d["name"], d["description"])
    else:
        datamap.set_topic_name(topic, d["name"], d["description"])


def insight_key(scope=None):
    return f"insights:{scope}" if scope else "insights"


def insight_rows(store, sids=None):
    """The sessions insights are written from: the newest 120 with a summary (within `sids`)."""
    with store.lock:
        rows = store.db.execute(
            "SELECT id, title, summary, project_name, cluster, substr(started,1,10) FROM sessions "
            "WHERE n_chunks>0 AND summary IS NOT NULL ORDER BY updated DESC").fetchall()
    if sids is not None:
        rows = [r for r in rows if r[0] in sids]
    return rows[:120]


def topic_sig(topics):
    """Which sessions sit together, not what the topics are called (names arrive later and shouldn't count)."""
    groups = sorted(sorted(t["sessions"]) for t in topics)
    return hashlib.sha1(json.dumps(groups).encode()).hexdigest()[:16]


INSIGHT_MIN_AGE = 6 * 3600  # never rewrite more often than this, however much changes


def insights_stale(store, d, sids=None, topics=None):
    """Out of date: the topics were re-clustered, or a fifth (at least 5) of the sessions they were written from
    are new or gone, and they're older than INSIGHT_MIN_AGE. Insights from before this was recorded count as
    out of date."""
    if not d or time.time() - d.get("generated", 0) < INSIGHT_MIN_AGE:
        return False
    basis = d.get("basis")
    if not basis:
        return True
    now = {r[0] for r in insight_rows(store, sids)}
    old = set(basis["sessions"])
    if topics is not None and basis.get("topics") != topic_sig(topics):
        return True
    return len(now ^ old) >= max(5, 0.2 * len(now | old))


def generate_insights(store, scope=None, sids=None, topics=None, codebase=""):
    """Corpus-level insights. With `scope`, only sessions in `sids` are read, `topics` are that scope's topics, and the
    result is cached under its own key. `codebase` is grounding text (unexplored areas, team activity)."""
    topics = topics if topics is not None else store.topics()
    member = {s: t["id"] for t in topics for s in t["sessions"]}
    rows = insight_rows(store, sids)
    sid = {f"S{i + 1}": r[0] for i, r in enumerate(rows)}
    lines = ["TOPICS:"] + [f"T{t['id'] + 1} {t['name']} ({len(t['sessions'])} sessions): {t['description']}" for t in topics]
    lines += ["", "SESSIONS (newest first):"] + [
        f"S{i + 1} [{r[5]}] [{r[3]}] [T{member.get(r[0], r[4] or 0) + 1}] {r[1]} - {(r[2] or '')[:200]}"
        for i, r in enumerate(rows)]
    if codebase:
        lines += ["", "CODEBASE:", codebase]
    d = llm.complete("\n".join(lines), INSIGHT_SYS, max_tokens=3000, temperature=0.5, schema=INSIGHT_SCHEMA)

    tname = {f"T{t['id'] + 1}": t["name"] for t in topics}
    stitle = {k: next((r[1] for r in rows if r[0] == v), k) for k, v in sid.items()}

    def readable(text):
        """The model cites T#/S# ids; show names instead of opaque ids."""
        text = re.sub(r"\s*\((?:[TS]\d+(?:,\s*)?)+\)", "", text)
        text = re.sub(r"\bT\d+\b/?", lambda m: tname.get(m.group(0).rstrip("/"), ""), text)
        text = re.sub(r"\bS\d+\b", lambda m: f"“{stitle[m.group(0)]}”" if m.group(0) in stitle else "", text)
        return re.sub(r"\(\s*\)|\s{2,}", " ", text).strip()

    def fix(item):
        for k in ("title", "insight", "first_step"):
            if k in item:
                item[k] = readable(item[k])
        item["sessions"] = [sid[s.strip()] for s in item.get("sessions", []) if s.strip() in sid]
        item["topics"] = [int(t.strip().lstrip("T")) - 1 for t in item.get("topics", [])
                          if t.strip().lstrip("T").isdigit()]
        if codebase and item.get("area") and item["area"] not in codebase:
            item["area"] = ""  # drop areas the model made up
        item["people"] = [p for p in item.get("people", []) if p and p in codebase]
        return item

    for k in ("themes", "connections", "open_threads", "unexplored"):
        d[k] = [fix(x) for x in d.get(k, [])]
    d["headline"] = readable(d.get("headline", ""))
    d["generated"] = time.time()
    d["scope"] = scope
    d["n_sessions"] = len(rows)
    d["basis"] = dict(sessions=[r[0] for r in rows], topics=topic_sig(topics))  # to tell when they're out of date
    with store.lock:
        store.meta(insight_key(scope), json.dumps(d))
        store.db.commit()
    return d


def summarize_team(store, team, key, top=10):
    """One LLM call describing what each of the most active people has been working on; cached under `key`."""
    ppl = team["people"][:top]
    if not ppl:
        return {}
    body = [f"Repository: {team['repo']} - commits from the last {team['days']} days.", ""]
    for i, p in enumerate(ppl):
        body.append(f"P{i + 1} {p['name']} - {p['commits']} commits; areas: "
                    + ", ".join(f"{a['area']} ({a['commits']})" for a in p["areas"]))
        body += [f"  - {c['subject'][:110]}" for c in p["subjects"][:12]]
    d = llm.complete("\n".join(body), TEAM_SYS, max_tokens=2200, temperature=0.3, schema=TEAM_SCHEMA)
    ids = {f"P{i + 1}": p["email"] for i, p in enumerate(ppl)}
    out = {ids[x["id"].strip()]: dict(focus=x["focus"].strip(), summary=x["summary"].strip())
           for x in d.get("people", []) if x.get("id", "").strip() in ids}
    with store.lock:
        store.meta(key, json.dumps(out))
        store.db.commit()
    return out


def pending(store):
    with store.lock:
        sess = [r[0] for r in store.db.execute(
            "SELECT id FROM sessions WHERE summary IS NULL AND n_chunks>0 ORDER BY updated DESC")]
        chunks = store.db.execute(
            "SELECT id, text FROM chunks WHERE summary IS NULL ORDER BY id DESC LIMIT 64").fetchall()
    topics = [t for t in store.topics() if not t["named"]] if not sess else []
    return sess, topics, chunks
