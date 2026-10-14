# Roadmap

This file tracks the improvements that we investigate, with their evidence, how we measure them and their status.

## How we decide

A change becomes the default only if it beats the agent's built-in tools, or the current Pensieve, on accuracy, speed
and cost. Optional features need a measured benefit and no loss of accuracy. We record what did not work in
[`bench/STRATEGIES.md`](bench/STRATEGIES.md).

**Status values:** idea · investigating · testing · shipped · dropped

| ID | Item | Area | Status |
|---|---|---|---|
| A1 | Embed each distinct chunk once | Indexing | idea |
| A2 | Quantized embedding model | Indexing | idea |
| A3 | Smaller or pruned embedding model | Indexing | testing |
| A4 | Settings that fit the hardware | Indexing | idea |
| A5 | Measure the energy that indexing uses | Indexing | idea |
| A6 | Real searches are slower than the benchmark | Search | idea |
| A7 | Search images, audio and video by content | Search | idea |
| B1 | Make the prompt hook the default | Agents | idea |
| B2 | Prompt hooks for Codex and Gemini CLI | Agents | idea |
| B3 | Benchmark other agents and models | Agents | idea |
| B4 | Tell the agent how sure a hit is | Agents | idea |
| B5 | A `pensieve search` shell command | Agents | idea |
| B6 | Hit rate of real searches | Agents | idea |
| B7 | Index agent memory files | Agents | testing |
| B8 | Show hits in the agent's own checkout | Agents | idea |
| B9 | Leave out the agent's own session | Agents | idea |
| B10 | `read` several hits in one call | Agents | idea |
| C1 | Test the two-step install in auto mode | Install | idea |
| C2 | Claude Code plugin | Install | idea |
| C3 | Confirm that agents load `~/.agents/skills` | Install | idea |
| C4 | Homebrew cask | Install | idea |
| D1 | Team features | Product | idea |
| D2 | Opt-in usage counts | Product | idea |
| D3 | A place for user feedback | Product | idea |

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
  embedding work went to copies. Most copies are in linked worktrees: databricks-bfp has 13,433 chunks in its main
  checkout and 39,884 more in 20 worktrees.
- **Try:** before embedding a chunk, look for a stored chunk with the same text and path, and reuse its vector.
- **Measure:** the time to index a new worktree, and the time of a first index of a repo with worktrees.
- **Status:** idea
- **Notes:**

### A2. Quantized embedding model

- **Try:** 8-bit and 4-bit versions of the model on MLX.
- **Measure:** chunks per second, @1 and @8, memory. Old vectors and new vectors must not mix: a new model needs a
  full re-index.
- **Status:** idea
- **Notes:**

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
    (A5), and quality on affinity (the large repo was not re-embedded).

### A4. Settings that fit the hardware

- **Evidence:** batch size (16), worker threads (4) and the model are the same on every Mac. Since 0.6.8, each index
  of 2,000 chunks or more stores the speed of the Mac (`embed_rate` in the index).
- **Try:** choose the batch size and model from the chip, the GPU cores and the memory. Lower the priority on battery.
- **Measure:** chunks per second on two or more Macs, for example an M1 Air and an M4 Max.
- **Status:** idea
- **Notes:**

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
- **Status:** idea
- **Notes:**

### A7. Search images, audio and video by content

- **Evidence:** EmbeddingGemma 2 puts text, images, audio and video in one vector space. Pensieve indexes media files
  by name only today, and its MLX port (`gemma_mlx.py`) has only the text part.
- **Try:** port the vision encoder (170M) to MLX, and index images (screenshots, diagrams, photos of whiteboards) in
  the same index as their text. Then the audio encoder (300M) for recordings. Only for an index on EmbeddingGemma 2.
- **Measure:** a query set of plain-language descriptions of images on this Mac; time and energy per image.
- **Status:** idea
- **Notes:**

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
- **Status:** idea
- **Notes:**

### B6. Hit rate of real searches

- **Try:** match each logged search with the files that the agent read or edited next.
- **Measure:** the share of searches whose top hits contain the file that the agent used.
- **Status:** idea
- **Notes:**

### B7. Index agent memory files

- **Evidence:** in a session on 2026-10-13 (`d48348bb`, databricks-bfp), the deciding fact ("a globally unique name is
  not sufficient match evidence") was only in a Claude Code memory file, `project_crm_link_scored.md`. The session
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

- **Evidence:** in the same session, the agent worked in the worktree `databricks-bfp-placeholders`, but every hit
  showed a path in `~/Desktop/databricks-bfp`. The agent then read the files in its own checkout with `sed`.
- **Try:** when the agent works in a worktree and the file is the same there, show the worktree path.
- **Status:** idea
- **Notes:**

### B9. Leave out the agent's own session

- **Evidence:** in the same session, its own conversation was the first session hit, and the fourth code hit.
- **Open question:** how the MCP server knows which session calls it.
- **Status:** idea
- **Notes:**

### B10. `read` several hits in one call

- **Evidence:** in the same session, the agent put several reads, a grep and a `cat` in one Bash call. The output was
  cut, so it needed one more call.
- **Risk:** new tools did not cut calls in the benchmark (batch 1). Test before shipping.
- **Status:** idea
- **Notes:**

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
- **Status:** idea
- **Notes:**

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
- **Status:** idea
- **Notes:**
