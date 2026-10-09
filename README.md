# Pensieve

Local search across your files, repositories and AI agent sessions, built to give your agents the best context for the job.

- **Find anything:** press **Control+Shift** anywhere on your Mac and search your documents, folders, code and past agent sessions by meaning or by exact words.
- **Superpowers for your agents:** agents connect over MCP and get the few most relevant files and passages instead of grepping your disk, which saves time and tokens. Each agent is scoped to what it's working on (by default, the repo it runs in).
- **Know who to ask:** git blame shows who owns what and who has the most domain knowledge, plus what each teammate has been working on.
- **Find knowledge gaps:** areas where most of the code was written by people no longer active in the repo, how many active people still know it, and who to ask now.

Everything runs on your Mac. Text is embedded with `Qwen3-Embedding-0.6B` and stored in SQLite (with a trigram full-text index for exact matches) plus [turbovec](https://pypi.org/project/turbovec/) vector indexes. AI-written summaries and insights are optional (built-in Qwen 3.5 9B, LM Studio, Claude Code or Codex).

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
| **Data** | Documents, code and agent sessions on one 3D map, laid out by meaning. Filter by type (Documents · Code · Agent sessions) or by folder; repos show as folders with a *code* badge. Color by type, folder, author, recency or topic. Inside a repo: git blame per line, owners, *who knows about X*, overlaps, and knowledge gaps. |
| **Insights** | Activity and topics, AI themes and open threads per repo or folder; your footprint in each repo, unexplored areas next to your work and who owns them, what each teammate shipped in the last 90 days, and knowledge gaps. |
| **Settings** | Everything below, plus copy-paste commands for connecting agents. |

## Use it from your agents (MCP)

Agents are only as good as the context they're given, so each agent connection works within a **scope**: a slice of the index, such as the repo it's working in.

```bash
claude mcp add pensieve -- pensieve mcp          # stdio; scope "auto" = the repo the agent was started in
claude mcp add pensieve -- pensieve mcp --scope payments
claude mcp add --transport http pensieve "http://127.0.0.1:8765/mcp?scope=payments"
```

- **auto** (the default for `pensieve mcp`) uses the agent's working directory: the first named scope that includes that repo, otherwise just that repo with its worktrees and the agent sessions run in it. Outside any repo it means everything. HTTP connections can't see the agent's folder, so name a scope there (no `?scope` = everything).
- **Named scopes** are lists of sources, e.g. `payments = repos/~/code/billing + repos/~/code/ledger + files/~/Documents/specs`. Create them in Settings → Scopes or with the `save_scope` tool. Everything stays in one index; a scope only filters, so nothing is embedded twice.
- Every tool respects the scope: search, read, similar, who_knows, list_repos, team, unexplored. An item outside it is refused.
- The search panel and the visualizer use `default_scope` (empty = everything).

For agents that only speak stdio, the JSON config is `{"mcpServers": {"pensieve": {"command": "pensieve", "args": ["mcp"]}}}`. `pensieve mcp` starts the backend if it isn't running.

Tools: `search`, `read`, `similar`, `who_knows`, `knowledge_gaps`, `list_repos`, `team`, `unexplored`, `open`, `current_scope`. Tool descriptions are sent to the agent on every turn, so these ten are all an agent sees by default. Connect with `?tools=all` (HTTP) or `pensieve mcp --all-tools` to add the ones that manage Pensieve itself: `show_search`, `show_visualizer`, `status`, `list_scopes`, `save_scope`, `delete_scope`, `list_sources`, `set_source`, `add_folder`, `remove_folder`, `get_settings`, `update_settings`, `reindex`.

### Get the most out of it: agent add-ons

Agents grep by habit, even with Pensieve connected. Optional add-ons fix that (Settings → Search & Agents → Agent add-ons, or the command line):

```bash
pensieve integrate                    # what's available and what's installed
pensieve integrate recommended        # the instructions block in ~/.claude/CLAUDE.md + the pensieve-search skill
pensieve integrate codex              # the same block in ~/.codex/AGENTS.md
pensieve integrate pensieve-teammates pensieve-plan
pensieve integrate claude --remove    # removes exactly what was added
```

- **Instructions block** ([`instructions.md`](pensieve/integrations/instructions.md)): a short "search before you grep" section, kept between `<!-- pensieve:start -->` markers. Paste it into any other agent's rules (Cursor, Windsurf, …).
- **`pensieve-search` skill** (recommended): query styles, when to fall back to grep, finding similar code before writing new code, recalling past sessions.
- **`pensieve-teammates`**: suggests who knows an area from git blame and drafts the question for you. It never messages anyone.
- **`pensieve-plan`**: plans for new code list prior art to reuse, earlier attempts, people to loop in and knowledge risks.

### Does it help? The benchmark

[`bench/`](bench) measures it with Claude Code itself. Tasks are generated from your indexed code (a chunk is described in plain words, or by a name it defines, an error string from it, or a mix), and Claude Sonnet runs headless with and without Pensieve; the answer is the file. On 72 held-out tasks across four repos (483 to 33k files):

| | Grep, Glob, Read | With Pensieve + instructions block |
|---|---|---|
| Tasks solved | 62 / 72 | **66 / 72** |
| Tool calls per lookup | 3.6 | **2.1** |
| Cost per lookup, all repos | $0.080 | **$0.075** |
| Cost per lookup, 33k-file monorepo | $0.105 | **$0.083** |

Pensieve's search alone puts the right file first for 96% of error strings, 92% of identifiers and 67% of descriptions (a single grep: 92%, 54%, 0%). In repos under ~1,800 files the tool descriptions cost more than the searches they save (a few percent per lookup). Run it on your repos:

```bash
uv run python bench/agent_search.py gen --repo ~/code/myrepo -n 40 --tasks tasks-v2.jsonl
uv run python bench/exact_tasks.py                      # adds name / error-string / mixed queries
uv run python bench/agent_search.py find --repo all --tasks tasks-v2.jsonl --styles query,desc,mixed,ident,literal
uv run python bench/agent_search.py run --repo ~/code/myrepo --tasks tasks-v2.jsonl --arms grep,guided
```

## What gets indexed

Everything is grouped into **Sources** (Settings → Sources, or the `list_sources` / `set_source` MCP tools). You can switch off a whole category or a single item; switching a source off also removes what it had indexed.

| Category | Items | Default |
|---|---|---|
| **AI agent sessions** | Claude Code (`~/.claude/projects`), Codex (`~/.codex/sessions`), Qwen Code (`~/.qwen/projects`) | on |
| **Files & folders** | `~/Documents`, `~/Desktop`, `~/Downloads`, plus any folder you add. Text, Markdown, code, PDF, Word and PowerPoint; other files by name; folders by name and what they contain. Each document's author comes from its metadata (PDF, Office, Spotlight) or its owner. | on |
| **Git repositories** | Repos your agents worked in; repos found under your home folder (4 levels deep) and inside your folders; each repo on its own | on |
| **Apps** | Obsidian vaults; Apple Notes (exported to Markdown in `~/.pensieve`, asks for permission) | Obsidian on, Notes off |

Repos are indexed as they are on disk: committed, staged, modified and untracked files, with uncommitted lines credited to you in blame. **Linked worktrees** (including `.claude/worktrees`) are indexed too: files that differ from the main checkout show up tagged with their branch, and disappear when the worktree is removed. Clones of the same remote count as one repo. Repos found by the sweep with more than 5,000 tracked files are skipped unless you switch them on.

**Staying current:** Pensieve watches your folders, repos, worktrees and agent transcripts (FSEvents) and re-indexes changes within seconds; a session that's still being written updates every 30 seconds. A full sweep every 5 minutes catches anything missed.

Files that look like secrets (password-manager emergency kits, keys, `.env`, credentials) are never indexed; see `exclude_files`.

Everything Pensieve stores lives in `~/.pensieve`. Delete that folder to start over.

## Settings

Change them in the visualizer (Settings), from an agent connected with `--all-tools` (`update_settings`), or in `~/.pensieve/settings.json`.

| Setting | Default |
|---|---|
| `folders` | `~/Documents`, `~/Desktop`, `~/Downloads` (manage in Sources) |
| `disabled` | sources switched off, e.g. `agents/codex`, `files/~/Downloads`, `repos/discover` (manage in Sources) |
| `sweep_roots`, `sweep_depth` | `~`, 4 |
| `max_repo_files` | 5000 (swept repos only) |
| `repos` | extra repos, always indexed |
| `exclude` | folder names never descended into (`node_modules`, `Library`, …) |
| `exclude_files` | filename globs never indexed (secrets) |
| `scopes`, `default_scope` | named index slices for agents (manage in Scopes); the panel's scope |
| `appearance` | `system` (or `light`, `dark`) |
| `ai`, `builtin_model`, `agent_model` | `auto` (not chosen yet), `mlx-community/Qwen3.5-9B-MLX-4bit`, `haiku` |
| `hotkey` | `ctrl+shift` (or a combo like `cmd+shift+space`) |
| `editor` | `default`, `vscode`, `cursor` or `zed` |
| `llm_url`, `llm_model` | `http://localhost:1234/v1`, `qwen/qwen3.5-9b` |

## AI insights (optional)

Search, maps, activity, keyword topics, your footprint in each repo and teammates' commits all work without AI. Summaries, topic names and the written insights need a model, and you choose which one writes them (Pensieve asks on first run; change it in Settings → AI & Insights or with `update_settings {"ai": ...}`):

| Engine | Where it runs | Notes |
|---|---|---|
| **Built-in: Qwen 3.5 9B** (`builtin`) | This Mac (MLX) | Apple Silicon, 16 GB memory. Downloads ~5 GB on first use, or reuses LM Studio's MLX copy if you have one. |
| **LM Studio / local server** (`server`) | This Mac | Any OpenAI-compatible server at `llm_url` running `llm_model`. |
| **Claude Code** (`claude`) | Anthropic's cloud | Uses your `claude` login; text is sent to Anthropic. No per-chunk summaries, to keep calls low. |
| **Codex** (`codex`) | OpenAI's cloud | Uses your `codex` login; text is sent to OpenAI. |
| **Off** (`none`) | | Everything except the AI-written parts. |

## Command line

```bash
pensieve                 # backend + visualizer in the browser (http://localhost:8765)
pensieve --no-open       # backend only (what the Mac app runs)
pensieve mcp             # MCP over stdio (--all-tools to include the settings tools)
pensieve integrate       # agent add-ons: instructions block and skills
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
