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

On an Apple Silicon Mac, the script downloads the latest `Pensieve.dmg` and copies the app to Applications. Then it installs the `pensieve` backend with the installer inside the app. You do not need git, the Xcode tools or Python. The app starts when you run the script in a terminal. When an agent runs it, the script installs only.

Then connect your agents:

```bash
pensieve integrate recommended
```

This command finds the agents on your Mac (Claude Code, Codex, Cursor, Gemini CLI, Qwen Code, Windsurf, Claude Desktop and VS Code) and gives each one Pensieve as an MCP server.

Then choose what to index. Pensieve indexes nothing until you choose, also after a reset. The app shows the choice when it opens. From a terminal or an agent chat:

```bash
pensieve setup                  # list the agent sessions, folders, repos and apps found, with their sizes
pensieve setup recommended      # index the ones marked [x]
pensieve setup agents/claude repos//Users/me/code/app   # or exactly these
```

The first index uses a lot of energy: the embedding model runs until it finishes, about 500 code files a minute on Apple Silicon. Settings → Sources shows what is syncing now.

On Linux and Intel Macs, the script installs [uv](https://docs.astral.sh/uv/) and the backend from source. Then it starts the backend and opens the visualizer in your browser.

Or by hand:

```bash
uv tool install git+https://github.com/TedHaley/pensieve   # the backend: `pensieve`
git clone https://github.com/TedHaley/pensieve && pensieve/mac/build.sh --install   # the Mac app (needs the Xcode tools)
```

On first run Pensieve downloads the embedding model (~1.2 GB), then indexes in the background. Search and the map fill in as it goes.

## The search panel

Press **Control+Shift** (tap both, release) to open it, or **Control+Shift+Space**. The tap needs Accessibility permission (System Settings → Privacy & Security → Accessibility → Pensieve); the Space combo works without it.

To open Pensieve with **⌘Space** instead of Spotlight, choose ⌘Space in the menu bar under **Search Shortcut**, or in Settings. Pensieve turns off the Spotlight shortcut while it runs. To give ⌘Space back to Spotlight, choose another shortcut. When Pensieve quits, Spotlight gets ⌘Space back too.

Pensieve starts at login by default. To stop this, turn off **Launch at Login** in the menu bar.

| Query | Finds |
|---|---|
| `pricing model` | anything about that, by meaning |
| `"rate limit"` | items containing those exact words (case-insensitive) |
| `"rate limit" retries` | contains the phrase, ranked by the meaning of the rest |
| `-draft` / `-"old notes"` | excludes a word or phrase |
| `kind:file` | documents and code (`kind:doc` documents only, `kind:code` code only) |
| `kind:session` | agent sessions only |
| `kind:folder person build` | folders whose path and contents match, e.g. `core/pipelines/person/build`; a repo is a folder too |
| `kind:repo` | repositories (on its own: all of them) |
| `kind:person spark jobs` | people who wrote the code about it; a plain search for a name also lists the person first |
| `by:parth terraform` | only what that person wrote (code by git blame, documents by author) |
| `ext:pdf` | one file type |
| `in:anastomo` | path or repo contains this |

A plain search also puts a folder first when its path names every word (`person build`). Under an empty field, **Options** lists all of these.

The panel also finds apps, System Settings panes, the individual settings in each pane, and quick toggles (Dark Mode, Wi‑Fi, Mute, Lock Screen, Sleep). These results come from the Mac app only, so they never show on the map. A strong name match shows first as the **Top Hit**, and **Tab** completes its name. On a toggle, **⌘Return** opens its pane in System Settings and does not change the setting. The panel ranks the apps and settings that you open frequently higher.

Under **On This Mac**, the panel also finds documents and folders anywhere on the Mac by name. These matches come from the Spotlight index, so Pensieve does not index them and they do not show on the map. The list leaves out code: source files and files in a git repo are found through the code index, which also searches their content. A file that Pensieve already indexes shows only in its usual group.

The panel calculates and converts units: `2^10`, `15% of 240`, `80 + 15%`, `sqrt(2)`, `3 ft in cm`, `72 f to c`, `2 cups in ml`. **Return** copies the result.

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

`pensieve integrate recommended` connects every agent on your Mac. For Claude Code, it runs the first command below. `--scope user` makes Pensieve available in every project. Without it, `claude mcp add` connects only the folder you run it in.

```bash
claude mcp add --scope user pensieve -- pensieve mcp   # stdio; scope "auto" = the repo the agent was started in
claude mcp add pensieve -- pensieve mcp --scope payments
claude mcp add --transport http pensieve "http://127.0.0.1:8765/mcp?scope=payments"
```

- **auto** (the default for `pensieve mcp`) uses the agent's working directory: the first named scope that includes that repo, otherwise just that repo with its worktrees and the agent sessions run in it. Outside any repo it means everything. In a repo that Pensieve does not index, search tells the agent to use grep. It does not return code from your other repos. HTTP connections can't see the agent's folder, so name a scope there (no `?scope` = everything).
- **Named scopes** are lists of sources, e.g. `payments = repos/~/code/billing + repos/~/code/ledger + files/~/Documents/specs`. Create them in Settings → Scopes or with the `save_scope` tool. Everything stays in one index; a scope only filters, so nothing is embedded twice.
- Every tool respects the scope: search, read, similar, who_knows, list_repos, team, unexplored. An item outside it is refused.
- The search panel and the visualizer use `default_scope` (empty = everything).

For agents that only speak stdio, the JSON config is `{"mcpServers": {"pensieve": {"command": "pensieve", "args": ["mcp"]}}}`. `pensieve mcp` starts the backend if it isn't running.

Tools: `search`, `read`, `similar`, `who_knows`, `knowledge_gaps`, `list_repos`, `team`, `unexplored`, `open`, `current_scope`. Tool descriptions are sent to the agent on every turn, so these ten are all an agent sees by default. Connect with `?tools=all` (HTTP) or `pensieve mcp --all-tools` to add the ones that manage Pensieve itself: `show_search`, `show_visualizer`, `status`, `list_scopes`, `save_scope`, `delete_scope`, `list_sources`, `set_source`, `add_folder`, `remove_folder`, `get_settings`, `update_settings`, `reindex`.

### Get the most out of it: agent add-ons

Agents grep by habit, even with Pensieve connected. Optional add-ons fix that (Settings → Agents → Agent add-ons, or the command line):

```bash
pensieve integrate                    # what's available and what's installed
pensieve integrate recommended        # for each agent on this Mac: the MCP server and the instructions block;
                                      # for Claude Code also the pensieve-search skill and both hooks
pensieve integrate cursor-mcp codex   # only these
pensieve integrate pensieve-teammates pensieve-plan
pensieve integrate claude --remove    # removes exactly what was added
```

- **MCP server** (`claude-mcp`, `codex-mcp`, `cursor-mcp`, `gemini-mcp`, `qwen-mcp`, `windsurf-mcp`, `claude-desktop-mcp`, `vscode-mcp`): adds Pensieve to the agent's MCP config for every project. For Claude Code, it runs `claude mcp add --scope user`. In other config files, Pensieve changes only its own entry. Restart an open agent to load the server.
- **Instructions block** ([`instructions.md`](pensieve/integrations/instructions.md)): a short "search before you grep" section in `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.gemini/GEMINI.md` or `~/.qwen/QWEN.md`, kept between `<!-- pensieve:start -->` markers. For other agents (Cursor, Windsurf, …), paste it into the agent's rules.
- **`pensieve-search` skill** (recommended): query styles, when to fall back to grep, finding similar code before writing new code, recalling past sessions.
- **`pensieve-teammates`**: suggests who knows an area from git blame and drafts the question for you. It never messages anyone.
- **`pensieve-plan`**: plans for new code list prior art to reuse, earlier attempts, people to loop in and knowledge risks.
- **`claude-hook`** (recommended): a `PostToolUse` hook in `~/.claude/settings.json`. After each Edit or Write, it sends the changed path to Pensieve. The agent's next search then always finds the new code, even when the file system event comes late. The hook adds about 50 ms to each edit.
- **`claude-repo-check`** (recommended): a `SessionStart` hook. When a session starts in a repo that Pensieve does not index, Claude asks you once whether to index it. If you say yes, Claude runs `pensieve repo index`. If you say no, Claude runs `pensieve repo skip` and uses grep in that repo. Pensieve keeps the answer as the repo's switch in Settings → Sources, so Claude does not ask again. To change it, use that switch or `pensieve repo index`.

### Does it help? The benchmark

[`bench/`](bench) measures it with Claude Code itself. Tasks are generated from your indexed code (a chunk is described in plain words, or by a name it defines, an error string from it, or a mix), and Claude Sonnet runs headless with and without Pensieve; the answer is the file. These are the 72 held-out tasks across four repos (504 to 34,847 files), each run twice, both setups in one batch, with Pensieve 0.5.6:

| | Grep, Glob, Read | With Pensieve + instructions block |
|---|---|---|
| Tasks solved | 123 / 144 (85%) | **131 / 144 (91%)** |
| Tool calls per lookup | 3.44 | **1.97** (−43%) |
| Cost per lookup | $0.050 | **$0.041** (−18%) |
| Tokens per lookup | 67k | **57k** (−16%) |
| Time per lookup | 10.9 s | **8.6 s** (−21%) |

Pensieve solved as many or more tasks and cost less in every repo: 15% less in the 504-file repo, 6–14% less in the 1,783 and 2,383-file repos, and 28% less in the 34,847-file monorepo, where it also needed 47% fewer tool calls. In 82 of the 144 runs the agent answered after a single search. Pensieve's search alone puts the right file first for 94% of names, 88% of error strings, 74% of name-and-description mixes and 58% of descriptions (a single grep: 54%, 92%, 0%, 0%).

Costs are Claude Code's reported API costs. They depend on how warm Claude's prompt cache is, so compare them only within one batch. Run it on your repos:

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
| **Files & folders** | `~/Documents`, `~/Desktop`, `~/Downloads`, plus any folder you add. Content of text, Markdown, RTF, Word, PowerPoint, Excel, Pages, Keynote, Numbers, PDF, mail (`.eml`), subtitles, data files and code; images, audio, video and other files by name; folders by name and what they contain. Each document's author comes from its metadata (PDF, Office, Spotlight) or its owner. | on |
| **Git repositories** | Repos your agents worked in; repos found under your home folder (4 levels deep) and inside your folders; each repo on its own | on |
| **Apps** | Obsidian vaults; Apple Notes (exported to Markdown in `~/.pensieve`, asks for permission) | Obsidian on, Notes off |

Repos are indexed as they are on disk: committed, staged, modified and untracked files, with uncommitted lines credited to you in blame. **Linked worktrees** (including `.claude/worktrees`) are indexed too: files that differ from the main checkout show up tagged with their branch, and disappear when the worktree is removed. Clones of the same remote count as one repo. Repos found by the sweep with more than 5,000 tracked files are skipped unless you switch them on.

**Staying current:** Pensieve watches your folders, repos, worktrees and agent transcripts (FSEvents). It indexes a change about half a second after the last save. A search indexes the changes that are waiting before it runs, so a search right after an edit finds the new text. Each code and document result is also compared with the file on disk:

- If the file was deleted, the result is removed.
- If the file changed, Pensieve indexes it again and repeats the search. If it cannot (for example, during a long first index), the result is marked as edited since indexed, and a code result gets the line where its text is now.

A session that is still being written updates every 30 seconds. A full sweep every 5 minutes catches anything missed.

**Speed:** a search takes about 0.1 to 0.2 seconds, also in a 35,000-file repo and while Pensieve indexes, because indexing pauses for a moment whenever a search runs. A first index runs as fast as the embedding model allows: git blame runs in parallel through a commit-graph that Pensieve keeps in `~/.pensieve/git` (it never writes to your repo), and the database writes run on their own thread. On an Apple Silicon Mac, a 35,000-file repo takes a little over an hour.

**The map** shows only what Pensieve reads the content of. Each source has a map switch next to its on/off switch. A source that is off the map stays searchable. In Settings → Sources → **File types**, you choose for each type of file:

- **Read content**, **Name only** or **Skip**.
- Whether files of this type show on the map.

Images, audio, video and other files that have no text are found by name only and never show on the map. Folders never show on the map.

**Long documents** are split into pieces of `chunk_chars` characters (1,500 by default). Pensieve reads at most `max_chunks` pieces of each document (2,000 by default, about 1,000 pages; 0 reads all). The Sources page shows a chart of how long your documents are, and how many are longer than the limit. When you change either setting, Pensieve reads your documents again.

Files that look like secrets (password-manager emergency kits, keys, `.env`, credentials) are never indexed; see `exclude_files`.

Everything Pensieve stores lives in `~/.pensieve`. Delete that folder to start over.

## Settings

Change them in the visualizer (Settings), from an agent connected with `--all-tools` (`update_settings`), or in `~/.pensieve/settings.json`.

| Setting | Default |
|---|---|
| `folders` | `~/Documents`, `~/Desktop`, `~/Downloads` (manage in Sources) |
| `disabled` | sources switched off, e.g. `agents/codex`, `files/~/Downloads`, `repos/discover` (manage in Sources) |
| `off_map` | sources that are searchable but not on the map, with the same ids as `disabled` (manage in Sources) |
| `file_types` | for each file type, `index` (`content`, `name` or `off`) and `map` (manage in Sources → File types) |
| `chunk_chars`, `max_chunks` | 1500, 2000 (0 = no limit) |
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
pensieve integrate       # agent add-ons: instructions block, skills and the edit hook
```

Flags: `--port`, `--data` (index location), `--repos PATH…` (extra repos for this run), `--llm-url`, `--llm-model`.

On Apple Silicon, the embedding model runs on MLX. On other machines, it runs on sentence-transformers and PyTorch (CUDA or CPU), which makes the install about 650 MB larger. The backend and visualizer work on Linux too; the search panel app is macOS only.

## Development

```bash
git clone https://github.com/TedHaley/pensieve && cd pensieve
uv run pensieve          # backend
mac/build.sh && open mac/build/Pensieve.app
```

To support another agent's transcripts, add a parser in `pensieve/parsers.py` and a line in `SOURCES` in `pensieve/config.py`.
