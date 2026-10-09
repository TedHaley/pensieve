"""Offline retrieval scorer for the code-finding tasks: which file does each method rank first?

Reads the main checkout's code chunks (text + stored vectors) straight from the index, embeds the task queries
with the production embedder, and scores ranking methods without touching the server. Methods are tried on the
dev split; the test split is only for confirming a winner.

  uv run python bench/retrieval.py                       # every method, dev split, both query styles
  uv run python bench/retrieval.py --split test -m dense,hybrid
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy import sparse

DB = Path.home() / ".pensieve" / "pensieve.db"
OUT = Path(__file__).parent / "out"
CACHE = OUT / "cache"

INSTRUCT = {
    "web": None,  # production: the model's default 'query' prompt (web search)
    "code": "Instruct: Given a description of what some code does, retrieve the source code that implements it\nQuery:",
}


def words_plain(text):
    """SQLite FTS5 unicode61-style tokens: runs of letters/digits, lowercased (snake_case splits, camelCase doesn't)."""
    return [w.lower() for w in re.findall(r"[A-Za-z0-9]+", text)]


SPLIT = "camel"


def tokens(text):
    return words(text) if SPLIT == "camel" else words_plain(text)


def words(text):
    """Identifier-aware tokens: camelCase and snake_case split into lowercase words (what grep users type)."""
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9]*", text):
        parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", w)
        out += [p.lower() for p in parts if len(p) > 1]
    return out


TESTY = re.compile(r"(^|/)(tests?|__tests__|spec|specs|__mocks__|mocks?|fixtures?|stories|e2e)/|"
                   r"([._-](test|spec|stories|mock)s?\.[a-z]+$)|(^|/)test_[^/]+$|/docs?/schema/")


SYMBOL = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function|module|interface|type|struct|fn|func|"
                    r"const|let|CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|FUNCTION))\s+([A-Za-z_][\w.]*)", re.M | re.I)


def symbols(texts, limit=20):
    seen = []
    for t in texts:
        for m in SYMBOL.finditer(t):
            if m.group(1) not in seen:
                seen.append(m.group(1))
    return seen[:limit]


VARIANTS = {  # name -> (model, max tokens, text format)
    "stored": None,
    "len1024": ("Qwen/Qwen3-Embedding-0.6B", 1024, "path"),
    "symbols": ("Qwen/Qwen3-Embedding-0.6B", 512, "symbols"),
}


class Repo:
    def __init__(self, root, variant="stored"):
        db = sqlite3.connect(DB)
        rows = db.execute("SELECT id, path, text, vec FROM code_chunks WHERE repo=? AND wt='' ORDER BY id",
                          (root,)).fetchall()
        self.root, self.ids = root, np.array([r[0] for r in rows])
        self.paths = [r[1] for r in rows]
        self.texts = [r[2] for r in rows]
        self.V = np.stack([np.frombuffer(r[3], np.float16) for r in rows]).astype(np.float32)
        self.files = sorted(set(self.paths))
        fidx = {f: i for i, f in enumerate(self.files)}
        self.fof = np.array([fidx[p] for p in self.paths])
        self.testy = np.array([bool(TESTY.search(f)) for f in self.files])
        if variant != "stored":
            self.V = self._reembed(variant)
        self._bm = {}

    def _reembed(self, variant):
        model, max_tokens, fmt = VARIANTS[variant]
        f = CACHE / f"v-{variant}-{Path(self.root).name}-{len(self.ids)}.npy"
        if f.exists():
            return np.load(f).astype(np.float32)
        from sentence_transformers import SentenceTransformer
        from pensieve import config
        m = SentenceTransformer(model, device=config.device())
        m.max_seq_length = max_tokens
        if fmt == "symbols":
            by = defaultdict(list)
            for p, t in zip(self.paths, self.texts):
                by[p].append(t)
            head = {p: f"{p}\nDefines: {', '.join(symbols(ts))}" for p, ts in by.items()}
            docs = [f"{head[p]}\n{t}" for p, t in zip(self.paths, self.texts)]
        else:
            docs = [f"{p}\n{t}" for p, t in zip(self.paths, self.texts)]
        order = np.argsort([len(d) for d in docs])  # length-sorted batches pad less
        V = np.zeros((len(docs), m.get_sentence_embedding_dimension()), np.float32)
        V[order] = m.encode([docs[i] for i in order], batch_size=16, normalize_embeddings=True, show_progress_bar=True)
        CACHE.mkdir(parents=True, exist_ok=True)
        np.save(f, V.astype(np.float16))
        return V

    def bm25(self, unit="chunk", k1=1.2, b=0.75):
        """Sparse BM25 matrix over chunks or whole files, path words included."""
        if unit in self._bm:
            return self._bm[unit]
        from sklearn.feature_extraction.text import CountVectorizer
        if unit == "chunk":
            docs = [f"{p} {t}" for p, t in zip(self.paths, self.texts)]
        elif unit == "path":
            docs = list(self.files)
        else:
            by = defaultdict(list)
            for p, t in zip(self.paths, self.texts):
                by[p].append(t)
            docs = [f"{f} " + " ".join(by[f]) for f in self.files]
        cv = CountVectorizer(analyzer=tokens, min_df=1)
        tf = cv.fit_transform(docs).tocsr().astype(np.float32)
        n, dl = tf.shape[0], np.asarray(tf.sum(1)).ravel()
        df = np.bincount(tf.indices, minlength=tf.shape[1])
        idf = np.log(1 + (n - df + 0.5) / (df + 0.5)).astype(np.float32)
        tf = tf.tocoo()
        denom = tf.data + k1 * (1 - b + b * dl[tf.row] / dl.mean())
        w = tf.data * (k1 + 1) / denom * idf[tf.col]
        M = sparse.csr_matrix((w, (tf.row, tf.col)), shape=tf.shape)
        self._bm[unit] = (cv, M)
        return self._bm[unit]

    def grep_counts(self, q):
        """A single case-insensitive grep for the whole query: files ranked by how often they contain it."""
        if not hasattr(self, "_lower"):
            self._lower = [t.lower() for t in self.texts]
        out = np.zeros(len(self.files), np.float32)
        ql = q.lower()
        for i, t in enumerate(self._lower):
            if ql in t:
                out[self.fof[i]] += t.count(ql)
        return out

    def defines(self):
        """symbol name -> files that define it."""
        if not hasattr(self, "_defs"):
            self._defs = defaultdict(set)
            for t, fi in zip(self.texts, self.fof):
                for m in SYMBOL.finditer(t):
                    self._defs[m.group(1).split(".")[-1]].add(int(fi))
        return self._defs

    def contains(self, term, word=True):
        """Files containing `term` (case-sensitive whole word for identifiers, case-insensitive text otherwise)."""
        out = np.zeros(len(self.files), bool)
        if word:
            rx = re.compile(r"(?<![\w])" + re.escape(term) + r"(?![\w])")
            for t, fi in zip(self.texts, self.fof):
                if term in t and rx.search(t):
                    out[fi] = True
        else:
            if not hasattr(self, "_lower"):
                self._lower = [t.lower() for t in self.texts]
            tl = term.lower()
            for t, fi in zip(self._lower, self.fof):
                if tl in t:
                    out[fi] = True
        return out

    def bm25_scores(self, q, unit="chunk"):
        cv, M = self.bm25(unit)
        qv = cv.transform([q])
        qv.data[:] = 1
        return np.asarray((M @ qv.T).todense()).ravel()


def embed_queries(texts, instruct):
    key = hashlib.sha1(json.dumps([texts, instruct]).encode()).hexdigest()[:16]
    f = CACHE / f"q-{key}.npy"
    if f.exists():
        return np.load(f)
    from pensieve.indexer import embedder
    m = embedder()
    v = m.encode(texts, batch_size=16, normalize_embeddings=True, show_progress_bar=False,
                 **({"prompt_name": "query"} if instruct is None else {"prompt": instruct}))
    CACHE.mkdir(parents=True, exist_ok=True)
    np.save(f, v)
    return v


def file_rank(scores_by_file, target):
    order = np.argsort(-scores_by_file, kind="stable")
    return int(np.flatnonzero(order == target)[0]) + 1


def to_files(chunk_scores, fof, nfiles, how="max"):
    out = np.full(nfiles, -np.inf, np.float32)
    np.maximum.at(out, fof, chunk_scores)
    return out


def rrf(*rankings, k=60):  # reciprocal-rank fusion
    s = 0
    for r in rankings:
        ranks = np.empty(len(r), np.int64)
        ranks[np.argsort(-r, kind="stable")] = np.arange(1, len(r) + 1)
        s = s + 1.0 / (k + ranks)
    return s


def methods(repo, qv, qtext):
    """Every method's per-file scores for one query."""
    dense = to_files(repo.V @ qv, repo.fof, len(repo.files))
    out = {"dense": dense}
    bmc = to_files(repo.bm25_scores(qtext, "chunk"), repo.fof, len(repo.files))
    bmf = repo.bm25_scores(qtext, "file")
    out["bm25-file"] = bmf
    out["grep"] = repo.grep_counts(qtext)
    out["bm25-chunk"] = bmc
    out["hybrid"] = rrf(dense, bmc)
    out["hybrid-f"] = rrf(dense, bmf)
    # weighted fusion: keywords as a lighter vote
    for w in (0.3, 0.5):
        out[f"wrrf{w}"] = rrf(dense, k=60) + w * rrf(bmc, k=60)
    # z-scored linear blend of dense and chunk-bm25
    z = lambda x: (x - x[np.isfinite(x)].mean()) / (x[np.isfinite(x)].std() + 1e-9)
    for w in (0.1, 0.2, 0.3):
        out[f"lin{w}"] = z(dense) + w * z(bmc)
    # file score from its two best chunks
    cs = repo.V @ qv
    order = np.lexsort((-cs, repo.fof))
    f_sorted, c_sorted = repo.fof[order], cs[order]
    first = np.r_[True, f_sorted[1:] != f_sorted[:-1]]
    second = np.r_[False, (~first[1:]) & first[:-1]]
    top2 = np.zeros(len(repo.files), np.float32)
    top2[f_sorted[second]] = c_sorted[second]
    out["dense-top2"] = dense + 0.2 * top2
    base = out["lin0.2"]
    for d in (0.5, 1.0):
        out[f"lin0.2-t{d}"] = base - d * repo.testy
    pth = repo.bm25_scores(qtext, "path")
    for w in (0.1, 0.2):
        out[f"lin0.2+p{w}"] = base + w * z(pth)
        out[f"lin0.2+p{w}-t0.5"] = base + w * z(pth) - 0.5 * repo.testy
    out.update(exact_boosts(repo, qtext, base - 0.5 * repo.testy))
    out["prod"] = prod(repo, qv, qtext)
    return out


IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def query_idents(repo, q):
    """Words in the query that look like code names: snake_case, camelCase, or a name the repo defines."""
    defs = repo.defines()
    out = []
    ws = IDENT.findall(q)
    for w in ws:
        if "_" in w.strip("_") or re.search(r"[a-z][A-Z]|[A-Z]{2,}[a-z]|[a-z]\d|^[A-Z][a-z]+[A-Z]", w) or \
                (len(ws) == 1 and w in defs):
            out.append(w)
    return out


def exact_boosts(repo, q, base):
    """Semantic base plus boosts for files that contain the query's exact text or code names (and define them)."""
    out = {}
    phrase = repo.contains(q.strip(), word=False) if len(q.strip()) >= 8 else np.zeros(len(repo.files), bool)
    names = query_idents(repo, q)
    has = np.zeros(len(repo.files), np.float32)
    dfn = np.zeros(len(repo.files), np.float32)
    for n in names:
        c = repo.contains(n)
        if c.any():
            has += c / np.log2(1 + c.sum())  # a name in hundreds of files says little about which one
        d = repo.defines().get(n)
        if d and len(d) <= 3:
            dfn[list(d)] += 1
    for A, B, C in ((3, 1, 2), (3, 2, 2), (5, 1, 1), (3, 2, 3)):
        out[f"smart{A}{B}{C}"] = base + A * phrase + B * has + C * dfn
    if phrase.any() and names != [q.strip()]:  # how often, and whether the case matches (not for a bare name)
        cnt = repo.grep_counts(q.strip())
        cased = _cased(repo, q.strip())
        out["smartc"] = base + 3 * phrase + np.log1p(cnt) + 1.0 * cased + 2 * has + 2 * dfn
    else:
        out["smartc"] = out["smart322"]
    return out


def prod(repo, qv, q, K=400, W=(0.2, 0.5, 3, 1, 1, 2, 2)):
    """What search.py does: candidates from the top-K chunks by meaning and by keywords plus exact phrase/name
    matches; file scores normalised over the candidate files only."""
    wb, wt, wp, wc, wcase, wn, wd = W
    cs = repo.V @ qv
    bm = repo.bm25_scores(q, "chunk")
    cand = set(np.argpartition(-cs, K)[:K].tolist())
    nz = np.flatnonzero(bm)
    cand |= set(nz[np.argsort(-bm[nz])[:K]].tolist())
    ql = q.strip()
    names = query_idents(repo, q)
    phrase_chunks = [i for i, t in enumerate(repo.texts) if len(ql) >= 8 and ql.lower() in t.lower()][:500]
    cand |= set(phrase_chunks)
    name_chunks = {}
    for n in names:
        rx = re.compile(r"(?<![\w])" + re.escape(n) + r"(?![\w])")
        name_chunks[n] = [i for i, t in enumerate(repo.texts) if n in t and rx.search(t)][:500]
        cand |= set(name_chunks[n])
    cand = np.array(sorted(cand))
    files = np.unique(repo.fof[cand])
    fpos = {f: i for i, f in enumerate(files)}
    sem = np.full(len(files), -1e9, np.float32)
    kw = np.zeros(len(files), np.float32)
    for c in cand:
        j = fpos[repo.fof[c]]
        sem[j] = max(sem[j], cs[c])
        kw[j] = max(kw[j], bm[c])
    z = lambda x: (x - x.mean()) / (x.std() + 1e-9)
    score = z(sem) + wb * z(kw) - wt * repo.testy[files]
    phrase = np.zeros(len(files), np.float32)
    cnt = np.zeros(len(files), np.float32)
    cased = np.zeros(len(files), np.float32)
    for c in phrase_chunks:
        j = fpos[repo.fof[c]]
        phrase[j] = 1
        cnt[j] += repo.texts[c].lower().count(ql.lower())
        cased[j] = max(cased[j], float(ql in repo.texts[c]))
    if names != [ql]:
        score += wp * phrase + wc * np.log1p(cnt) + wcase * cased
    else:
        score += wp * phrase
    defs = repo.defines()
    for n in names:
        hit = np.zeros(len(files), np.float32)
        for c in name_chunks[n]:
            hit[fpos[repo.fof[c]]] = 1
        if hit.any():
            score += wn * hit / np.log2(1 + hit.sum())
        d = defs.get(n)
        if d and len(d) <= 3:
            for f in d:
                if f in fpos:
                    score[fpos[f]] += wd
    out = np.full(len(repo.files), -1e9, np.float32)
    out[files] = score
    return out


def _cased(repo, q):
    out = np.zeros(len(repo.files), bool)
    for t, fi in zip(repo.texts, repo.fof):
        if q in t:
            out[fi] = True
    return out


_rerankers = {}
RERANKERS = {
    "qwen": "tomaarsen/Qwen3-Reranker-0.6B-seq-cls",
    "bge": "BAAI/bge-reranker-v2-m3",
    "jina": "jinaai/jina-reranker-v2-base-multilingual",
}


def reranker(name):
    if name not in _rerankers:
        from sentence_transformers import CrossEncoder
        from pensieve import config
        kw = {"trust_remote_code": True} if name == "jina" else {}
        _rerankers[name] = CrossEncoder(RERANKERS[name], device=config.device(), max_length=768, **kw)
    return _rerankers[name]


def rerank_scores(name, tid, style, query, docs):
    """Cross-encoder scores for (query, doc) pairs, cached per task so method tweaks don't re-run the model."""
    key = hashlib.sha1(json.dumps([name, tid, style, docs]).encode()).hexdigest()[:20]
    f = CACHE / f"rr-{key}.npy"
    if f.exists():
        return np.load(f)
    m = reranker(name)
    q = query
    if name == "qwen":  # the seq-cls conversion expects Qwen3-Reranker's chat template
        pre = ('<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the '
               'Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n')
        suf = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
        ins = "Given a description of what some code does, retrieve the source code that implements it"
        pairs = [(f"{pre}<Instruct>: {ins}\n<Query>: {q}\n", f"<Document>: {d}{suf}") for d in docs]
    else:
        pairs = [(q, d) for d in docs]
    sc = np.asarray(m.predict(pairs, batch_size=8, show_progress_bar=False), np.float32)
    np.save(f, sc)
    return sc


def candidates(repo, qv, file_scores, n, per_file=1, chars=1500):
    """Top-n files by the first-stage score, each as its path plus its best-matching chunk(s) by meaning."""
    top = np.argsort(-file_scores, kind="stable")[:n]
    cs = repo.V @ qv
    docs = []
    for fi in top:
        idx = np.flatnonzero(repo.fof == fi)
        best = idx[np.argsort(-cs[idx])[:per_file]]
        docs.append(repo.files[fi] + "\n" + "\n...\n".join(repo.texts[i][:chars] for i in sorted(best)))
    return top, docs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev")
    ap.add_argument("-m", "--methods", default="")
    ap.add_argument("--instruct", default="web,code")
    ap.add_argument("--style", default="query,desc,mixed,ident,literal")
    ap.add_argument("--repos", default="")
    ap.add_argument("--show", action="store_true", help="per-task ranks")
    ap.add_argument("--rerank", default="", help="reranker names (qwen,bge,jina) applied to --base")
    ap.add_argument("--base", default="hybrid", help="first-stage method the reranker reorders")
    ap.add_argument("--topn", type=int, default=30)
    ap.add_argument("--variant", default="stored", help="chunk embeddings: " + ", ".join(VARIANTS))
    ap.add_argument("--qmodel", default="", help="query embedding model, when the variant uses another model")
    ap.add_argument("--tok", default="camel", help="BM25 tokens: camel | plain (SQLite unicode61)")
    a = ap.parse_args()
    global SPLIT
    SPLIT = a.tok
    if a.split == "sessions":  # real first prompts; any file the session edited counts
        tasks = [json.loads(l) for l in (OUT / "sessions.jsonl").read_text().splitlines()]
        a.style = "query"
    else:
        tasks = [json.loads(l) for f in sorted(OUT.glob("*/tasks-v2.jsonl")) for l in f.read_text().splitlines()]
        tasks = [t for t in tasks if a.split == "all" or t["split"] == a.split]
    if a.repos:
        tasks = [t for t in tasks if Path(t["repo"]).name in a.repos.split(",")]
    want = set(a.methods.split(",")) if a.methods else None
    res = defaultdict(list)  # (instruct, style, method) -> ranks
    by_repo = defaultdict(list)
    for t in tasks:
        by_repo[t["repo"]].append(t)
    for root, ts in by_repo.items():
        repo = Repo(root, a.variant)
        print(f"{Path(root).name}: {len(repo.ids)} chunks, {len(repo.files)} files, {len(ts)} tasks", file=sys.stderr)
        for ins in a.instruct.split(","):
            for style in a.style.split(","):
                tss = [t for t in ts if t.get(style)]
                if not tss:
                    continue
                Q = embed_queries([t[style] for t in tss], INSTRUCT[ins])
                for t, qv in zip(tss, Q):
                    ans = t.get("files") or (t.get("ident_files") if style == "ident" else None) or [t["file"]]
                    targets = [repo.files.index(f) for f in ans if f in repo.files]
                    ms = methods(repo, qv, t[style])
                    for rr in filter(None, a.rerank.split(",")):
                        top, docs = candidates(repo, qv, ms[a.base], a.topn)
                        r_sc = rerank_scores(rr, t["id"], style, t[style], docs)
                        sc = ms[a.base].copy()
                        sc[top] = sc.max() + 10 + r_sc  # reranked candidates first, in reranker order
                        ms[f"{a.base}>{rr}"] = sc
                        # blend: first-stage and reranker scores, both z-scored over the candidates
                        zb = (ms[a.base][top] - ms[a.base][top].mean()) / (ms[a.base][top].std() + 1e-9)
                        zr = (r_sc - r_sc.mean()) / (r_sc.std() + 1e-9)
                        for w in (0.25, 0.5):
                            sc = ms[a.base].copy()
                            sc[top] = sc.max() + 10 + zb + w * zr
                            ms[f"{a.base}+{rr}{w}"] = sc
                    for m, sc in ms.items():
                        if want and m not in want:
                            continue
                        r = min(file_rank(sc, x) for x in targets)
                        res[(ins, style, m)].append(r)
                        if a.show:
                            print(f"  {t['id']} {ins} {style} {m}: {r}")
    print(f"\n{len(tasks)} tasks ({a.split})\n{'instruct':<9}{'style':<7}{'method':<12}{'R@1':>6}{'R@5':>6}{'R@10':>6}{'MRR':>7}{'n':>5}")
    for (ins, style, m), rs in sorted(res.items()):
        rs = np.array(rs)
        print(f"{ins:<9}{style:<7}{m:<12}{(rs <= 1).mean():6.2f}{(rs <= 5).mean():6.2f}{(rs <= 10).mean():6.2f}"
              f"{(1 / rs).mean():7.3f}{len(rs):5}")


if __name__ == "__main__":
    main()
