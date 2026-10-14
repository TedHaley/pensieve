# Search strategies: what we tried, and what it gave

This page lists every search strategy we tried for Pensieve, with its accuracy and speed against grep on one
validation set. It has three parts: ranking strategies (one search, no agent), changes for agents (Claude Code with
Pensieve), and what we suggest next.

## How to read the numbers

**The validation set** ([`validation.py`](validation.py), `bench/out/validation.jsonl`) has 2,377 queries in four
repos (504 to 34,847 files). Each query has one right file. The queries are of six kinds:

| Kind | Queries | Example |
|---|---:|---|
| question | 334 | "email sent once when free trial ends, skip sync-only users" |
| name+words | 333 | "pg_inherits check whether table is a partition" |
| name | 219 | `batch_update` |
| string | 714 | `"No archived versions to publish."`, typed without quotes |
| ambiguous | 404 | a string from the code that 2 or more files contain |
| runtime | 373 | a message as it shows up in a log: `company_id[20879] not found in directory` |

- **@1, @3, @8:** the share of queries where the right file is first, in the top 3, or in the top 8 (the MCP tool shows 8).
- **test @1:** only the 1,302 queries from tasks that we never used to tune Pensieve.
- **grep:** one case-insensitive grep, with files ranked by match count and ties in random order. For runtime
  messages, two more grep rows: what an agent without the source would grep (the longest part without digits), and
  the template's real fixed text (an upper limit for grep).
- **ms p50:** the median time of one search through the MCP tool, on an Apple M4 Max. Other benchmarks ran at the
  same time, so compare times only within one table.

For an ambiguous string, the query does not say which copy is meant, and the set chooses the answer at random. No
tool can do better than chance there at @1.

## Ranking strategies (one search, no agent)

The MCP rows send each query through the `search` tool of a running server, scoped to the repo as for an agent. The
other rows score each method offline ([`retrieval.py`](retrieval.py) `--tasks`), on the main checkout only.

| Strategy | question | name+words | name | string | ambiguous | runtime | all @1 | all @3 | all @8 | test @1 | ms p50 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| grep, whole query | 0.0 | 0.0 | 51.3 | 85.2 | 39.7 | 0.0 | 37.1 | 49.4 | 53.1 | 35.2 | |
| grep, longest part without digits | 0.0 | 0.0 | 51.3 | 85.2 | 39.7 | 20.5 | 40.3 | 53.8 | 58.4 | 38.5 | |
| grep, the template's real fixed text | 0.0 | 0.0 | 51.3 | 85.2 | 39.7 | 62.1 | 46.8 | 62.1 | 67.2 | 44.8 | |
| **MCP 0.6.3** | 47.0 | 71.2 | 93.2 | 80.8 | 35.6 | 31.4 | 60.4 | 78.6 | 88.1 | 61.0 | 83 |
| **MCP + grep order, fragments, fixes** | 47.6 | 70.9 | 93.6 | 84.6 | 37.1 | 56.6 | 65.8 | 85.1 | 91.6 | 66.7 | 85 |
| **MCP 0.6.4** (+ one model call per query) | 47.9 | 70.9 | 94.1 | 84.6 | 37.1 | 56.3 | **65.9** | **85.1** | **91.6** | **66.7** | 73 |
| meaning only (dense), web-search instruction | 33.8 | 65.5 | 52.1 | 36.6 | 15.8 | 23.9 | 36.1 | 53.8 | 66.8 | 38.0 | |
| meaning only (dense), code-search instruction | 41.3 | 65.5 | 57.5 | 35.0 | 14.1 | 22.0 | 36.6 | 53.9 | 65.5 | 39.2 | |
| keywords only (BM25 on chunks) | 32.9 | 73.3 | 44.7 | 67.6 | 29.0 | 42.4 | 50.9 | 73.0 | 83.2 | 50.9 | |
| keywords only (BM25 on whole files) | 31.7 | 66.1 | 43.8 | 60.4 | 23.3 | 36.2 | 45.5 | 66.6 | 78.9 | 46.3 | |
| RRF fusion of dense and BM25 | 41.6 | 73.0 | 55.7 | 49.0 | 23.5 | 36.5 | 45.6 | 64.8 | 78.4 | 46.8 | |
| weighted RRF (BM25 at 0.3) | 44.9 | 70.3 | 58.9 | 41.9 | 20.5 | 27.3 | 41.9 | 60.8 | 72.4 | 44.2 | |
| z-score blend, dense + 0.2 BM25 (`lin0.2`) | 48.8 | 76.3 | 60.7 | 51.7 | 25.5 | 35.9 | 48.6 | 66.7 | 78.0 | 49.8 | |
| `lin0.2`, test files ranked lower | 51.2 | 77.5 | 63.0 | 51.7 | 23.3 | 36.2 | 49.0 | 66.5 | 78.2 | 50.4 | |
| `lin0.2` + path words | 47.6 | 73.0 | 58.4 | 48.0 | 24.5 | 32.2 | 45.9 | 63.7 | 75.5 | 47.4 | |
| dense, file score includes its 2nd-best chunk | 40.4 | 58.3 | 49.8 | 35.0 | 13.9 | 22.3 | 34.8 | 51.4 | 63.9 | 36.5 | |
| `lin0.2` + exact-text and name boosts | 51.5 | 75.4 | 91.3 | 81.0 | 36.4 | 38.9 | 62.8 | 82.0 | 89.6 | 63.2 | |
| 0.6.3 ranking rules, offline | 52.7 | 73.6 | 93.6 | 84.2 | 40.1 | 37.5 | 64.3 | 82.5 | 89.5 | 65.1 | |
| 0.6.3 rules, top 30 re-sorted by Qwen3-Reranker-0.6B | 54.8 | 72.7 | 74.9 | 50.1 | 25.7 | 34.3 | 49.6 | 69.4 | 81.0 | 50.9 | ~2,200 more |
| 0.6.3 rules + 0.25 × Qwen3-Reranker score | 54.5 | 73.6 | 94.1 | 84.3 | 39.4 | 39.4 | 64.8 | 82.8 | 89.9 | 66.1 | ~2,200 more |
| 0.6.3 rules, top 30 re-sorted by bge-reranker-v2-m3 | 41.6 | 71.8 | 64.4 | 59.4 | 29.2 | 35.9 | 50.3 | 70.0 | 81.8 | 51.0 | ~2,200 more |
| 0.6.3 rules + 0.25 × bge-reranker score | 50.6 | 76.3 | 93.6 | 83.6 | 38.4 | 41.0 | 64.5 | 83.2 | 90.0 | 65.5 | ~2,200 more |

What the table shows:

- **No single signal is enough.** Meaning alone finds 36–37% first, and keywords alone 46–51%. Grep alone finds 37%:
  it cannot match plain words, but it is strong on exact strings.
- **The z-score blend beats rank fusion.** `lin0.2` gets 48.6% against 45.6% for RRF and 41.9% for weighted RRF.
- **Exact-text and name boosts give the largest gain.** Names go from 61% to 91–94% first, and all queries from 49% to
  63–64%.
- **0.6.4 is better than 0.6.3 by 5.5 points overall** (60.4% → 65.9% first, 78.6% → 85.1% in the top 3). Most of
  the gain is on runtime messages (31% → 56%) and strings (81% → 85%). Three changes made it: grep's order among files
  that contain the query, fragment search for runtime messages, and two bugs fixed (a linked worktree's copy hid the
  main file, and `--` excluded every result with a hyphen).
- **0.6.4 beats grep on every kind except one.** On strings (84.6% against 85.2%) and ambiguous strings (37.1%
  against 39.7%) the two are level within noise. On runtime messages, only the oracle grep, which is given the
  template's real fixed text, does better (62% against 56%).
- **Rerankers are not worth their time.** Re-sorted by a reranker alone, names fall from 94% to 65–75%. Blended at
  0.25, the Qwen3 reranker adds 0.5 points overall (2 points on questions). Each reranker adds about 2.2 s per query,
  about 30 times the time of a search.
- **The time for one search went from 83 to 73 ms** at the median through MCP. The grep order and the fragment search
  added 2 ms, and one model call per query instead of two saved 12 ms.

### Embedding variants (three smaller repos, 1,627 queries)

These need a full re-embedding, so they ran only on the three smaller repos.

| Strategy | question | name+words | name | string | ambiguous | runtime | all @1 | all @3 | test @1 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| dense, chunks as stored (512 tokens) | 49.7 | 72.4 | 61.3 | 37.2 | 15.8 | 25.7 | 38.9 | 57.7 | 40.9 |
| dense, 1024-token chunks | 50.3 | 71.4 | 60.4 | 36.8 | 16.2 | 25.4 | 38.7 | 57.8 | 40.2 |
| dense, the names a file defines added to each chunk | 52.8 | 71.0 | 66.0 | 34.8 | 14.5 | 25.7 | 38.4 | 56.8 | 39.8 |
| 0.6.3 rules, chunks as stored | 62.1 | 79.0 | 96.2 | 88.9 | 40.3 | 42.3 | 68.0 | 86.5 | 68.7 |
| 0.6.3 rules, 1024-token chunks | 62.6 | 78.6 | 95.3 | 89.1 | 39.3 | 42.3 | 67.9 | 86.5 | 68.2 |
| 0.6.3 rules, names added to each chunk | 60.0 | 76.7 | 95.3 | 88.7 | 37.0 | 43.4 | 66.9 | 86.4 | 66.9 |

Neither variant helps the full ranking. Longer chunks change nothing. Names added to each chunk help dense search on
questions and names (+3 and +5 points), but the full ranking already finds names, and it loses 1 point overall.

Not tried: Qwen3-Embedding-4B (downloaded, never measured).

**Another model: EmbeddingGemma 2** (`google/embeddinggemma-2`, its 270M text part, 768 dimensions), on the same
three repos and 1,625 queries. Documents are embedded as `title: <path> | text: <chunk>`, queries with its
search-query prefix (the code-retrieval prefix is 0.4 points worse).

| Strategy | question | name+words | name | string | ambiguous | runtime | all @1 | all @3 | test @1 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| dense, Qwen3-Embedding-0.6B | 49.7 | 72.7 | 61.3 | 37.2 | 15.8 | 25.7 | 39.0 | 57.8 | 41.0 |
| dense, EmbeddingGemma 2 | 55.4 | 77.0 | 51.9 | 38.5 | 13.2 | 30.1 | 40.2 | 59.0 | 40.6 |
| 0.6.3 rules, Qwen3-Embedding-0.6B | 62.1 | 79.4 | 96.2 | 89.1 | 40.3 | 42.3 | 68.1 | 86.6 | 68.8 |
| 0.6.3 rules, EmbeddingGemma 2 | 64.6 | 82.8 | 94.3 | 88.7 | 38.3 | 40.8 | 68.0 | 87.1 | 67.4 |
| grep, whole query (the same queries) | | | | | | | 39.6 | 53.2 | 37.1 |

- **The same quality, at about twice the speed.** Over all queries the two models are level (68.0 against 68.1 @1;
  each ranks first on about 110 queries where the other does not). EmbeddingGemma 2 is better on questions (+2.5)
  and names with words (+3.4), and worse on bare names (−1.9) and ambiguous strings (−2.0).
- **Speed on the same 1,200 chunks (M4 Max):** Qwen3 on MLX (the shipped path) 22.3 chunks/s; EmbeddingGemma 2 on
  MLX 42.7 chunks/s, with each batch padded to a multiple of 64 tokens (24.9 chunks/s without: each new length
  builds new GPU kernels). The padding does not speed up Qwen3 (20.7 chunks/s).
- **What shipped:** EmbeddingGemma 2 as an option in Settings → General → Search model, on MLX (`gemma_mlx.py`,
  cosine 0.996 or more to sentence-transformers). An index keeps the model it was built with; switching embeds the
  stored chunk text again in the background (`reembed.py`).

## Changes for agents (Claude Code with Pensieve)

[`agent_search.py`](agent_search.py) runs Claude Sonnet headless through Claude Code on 72 held-out tasks in the same
four repos, each twice. An arm is one setup. All arms of a batch run interleaved, in one batch.

- **grep:** Read, Grep and Glob only.
- **guided:** those tools plus Pensieve's `search`, `read` and `similar`, and the "search before you grep"
  instructions block ([`instructions.md`](../pensieve/integrations/instructions.md)). This is the recommended install.
- **guided + X:** guided with one change.

Cost depends on the state of Claude's prompt cache, so compare costs only within one batch. Calls, tokens and time
can be compared between batches.

### Batch 1: one change at a time (1,296 runs)

| Arm | Found | Calls per answer | ≤1 call | Grep/Glob calls | Tokens (median) | Seconds (median) | Found vs guided (won–lost) |
|---|---:|---:|---:|---:|---:|---:|---:|
| grep | 70.8% | 2.89 | 42% | 2.33 | 46k | 12.7 | 4–28 |
| guided | 87.5% | 1.97 | 54% | 0.93 | 51k | 10.9 | |
| + **inject**: Pensieve's matches added to the prompt | 89.6% | **1.07** | **77%** | 0.56 | **30k** | **8.6** | 6–3 |
| + map: an overview of the repo's folders in the system prompt | 91.0% | 1.73 | 65% | 0.36 | 47k | 10.5 | 5–0 |
| + defs: `definition` and `references` tools | 91.0% | 1.98 | 52% | 0.97 | 52k | 10.9 | 6–1 |
| + tags: each hit says why it ranked | 89.6% | 2.00 | 56% | 0.94 | 51k | 10.8 | 6–3 |
| + grep: an indexed `grep` tool that lists every match | 85.4% | 1.96 | 56% | 0.78 | 51k | 11.2 | 3–6 |
| + many: `search_many`, several queries in one call | 87.5% | 2.08 | 52% | 1.02 | 52k | 11.6 | 4–4 |
| + fulldef: all of the first hit's code when it is certain | 87.5% | 2.19 | 55% | 1.08 | 51k | 11.3 | 4–4 |

Agents used the new tools (19 `definition`, 36 `grep` and 34 `search_many` calls), but only `inject` changed the
number of calls. The indexed grep answered in 0.1–1 s on the 34,847-file repo, against 6–11 s for ripgrep, but
agents did not need it.

### Batch 2: confirming (576 runs)

| Arm | Found | Calls per answer | ≤1 call | Tokens (median) | Seconds (median) | Cost per answer | Found vs guided |
|---|---:|---:|---:|---:|---:|---:|---:|
| grep | 72.9% | 3.31 | 39% | 46k | 9.7 | $0.047 | 5–23 |
| guided | 85.4% | 1.81 | 55% | 51k | 8.5 | $0.033 | |
| + inject (the real hook) | 88.9% | **1.04** | **78%** | **30k** | **6.5** | $0.039 | 8–3 |
| + inject + map + defs | 88.2% | 0.97 | 81% | 31k | 6.9 | $0.040 | 9–5 |

`inject` shipped in 0.6.4 as the optional hook `claude-prompt-search`. Map and defs add almost nothing on top of it.

### Batch 3: cost (288 runs, the user's own hooks kept out)

| Arm | Found | Calls per answer | Turns | Cache writes | Cache reads | Output | Cost per answer |
|---|---:|---:|---:|---:|---:|---:|---:|
| guided | 92.4% | 1.72 | 2.72 | 7,376 | 41,107 | 373 | $0.0374 |
| + inject | 88.9% | 1.15 | 2.15 | 7,633 | 31,735 | 367 | $0.0374 |

The cost per answer is the same. The hook adds about 250 tokens of cache writes (the matches are new text on each
prompt), and saves about 9,400 tokens of cache reads (one turn fewer). The higher cost in batch 2 came from the state
of the prompt cache, not from the hook.

Over the three batches, `inject` cut tool calls per answer by 33–46%. Its accuracy is level with guided: it won 16
answers and lost 13 (6–3, 8–3, then 2–7). In some of the lost answers, the agent answered from the injected hits
without reading a file. The hook's text says "If a hit answers it, use it". A line like the instructions block's "if
two hits fit, read both" is the next thing to test.

In all three batches, the user's own Claude Code hook that asks before indexing a repo ran inside some benchmark
sessions and ended 12–42 runs early. These runs were spread over all arms, so the comparisons hold, but the "found"
rates are a little low. `agent_search.py` now loads only the repo's settings (`--setting-sources project,local`).

### Earlier agent runs (other task sets, Pensieve 0.4–0.5)

| Version | What changed | Result |
|---|---|---|
| pilot (0.4.2) | Pensieve tools, no instructions | The agent made 0 Pensieve calls in 10 tasks. Without instructions, agents don't use it. |
| v2 | code-search instruction, `lin0.2` blend, exact and name boosts, test files lower | guided 66/72 against grep 62/72, 2.2 against 3.6 calls |
| v3 | 10 tools by default instead of 23 | the fixed cost per run about 45% lower |
| v4 | one line per hit (`path:line snippet [id]`), about 380 tokens per search instead of 1,400 | same accuracy |
| v5a | each hit shows its enclosing definition and the matched line; "names you would guess" in the tool description | 1.95 calls; half of the runs still made a follow-up call |
| v5b | numbered lines of the top 3 hits; other worktrees' copies ranked last | 1.86 calls |
| v5d | "if two hits fit, read both" | fixed early wrong answers on tasks with two candidates |
| v5test | all of v5, held-out tasks | 131/144 against 123/144 for grep, 43% fewer calls, 18% cheaper, 21% faster |

### Clean runs: Pensieve in every run against grep (2026-10-13)

Batches 1–3 loaded the user's own Claude Code settings: in them, guided used Pensieve in only 93–119 of 144 runs.
These two batches load only the repo's settings (`--setting-sources project,local`). The user's global
`~/.claude/CLAUDE.md` still loads in every arm (it is not a settings file): in 3–4 grep runs per batch, the agent
tried a Pensieve tool that the arm does not have, then used grep. `only` is Pensieve's tools and Read, with no Grep or
Glob. v8 adds a note to a hit whose matched code is a near-copy of a higher hit's (`_mark_near_copies`).

| Batch | Arm | Used Pensieve | Found | Without twin tasks | Seconds (mean) | Cost per answer | Calls |
|---|---|---:|---:|---:|---:|---:|---:|
| v7clean | grep | — | 129/144 | 123/134 | 15.2 | $0.050 | 3.34 |
| v7clean | guided | 144/144 | 129/144 | 123/134 | 11.4 | $0.038 | 1.72 |
| v7clean | only | 144/144 | 127/144 | 121/134 | 11.1 | $0.037 | 1.60 |
| v8 | grep | — | 129/144 | 123/134 | 9.5 | $0.051 | 3.42 |
| v8 | guided | 144/144 | 129/144 | 123/134 | 6.8 | $0.039 | 1.81 |
| v8 | only | 144/144 | 130/144 | 123/134 | 6.5 | $0.038 | 1.70 |

- **Accuracy is level with grep; cost and time are not.** Guided is 23–25% cheaper and 25–29% faster than grep;
  `only` is 25–27% cheaper and 27–32% faster. The accuracy gap in batches 1–3 (73–80% for grep) came from runs with
  the user's own settings.
- **Plain questions** (the `query` style, 32 runs per arm in v8): grep 24, guided 24, only 25 found. Grep gets there
  by searching more: 15.8 s against 8.3 s (guided) and 6.7 s (only).
- **Why both miss the same tasks.** 6 tasks are missed in every run of every arm: the description fits a near-copy
  as well as the answer (an identical `mark.js` in two folders, `credible (1).tpl.sql` beside `credible.tpl.sql`,
  two Ruby permission classes that differ only in the gating class they call). Pensieve ranked the answer first or
  second in most of them, but nothing in the description tells the copies apart. `bench/twins.py` marks the tasks
  whose answer chunk has a near-identical twin (at least 90% the same text), and `bench/clean_report.py` reports
  them apart. It finds 5 of the 72 test tasks; some of the 6 differ by more than 10%.
- **The near-copy note changed nothing measurable:** on the twin tasks, guided found 6 of 10 runs in both batches.
- **This task set suits grep:** most descriptions name words that are in the code, and an agent that runs grep
  several times reaches the same file. It cannot show an accuracy gain; it does show the cost and time saved.

### Repo overviews: the `overview` tool

The task: "Give me a high-level overview of this repository" in each of the five repos. `overview` shows where things
are in a repo, from the index: size, languages, folders two levels deep, the README's start, dependencies, the largest
files, and topics (files grouped by meaning) with their files and authors. It answers in about 1 s.

- **grep:** Read, Grep and Glob only.
- **overview tool:** the agent calls `overview`, and its instructions say to read a part's main files before it
  describes the part, and to name only paths that it saw.
- **overview at start:** the same overview, given in the prompt before the agent starts (as a SessionStart hook
  would). The time to get it counts.

A first judge listed the claims it thought false. A second Claude run checked each one in the code, and only
confirmed claims count. The second run rejected about half of them, so a single judge is not enough. 3 runs per repo
and setup.

**Repos with their CLAUDE.md** (4 of the 5 have one, 136–394 lines; Claude Code loads it into every session):

| Setup | Wrong claims (15 answers) | Coverage | Seconds (median) | Calls | Tokens | Cost |
|---|---:|---:|---:|---:|---:|---:|
| grep | 3 | 3.7 / 5 | 10.0 | 1.5 | 25.5k | $0.035 |
| overview tool | 2 | 5.0 / 5 | 11.9 | 1.2 | 36.6k | $0.044 |
| overview at start | 2 | 4.6 / 5 | 11.0 | 0.7 | 27.7k | $0.042 |

**The same repos without any CLAUDE.md** (the agent works in a copy of the tracked files; Pensieve uses the index of
the original):

| Setup | Wrong claims (15 answers) | Coverage | Seconds (median) | Calls | Tokens | Cost |
|---|---:|---:|---:|---:|---:|---:|
| grep | 10 | 4.3 / 5 | 13.0 | 4.1 | 35.1k | $0.056 |
| **overview tool** | **4** | **4.6 / 5** | **12.0** | **1.4** | **28.1k** | **$0.034** |
| overview at start | 6 | 4.3 / 5 | 13.2 | 1.9 | 30.2k | $0.044 |

- **With a CLAUDE.md, grep needs no tool:** the agent rewrites the notes, often with 0 calls. `overview` only adds
  text to read, so it is 1–2 s slower.
- **Without one, the overview tool is more accurate and faster:** 60% fewer wrong claims, 1 s faster, a third of the
  calls, and 39% cheaper. It was faster in three of the five repos.
- **Stale notes cause errors that the overview prevents.** Affinity's CLAUDE.md says `workspaces/apps/` holds the CRM
  app, but it does not. Grep repeated that in 4 of 6 answers. The overview lists what `workspaces/apps/` holds, and
  no overview run repeated it.
- **Some errors happen in every setup:** "PySpark 3.5" (the repo pins 4.1.0) and "a bronze/silver/gold layout" (the
  notes say core/ has none). They come from the model's own assumptions.
- **What shipped:** `overview` as a default tool, and an instructions line to call it for a repo with no CLAUDE.md.
- Earlier versions of the tool were less accurate. Each version is a separate fix:
  - with only 2 topics for a 73-file repo, the agent left out the indexer and the web UI
  - small repos (150 files or fewer) now list every file
  - the largest files are listed, so one big file cannot hide in a topic
  - the README's start and the dependencies say what the repo is
  - two folder levels let the agent check the notes against the code

## Suggestions for agentic use

These are the changes that we expect to save an agent the most calls, in order:

1. **Make `claude-prompt-search` the default once its accuracy holds.** It is the only change that cut calls by a
   third or more, and it costs the same. First test a text that asks the agent to read both files when two hits fit.
   Equivalent hooks for Codex and Gemini CLI would give the same to those agents.
2. **Embed each distinct chunk only once.** Indexing is bound by the embedding model: on pomander and databricks-bfp,
   embedding took 99% of the wall time (about 30 chunks per second on the GPU), and git blame ran beside it. Linked
   worktrees are mostly copies of the main checkout, so reusing the vector of an identical chunk would index a
   worktree in seconds.
3. **Tell the agent how sure a result is.** Tags did not cut calls when the agent searched for itself, but a hit that
   says "exact text, in 1 file" can let the agent skip a confirming grep. Test it again with `inject`.
4. **A `pensieve search` command for the shell.** Agents without MCP can then use Pensieve, and so can scripts and
   hooks.
5. **Index what agents look for and Pensieve skips.** Hand-written files over 200 KB were not indexed (fixed after
   0.6.4). Generated files stay out.
6. **Measure real sessions.** The Usage tab logs every MCP call and every hook call. If we match them with what the
   agent read or edited next, we get the hit rate of real searches, not of benchmark tasks.

## How to run it

```bash
PENSIEVE_DATA=<copy> .venv/bin/python bench/validation.py                    # the validation set
PENSIEVE_DATA=<copy> .venv/bin/python bench/retrieval.py --tasks bench/out/validation.jsonl --ranks <dir>
.venv/bin/python bench/mcp_bench.py run --url http://localhost:8796 --tasks bench/out/validation.jsonl --out run.json
PENSIEVE_DATA=<copy> .venv/bin/python bench/report.py --grep-data <copy> name=run.json name2=<dir>/code-prod.json
.venv/bin/python bench/agent_search.py run --repo ~/code/repo --tasks tasks-v2.jsonl --split test \
    --arms grep,guided,guided+inject --reps 2 --label mylabel
```

Use a copy of an index (`PENSIEVE_DATA`), never `~/.pensieve`. Run a server on the copy for the MCP and agent runs.
