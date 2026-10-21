"""Context inheritance: a component carries the links of the components before it in its context chain.

A chain runs from one reset to the next. A reset is a `/clear` in the user's turns. A continuation summary
("This session is being continued from a previous conversation") does not reset: it restates the earlier context, so
its links are carried forward like any other. Inherited links resolve references such as "the PR" or "the branch"
in a later component. They are kept apart from a component's own links, and they never set a goal.

  python bench/inherit.py    # reads components.jsonl and the session maps; writes components_inherit.jsonl

Each output row keeps the component's own fields and adds: chain (the chain number in its session), inherited
(links from earlier components in the same chain, not its own), and carried (true when the chain holds a continuation
summary before the component)."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SCENES = ROOT / "bench" / "out" / "scenes"
EXCHANGE = re.compile(r"^\[e(\d+)\] U: (.*)$")
CLEAR = re.compile(r"(<command-name>/clear|^/clear\b)")
CONTINUED = re.compile(r"being continued from a previous conversation")


def maps():
    """Session id -> {'clear': [exchange numbers with a /clear], 'carry': [exchange numbers with a continuation]}."""
    out = {}
    for f in (SCENES / "in").glob("*.txt"):
        lines = f.read_text().splitlines()
        sid = lines[0].split(" | ")[0].replace("# session ", "").strip()
        clear, carry = [], []
        for line in lines:
            m = EXCHANGE.match(line)
            if not m:
                continue
            k = int(m.group(1))
            if CLEAR.search(m.group(2)):
                clear.append(k)
            if CONTINUED.search(m.group(2)):
                carry.append(k)
        out[sid] = dict(clear=clear, carry=carry)
    return out


def inherit(comps: list[dict], marks: dict) -> list[dict]:
    by_session = {}
    for c in comps:
        by_session.setdefault(c["session_id"], []).append(c)
    rows = []
    for sid, group in by_session.items():
        group.sort(key=lambda c: c["exchanges"][0])
        m = marks.get(sid, {"clear": [], "carry": []})
        chain, chain_links, chain_carry = 0, [], False
        for c in group:
            start = c["exchanges"][0]
            before = prev_end(group, c)
            new_chain = any(before < k <= start for k in m["clear"])  # a /clear between the two components
            if new_chain:
                chain += 1
                chain_links, chain_carry = [], False
            inherited = list({(l["kind"], l["key"]): l for l in chain_links}.values())
            # The nearest earlier PR or branch in the chain: what "the PR" or "the branch" most likely means here.
            nearest = {}
            for l in chain_links:
                if l["kind"] in ("pr", "branch"):
                    nearest[l["kind"]] = l
            resolved = [dict(kind=k, key=v["key"]) for k, v in nearest.items()]
            own_sink = [l for l in c["links"] if l["kind"] == "pr"] + ([{"kind": "branch", "key": c["branch"]}]
                                                                        if c.get("branch") else [])
            rows.append(dict(c, chain=chain, inherited=inherited, carried=chain_carry,
                             resolved=resolved if not own_sink else []))
            chain_links = chain_links + list(c["links"]) + ([{"kind": "branch", "key": c["branch"]}]
                                                            if c.get("branch") else [])
            if any(c["exchanges"][0] <= k <= c["exchanges"][1] for k in m["carry"]):
                chain_carry = True
    return rows


def prev_end(group: list[dict], c: dict) -> int:
    """The end exchange of the component before `c` in its session (or -1 for the first)."""
    idx = group.index(c)
    return group[idx - 1]["exchanges"][1] if idx else -1


def main():
    comps = [json.loads(l) for l in (SCENES / "components.jsonl").read_text().splitlines()]
    rows = inherit(comps, maps())
    out = SCENES / "components_inherit.jsonl"
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    with_inh = sum(1 for r in rows if r["inherited"])
    carried = sum(1 for r in rows if r["carried"])
    chains = len({(r["session_id"], r["chain"]) for r in rows})
    print(f"components {len(rows)}; with inherited links {with_inh}; carried over a summary {carried}; "
          f"chains {chains}; written {out}")


if __name__ == "__main__":
    main()
