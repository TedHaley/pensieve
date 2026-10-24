"""Tests of components (pensieve/segment.py) and of the components in a digest.
Run: python tests/segment.py

The sessions are made up. The words are chosen so that two themes share no words."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pensieve import digest, segment  # noqa: E402
from pensieve.parsers import Session  # noqa: E402

WEBHOOK = ("Retry the webhook delivery with exponential backoff in the dispatcher. ",
           "The dispatcher retries each webhook three times before it gives up.")
PRICING = ("Rewrite the pricing page copy for the annual plan tiers. ",
           "The annual plan copy now lists the tiers and the seat limits.")


def exchange(user, reply):
    return [("user", user), ("assistant", reply)]


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))
    return ok


def run():
    results = []

    turns = exchange(*WEBHOOK) + exchange(*WEBHOOK) + exchange(*PRICING) + exchange(*PRICING)
    comps = segment.components(turns)
    results.append(check("two themes give two components", [c["turns"] for c in comps], [(0, 4), (4, 8)]))

    turns = exchange(*WEBHOOK) + exchange("ok", "Done.") + exchange(*WEBHOOK)
    comps = segment.components(turns)
    results.append(check("a short reply joins the component before it", [c["turns"] for c in comps], [(0, 6)]))

    turns = exchange(*WEBHOOK) + exchange(*WEBHOOK) + exchange(*WEBHOOK)
    results.append(check("one theme is one component", len(segment.components(turns)), 1))

    results.append(check("empty session has no components", segment.components([]), []))

    s = Session(id="s-9", source="claude", path="/tmp/x.jsonl",
                turns=exchange(*WEBHOOK) + exchange(*WEBHOOK)
                + exchange(*PRICING) + exchange("See https://github.com/acme/site/pull/7 for the copy.", "Read it."))
    d = digest.build(s)
    results.append(check("digest has components with ids", [c["id"] for c in d["components"]],
                         ["s-9#0", "s-9#1"]))
    results.append(check("a link goes into the component that mentions it",
                         [[link["key"] for link in c["links"]] for c in d["components"]],
                         [[], ["acme/site#7"]]))
    results.append(check("the session keeps the union of its links",
                         [link["key"] for link in d["links"]], ["acme/site#7"]))
    results.append(check("schema version", d["schema"], "session_digest/0.2"))

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
