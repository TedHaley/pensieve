# Pensieve

Spotlight for your work, made for pairing with AI agents. Press **Control+Shift** anywhere on your Mac to search your documents, git repos, and past AI agent sessions by meaning or by exact words. One click opens a 3D map of it all: your code with git blame and who to ask, your files, and your Claude Code, Codex and Qwen Code sessions.

Agents get the same index over MCP: they can search, read, find who knows a topic, and configure Pensieve.

Everything runs on your machine. Text is embedded with `Qwen3-Embedding-0.6B`, stored in SQLite (with a trigram full-text index for exact matches) plus [turbovec](https://pypi.org/project/turbovec/) vector indexes, and laid out in 3D with UMAP. Optional summaries and insights come from a local model via any OpenAI-compatible server (LM Studio by default).

## Install

```bash
curl -fsSL https://tedhaley.ca/pensieve/install.sh | sh
```

This installs [uv](https://docs.astral.sh/uv/) if needed and the `pensieve` backend. On a Mac with the Xcode command-line tools (`xcode-select --install`), it also builds the Pensieve app into `~/Applications` and starts it. Otherwise it starts the backend and opens the visualizer in your browser.

Or by hand:

```bash
uv tool install git+https://github.com/TedHaley/pensieve   # the backend: `pensieve`
git clone https://github.com/TedHaley/pensieve && pensieve/mac/build.sh --install   # the Mac app
```

On first run Pensieve downloads the embedding model (~1.2 GB), then indexes in the background. Search and the map fill in as it goes.

## The search panel

Press **Control+Shift** (tap both, release) to open it, or **Control+Shift+Space**. The tap needs Accessibility permission (System Settings → Privacy & Security → Accessibility → Pensieve); the Space combo works without it.

| Query | Finds |
|---|---|
| `pricing model` | anything about that, by meaning |
| `"rate limit"` | items containing those exact words (case-insensitive) |
| `"rate limit" retries` | contains the phrase, ranked by the meaning of the rest |
| `-draft` / `-"old notes"` | excludes a word or phrase |
| `kind:code` `kind:file` `kind:session` | one kind only |
| `ext:pdf` | one file type |
| `in:anastomo` | path or repo contains this |

**Return** opens · **⌘Return** reveals in Finder · **⌥Return** shows it on the map · **⌘C** copies the path · **⌘O** opens the visualizer · **Esc** closes.

Code results open at the matching line in your editor if you set `editor` to `vscode`, `cursor` or `zed`.

## The visualizer

| | |
|---|---|
| **Code** | Each repo as a 3D map with git blame, or *All repositories* in one joint layout. Browse the folder tree with owners, find *who knows about X*, spot overlapping areas (also across repos), and jump between code and the sessions that touched it. |
| **Files** | Your documents as a map, colored by folder, type, topic or age. Click for a preview, similar files, Open and Reveal in Finder. |
| **Sessions** | Every agent session (or conversation moment) as a point, colored by code area, topic, project, agent or recency, with a hierarchical repo/folder filter. |
| **Insights** | Activity and topics, AI themes and open threads per repo or folder; your footprint in each repo, unexplored areas next to your work and who owns them, and what each teammate shipped in the last 90 days. |
| **Settings** | Everything below, plus copy-paste commands for connecting agents. |

## Use it from your agents (MCP)

The backend serves MCP at `http://127.0.0.1:8765/mcp`.

```bash
claude mcp add --transport http pensieve http://127.0.0.1:8765/mcp     # Claude Code
```

For agents that only speak stdio, use `pensieve mcp` as the command. It starts the backend if it isn't running:

```json
{"mcpServers": {"pensieve": {"command": "pensieve", "args": ["mcp"]}}}
```

Tools: `search`, `read`, `similar`, `who_knows`, `list_repos`, `team`, `unexplored`, `open`, `show_search`, `show_visualizer`, `status`, `list_sources`, `set_source`, `add_folder`, `remove_folder`, `get_settings`, `update_settings`, `reindex`.

## What gets indexed

Everything is grouped into **Sources** (Settings → Sources, or the `list_sources` / `set_source` MCP tools). You can switch off a whole category or a single item; switching a source off also removes what it had indexed.

| Category | Items | Default |
|---|---|---|
| **AI agent sessions** | Claude Code (`~/.claude/projects`), Codex (`~/.codex/sessions`), Qwen Code (`~/.qwen/projects`) | on |
| **Files & folders** | `~/Documents`, `~/Desktop`, `~/Downloads`, plus any folder you add. Text, Markdown, code, PDF, Word and PowerPoint; other files by name. | on |
| **Git repositories** | Repos your agents worked in; repos found under your home folder (4 levels deep) and inside your folders; each repo on its own | on |
| **Apps** | Obsidian vaults; Apple Notes (exported to Markdown in `~/.pensieve`, asks for permission) | Obsidian on, Notes off |

Repos are indexed as they are on disk: committed, staged, modified and untracked files, with uncommitted lines credited to you in blame. **Linked worktrees** (including `.claude/worktrees`) are indexed too: files that differ from the main checkout show up tagged with their branch, and disappear when the worktree is removed. Clones of the same remote count as one repo. Repos found by the sweep with more than 5,000 tracked files are skipped unless you switch them on.

**Staying current:** Pensieve watches your folders, repos, worktrees and agent transcripts (FSEvents) and re-indexes changes within seconds; a session that's still being written updates every 30 seconds. A full sweep every 5 minutes catches anything missed.

Files that look like secrets (password-manager emergency kits, keys, `.env`, credentials) are never indexed; see `exclude_files`.

Everything Pensieve stores lives in `~/.pensieve`. Delete that folder to start over.

## Settings

Change them in the visualizer (Settings), from an agent (`update_settings`), or in `~/.pensieve/settings.json`.

| Setting | Default |
|---|---|
| `folders` | `~/Documents`, `~/Desktop`, `~/Downloads` (manage in Sources) |
| `disabled` | sources switched off, e.g. `agents/codex`, `files/~/Downloads`, `repos/sweep` (manage in Sources) |
| `sweep_roots`, `sweep_depth` | `~`, 4 |
| `max_repo_files` | 5000 (swept repos only) |
| `repos` | extra repos, always indexed |
| `exclude` | folder names never descended into (`node_modules`, `Library`, …) |
| `exclude_files` | filename globs never indexed (secrets) |
| `hotkey` | `ctrl+shift` (or a combo like `cmd+shift+space`) |
| `editor` | `default`, `vscode`, `cursor` or `zed` |
| `llm_url`, `llm_model` | `http://localhost:1234/v1`, `qwen/qwen3.5-9b` |

### Optional: a local LLM

Summaries, topic names and insights need an LLM. Without one, search, maps and code views still work and the status pill says **LLM offline**. Install [LM Studio](https://lmstudio.ai), download `qwen/qwen3.5-9b` (or any chat model), and start its server on port 1234.

## Command line

```bash
pensieve                 # backend + visualizer in the browser (http://localhost:8765)
pensieve --no-open       # backend only (what the Mac app runs)
pensieve mcp             # MCP over stdio
```

Flags: `--port`, `--data` (index location), `--repos PATH…` (extra repos for this run), `--llm-url`, `--llm-model`.

Runs on Apple Silicon (MPS), CUDA, or CPU. The backend and visualizer work on Linux too; the search panel app is macOS only.

## Development

```bash
git clone https://github.com/TedHaley/pensieve && cd pensieve
uv run pensieve          # backend
mac/build.sh && open mac/build/Pensieve.app
```

To support another agent's transcripts, add a parser in `pensieve/parsers.py` and a line in `SOURCES` in `pensieve/config.py`.
