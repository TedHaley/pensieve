# Roadmap

This file tracks the improvements that we investigate, with their evidence, how we measure them and their status.

## How we decide

A change becomes the default only if it beats the agent's built-in tools, or the current Pensieve, on accuracy, speed
and cost. Optional features need a measured benefit and no loss of accuracy. We record what did not work in
[`bench/STRATEGIES.md`](bench/STRATEGIES.md).

**Status values:** idea · investigating · testing · shipped · dropped

| ID | Item | Area | Status |
|---|---|---|---|
| A1 | Embed each distinct chunk once | Indexing | shipped |
| A2 | Quantized embedding model | Indexing | dropped |
| A3 | Smaller or pruned embedding model | Indexing | shipped |
| A4 | Settings that fit the hardware | Indexing | investigating |
| A5 | Measure the energy that indexing uses | Indexing | idea |
| A6 | Real searches are slower than the benchmark | Search | investigating |
| A7 | Search images, audio and video by content | Search | testing |
| A8 | Index folders while agent sessions catch up | Indexing | testing |
| B1 | Make the prompt hook the default | Agents | testing |
| B2 | Prompt hooks for Codex and Gemini CLI | Agents | idea |
| B3 | Benchmark other agents and models | Agents | idea |
| B4 | Tell the agent how sure a hit is | Agents | idea |
| B5 | A `pensieve search` shell command | Agents | testing |
| B6 | Hit rate of real searches | Agents | testing |
| B7 | Index agent memory files | Agents | shipped |
| B8 | Show hits in the agent's own checkout | Agents | testing |
| B9 | Leave out the agent's own session | Agents | testing |
| B10 | `read` several hits in one call | Agents | testing |
| B11 | Index where your agents work | Agents | testing |
| B12 | Rank search hits by usage | Search | idea |
| B13 | Help the agent keep the user's system lean | Agents | testing |
| C1 | Test the two-step install in auto mode | Install | idea |
| C2 | Claude Code plugin | Install | testing |
| C3 | Confirm that agents load `~/.agents/skills` | Install | testing |
| C4 | Homebrew cask | Install | idea |
| D1 | Team features | Product | idea |
| D2 | Opt-in usage counts | Product | idea |
| D3 | A place for user feedback | Product | testing |
| D4 | Find people with similar or complementary work | Product | idea |
| D5 | The Context tab: context cards across sources, in five states | Product | testing |
| D6 | Context cards as files to share | Product | testing |
| D7 | Threads: one goal followed over time | Product | idea |
| E1 | Connections as one list | Connections | idea |
| E2 | Remote disks | Connections | idea |
| E3 | Database metadata | Connections | idea |
| E4 | Collect from MCP servers, read-only | Connections | testing |
| E5 | Rolling expiry for collected items | Connections | idea |

## A. Indexing efficiency

Early feedback says that the first index is taxing. Embedding takes about 99% of the indexing time, at about 30
chunks a second on an M4 Max (`bench/STRATEGIES.md`). The model is `Qwen3-Embedding-0.6B` on MLX, not quantized,
with batches of 16 and chunks of up to 512 tokens.

For each change in this section, measure on the validation set (`bench/validation.py`, 2,377 queries):
- chunks per second
- right file first (@1, now 65.9%) and in the top 8 (@8, now 91.6%)
- peak memory
- energy, when A5 is done

### A1. Embed each distinct chunk once

- **Evidence:** the index on this Mac has 221,954 code chunks but only 178,044 distinct texts. About 20% of the
  embedding work went to copies. Most copies are in linked worktrees: one repo has 13,433 chunks in its main
  checkout and 39,884 more in 20 worktrees.
- **Try:** before embedding a chunk, look for a stored chunk with the same text and path, and reuse its vector.
- **Measure:** the time to index a new worktree, and the time of a first index of a repo with worktrees.
- **Status:** shipped
- **Notes:**
  - 2026-10-13: in the index on this Mac, 40,222 of 42,324 worktree chunks (95%) have the same path and text as
    another chunk. Built: before a group of chunks is embedded, a chunk with the same path and text as a stored one
    copies its vector (`Repos._known_vectors`). An edited file keeps the vectors of its unchanged chunks: they are
    read before the old version is dropped.
  - On the pensieve repo, a worktree of 672 chunks indexed in 10.8 s with 396 vectors copied (about 26 s before).
    A copied vector matches a fresh one (cosine 0.9995, the float16 rounding). Re-indexing an edited `app.js` copied
    every unchanged chunk's vector.
  - Shipped in 0.7.0. 2026-10-14, first index of a scratch clone of the pensieve repo with 3 worktrees at older
    tags, EmbeddingGemma 2, M4 Max:

    | | Copies on | Copies off |
    |---|---|---|
    | Chunks embedded | 1,646 | 3,106 |
    | Vectors copied | 1,460 | 0 |
    | The 3 worktrees | 23.6 s | 149.3 s |
    | Total | 85.9 s | 236.4 s |

  - The Mac was busy with other work during both runs. The main checkout took 61 s and 86 s for the same 1,218
    chunks, so the times vary by about 40%. The chunk counts do not vary.

### A2. Quantized embedding model

- **Try:** 8-bit and 4-bit versions of the model on MLX.
- **Measure:** chunks per second, @1 and @8, memory. Old vectors and new vectors must not mix: a new model needs a
  full re-index.
- **Status:** dropped
- **Notes:**
  - 2026-10-14, EmbeddingGemma 2 on MLX (`nn.quantize` on its linear layers, group size 64), 800 code chunks on an
    M4 Max: bf16 45.9 chunks/s; 8-bit 43.7 (cosine to bf16 at least 0.992); 4-bit 38.4 (at least 0.946, mean
    0.977). The model is small, so dequantizing costs more than the smaller weights save. Memory is not a limit
    (542 MB). Revisit only for a download smaller than 550 MB, which needs weights hosted by us.

### A3. Smaller or pruned embedding model

- **Evidence:** 1024-token chunks gave no gain over 512 (`bench/STRATEGIES.md`, embedding variants).
- **Try:** a smaller embedding model, fewer layers, or shorter inputs (256 tokens).
- **Measure:** as A2.
- **Status:** testing
- **Notes:**
  - Candidate: `google/embeddinggemma-2` (2026-10-06, Apache 2.0). Text and code use only its 270M text part,
    against 600M for Qwen3-Embedding-0.6B. Vectors have 768 dimensions (Matryoshka to 512, 256 or 128), against
    1024 now. Context is 8,192 tokens. Not checked: its code-search quality (one secondary source says 78.68 on MTEB
    Code) and an MLX path (`MLXEmbedder` runs only Qwen3 models). First test it through sentence-transformers on the
    validation set (`bench/retrieval.py`). A new model needs a full re-index.
  - 2026-10-13, tested on the three smaller repos (1,625 queries): the full ranking puts the right file first 68.0%
    of the time, against 68.1% with Qwen3 and 39.6% with grep. On MLX it embeds 42.7 chunks/s, against 22.3 for
    Qwen3 (1.9x), with batches padded to a multiple of 64 tokens. Details: `bench/STRATEGIES.md`.
  - Built: Settings → General → Search model. An index keeps its model, so no one has to index again; switching
    embeds the stored text with the new model in the background and swaps at a restart. Not yet measured: energy
    (A5), and quality on the largest repo (it was not re-embedded).

### A4. Settings that fit the hardware

- **Evidence:** batch size (16), worker threads (4) and the model are the same on every Mac. Since 0.6.8, each index
  of 2,000 chunks or more stores the speed of the Mac (`embed_rate` in the index).
- **Try:** choose the batch size and model from the chip, the GPU cores and the memory. Lower the priority on battery.
- **Measure:** chunks per second on two or more Macs, for example an M1 Air and an M4 Max.
- **Status:** investigating
- **Notes:**
  - 2026-10-14, M4 Max, 800 code chunks embedded the way indexing does it (one call per slice, longest first):
    slices of 4, 8, 16 and 32 texts give 41.8–42.7 chunks/s with EmbeddingGemma 2 and 22.6–24.0 with Qwen3. The
    slice size (`BULK_SLICE` = 8, chosen so a search waits for at most one slice) costs nothing here.
  - Needs another Mac: the same measurement on an M1 or M2 with 8–16 GB. `embed_rate` in each index already records
    the real speed of a Mac after its first big index.

### A5. Measure the energy that indexing uses

- **Try:** record `powermetrics` (CPU, GPU and package power) during a first index of a fixed repo.
- **Measure:** joules per 1,000 chunks. This becomes the baseline for A1–A4.
- **Status:** idea
- **Notes:**

### A6. Real searches are slower than the benchmark

- **Evidence:** in the usage log on this Mac (33 searches on 2026-10-12 and 13), the median search took 1.1 s, the
  slowest 22 s. The benchmark median is 73 ms.
- **Hypothesis:** these searches ran while Pensieve was indexing. Not checked.
- **Try:** compare the time of each logged search with the indexing status at that time.
- **Status:** investigating
- **Notes:**
  - 2026-10-13, measured with the same 15 searches through the MCP tool on a copy of the index (264K chunks):
    median 89–109 ms idle, and 348–352 ms while a repo indexes. During indexing a search waits for the current
    embedding slice (embed phase 128–200 ms) and the database lock (corpora phase 89–149 ms).
  - The real searches since 0.6.8 (7, median 1.1 s) are slow in other phases: "folders+people" (366–506 ms in 3),
    "on-disk" (263–484 ms in 3) and "catch-up" (313 and 1,503 ms in 2). The vector search ("corpora") was slow in 2
    (359 and 932 ms). Most look like the first search in a scope: the folder and people data are built then.
  - On a cold server, the first search in each of four scopes took 70–398 ms, and none was slow in "folders+people"
    or "on-disk". So the real slow searches waited for something else. The usage log now records each search's CPU
    time and its wait for the index lock: wall time well above CPU time means it waited (lock, threads, disk).
  - Splitting the vector index by repo would speed up only the corpora phase, which is 56–67 ms when idle.
  - 2026-10-14, the 8 real searches over 500 ms since those fields: CPU time was 116–285 ms in each, and wall time
    was 519–4,892 ms. So every slow search waited. The index lock explains part of 5 of them (290–1,777 ms).
  - In 2 searches at 15:05 (1.5 s and 1.7 s), the corpora phase took 1.24–1.26 s with a lock wait of 9 ms or less.
    At that time, a repo was indexing and the built-in LLM was writing summaries.
  - Hypothesis, not checked: these searches wait for the CPU, the GPU or the disk, which indexing and the LLM
    also use. CPU time counts only the search's own thread.
  - The usage log now also records, for each search, the page faults that read the disk ("faults"), and what was
    indexing when the search started ("busy": repos, files or sessions).
  - 2026-10-15: "busy" also has "llm" when an LLM completion is running at the start of the search.
  - Next: release these fields, then read "faults" and "busy" on the next slow real searches. The running server
    (0.7.6) does not record them yet.

### A7. Search images, audio and video by content

- **Evidence:** EmbeddingGemma 2 puts text, images, audio and video in one vector space. Before A7, Pensieve indexed
  media files by name only, and its MLX port (`gemma_mlx.py`) had only the text part.
- **Try:** port the image and sound encoders to MLX. Index images, video and sound in the same index as text, only for
  an index on EmbeddingGemma 2. Each kind is off by default, and its model part downloads when it is switched on.
- **Measure:** queries that describe what a file shows or says; agreement with the torch reference; time per file.
- **Status:** testing (images, video and audio)
- **Notes:**
  - All results are on an M4 Max. "Chance" is the hit rate at rank 1 when the order is random.

    | Set | Files | @1 | @5 | @10 | MRR | Chance @1 |
    |---|---|---|---|---|---|---|
    | Images on this Mac (my descriptions) | 66 | 82% | 94% (@3) | 100% | 0.90 | 1.5% |
    | Same images, by file name only | 66 | 65% | 71% (@3) | 76% | 0.69 | 1.5% |
    | Video: MSR-VTT test clips, their captions | 80 | 76% | 92% | 96% | 0.83 | 1.3% |
    | Sound: Clotho test clips, their first caption | 80 | 16% | 49% | 64% | 0.32 | 1.3% |
    | Speech: LibriSpeech test-clean, 6 words of what is said | 80 | 91% | 95% | 100% | 0.93 | 1.3% |

  - Bias in the images row: I wrote the queries after I looked at the images. The video, sound and speech rows use the
    captions and transcripts of public datasets.
  - The MLX port gives the same vectors as the torch reference (cosine 0.998 or more for images, video and sound).
    Video inputs are longer than 512 tokens, so the text model now uses its sliding window mask (`|i - j| <= 512`).
  - The sound encoder's attention is written as a band of 12 frames. A test with random weights matches the
    reference to 3e-7. Note: transformers' eager attention inverts this mask. The real model uses sdpa.
  - Speed: 8 images a second. Video with its sound: 22.5 minutes in 138 s (about 6 minutes for an hour). Sound only:
    128 s of sound a second.
  - The model reads at most 32 frames (1 a second) or 30 s of sound. So Pensieve cuts a video into 32 s windows and
    sound into 30 s windows, each one a chunk with its time ("long_talk.wav 0:30-1:00"). The cap is 240 windows a file.
  - Decoding: the Mac app carries `pensieve-media` (AVFoundation), so the backend needs no ffmpeg. PyAV is the
    fallback for a backend without the app. AVFoundation's own resampler cut the sound hit rate at rank 1 from 20% to
    11%. Now both decoders give samples at the file's own rate, and scipy's `resample_poly` gives 16 kHz.
  - The install layer (`pensieve/content.py`): Settings → Models → "What the search model reads", the setup sheet,
    `setup_start(content=[...])` and `pensieve setup ... images video audio`. A download shows its progress. Until
    it is done, those files are found by name. Tested from an empty model cache: audio (611 MB) and image (335 MB)
    parts downloaded, then the files were indexed and found.
  - Open: energy per file is not measured. Images in git repos are read; video and sound in repos are not.
    `~/Pictures` is in the default exclude list, so photos there stay out of the index.

### A8. Index folders while agent sessions catch up

- **Evidence:** the files loop indexes the agent-session backlog before it sweeps folders (`server.py`, `watcher`).
  On a scratch index with session sources on, folders had 0 files after 11 minutes, while 4,232 session chunks were
  indexed. A user with many sessions and new folders waits for every session first.
- **Try:** sweep folders between batches of sessions, or in their own loop.
- **Measure:** the time to the first indexed document on a new index with a large session backlog.
- **Status:** testing
- **Notes:**
  - 2026-10-14: built. `Store.sync_files` stops after 30 s (`SESSION_BATCH_SECONDS`) and sets `behind`. The
    watcher then sweeps folders and goes on with the sessions without the poll pause.
  - Measured with a real server on a scratch home: 600 sessions (8,936 chunks) and 40 documents. The first
    document was indexed at 31 s, against 365 s before. One run each.
  - The session speed differed between the two runs (about 12 and 30 chunks/s) because of other work on the Mac.
    So the runs do not show whether the change slows the sessions.
  - Open: the first full folder sweep has no time limit. A large new folder still makes the sessions wait.
    `files._sync` drops the files that it did not see, so it cannot simply stop partway.

## B. Agents use Pensieve when it is better

Agents use their own tools unless they are told to use Pensieve. With the instructions block, they used it in every
benchmark run. The instructions keep grep for the cases where grep is better.

### B1. Make the prompt hook the default

- **Evidence:** `claude-prompt-search` cut tool calls by 33–46% over three batches, at the same cost. Its accuracy was
  level: 16 answers won, 13 lost. In some lost answers, the agent answered from the injected hits without reading a
  file.
- **Try:** add "if two hits fit, read both" to the hook's text, then run the agent benchmark again.
- **Measure:** found rate, calls, cost and time against guided, in one batch.
- **Status:** testing
- **Notes:**
  - 2026-10-17: the hook now answers by the kind of prompt: the context card of the work that a prompt names, the
    task list for a standup or open loops, `who_knows` for "who should review", past sessions for "why did we", and
    code hits otherwise. On the workflow benchmark (D5) it cut tool calls from 4.0 to 2.7 and cost from $0.033 to
    $0.026 a run against the MCP tools alone, with the same facts. It is in the plugin and recommended in
    `pensieve integrate`. Not measured again: the code-search benchmark with the new hook text.

### B2. Prompt hooks for Codex and Gemini CLI

- **Try:** the same hook for the agents that have prompt hooks.
- **Measure:** as B1, with that agent. Needs B3.
- **Status:** idea
- **Notes:**

### B3. Benchmark other agents and models

- **Evidence:** every agent benchmark used Claude Sonnet in Claude Code.
- **Try:** Codex and Cursor, and one more Claude model, on the same 72 tasks.
- **Status:** idea
- **Notes:**

### B4. Tell the agent how sure a hit is

- **Evidence:** tags did not cut calls when the agent searched for itself.
- **Try:** tags such as "exact text, in 1 file" in the hits that the prompt hook adds.
- **Status:** idea
- **Notes:**

### B5. A `pensieve search` shell command

- **Why:** agents without MCP, scripts and hooks can then use Pensieve.
- **Status:** testing
- **Notes:**
  - 2026-10-13: `pensieve search "<query>" [--kind code] [--limit 8] [--scope auto]` calls the server's MCP search
    tool, scoped like an agent started in the current folder, and prints the same lines. It starts the server if it
    is not running.

### B6. Hit rate of real searches

- **Try:** match each logged search with the files that the agent read or edited next.
- **Measure:** the share of searches whose top hits contain the file that the agent used.
- **Status:** testing
- **Notes:**
  - 2026-10-14: `bench/hit_rate.py` matches each search in the usage log to the same call in its Claude Code
    transcript and looks at the next 5 tool calls. Of 35 matched searches (2026-10-12 to 14, almost all by agents
    working on Pensieve, on versions before 0.7.0): 18 used a hit, 14 grepped elsewhere, 3 returned nothing.
  - Some of the misses were for code that the old version did not index (`app.js`, over 200 KB, before 0.6.5) or
    a repo that is not indexed (the website). Run it again after a week on 0.7.x.
  - 2026-10-14, run again: 52 searches by Claude Code, 41 matched to a transcript. 21 used a hit, 16 grepped
    elsewhere, 3 returned nothing, and 1 opened nothing. That is 51% against 51% before (18 of 35). Most of the
    week on 0.7.x is still to come. Several misses were for a website or a repo outside the agent's scope.
  - Next: run it again on 2026-10-21. Also count the `read` calls with several ids (B10).

### B7. Index agent memory files

- **Evidence:** in a session on 2026-10-13, the deciding fact was only in a Claude Code memory file. The session
  that found it was deleted. Pensieve indexes none of the 172 memory files in `~/.claude/projects/*/memory/`.
- **Try:** index each memory file with its project, as its own kind, and show it in session searches.
- **Measure:** the share of session searches that return a memory file in the top 3, and agent answers on questions
  about earlier decisions.
- **Status:** testing
- **Notes:**
  - 2026-10-13: each memory file (not `MEMORY.md`) is indexed as a one-turn session with the title "Memory: …" and
    the project of its transcripts. A deleted memory file leaves the index; a deleted transcript stays. Indexing 161
    files took 19 s.
  - On a copy of the index on this Mac, the file with the finding ranks 3rd for the traced query, and 5th for two
    questions in plain words. Before, it was not in the index.
  - Open: in a long memory file (21 chunks), the passage that the hit shows is not the one with the finding. `read`
    returns the whole file, so the agent still gets it.

### B8. Show hits in the agent's own checkout

- **Evidence:** in the same session, the agent worked in a linked worktree, but every hit showed a path in the
  main checkout. The agent then read the files in its own checkout with `sed`.
- **Try:** when the agent works in a worktree and the file is the same there, show the worktree path.
- **Status:** testing
- **Notes:**
  - 2026-10-13: built (`_to_own_checkout` in `mcp_server.py`). In a test worktree, 3 of 4 hits
    now show the worktree's own path, relative to it. The fourth file differs in that worktree, so its hit keeps the
    main checkout's path and says "(main checkout; differs in your worktree)".

### B9. Leave out the agent's own session

- **Evidence:** in the same session, its own conversation was the first session hit, and the fourth code hit.
- **Open question:** how the MCP server knows which session calls it.
- **Status:** testing
- **Notes:**
  - 2026-10-14: MCP does not say which session calls. The search tool now leaves out the sessions in the agent's
    folder that were written to in the last 5 minutes (`_without_live_sessions`). On a copy of the index, this
    conversation ranked first for a query about the release scripts; with the change it is gone and the earlier
    sessions remain. It also leaves out a second live session in the same folder.
  - Not done: the code hit. The session's own text is not in code results; the fourth code hit in the trace was
    a test file, not the session.

### B10. `read` several hits in one call

- **Evidence:** in the same session, the agent put several reads, a grep and a `cat` in one Bash call. The output was
  cut, so it needed one more call.
- **Risk:** new tools did not cut calls in the benchmark (batch 1). Test before shipping.
- **Status:** testing
- **Notes:**
  - 2026-10-14: no new tool. `read` takes several ids separated by spaces (up to 8), which share `max_chars`, and
    returns `items`; one id works as before. An id outside the agent's scope gets its own error in the list.
  - Not measured: whether agents use it. Check in B6's next run.

### B11. Index where your agents work

- **Evidence:** on one test Mac, two thirds of the files that agent sessions read or edited were outside the index.
  Most were in one busy repo that was not indexed.
- **Try:** rank the repos and folders by agent use, from the sessions' working folders and the files they read and
  edited (`pensieve/places.py`). Offer the places that aren't indexed, and index the busiest repos first.
- **Measure:** the share of agent file activity inside the index, before and after.
- **Status:** testing
- **Notes:**
  - Settings → Sources → "Where your agents work" lists them with an Index button (`/api/places`). Setup pre-checks
    the repos found in the transcripts, busiest first. It reads only the transcript files: 2 s for 6,812 files.
  - The repo sweep indexes repos in order of agent use.
  - Tested on a copy of a real index: the Index button's API call indexed a 483-file repo in 3.5 minutes.

### B12. Rank search hits by usage

- **Evidence:** B11's ranking says which repos and files the agents use most. Search does not use it yet.
- **Try:** a small boost for hits in the files and repos that the agents read and edit most.
- **Risk:** it can push a right answer down when the user asks about code they seldom touch. Measure before shipping.
- **Measure:** the validation set (`bench/validation.py`) @1 and @8, and a benchmark run against grep.
- **Status:** idea

### B13. Help the agent keep the user's system lean

- **Evidence:** on one test Mac, a repo's linked worktrees held 3.3 times as many chunks as its main checkout. A third
  of those worktrees had no commit and no agent session in 14 days.
- **Try:** an MCP tool, `stale`, that lists them with a verdict and the command, so the agent offers a cleanup and the
  user decides. Pensieve does not choose what the user keeps: it reports, and the index follows the disk.
- **Measure:** worktrees and chunks removed after the agent offers a cleanup.
- **Status:** testing
- **Notes:**
  - Verdict "remove": clean, and its commits are merged or pushed. "review": it has unmerged and unpushed commits or
    uncommitted changes, and the tool gives the push command that keeps them, not a delete command.
    Throwaway folders (`.venv`, `node_modules`, caches) don't count as work. They make `git worktree remove` need
    `--force`, and the item lists them.
  - The SessionStart hook adds a one-line hint in a repo with stale worktrees, from the last report only (the hook
    waits 3 s at most). Tested on a copy of this index: the first session start warms the report, the next has the hint.
  - 2026-10-14: `stale` also lists two more kinds:
    - Local branches that are merged into the default branch, idle 14 days and not checked out. The command is
      `git branch -d`. A branch that `git branch -d` refuses gets "review" and no command.
    - Indexed repos with no agent session and no git activity in 3 months. The command is the new
      `pensieve repo remove`, which removes the repo from the index only.
  - Squash-merged branches are not listed, because git cannot prove that they are merged.
  - The SessionStart hint now counts merged branches.
  - Tested on scratch repos. On a copy of the index on this Mac, it found 26 merged branches in 3 repos and no
    unused repo. With no cache, the report took 34 s.
  - Not tested: `pensieve repo remove` against a running server.
  - 2026-10-15: squash-merged branches get "review" with no command. A branch is squash-merged when its whole
    change (merge base to tip, as one diff) has the patch ID of a commit in the default branch (the last 2,000).
    `git branch -d` refuses these branches, so the item says that `git branch -D` deletes them.
  - Measured on the 5 repos indexed on this Mac: of 318 local branches that are not merged, 225 are squash-merged.
    The check took 14.4 s in all, 6.7 s at most for one repo. One diff per branch made it 36.8 s, so all diffs go
    through one `git diff-tree --stdin`.
  - Not found: a branch that changed after its squash, and a squash that git had to adjust to the default branch.
  - Next: check how long the full `stale` report takes now, and whether the agent offers the cleanup.

## C. Install

### C1. Test the two-step install in auto mode

- **Evidence:** auto mode in Claude Code blocks an agent that runs a script from the internet. Since 2026-10-13, the
  website has the user run the install command, and the agent runs only `pensieve integrate` and `pensieve setup`.
- **Try:** the website prompt in a new Claude Code session in auto mode, on a Mac without Pensieve.
- **Status:** idea
- **Notes:**

### C2. Claude Code plugin

- **Why:** `/plugin install` is a command that the user types, so auto mode does not check it. A plugin can hold the
  MCP server, the skill and the hooks. The app still needs its own install.
- **Status:** testing
- **Notes:**
  - 2026-10-15: built. The repo root is the plugin and its marketplace (`.claude-plugin/`), so the skills and the
    instructions have one copy. The plugin has the MCP server (`${HOME}/.local/bin/pensieve mcp`), the 3 skills, the
    instructions (a SessionStart hook prints `instructions.md`) and the 2 recommended hooks. The prompt hook is
    not in it (B1).
  - When the plugin is installed, `pensieve integrate` refuses the Claude Code MCP server, instructions block and
    those 2 hooks, and makes no skill link in `~/.claude/skills`. Otherwise Claude gets each one twice.
  - Tested with Claude Code 2.1.300: `--plugin-dir` in a `claude -p` session (14 tools, 3 skills, the
    instructions from the hook), and an install from a local clone of the marketplace (2.3 MB in the cache).
  - 2026-10-15: released in 0.7.7. An install from GitHub (`claude plugin marketplace add TedHaley/pensieve`, then
    `claude plugin install pensieve@pensieve`) worked with an empty home folder.
  - Measure: B6's hit rate for plugin sessions against `integrate` sessions.

### C3. Confirm that agents load `~/.agents/skills`

- **Evidence:** the docs of Codex, Gemini CLI, Cursor, VS Code, Windsurf, opencode, Amp and Goose say that they read
  `~/.agents/skills`. Not tested on real installs.
- **Try:** install the skill, then ask each agent to list its skills.
- **Status:** testing
- **Notes:**
  - 2026-10-14: a test skill went into `~/.agents/skills` only. Each agent below found it.

    | Agent | Version | How we know |
    |---|---|---|
    | Codex | 0.162.1 | `codex debug prompt-input` puts the skill in the model's prompt |
    | Gemini CLI | 0.64.0 | `gemini skills list` |
    | opencode | 1.19.0 | `opencode debug skill` |
    | Qwen Code | 0.25.0 | The agent named the skill when asked, and `~/.qwen/skills` was empty |
    | Cursor | 3.24.18 | Its code has `~/.agents/skills` as a user skills folder; not run |
    | VS Code | 1.141.0 | Its code has `~/.agents/skills` as a personal skills folder; not run |

  - Qwen Code now reads `~/.agents/skills`. The README says that it does not. Older versions can need the link
    in `~/.qwen/skills`, so the link stays.
  - opencode also reads `~/.claude/skills`. With the link there, opencode and Qwen Code each list the skill once.
  - Not tested: Windsurf, Amp and Goose. They are not installed on this Mac.

### C4. Homebrew cask

- **Why:** many Mac developers trust `brew install --cask` more than a script.
- **Status:** idea
- **Notes:**

## D. Product

### D1. Team features

- **Why:** "who knows this code" and knowledge risk are most useful to a team lead. A team edition would sync
  metadata only, not code.
- **Before building:** talk to engineering managers. Check the employment agreement and the MIT license first.
- **Status:** idea
- **Notes:**

### D2. Opt-in usage counts

- **Why:** Pensieve sends no usage data, so there is no count of active users. Downloads include updates and our own
  installs.
- **Rule:** opt-in only, anonymous, and never content.
- **Status:** idea
- **Notes:**

### D3. A place for user feedback

- **Try:** GitHub Discussions, or an issue template, linked from the app and the website.
- **Status:** testing
- **Notes:**
  - 2026-10-14: built. Agents report with the MCP tool `report_feedback` (on by default; the instructions block
    asks them to call it when Pensieve fails them). The user reports in Settings → Feedback. Reports stay in
    `~/.pensieve/feedback.jsonl`; Send to GitHub opens a filled-in issue (bug or idea template) for the user to
    read before posting, because an agent's report can hold private code or paths.
  - 2026-10-14: the website footer and `site/llms.txt` link to the GitHub issue chooser (bug and idea templates).
    The site goes live when this reaches `main`.
  - Measure: reports per week, and how many are sent.

### D4. Find people with similar or complementary work

- **Why:** people who research or work in the same field can find each other. People can also find friends.
  Pensieve already knows the topics of each user from the local index.
- **Risk:** an embedding does not hide the text. Inversion attacks rebuild text from its vector. Vec2Text (Morris
  et al., 2023) rebuilt 92% of 32-token inputs exactly. Treat a shared chunk embedding as the chunk itself.
- **Try:**
  - Pensieve makes topic labels on the machine. The user edits the labels before they leave the machine. Only the
    labels go out.
  - If vectors are necessary, share one profile vector: the average of many chunks, with differential-privacy
    noise. This lowers the inversion risk, but it does not remove it.
  - Show a match only when both people opt in. Private set intersection on hashed topic IDs can find matches, and
    the server does not see the topics.
  - Complementary matches: cosine similarity finds only similar people. Match what one person lacks
    (`knowledge_gaps`) with what another person knows (`who_knows`).
- **Before building:** this needs a server, identities and moderation. The local index needs none of these. People
  index work machines, so the employer can own the signal. Check the employment agreement, as for D1.
- **Measure:** 5 to 10 consenting people make profiles on their machines. Each person rates the matches that they
  get.
- **Status:** idea
- **Notes:**

### D5. The Context tab: context cards across sources, in five states

- **States** (`pensieve/task_states.py`): latent, conscious, subconscious, etched and forgotten. Each one has a
  meaning and an automatic rule. Forgotten: not done, quiet for 21 days (14 for latent work), no open PR and no
  uncommitted work. A state set by hand (on the card, or by an agent with `set_task_state`) holds until
  the task has new activity. Each change by hand is a row in `task_states`, so a task keeps its history.
- **MCP:** `tasks` (by state, within the agent's scope), `task` (one card, as Markdown) and `set_task_state`. Test:
  an agent picks a subconscious task with `tasks`, finishes it from the `task` card, and marks it etched.
- 2026-10-16: on the copied index (30 days): 5 conscious, 18 subconscious, 37 latent and 23 etched, once the PR
  states arrived. Many latent tasks are single sessions that only asked questions. Check whether these belong on the
  tab at all.

- **Why:** one piece of work is spread over a ticket, a Slack thread, a branch, agent sessions and a PR. It is hard
  to follow across these services. A tab that joins them shows the progress of the work in one place.
- **Built:** `pensieve/work.py` and the Data tab. A task joins these by:
  - ticket keys (`ABC-123`) in branch names, commit subjects, session text and connection items, when the key's
    prefix is a known project (from collected tickets or branch names);
  - the git branch of a session (from the Claude Code transcript), for feature branches only;
  - for work without a ticket: two sessions in one repo, at most 3 days apart, that edited 2 specific files in
    common, or one file that only they edited. A file that more than 3 sessions edited does not count.
  Each task shows its data points newest first, and an AI note from the insights engine. The note is written again
  when the task's data points change. Coworkers: `git log --remotes` in the task's folders, by people who are not the
  user. Related: a search of the connections for the task's title.
- **Comments:** keep comments in Pensieve first. Writing a comment back to Jira or Slack posts as the user, so each
  one needs the user's approval. Not built.
- **Questions:**
  - A branch with more than 6 sessions, or sessions over more than 4 days, counts as long-lived and holds many
    tasks. Check these limits on more repos.
  - Remote branches are only as new as the last fetch. The upstream watcher fetches the default branch only.
  - The Claude Code engine for notes needs `--strict-mcp-config --tools ""`. With the user's MCP tools loaded, it
    tried to look things up and stopped at `--max-turns 1`. Saved commands get the flags when settings load.
- **Depends on:** E4.
- **Measure:** the share of open tickets that join to at least one branch or session, and how often the user opens
  the tab.
- **Status:** testing
- **Notes:**
  - 2026-10-16: on the real index (30 days): 114 tasks in 0.2 s (49 with a ticket, 10 on a feature branch, 55
    without either). Notes: 9 of 9 written once the engine flags were added, against 2 of 10 before. Coworker lookup
    takes 0.1 to 0.6 s for each task.
  - 2026-10-17: measured with `bench/workflows.py`: 17 developer requests in 3 repos on one Mac (pick up work, open
    loops, standup, why a change was made, who should review). A judge scores facts that were checked by hand
    against git, GitHub and the transcripts, and wrong claims. 6 of the 17 requests were never used to tune.
    Sonnet, 2 runs of each request in each arm:

    | Arm | Facts | Wrong claims a run | Tool calls | $ a run | Seconds |
    |---|---:|---:|---:|---:|---:|
    | Claude Code with git, gh and grep | 0.64 | 0.35 | 6.9 | 0.051 | 23 |
    | + Pensieve MCP | 0.87 | 0.03 | 4.0 | 0.033 | 16 |
    | + Pensieve MCP and the prompt hook | 0.88 | 0.09 | 2.7 | 0.026 | 12 |

    Without Pensieve, an agent can't answer from session history at all (0.00 on requests whose answer is only in
    a transcript). The first round showed that cards also cause wrong claims when they are stale. The fixes, in the
    order of the four context failures:
    - Poisoning (validate): `reconcile` marks a note sentence that git or GitHub contradicts ("push the commits"
      when every commit is pushed) as outdated. The card quotes the last session's final question word for word.
      Notes read the last 2,400 characters of the session and must not guess how something was done. A session in
      one repo no longer joins a ticket whose project lives in another repo (this benchmark's own session joined
      one before).
    - Distraction (compact): `task` returns a compact card by default (median about 440 tokens, against 570–1,150).
      `task(id, full=true)` keeps the long form. `tasks(query=…)` finds one card without a list of 30.
    - Confusion (curate): no memory files, artifacts or the main checkout's "behind" count on a card. `tasks`
      leaves out sessions that only asked questions.
    - Clash (prune): a PR of a branch that another task owns shows as a branch PR and in "Since the last session",
      so a card no longer says "not merged" for a merged PR.
  - Not fixed: gh shows "state unknown" for PRs in a repo that only a second gh account can see. A fix would try
    each logged-in account; the auto-mode classifier blocks an agent from writing code that reads gh tokens.
  - A skill for these workflows (`pensieve-work`) added calls and tokens for no gain in facts: dropped.

### D6. Context cards as files to share

- **Why:** a card holds what a teammate needs to pick up a piece of work: where it stands, what is left, the
  question that waits on the owner, the PRs and the decisions. Today it lives only in the app and in an agent's
  session.
- **Built:** `pensieve/cards.py`, `pensieve/resources.py` and `context-card.schema.json` (`context-card/1`). One
  data model, two renderings:
  - JSON, the source of truth, from the same task and validation as the text card (`work.reconcile`, the last
    question word for word);
  - one HTML page with no outside files, light and dark, that holds the JSON. `cards.from_html` reads it back.
  Agents in a session keep the compact text card: HTML costs several times the tokens for the same facts.
  `cards.share` (`--share`) removes local paths, session ids and the resume command, and replaces secrets with
  `[secret]`. `GET /api/work/card?id=…&format=html|json|agent&share=true` and `pensieve card <id>` make a card.
- **Questions:**
  - A shared card holds model text and the last session's words, which can name people and internal systems.
    Show a preview before a card leaves the Mac, and let the user edit it.
  - A static file is a snapshot. A live card (it changes when its PR merges) needs a place to sync to: D1.
  - Handoff: an agent that gets a card from a teammate resumes from it. Test with the workflow benchmark: give the
    card of another person's task and measure facts and calls.
- **Status:** testing
- **Notes:**
  - 2026-10-17: a card has a `remote` part (git remote, branch and commit, PRs, tickets, links, artifacts) and a
    `local` part (folder paths, the resume command, session ids). `cards.share` drops `local` and every local path,
    and makes a branch card's id portable (`b:org/repo@branch`). `handoff` gives the next step, the commands that
    get the work (`gh pr checkout …`) and the commands that refresh the live state.
  - Two renderings: HTML for people, `cards.render_agent` (compact JSON) for agents. In the prompt hook, on 10
    requests about one piece of work, 3 runs each (Sonnet): the text card got 0.91 facts, 0.13 wrong claims and
    $0.024 a run; the JSON card got 0.94, 0.07 and $0.032. The difference in facts is within noise, and JSON costs a
    third more. The hook keeps the text card (`?card=json` for the JSON one); shared cards go to agents as JSON.
  - 2026-10-17: a local card and a shared card have the same core (problem, goal, state, next, and the question
    that waits; notes version 8) and differ in their resources. Each resource has a reach: local, git, mcp,
    restricted or public (`pensieve/resources.py`). The Share button opens a preview: Pensieve reads each local and
    restricted resource whole (transcript, diff, memory file, the session text around an artifact) and the insights
    engine summarizes it for the goal. The person writes the ask, edits the core and the summaries, and unticks what
    doesn't belong. Saving makes the card fit to send again.
  - Measured, shared: a teammate's agent in a fresh clone from GitHub, with the owner's folders closed, on 4 pieces
    of work, 3 runs each (Sonnet). No card: 0.11 facts, 0.83 wrong claims, $0.070 a run. The redacted local card:
    0.76, 0.00, $0.049. The new shared card: 0.85, 0.00, $0.044. It gains most where the work has uncommitted
    changes (0.47 to 0.80): only the summaries carry them. A first version lost the follow-ups that a session ended
    with, and named a key file on the owner's Desktop; session summaries now end with the follow-ups, and a shared
    core leaves out sentences about the owner's machine.
  - Measured, local (13 requests whose facts did not change during the day): with the prompt hook, 0.90 facts and
    no wrong claims, against 0.87 and 0.12 with the old note fields, at $0.033 against $0.024 a run (longer notes).
  - Open: a card's id changes when its branch becomes long-lived (more than 4 days of sessions), which a shared
    thread can't follow.

### D7. Threads: one goal followed over time

- **Why:** a card's id is a snapshot of its members, so it changes when a member comes or goes. A hand-set state,
  a note and a published copy need an id that stays. Today two goals on one long-lived branch can join into one
  card, and one goal can change its id.
- **Spec:** [`docs/threads.md`](docs/threads.md). The goal is the identity of a thread; members (tickets, PRs,
  branches, commits, sessions) are its evidence, in a set order; embeddings can split or suggest, never join.
  A session can be a member of several threads. An ended thread can start again, with no time limit.
- **Try:** a thread id next to the snapshot id. Link each new snapshot to its best match by weighted member
  overlap and goal distance, and record start, continue, split, merge, fork, end and revive.
- **Measure:** replay the last 30 days, as of each day. Label a sample of links as same work or different work.
  Compare members only, embeddings only and both, by wrong joins and wrong splits, and set the thresholds.
- **Status:** idea
- **Notes:**

## E. Connections

Pensieve searches the disk of one machine today. A connection is a place that a search can reach: the local disk,
the disk of another machine, or the metadata of a database. Search, scopes and `who_knows` then work the same on
each connection, and each hit shows its connection.

### E1. Connections as one list

- **Why:** E2 and E3 add places to search. One list in Settings and one `connection` field on each hit keep search,
  scopes and the agent tools the same for each place.
- **Try:** make the local disk the first connection. Its sources (repos, folders, agent sessions) stay as they are.
- **Questions:**
  - Names: the README uses "connection" for an agent's MCP connection, and the app uses "Sources" for what the
    local disk indexes. Pick one word for each meaning before the UI shows "Connections".
  - Build E1 only when E2 or E3 is built. Without them, the list has one item.
- **Status:** idea
- **Notes:**

### E2. Remote disks

- **Why:** many developers work on more than one machine: a laptop, a desktop, a remote dev box. Each Pensieve
  sees only its own repos, documents and agent sessions. A search on one machine misses the session or the
  branch that is on the other.
- **Who needs it:** with herdr, the agents run on the remote machines, and the user watches them from one device.
  An agent on a remote machine uses the Pensieve on that machine, so it finds that machine's code already. A
  federated search helps in two cases:
  - The user, on one device, looks for something and does not know which machine has it.
  - An agent on one machine needs data that only another machine has, for example the user's documents.
- **Try first:** `pensieve search` is a shell command (B5). `ssh <host> pensieve search "<query>"` can search a
  remote machine with nothing new. A `pensieve search --machines a,b` that does this on each machine and merges
  the output shows if the user searches other machines. Build the rest only if they do.
- **Try:** federated search for one user. Each machine keeps its own index. A search sends the query to each machine
  that is online, then merges the hits. Nothing is copied, so a removed file is gone from every search.
- **Model:** herdr (herdr.dev) connects machines for terminal agents in the same way:
  - `herdr machine add <ssh host>` adds a machine. OpenSSH does all authentication, so herdr stores no keys and
    opens no port. Each machine runs its own server, and the client talks to it through an SSH bridge.
  - Setup offers to install herdr on the remote, and only with the user's yes. Background reconnects never prompt,
    install or restart anything.
  - When a machine is asleep or offline, herdr shows its last state dimmed and retries, with a back-off to 2
    minutes at most.
  - For Pensieve: `pensieve machine add <ssh host>`, and the bridge reaches the remote server on its own 127.0.0.1.
    The server then keeps listening on 127.0.0.1 only.
- **Questions:**
  - A remote dev box is often Linux. Without MLX, the indexer uses sentence-transformers (CUDA or CPU), with the
    same vectors (cosine 0.998 or more). Not checked: the backend on Linux, and the indexing speed on a CPU.
  - Scores from two indexes are comparable only with the same embedding model. Merge by rank if the models differ.
  - Two clones of the same repo give the same hits twice. Show one, keyed by the remote and the path.
  - `read` and `similar` on a hit from another machine must fetch it from that machine.
  - Scope "auto" uses the agent's folder, which exists on one machine only. Map it by the repo remote.
  - A search must not wait for a machine that is asleep. Give each machine a short time limit, then show which
    machines did not answer.
- **Related:** D1, and the design note for team sharing in PR #1. That note shares session cards between people.
  This item searches one person's own machines, so it can return full content.
- **Measure:** search time with 2 machines against 1, and how often a used hit comes from another machine.
- **Status:** idea
- **Notes:**

### E3. Database metadata

- **Why:** "which table has the churn dates?" is a search by meaning. Agents look for the table, column or view
  with grep through migrations, or they ask the user.
- **Try:** index the schema of a database connection: tables, columns, views, comments, and saved queries. Search
  then finds the right table, and the agent queries it with its own database tool.
- **Rule:** metadata only, never rows. Rows are large and change all the time. The database has its own access
  control, and an index of rows copies data that the database protects.
- **Questions:**
  - Credentials: read them from the user's existing client files, or ask. Pensieve stores no password.
  - Freshness: read the schema again on a schedule, or when a migration file changes.
  - Which databases first: Postgres and SQLite cover most local work.
- **Measure:** the share of agent questions about data that find the right table in the top 3 hits.
- **Status:** idea
- **Notes:**

### E4. Collect from MCP servers, read-only

- **Why:** tickets, messages and pages are part of the work, but they live in other services. Pensieve keeps each
  item over time, with its changes, so it is searchable next to the code and sessions.
- **Rule:** read-only. A tool is read-only if its name has a read verb and no write verb. A server's
  `readOnlyHint: false` or `destructiveHint: true` makes a tool a write tool. Pensieve never calls a write tool.
- **Built:**
  - Sources → Connections lists the MCP servers of the insights engine when it is Claude Code. Pensieve reads them
    from the startup event of `claude -p --output-format stream-json --verbose` (about 3 s). The process stops before
    the model answers.
  - To collect, Pensieve runs Claude Code with `--tools ""`, the read tools of one server in `--allowedTools`, and its
    write tools and every other server in `--disallowedTools`. It keeps the raw tool results from the stream, not the
    model's answer. A large result that Claude Code saves to a file is read from that file.
  - Own servers (`mcp_servers`): for any engine, also the built-in one. Pensieve connects with the MCP SDK (URL with
    headers, or command with environment). The engine plans the search calls. Pensieve then calls the server's get
    tool for each item that it found.
  - From agent sessions: the MCP results in Claude Code transcripts, also from sessions indexed before.
  - Items are Markdown in `~/.pensieve/connections/<server>/`, indexed as files. The raw JSON and a list of field
    changes are in hidden folders. The Markdown ends with the changes, for example "status changed from Triaged to Done".
- **Questions:**
  - Codex: list its servers with `codex mcp list`. Not built, and Codex is not installed on the test Mac.
  - The Claude Code startup event shows a slow server as `pending`, without tools. Pensieve keeps the tools from the
    last check.
  - Only the agent-sessions source is backfilled from the past. A new connection starts `connections_days` back.
- **Measure:** the share of searches that return a connection item that the agent or user then uses, and the cost of a
  collection run.
- **Status:** testing
- **Notes:**
  - 2026-10-16: tested on a scratch data folder.
    - Atlassian through Claude Code: 48 Jira items in 27 s. A run that only checks for changes used 105k tokens,
      mostly cache reads ($0.006 at API prices). A manual run before the other servers were blocked used 281k new
      tokens ($0.30).
    - A local test server with a bearer token: a wrong token is refused. The write tool `create_issue` was never
      called.
    - The agent-sessions backfill: 183 items from past transcripts in 9 s.
    - Through the backend: discover, switch on, collect, index, then search finds the items. Switching a connection
      off removes its items from search.

### E5. Rolling expiry for collected items

- **Why:** much of what E4 collects is transient: alerts, bot messages, closed tickets, pages read once. Without an
  end date, the index and `~/.pensieve/connections` grow with each collection run.
- **Try:** a retention period for each connection, for example 90 days by default. An item expires when it has not
  changed and has not been used in that period. Expiry removes its Markdown file and raw JSON, and the next sweep
  removes it from the index.
- **Keep:**
  - Items that a search hit led to: the agent or the user opened or read them.
  - Items that join to code: a ticket key in a branch, commit or session (see D5).
  - A short line for each expired item, so the D5 timeline still shows that it existed.
- **Questions:**
  - Expire by item age, or also cap the size of each connection (for example the 5,000 newest items)?
  - Bot and alert messages can expire sooner. Find them by author, or by how often the same text repeats.
  - The agent-sessions source keeps what an agent used. Expire it with its session, or keep it longer?
- **Measure:** the size of `~/.pensieve/connections` and the number of connection chunks over time, and the share of
  used hits that point to an expired item.
- **Status:** idea
- **Notes:**
