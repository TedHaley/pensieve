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
| A8 | Index folders while agent sessions catch up | Indexing | idea |
| B1 | Make the prompt hook the default | Agents | idea |
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
| C2 | Claude Code plugin | Install | idea |
| C3 | Confirm that agents load `~/.agents/skills` | Install | testing |
| C4 | Homebrew cask | Install | idea |
| D1 | Team features | Product | idea |
| D2 | Opt-in usage counts | Product | idea |
| D3 | A place for user feedback | Product | testing |

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
  - Next: read "faults" and "busy" on the next slow real searches. Also record whether the LLM was busy.

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
- **Status:** idea

## B. Agents use Pensieve when it is better

Agents use their own tools unless they are told to use Pensieve. With the instructions block, they used it in every
benchmark run. The instructions keep grep for the cases where grep is better.

### B1. Make the prompt hook the default

- **Evidence:** `claude-prompt-search` cut tool calls by 33–46% over three batches, at the same cost. Its accuracy was
  level: 16 answers won, 13 lost. In some lost answers, the agent answered from the injected hits without reading a
  file.
- **Try:** add "if two hits fit, read both" to the hook's text, then run the agent benchmark again.
- **Measure:** found rate, calls, cost and time against guided, in one batch.
- **Status:** idea
- **Notes:**

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
  - Next: other things that are stale, for example merged branches, and repos that no agent used in months.

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
- **Status:** idea
- **Notes:**

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
