"""Tests of session digests (pensieve/digest.py): link keys, the digest shape, and the schema file.
Run: python tests/digest.py

Sessions here are made up. The repo and ticket names are examples."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pensieve import digest  # noqa: E402
from pensieve.parsers import Session  # noqa: E402


def check(name, got, want):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))
    return ok


def keys(links):
    return {(r["kind"], r["key"]): r["count"] for r in links}


def run():
    results = []
    text = ("Opened https://github.com/acme/widgets/pull/12. Then see acme/widgets#12 and "
            "https://github.com/acme/widgets/pull/12/files.")
    links = digest.extract_links(text)
    results.append(check("pr: two URLs and org/repo#n give one key (three mentions)",
                         keys(links).get(("pr", "acme/widgets#12")), 3))
    results.append(check("pr: trailing period is not in the ref",
                         [r["ref"] for r in links if r["kind"] == "pr"][0],
                         "https://github.com/acme/widgets/pull/12"))

    text = "ABC-12 is blocked. Fixed ABC-12 today. The file is UTF-8 and uses SHA-256 and ISO-8601."
    results.append(check("ticket: ABC-12 counted twice", keys(digest.extract_links(text)).get(("ticket", "ABC-12")), 2))
    results.append(check("ticket: UTF-8, SHA-256 and ISO-8601 are not tickets",
                         [r for r in digest.extract_links(text) if r["kind"] == "ticket"],
                         [dict(kind="ticket", key="ABC-12", ref="ABC-12", count=2)]))

    text = "Run `git checkout -b feature/retry-webhooks` first, then git push -u origin feature/retry-webhooks."
    results.append(check("branch: name after checkout -b or push -u origin",
                         keys(digest.extract_links(text)).get(("branch", "feature/retry-webhooks")), 2))

    text = "See https://Docs.Example.com/Page/?q=1#part and https://docs.example.com/Page/ for more."
    results.append(check("url: one key for the same page",
                         keys(digest.extract_links(text)).get(("url", "https://docs.example.com/Page")), 2))

    text = "Read <https://docs.example.com/a/b&gt;, then https://docs.example.com/a/b."
    results.append(check("url: HTML entity left after an escaped bracket is dropped",
                         keys(digest.extract_links(text)).get(("url", "https://docs.example.com/a/b")), 2))

    s = Session(id="s-1", source="claude", path="/tmp/x.jsonl", started="2026-10-01T09:00:00Z",
                updated="2026-10-01T10:00:00Z",
                turns=[("user", "Fix ABC-7. Secret text here."), ("assistant", "Done, see acme/app#3.")])
    d = digest.build(s)
    results.append(check("build: schema and ids", (d["schema"], d["session_id"], d["source"]),
                         ("session_digest/0.1", "s-1", "claude")))
    results.append(check("build: no transcript text in the digest",
                         "Secret text" in json.dumps(d), False))
    results.append(check("build: links from both turns",
                         sorted(keys(d["links"])), [("pr", "acme/app#3"), ("ticket", "ABC-7")]))

    schema = json.loads((ROOT / "pensieve" / "session_digest.schema.json").read_text())
    results.append(check("schema: required fields match what build writes",
                         sorted(schema["required"]), sorted(d.keys())))
    results.append(check("schema: link kinds match extract_links",
                         schema["properties"]["links"]["items"]["properties"]["kind"]["enum"],
                         ["pr", "ticket", "branch", "url"]))

    print(f"\n{sum(results)} of {len(results)} passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
