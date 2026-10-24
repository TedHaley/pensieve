"""Mentions: a branch, PR or ticket named in a session's text. A mention is weak. It never makes a join by itself."""
from pensieve import digest
from pensieve.extract import MENTION, Edge, Session, extractor


@extractor("mentions")
def mentions(session: Session):
    text = "\n".join(t for _, t in session.turns if t)
    seen = set()
    for link in digest.extract_links(text):
        kind = link["kind"]
        if kind == "url":
            continue
        key = link["key"]
        if kind == "ticket" or kind == "pr":
            target = ("ticket" if kind == "ticket" else "pr", key)
        elif kind == "branch":
            target = ("branch", key)
        else:
            continue
        if target in seen:
            continue
        seen.add(target)
        yield Edge(session.id, target[0], target[1], MENTION, "named in text", "mentions")
