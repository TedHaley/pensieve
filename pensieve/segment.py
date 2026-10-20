"""Components: the parts of one session that are about the same theme.

A session is a list of exchanges (a user turn and the agent's replies after it). `components` joins neighbouring
exchanges while they share words with the component so far. A new component starts where the words change. A
component can be one exchange or many, so the length varies. Short replies ("ok", "yes") join the component before
them, because they carry no theme of their own.

Words are the only signal here. The same function can take embeddings later. Tune SHIFT and MIN_EXCHANGES on the
eval, not by eye."""
import math
import re
from collections import Counter

# A new component starts when an exchange shares less than this much of its words with the component so far.
SHIFT = 0.12
# A component keeps at least this many exchanges before it can end.
MIN_EXCHANGES = 2
# An exchange with fewer content words than this has no theme of its own: it joins the component before it.
MIN_WORDS = 4

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
STOP = set("""
about above after again against also always and any are because been before being below between both but
can could did does doing done down during each few for from further get got had has have having her here hers
him his how into its itself just let like make more most much must myself need new not now off once only other
our ours out over own same she should some such than that the their theirs them then there these they this
those through too under until very was were what when where which while who whom why will with would yes you
your yours the its lets let's dont don't it's i'm i'll ok okay thanks sure look see also use using used one
""".split())


def exchanges(turns: list) -> list[list[int]]:
    """Group turn indexes into exchanges: each starts at a user turn. Turns before the first user turn form one."""
    out: list[list[int]] = []
    for i, (role, _) in enumerate(turns):
        if role == "user" or not out:
            out.append([i])
        else:
            out[-1].append(i)
    return out


def words(text: str) -> Counter:
    """Content words of a text, lower case. Identifiers keep their underscores."""
    return Counter(w.lower() for w in _WORD.findall(text) if w.lower() not in STOP)


def cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b[k] for k, v in a.items() if k in b)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def components(turns: list, shift: float = SHIFT, min_exchanges: int = MIN_EXCHANGES) -> list[dict]:
    """Components of a session. `turns` is the parsed list of (role, text). Each component says which turns it holds
    and its words, so a caller can catalog the links in it."""
    groups = exchanges(turns)
    if not groups:
        return []
    vecs = [words(" ".join(turns[i][1] for i in g)) for g in groups]
    out: list[list[int]] = []  # each component: a list of exchange positions
    cur: list[int] = [0]
    centre = Counter(vecs[0])
    for k in range(1, len(groups)):
        v = vecs[k]
        thin = sum(v.values()) < MIN_WORDS
        if not thin and len(cur) >= min_exchanges and cosine(v, centre) < shift:
            out.append(cur)
            cur, centre = [k], Counter(v)
        else:
            cur.append(k)
            centre.update(v)
    out.append(cur)
    result = []
    for n, positions in enumerate(out):
        turn_ids = [i for p in positions for i in groups[p]]
        result.append(dict(index=n, turns=(min(turn_ids), max(turn_ids) + 1), exchanges=len(positions)))
    return result
