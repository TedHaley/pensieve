# Pensieve

<!-- mcp-name: io.github.TedHaley/pensieve -->

[![Release](https://img.shields.io/github/v/release/TedHaley/pensieve)](https://github.com/TedHaley/pensieve/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform: macOS](https://img.shields.io/badge/platform-macOS%20%28Apple%20Silicon%29-lightgrey.svg)](https://pensieve.tools)
[![MCP server](https://img.shields.io/badge/MCP-server-blue.svg)](#mcp-server)

One place for all your information, for context engineering: your files, repositories, agent sessions, tickets and messages, so that you and your agents get the right context for each task. Your context is your own: Pensieve reads only what you can access, it only reads, and it keeps everything on your Mac.

- **Find anything:** press **Control+Shift** anywhere on your Mac and search your documents, folders, code and past agent sessions by meaning or by exact words.
- **Superpowers for your agents:** agents connect over MCP and get the few most relevant files and passages instead of grepping your disk, which saves time and tokens. Each agent is scoped to what it's working on (by default, the repo it runs in).
- **Bring in your other tools:** connections collect your tickets, threads and pages from MCP servers (Jira, Slack, Linear and others) with read-only tools, and keep each change over time.
- **Pick work back up:** the Context tab has a context card for each piece of your work, in five states from latent to etched. Each card has its sessions, PRs, tickets, folder and a button that resumes the last session. Your agents get the same tasks over MCP.
- **Know who to ask:** git blame shows who owns what and who has the most domain knowledge, plus what each teammate has been working on.
- **Find knowledge gaps:** areas where most of the code was written by people no longer active in the repo, how many active people still know it, and who to ask now.

Everything runs on your Mac. Text is embedded with `Qwen3-Embedding-0.6B` and stored in SQLite (with a trigram full-text index for exact matches) plus [turbovec](https://pypi.org/project/turbovec/) vector indexes. AI-written summaries and insights are optional (built-in Qwen 3.5 9B, LM Studio, Claude Code or Codex).

## Install

```bash
curl -fsSL https://pensieve.tools/install.sh | sh
```

On an Apple Silicon Mac, the script downloads the latest `Pensieve.dmg` and copies the app to Applications. Then it installs the `pensieve` backend with the installer inside the app. You do not need git, the Xcode tools or Python. The app starts when you run the script in a terminal. When an agent runs it, the script installs only.

Then connect your agents:

```bash
pensieve integrate recommended
```

This command finds the agents on your Mac (Claude Code, Codex, Cursor, Gemini CLI, Qwen Code, Windsurf, Claude Desktop and VS Code) and gives each one Pensieve as an MCP server.

Then set up Pensieve. Pensieve indexes nothing until you finish setup, also after a reset. When the app opens, it shows the steps:

1. Read what Pensieve does.
2. Choose how you use your Mac: software development, AI coding agents, documents and office work, or research and notes.
3. Let Pensieve look for sources in the folders that those choices need. macOS can ask you to allow access to some folders. Pensieve reads no folder before this step.
4. Choose what to index from what it found. Add folders or repos that it did not find.
5. Choose whether Pensieve reads images, video and audio by what they show or say. These are off by default. Then Pensieve finds those files by name.
6. Choose the generation model, which writes task notes and insights.
7. Start indexing.

An agent can do the same setup in its chat. Until setup is done, every MCP connection also gets the `setup_guide`, `setup_scan`, `setup_add_source` and `setup_start` tools. `setup_guide` tells the agent which questions to ask, in order. From a terminal:

```bash
pensieve setup                  # list the agent sessions, folders, repos and apps found, with their sizes
pensieve setup recommended      # index the ones marked [x]
pensieve setup agents/claude repos//Users/me/code/app   # or exactly these
pensieve setup recommended images audio                 # and read images and sound by content
```

The first index uses a lot of energy: the embedding model runs until it finishes, about 500 code files a minute on Apple Silicon. Settings → Sources shows what is syncing now.

On Linux and Intel Macs, the script installs [uv](https://docs.astral.sh/uv/) and the backend from source. Then it starts the backend and opens the Map in your browser.

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
| `kind:folder invoice export` | folders whose path and contents match, e.g. `src/billing/invoice_export`; a repo is a folder too |
| `kind:repo` | repositories (on its own: all of them) |
| `kind:person payment retries` | people who wrote the code about it; a plain search for a name also lists the person first |
| `person:sam` | that person; the same as `kind:person sam` |
| `by:sam terraform` | only what that person wrote (code by git blame, documents by author) |
| `ext:pdf` | one file type |
| `in:my-app` | path or repo contains this |

A plain search also puts a folder first when its path names every word (`person build`). Under an empty field, **Options** lists all of these.

The panel also finds apps, System Settings panes, the individual settings in each pane, and quick toggles (Dark Mode, Wi‑Fi, Mute, Lock Screen, Sleep). These results come from the Mac app only, so they never show on the map. A strong name match shows first as the **Top Hit**, and **Tab** completes its name. On a toggle, **⌘Return** opens its pane in System Settings and does not change the setting. The panel ranks the apps and settings that you open frequently higher.

Under **On This Mac**, the panel also finds documents and folders anywhere on the Mac by name. These matches come from the Spotlight index, so Pensieve does not index them and they do not show on the map. The list leaves out code: source files and files in a git repo are found through the code index, which also searches their content. A file that Pensieve already indexes shows only in its usual group.

The panel calculates and converts units: `2^10`, `15% of 240`, `80 + 15%`, `sqrt(2)`, `3 ft in cm`, `72 f to c`, `2 cups in ml`. **Return** copies the result.

**Return** opens · **⌘Return** reveals in Finder · **⌥Return** shows it on the map · **⌘C** copies the path · **⌘O** opens the Map · **Esc** closes.

Code results open at the matching line in your editor if you set `editor` to `vscode`, `cursor` or `zed`.

## The app window

The app opens on Context. The tabs are Context, Map, Insights and Settings (keys 1 to 4).

| | |
|---|---|
| **Context** | A context card for each piece of your work of the last 7, 30 or 90 days, filtered by state. Each card has the folders where the work is (repo, branch, worktree, uncommitted files, ahead or behind), its PRs (state, CI checks and review), its tickets (status and assignee) and its sessions. A short AI note says what is done and what is left. Click a session, or Resume last session, to resume it in Terminal. On request, a card shows coworkers' recent commits in the same folders, and related tickets and threads. Sessions join one card by ticket keys, feature branches, the files they edited, and titles that mostly match within 3 days, so work without a ticket gets a card too. Sessions in temp folders or under `~/Library` are left out: apps and scripts start those. |
| **Map** | Documents, code and agent sessions on one 3D map, laid out by meaning. The sidebar groups the sources as Settings → Sources does (agent sessions, repositories, files and folders, apps, connections): show or hide a group or one item, or click an item to see only it. Color by context card, type, folder, author, recency or topic. Colored by context, each point takes its card's state color, each card's name sits where its points are, and clicking a name opens the card with Resume. Inside a repo: git blame per line, owners, *who knows about X*, overlaps, and knowledge gaps. |

Each task has one of five states. Pensieve sets the state from the task's evidence. You can set a state yourself on the card, and an agent can set one over MCP. A state set by hand holds until the task has new activity.

| State | Meaning | Set automatically when |
|---|---|---|
| **Conscious** | You are working on it now. | A session is running, or the task has material work and activity in the last 3 days, and it is not done. Material work is edits, commits, a branch or a PR. |
| **Subconscious** | It is not done and has gone quiet. Revisit it. | It has material work that is not done, with no activity in the last 3 days. |
| **Latent** | A ticket you have not started. | It has a ticket that is not done (or whose status is not known), with no edits, commits, branch or PR yet. |
| **Etched** | Done. | A PR is merged, nothing happened after the merge, and no PR is open. Or its own PR (the PR from its branch) is merged and no PR is open. Or its PRs are merged or closed, or its ticket is done, and nothing is left uncommitted. |
| **Forgotten** | Not finished, and it will likely stay that way. | It has material work that is not done, quiet for 21 days, with no open PR and no uncommitted work. Or it is a conversation with no ticket and no edits, and its session has ended. |
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
- The search panel and the Map use `default_scope` (empty = everything).

### MCP server

Install Pensieve first (see [Install](#install)). The install puts the `pensieve` command in `~/.local/bin`. Then add this entry to the MCP configuration of your client (Claude Desktop, Cursor, Windsurf and others). Replace `/Users/you` with your home folder. Desktop apps do not always have `~/.local/bin` on their `PATH`, so give the full path.

```json
{
  "mcpServers": {
    "pensieve": {
      "command": "/Users/you/.local/bin/pensieve",
      "args": ["mcp"]
    }
  }
}
```

The server uses stdio. `pensieve mcp` starts the backend if it is not running. To use HTTP, connect to `http://127.0.0.1:8765/mcp` while the backend runs. `pensieve integrate recommended` writes this entry for you, with the full path, in each agent that it finds.

Tools: `search`, `read`, `similar`, `overview`, `who_knows`, `knowledge_gaps`, `list_repos`, `team`, `unexplored`, `open`, `current_scope`, `index_path`, `report_feedback`, `stale`, `tasks`, `task`, `set_task_state`. Tool descriptions are sent to the agent on every turn, so these seventeen are all an agent sees by default. `tasks` lists your tasks by state, within the agent's scope. `task` gives one task's card: its folder and branch, PRs, tickets, what is left, the command that resumes its last session, and where that session stopped. When an agent finishes a task, it marks it etched with `set_task_state`, and you see the change on the Context tab. `stale` lists what looks abandoned, so that your agent can offer to clean it up. It finds three kinds. Git worktrees with no commit and no agent session in 14 days: "remove" (clean, and its commits are merged or pushed, with the command) or "review" (it has work that exists nowhere else). Local branches that are merged into the default branch, idle for 14 days and not checked out: the command is `git branch -d`, which refuses unmerged work. Squash-merged branches get "review" and no command: their whole change is one commit in the default branch, but `git branch -d` refuses them, because their own commits are not there. Indexed repos with no agent session and no git activity in 3 months: the command is `pensieve repo remove`, which removes the repo from Pensieve's index only. The repo stays on disk. Pensieve only reports: the agent asks you before it removes anything, and the index drops a removed worktree on its next sweep. With the session-start hook, a session in a repo with stale worktrees or merged branches gets a one-line hint to offer this at a natural pause. `overview` shows where things are in a repo: its size, languages, folders two levels deep, the README's start, its dependencies and its largest files, then its topics (files grouped by meaning) with their files and authors. It reads the index, not the files. `index_path` indexes a folder when you ask the agent to: a git repo, a folder of repos, a folder of agent sessions, or documents. Pensieve finds out which kind it is. Connect with `?tools=all` (HTTP) or `pensieve mcp --all-tools` to add the ones that manage Pensieve itself: `show_search`, `show_visualizer`, `status`, `list_scopes`, `save_scope`, `delete_scope`, `list_sources`, `set_source`, `add_folder`, `remove_folder`, `get_settings`, `update_settings`, `reindex`. Until setup is done, every connection also gets the setup tools: `setup_guide`, `setup_scan`, `setup_add_source`, `setup_start`. While the generation model is your agent, every connection also gets `pending_work` and `submit_work`.

### Get the most out of it: agent add-ons

Agents grep by habit, even with Pensieve connected. Optional add-ons fix that (Settings → Agents → Agent add-ons, or the command line):

```bash
pensieve integrate                    # what's available and what's installed
pensieve integrate recommended        # for each agent on this Mac: the MCP server, the instructions block and
                                      # the pensieve-search skill; for Claude Code also both hooks
pensieve integrate cursor-mcp codex   # only these
pensieve integrate pensieve-teammates pensieve-plan
pensieve integrate claude --remove    # removes exactly what was added
```

- **MCP server** (`claude-mcp`, `codex-mcp`, `cursor-mcp`, `gemini-mcp`, `qwen-mcp`, `windsurf-mcp`, `claude-desktop-mcp`, `vscode-mcp`): adds Pensieve to the agent's MCP config for every project. For Claude Code, it runs `claude mcp add --scope user`. In other config files, Pensieve changes only its own entry. Restart an open agent to load the server.
- **Instructions block** ([`instructions.md`](pensieve/integrations/instructions.md)): a short "search before you grep" section in `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.gemini/GEMINI.md` or `~/.qwen/QWEN.md`, kept between `<!-- pensieve:start -->` markers. For other agents (Cursor, Windsurf, …), paste it into the agent's rules.
- **Skills** go once into `~/.agents/skills`, the user-level skills folder that Codex, Gemini CLI, opencode, Qwen Code (0.25 and later), Cursor, VS Code, Windsurf and other agents read. Claude Code reads only its own folder, so it gets a link to that copy in `~/.claude/skills`. Older versions of Qwen Code also read only their own folder, so they get a link in `~/.qwen/skills`. Pensieve makes no folder for an agent that is not installed. If an agent's skills folder is a link to a folder that does not exist, Pensieve leaves it and tells you.
- **`pensieve-search` skill** (recommended): query styles, when to fall back to grep, finding similar code before writing new code, recalling past sessions.
- **`pensieve-teammates`**: suggests who knows an area from git blame and drafts the question for you. It never messages anyone.
- **`pensieve-plan`**: plans for new code list prior art to reuse, earlier attempts, people to loop in and knowledge risks.
- **`claude-hook`** (recommended): a `PostToolUse` hook in `~/.claude/settings.json`. After each Edit or Write, it sends the changed path to Pensieve. The agent's next search then always finds the new code, even when the file system event comes late. The hook adds about 50 ms to each edit.
- **`claude-repo-check`** (recommended): a `SessionStart` hook. When a session starts in a repo that Pensieve does not index, Claude asks you once whether to index it. If you say yes, Claude runs `pensieve repo index`. If you say no, Claude runs `pensieve repo skip` and uses grep in that repo. Pensieve keeps the answer as the repo's switch in Settings → Sources, so Claude does not ask again. To change it, use that switch or `pensieve repo index`.
- **`claude-prompt-search`** (recommended): a `UserPromptSubmit` hook. When you send a prompt in a repo that Pensieve indexes, Pensieve adds what the prompt needs:
  - a prompt about one piece of work ("where did I leave off on ABC-12", a branch, a PR number): its context card;
  - a standup or "what's still open": the list of your tasks;
  - "who knows this code" or "who should review": the people from `who_knows`;
  - "why did we …": the past agent sessions that match;
  - any other prompt: the 5 best code matches, as the `search` tool shows them.

  The hook adds about 300 ms and 400 to 1,500 tokens to each prompt. Slash commands, prompts of 1 or 2 words and folders that Pensieve does not index get nothing.

### Claude Code plugin

The plugin gives Claude Code the MCP server, the three skills, the instructions block and the three recommended hooks. It does not change your config files. Install Pensieve first, because the plugin starts `~/.local/bin/pensieve`.

```
/plugin marketplace add TedHaley/pensieve
/plugin install pensieve@pensieve
```

- Use the plugin or `pensieve integrate` for Claude Code, not both. When the plugin is installed, `pensieve integrate` does not add the Claude Code MCP server, instructions block, hooks or skill link. When Pensieve starts, it removes the hooks that `pensieve integrate` added before, so that Claude Code does not run each hook twice.
- The plugin's tools are named `mcp__plugin_pensieve_pensieve__*`.
- If you run the backend on another port, set `PENSIEVE_PORT` in the environment that starts Claude Code.

### Does it help? The benchmark

[`bench/`](bench) measures it with Claude Code itself. Tasks are generated from your indexed code (a chunk is described in plain words, or by a name it defines, an error string from it, or a mix), and Claude Sonnet runs headless with and without Pensieve; the answer is the file. These are the 72 held-out tasks across four repos (504 to 34,847 files), each run twice, both setups in one batch. This is the latest batch (v8, October 2026), with the Mac's own Claude Code settings kept out:

| | Grep, Glob, Read | With Pensieve + instructions block |
|---|---|---|
| Tasks solved | 129 / 144 (90%) | **129 / 144 (90%)** |
| Tool calls per lookup | 3.42 | **1.81** (−47%) |
| Cost per lookup | $0.051 | **$0.039** (−23%) |
| Tokens per lookup | 74k | **52k** (−30%) |
| Time per lookup | 9.5 s | **6.8 s** (−29%) |

Pensieve solves as many tasks as grep, with about half the tool calls. It needs fewer tool calls in every repo (33% to 55% fewer) and costs less in every repo (8% to 33% less). The largest saving is in the 34,847-file monorepo. In 88 of the 144 runs, the agent answers with one tool call or none (grep: 46). An earlier batch (v5test, Pensieve 0.5.6) solved 131 tasks against 123 with grep, but the two later clean batches show the same accuracy as grep. Pensieve's search alone puts the right file first for 94% of names, 88% of error strings, 74% of name-and-description mixes and 58% of descriptions (a single grep: 54%, 92%, 0%, 0%).

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
| **AI agent sessions** | Claude Code (`~/.claude/projects`, with its memory files), Codex (`~/.codex/sessions`), Qwen Code (`~/.qwen/projects`) | on |
| **Files & folders** | `~/Documents`, `~/Desktop`, `~/Downloads`, plus any folder you add. Content of text, Markdown, RTF, Word, PowerPoint, Excel, Pages, Keynote, Numbers, PDF, mail (`.eml`), subtitles, data files and code; images, video and audio by what they show or say (if you switch them on) or by name; other files by name; folders by name and what they contain. Each document's author comes from its metadata (PDF, Office, Spotlight) or its owner. | on |
| **Git repositories** | Repos your agents worked in; repos found under your home folder (4 levels deep) and inside your folders; each repo on its own | on |
| **Apps** | Obsidian vaults; Apple Notes (exported to Markdown in `~/.pensieve`, asks for permission) | Obsidian on, Notes off |
| **Connections** | Tickets, messages and pages from MCP servers (Jira, Slack, Linear and others), and the MCP results in your agent sessions | each one off |

**Connections** collect data from MCP servers with read-only tools. Pensieve keeps each item over time, with a list of its changes.

Pensieve finds your MCP servers by itself, whatever the generation model is:

- **Claude Code's servers:** when Claude Code is on this Mac, Sources lists its MCP servers, including the claude.ai connectors of your account. To collect, Pensieve runs Claude Code headless (Haiku) with only the read tools of one server. Every other tool is blocked.
- **Your other agents' servers:** Pensieve reads the MCP config of Codex, Cursor, Gemini CLI, Qwen Code, Windsurf, Claude Desktop and VS Code, and connects to those servers itself, with the credentials in that config. A server that Claude Code also has is left to Claude Code.
- **Your own servers:** add a server in Sources. Give a URL with headers (for example `Authorization: Bearer …`), or a command with environment variables. Pensieve connects to it itself. Pensieve never shows the header or environment values again. They stay in `~/.pensieve/settings.json`, which only you can read.

For the servers that Pensieve connects to itself, the generation model chooses the search calls, and Pensieve makes them.
- **From your agent sessions:** the results that your agents already got from MCP tools. Pensieve fetches nothing new for this source.

A tool is read-only if its name has a read verb (get, list, search, read, fetch and others) and no write verb (create, update, send, delete and others). A server's `readOnlyHint: false` or `destructiveHint: true` makes a tool a write tool. Pensieve never calls a write tool. Each connection that is on is collected every 6 hours (`connections_every_hours`); **Collect now** collects at once. The first collection goes back 30 days (`connections_days`). Items are Markdown files in `~/.pensieve/connections`.

Repos are indexed as they are on disk: committed, staged, modified and untracked files, with uncommitted lines credited to you in blame. **Linked worktrees** (including `.claude/worktrees`) are indexed too: files that differ from the main checkout show up tagged with their branch, and disappear when the worktree is removed. Clones of the same remote count as one repo. Repos your agents work in, and repos you add, are indexed at any size. A repo that only the sweep found, with more than 5,000 tracked files and no agent use, is listed in Sources for you to switch on instead of being indexed on its own.

**Staying current:** Pensieve watches your folders, repos, worktrees and agent transcripts (FSEvents). It indexes a change about half a second after the last save. A search indexes the changes that are waiting before it runs, so a search right after an edit finds the new text. Each code and document result is also compared with the file on disk:

- If the file was deleted, the result is removed.
- If the file changed, Pensieve indexes it again and repeats the search. If it cannot (for example, during a long first index), the result is marked as edited since indexed, and a code result gets the line where its text is now.

A session that is still being written updates every 30 seconds. A full sweep every 5 minutes catches anything missed.

**Speed:** a search takes about 0.1 to 0.2 seconds, also in a 35,000-file repo and while Pensieve indexes, because indexing pauses for a moment whenever a search runs. A first index runs as fast as the embedding model allows: git blame runs in parallel through a commit-graph that Pensieve keeps in `~/.pensieve/git` (it never writes to your repo), and the database writes run on their own thread. On an Apple Silicon Mac, a 35,000-file repo takes a little over an hour.

**The map** shows only what Pensieve reads the content of. Each source has a map switch next to its on/off switch. A source that is off the map stays searchable. In Settings → Sources → **File types**, you choose for each type of file:

- **Read content**, **Name only** or **Skip**.
- Whether files of this type show on the map.

**Images, video and audio** can be read by what they show or say. Switch them on in Settings → Models → **What the search model reads**. They need the EmbeddingGemma 2 search model. When you switch one on, Pensieve downloads its part of the model and shows the progress. Until the download is done, those files are found by name.

| Kind | What Pensieve reads | Download | Speed on an M4 Max |
|---|---|---|---|
| Images | PNG, JPEG, GIF, WebP, BMP, TIFF and HEIC, in your folders and git repos | 335 MB | about 8 images a second |
| Video | What it shows, 32 seconds at a time. With audio on, also what is said in it. | 335 MB (the same as images) | about 6 minutes for an hour of video |
| Audio | What is said or heard, 30 seconds at a time | 611 MB | about 30 seconds for an hour of sound |

Each 30- or 32-second window of a video or a recording is a search hit with its time, for example `meeting.m4a 12:30-13:00`. The Mac app decodes video and sound with macOS (AVFoundation). A backend without the app needs PyAV (`pip install av`).

Other files that have no text are found by name only and never show on the map. Folders never show on the map.

**Long documents** are split into pieces of `chunk_chars` characters (1,500 by default). Pensieve reads at most `max_chunks` pieces of each document (2,000 by default, about 1,000 pages; 0 reads all). The Sources page shows a chart of how long your documents are, and how many are longer than the limit. When you change either setting, Pensieve reads your documents again.

Files that look like secrets (password-manager emergency kits, keys, `.env`, credentials) are never indexed; see `exclude_files`.

Everything Pensieve stores lives in `~/.pensieve`. Delete that folder to start over.

## Settings

Change them in the Map window (Settings), from an agent connected with `--all-tools` (`update_settings`), or in `~/.pensieve/settings.json`.

| Setting | Default |
|---|---|
| `folders` | `~/Documents`, `~/Desktop`, `~/Downloads` (manage in Sources) |
| `disabled` | sources switched off, e.g. `agents/codex`, `files/~/Downloads`, `repos/discover` (manage in Sources) |
| `off_map` | sources that are searchable but not on the map, with the same ids as `disabled` (manage in Sources) |
| `file_types` | for each file type, `index` (`content`, `name` or `off`) and `map` (manage in Sources → File types) |
| `chunk_chars`, `max_chunks` | 1500, 2000 (0 = no limit) |
| `sweep_roots`, `sweep_depth` | `~`, 4 |
| `max_repo_files` | 5000 (found repos with no agent use only) |
| `repos` | extra repos, always indexed |
| `exclude` | folder names never descended into (`node_modules`, `Library`, …) |
| `exclude_files` | filename globs never indexed (secrets) |
| `scopes`, `default_scope` | named index slices for agents (manage in Scopes); the panel's scope |
| `appearance` | `system` (or `light`, `dark`) |
| `embed_model` | the search model for the index (manage in Settings → Models); empty: the index's own |
| `ai`, `builtin_model` | `auto` (not chosen yet), `mlx-community/Qwen3.5-9B-MLX-4bit` |
| `cli_command`, `cli_privacy` | the command for `ai: cli`, and `cloud` or `local` |
| `hotkey` | `ctrl+shift` (or a combo like `cmd+shift+space`) |
| `editor` | `default`, `vscode`, `cursor` or `zed` |
| `llm_url`, `llm_model` | `http://localhost:1234/v1`, `qwen/qwen3.5-9b` |

## Models

Pensieve uses two models. You choose both at setup, and change them in Settings → Models.

### Search model

The search model turns text into vectors, so that search finds things by meaning. It always runs on your Mac.

| Model | Notes |
|---|---|
| **EmbeddingGemma 2** (`google/embeddinggemma-2`) | The default for a new index. Its 270M-parameter text part runs on MLX. About twice as fast to index as Qwen3 and 25% smaller, with the same search quality in our tests (`bench/STRATEGIES.md`). Pensieve downloads only its text part: 550 MB the first time. |
| **Qwen3 Embedding 0.6B** (`Qwen/Qwen3-Embedding-0.6B`) | The model of every index made before 0.6.9. A 1.2 GB download the first time. |

An index keeps the model it was built with: an update does not change it. If you choose another model, Pensieve warns you first. Then it embeds the text it already holds with the new model, in the background. It does not read your files or run git blame again, and search keeps working with the old model. When the new vectors are ready, the backend restarts and uses them.

### Generation model

Search, maps, activity, keyword topics, your footprint in each repo and teammates' commits all work without a generation model. Task notes, summaries, topic names and the written insights need one. Pensieve asks on first run. Change it in Settings → Models, or with `update_settings {"ai": ...}`:

| Engine | Where it runs | Notes |
|---|---|---|
| **Built-in: Qwen 3.5 9B** (`builtin`) | This Mac (MLX) | Apple Silicon, 16 GB memory. Downloads ~5 GB on first use, or reuses LM Studio's MLX copy if you have one. |
| **LM Studio / local server** (`server`) | This Mac | Any OpenAI-compatible server at `llm_url` running `llm_model`. |
| **Command line** (`cli`) | Where the tool runs | An AI command-line tool you already use, set in `cli_command`. Templates: Claude Code, Codex, Qwen Code, Gemini CLI and Ollama. Pensieve runs the command once per job, in an empty folder. The prompt goes on stdin, or in place of `{prompt}`; `{out}` is a file for the answer. The command must answer without using tools. No per-chunk summaries, to keep calls low. |
| **Your agent** (`agent`) | Where your agent runs | The agents connected over MCP write the jobs with their own model. Pensieve keeps the jobs in a queue, and nothing runs in the background. Ask an agent to "do Pensieve's pending work": it reads the jobs with `pending_work` and answers each one with `submit_work`. Several sessions can work at the same time: each one gets different jobs. Settings shows each session that answered a job in the last 24 hours. Summaries of each conversation chunk are not queued. |

A saved `none` (the old Off) loads as `agent`.

## Command line

```bash
pensieve                 # backend + Map in the browser (http://localhost:8765)
pensieve --no-open       # backend only (what the Mac app runs)
pensieve mcp             # MCP over stdio (--all-tools to include the settings tools)
pensieve integrate       # agent add-ons: instructions block, skills and the edit hook
pensieve search "retry failed webhook delivery" --kind code   # what an agent's search returns, scoped to this repo
pensieve repo status     # whether Pensieve indexes this repo; also index, skip (stop syncing it) or remove (drop it from the index)
pensieve card <task id> -o card.html   # a context card as one HTML page; --json or --agent for the data, --share to send it
```

A context card follows [`context-card.schema.json`](pensieve/context-card.schema.json) (`context-card/1`). Every card has the same core: the **problem**, the **goal**, the **state** and what is **next**, plus the question that waits on you. Its resources are the supporting context, and each one has a reach ([`resources.py`](pensieve/resources.py)):

| Reach | Resources | In a shared card |
|---|---|---|
| local | session transcripts, agent memory files, uncommitted or unpushed changes, files outside git, folders | a summary, written from the whole document when you share; no path |
| git | the branch on the remote, files at a pushed commit, PRs | a link (a permalink at the commit) |
| mcp | tickets, messages, pages from your connections | a link and its service, with a summary. A message goes as a link only, without its text. |
| restricted | Claude artifacts | a link marked "may need access", with a summary |
| public | public web pages | a link |
| private | direct messages, group messages, private channels | nothing: no link, no text, no summary. Your local cards and agents use them, and the note's problem, goal, state and next are written without them. |

On the Context tab, the eye button shows a card's page, and the Compact view lists one card a line (a click opens it in full). Each card belongs to a thread: one goal followed over time ([`docs/threads.md`](docs/threads.md)). Pensieve saves a thread's card when it changes. The thread chip on a card opens its saved cards, and "New session from card" starts an agent that reads a card first. Agents get the same with `thread_history`. The Share button at the bottom right of each card opens a preview. Pensieve reads the local and restricted resources whole and writes a summary of each one. You write what you need from the reader, edit the core and the summaries, untick what doesn't belong, and save one page to Downloads. The page holds the card's JSON in a `<script type="application/json" id="context-card">` element, so the reader's agent reads the same file. A shared card has no local paths, session ids, resume command or secrets, and leaves out sentences about your own machine. Agents share a card with the `share_card` MCP tool.

Flags: `--port`, `--data` (index location), `--repos PATH…` (extra repos for this run), `--llm-url`, `--llm-model`.

On Apple Silicon, the embedding model runs on MLX. On other machines, it runs on sentence-transformers and PyTorch (CUDA or CPU), which makes the install about 650 MB larger. The backend and the Map work on Linux too; the search panel app is macOS only.

## Privacy

Pensieve runs on your Mac. It keeps the index, the settings and the feedback reports in `~/.pensieve`. It sends no usage data and has no account.

Pensieve connects to other hosts only for these jobs:

- **Models:** on first run, it downloads the models from Hugging Face.
- **Updates:** the app and the Map ask GitHub for the latest release.
- **Pull requests:** for a context card, the `gh` command gets the PRs of your branches from GitHub, with your own `gh` login.
- **Connections:** if you turn on a connection, Pensieve reads that MCP server with read-only tools.
- **Generation model:** if you choose Claude Code or Codex, the text to summarize goes to that agent and its model provider. The built-in model and LM Studio stay on your Mac.

The backend, its MCP endpoint and the hooks use only `127.0.0.1`. An agent that you connect gets search results, and it sends them to its own model provider. Pensieve does not control that agent.

## Feedback

Report a bug or an idea in Settings → Feedback, or on [GitHub issues](https://github.com/TedHaley/pensieve/issues/new/choose). Agents report too: when a Pensieve tool fails or a search misses code that the agent then finds another way, the agent calls the MCP tool `report_feedback`. Reports stay on your Mac, in `~/.pensieve/feedback.jsonl`. Settings → Feedback lists them, and Send to GitHub opens a filled-in issue that you read before you post it.

## Development

```bash
git clone https://github.com/TedHaley/pensieve && cd pensieve
uv run pensieve          # backend
mac/build.sh && open mac/build/Pensieve.app
```

To support another agent's transcripts, add a parser in `pensieve/parsers.py` and a line in `SOURCES` in `pensieve/config.py`.
