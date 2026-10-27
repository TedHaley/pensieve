# Pensieve

<!-- mcp-name: io.github.TedHaley/pensieve -->

[![Release](https://img.shields.io/github/v/release/TedHaley/pensieve)](https://github.com/TedHaley/pensieve/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform: macOS](https://img.shields.io/badge/platform-macOS%20%28Apple%20Silicon%29-lightgrey.svg)](https://pensieve.tools)
[![MCP server](https://img.shields.io/badge/MCP-server-blue.svg)](#mcp-server)

One place for all your information, for context engineering: your files, repositories, agent sessions, tickets and messages, so that you and your agents get the right context for each goal. Your context is your own: Pensieve reads only what you can access, it only reads, and it keeps everything on your Mac.

- **Search what you scoped:** in Developer mode, you and your agents search your repos, agent sessions and chosen folders by meaning or by exact words. Narrow a search to a time (`since:3d`, `until:2026-10-01`) or to one project's sessions (`project:my-app`).
- **Superpowers for your agents:** agents connect over MCP and get the few most relevant files and passages instead of grepping your disk, which saves time and tokens. Each agent is scoped to what it's working on (by default, the repo it runs in).
- **Bring in your other tools:** connections collect your tickets, threads and pages from MCP servers (Jira, Linear, Asana, Slack and others) with read-only tools, and keep each change over time.
- **Pick work back up, and close it out:** the Threads tab has a thread for each agent session that wrote something, in six states: Open, To-do, Thoughts, Loose ends, Closed and Forgotten. Each thread has its PRs, tickets, branch, folder, the steps left to close it, and a button that resumes the session. Your agents get the same threads over MCP, and offer to close the one closest to done.
- **Know who to ask:** git blame shows who owns what and who has the most domain knowledge, plus what each teammate has been working on.
- **Find knowledge gaps:** areas where most of the code was written by people no longer active in the repo, how many active people still know it, and who to ask now.

Pensieve has two modes. **Threads mode** (the default, and the one to start with) reads your agent sessions, git and gh, and your issue tracker. It needs no index and no model. **Developer mode** extends Threads mode: it indexes the repos and files that you choose, so that agents can search them, and it adds the Map, People and Insights tabs. It uses the GPU.

Everything runs on your Mac. In Developer mode, text is embedded with the search model (see [Models](#models)) and stored in SQLite (with a word full-text index for exact matches) plus [turbovec](https://pypi.org/project/turbovec/) vector indexes. Summaries and insights are written by the agent that you connect over MCP, or by a background engine that you choose in Settings → Models.

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

1. **Welcome:** read what Pensieve does.
2. **Choose a mode:** Threads mode or Developer mode.
3. **Connect your issue tracker:** Jira, Linear or Asana. Each one has a Sign in button, which opens the tracker's sign-in in your browser (OAuth, read-only tools). This step is optional. You can also connect a tracker later in Settings → Sources → Cloud connectors.
4. **Start**, in Threads mode. Developer mode has these steps before Start:
   1. **Allow Pensieve to look** for sources in your folders. macOS can ask you to allow access to some folders. Pensieve reads no folder before this step.
   2. **Choose what to index** from what it found. Add folders or repos that it did not find. Choose whether Pensieve reads images, video and audio by what they show or say. These are off by default. Then Pensieve finds those files by name.
   3. **Choose the search model.**

To change the mode, use Settings. A mode change runs the setup again from the mode step.

An agent can do the same setup in its chat. Until setup is done, every MCP connection also gets the `setup_guide`, `setup_scan`, `setup_add_source` and `setup_start` tools. `setup_guide` tells the agent which questions to ask, in order. From a terminal:

```bash
pensieve setup threads          # Threads mode: read every agent session folder found, index nothing
pensieve setup                  # Developer mode: list the agent sessions, folders, repos and apps found, with their sizes
pensieve setup recommended      # index the ones marked [x]
pensieve setup agents/claude repos//Users/me/code/app   # or exactly these
pensieve setup recommended images audio                 # and read images and sound by content
```

In Developer mode, the first index uses a lot of energy: the embedding model runs until it finishes, about 500 code files a minute on Apple Silicon. Settings → Sources shows what is syncing now.

On Linux and Intel Macs, the script installs [uv](https://docs.astral.sh/uv/) and the backend from source. Then it starts the backend and opens the Map in your browser.

Or by hand:

```bash
uv tool install git+https://github.com/TedHaley/pensieve   # the backend: `pensieve`
git clone https://github.com/TedHaley/pensieve && pensieve/mac/build.sh --install   # the Mac app (needs the Xcode tools)
```

In Developer mode, Pensieve downloads the search model on first run (see [Models](#models)), then indexes in the background. Search and the map fill in as it goes.

## Search

In Developer mode, your agents search the index over MCP, and you search it in the app with **⌘K**. Search covers only the sources that you chose: your repos, agent sessions, folders and connections. Both use the same query syntax.

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
| `since:3d` / `until:2026-10-01` | only what changed in that time: a session by its last activity, code by its last commit, a file by its last change. Takes a date, a time back (`12h`, `3d`, `2w`, `1m`), `today` or `yesterday`; `until:` includes that day |
| `project:my-app` | agent sessions of that project only |
| `kind:session since:yesterday` | with no words: the matching sessions, newest first |

A plain search also puts a folder first when its path names every word (`person build`).

Code results open at the matching line in your editor if you set `editor` to `vscode`, `cursor` or `zed`.

## The app window

The app opens on Threads. The tabs are Threads, People, Map, Insights and Settings (keys 1 to 5). People, Map and Insights show only in Developer mode.

| | |
|---|---|
| **Threads** | A thread for each agent session that wrote something (edits, commits, a branch, a PR) in the last 7, 30 or 90 days, filtered by state. The default filter is Open, To-do, Thoughts and Loose ends. Each thread has the folders where the work is (repo, branch, worktree, uncommitted files, ahead or behind), its PRs (state, CI checks and review) and its tickets (status and assignee). The steps left say who can do each one: you, your agent or the tracker. A thread joins a ticket by its key in the branch name (in any case, for a ticket that Pensieve knows), the session title, commit subjects and PR titles. Sessions in temp folders or under `~/Library` are left out: apps and scripts start those. |
| **People** | The people you work with, as a network around you. The nearest people are closest to you and largest. Each person is one record from their git emails, GitHub login and Slack user. Pensieve joins these on the same email, then the GitHub login, then the same full name. Closeness comes from files you both edited, pull request reviews, your threads, and the Slack threads and direct messages that you can see. A click opens the person: their role (the Slack title, or inferred from their commits), their repos and pull requests, and how you work together. Merge or split people there. These changes stay in `~/.pensieve/people`. Pictures come from Slack, then GitHub, and Pensieve keeps a copy in the same folder. |
| **Map** | Documents, code and agent sessions on one 3D map, laid out by meaning. The sidebar groups the sources as Settings → Sources does (agent sessions, repositories, files and folders, apps, connections): show or hide a group or one item, or click an item to see only it. Color by type, folder, author, recency or topic. Inside a repo: git blame per line, owners, *who knows about X*, overlaps, and knowledge gaps. |
| **Insights** | Activity and topics, AI themes and open threads per repo or folder; your footprint in each repo, unexplored areas next to your work and who owns them, what each teammate shipped in the last 90 days, and knowledge gaps. |
| **Settings** | Everything below, plus copy-paste commands for connecting agents. |

### Thread states

Each thread has one of six states. Pensieve sets the state from the thread's data sources: its PRs, branches and tickets. You can set a state yourself on the thread. A state set by hand holds until the thread has new activity. An agent closes a thread in git, GitHub or the tracker, and does not set the state.

| State | Meaning | Set automatically when |
|---|---|---|
| **Open** | You are working on it now. | A session is running, or the thread has work (edits, commits, a branch or a PR) in the last 3 days, and it is not done. |
| **To-do** | A ticket of yours that no session, branch or PR has picked up. | A ticket assigned to you (Jira, Linear or Asana) is not done, with no session, branch or PR on it yet. |
| **Thoughts** | A session with no data source: no PR, branch or ticket that it wrote to. | The session was active in the last 2 days. |
| **Loose ends** | Work that is not done and has gone quiet. Close it out. | The work is not done (an open PR, a ticket that is not done, uncommitted files, or steps left), with no activity in the last 3 days. |
| **Closed** | Done. A closed thread does not open again: new work starts a new thread. | Every data source is verified done: its PR is merged or closed, its ticket is done, or its branch is merged or deleted. |
| **Forgotten** | Not finished, and it will likely stay that way. | A thought is quiet for 2 days. Work with a data source that is not done is never forgotten automatically: it stays a loose end until you set it aside by hand. |

### Thread cards

- **A card** has the title, the state and the time. Its buttons are Resume session (with a copy of the command), Fork session (with a copy of the prompt), Details, Ship, Discard, the thread id button and a preview (the eye). The id button copies the thread's id, for the `thread` MCP tool. Resume session reads the session's folder from its transcript, so it works in Threads mode too.
- **A to-do card** has **Start session in worktree**, Details and Discard. Start session in worktree makes `<repo>/.claude/worktrees/<branch>`, on a branch named from the ticket's key and title, from the default branch. Then it starts the agent with the ticket's title, URL and description. The first to-do of a ticket project asks which repo to use. Pensieve keeps the answer in the `ticket_repos` setting.
- **A cluster** holds the threads of one branch, newest session first, joined by a tree line. The session with the largest share of the branch's surviving lines holds the branch (on a tie, the later session). Its card says "holds the branch · N% of the branch's lines", and its Resume session button takes the state color. The cluster header has the repo and branch, the uncommitted files, Ship, Discard, **Fork from all N**, a copy of the prompt and a preview. The cards inside a cluster have no Ship or Discard buttons. A card that lost its branch links to the thread that holds it.
- **Ship** and **Discard** open a modal with the steps left. Ship: commit, push, open a PR, review and merge, and mark the ticket done after the merge. Discard: drop the uncommitted files, close the PR without a merge, delete the branch (local and remote), and mark the ticket cancelled. The modal's button (Start shipping or Start discarding) starts an agent session in Terminal. The agent does the steps and asks before each step that leaves the Mac. Ship and Discard show for Open, To-do, Loose ends and Forgotten.
- **Details** has the Conversation snippet (the last words; for a to-do, the ticket description), Related sessions, Data sources in session (PRs, tickets, branches), Files and Activity. Files lists what the session made for people (a Notion page it wrote, a Slack message it sent, an Artifact), then what it used as context (a Slack thread, an email or a page that the agent read, a link in the conversation, a local document). An Artifact that the session published with Claude Code shows its title and the date when the session created or last updated it. Code files that it edited are not on it. Related sessions is one list, newest first: the sessions on the same branch, PR or ticket, and the sessions that edited the same files ("≈ similar context"). Files is collapsed by default and shows up to 20 files, web links (such as artifacts) first.
- **Search threads** finds the words on the page. Put words in quotes ("exact words") to find only threads that have them. Put a minus before a word (-word) to leave threads out.
- **The status** in the menu at the top right says "Collecting from <name>…" while a connection collects.

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
- Every tool respects the scope: search, read, similar, people, repo, list_repos, threads. An item outside it is refused.
- Search in the app and the Map use `default_scope` (empty = everything).

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

Tools: `search`, `read`, `similar`, `people`, `repo`, `projects`, `list_repos`, `current_scope`, `open`, `index_path`, `report_feedback`, `stale`, `threads`, `thread`, `cluster`, `thread_page`, `start_todo`. Tool descriptions are sent to the agent on every turn, so these seventeen are all an agent sees by default.

- `people` answers three questions. With `name`, it gives one person from the People tab: their role, repos, recent pull requests, and how you work with them. With `topic`, it gives who knows the topic, ranked by the code they wrote. With only `repo`, it gives the people of the repo and their areas.
- `repo` gives where things are in a repo (`part="overview"`, the default). That is its size, languages, folders two levels deep, the README's start, its dependencies, its largest files, and its topics with their files and authors. It reads the index, not the files. `part="gaps"` gives the areas whose authors left and who still knows them. `part="unexplored"` gives the areas next to your work that you have not touched.
- `threads` lists your threads that are not done (by default the states open, todo and loose_ends), within the agent's scope, the closest to done first. Each thread shows its steps left: who can do each one, and the PR, branch or ticket that it acts on.
- `thread` gives one thread: its folder and branch, PRs, tickets, the steps left, the command that resumes its last session, and where that session stopped.
- `cluster` gives the threads of one branch, newest first, with the share of each session and two lists of steps: one that ships the branch and one that discards it.
- `thread_page` saves a thread's page, or the page of a branch's threads, as one HTML file in `~/Downloads`, with the JSON inside.
- `start_todo` starts a to-do: it makes the worktree and gives the agent its path and the prompt that reads the ticket first. Without a repo for the ticket's project, it returns the repos to choose from.

Pensieve only reads: it sets each state from the evidence. An agent closes a thread when it merges the PR, closes the ticket or deletes the branch. It does not set the state. `set_thread_state` is only in `?tools=all`. `stale` lists what looks abandoned, so that your agent can offer to clean it up. It finds three kinds. Git worktrees with no commit and no agent session in 14 days: "remove" (clean, and its commits are merged or pushed, with the command) or "review" (it has work that exists nowhere else). Local branches that are merged into the default branch, idle for 14 days and not checked out: the command is `git branch -d`, which refuses unmerged work. Squash-merged branches get "review" and no command: their whole change is one commit in the default branch, but `git branch -d` refuses them, because their own commits are not there. Indexed repos with no agent session and no git activity in 3 months: the command is `pensieve repo remove`, which removes the repo from Pensieve's index only. The repo stays on disk. Pensieve only reports: the agent asks you before it removes anything, and the index drops a removed worktree on its next sweep. With the session-start hook, a session in a repo with stale worktrees or merged branches gets a one-line hint to offer this at a natural pause. `index_path` indexes a folder when you ask the agent to: a git repo, a folder of repos, a folder of agent sessions, or documents. Pensieve finds out which kind it is. Connect with `?tools=all` (HTTP) or `pensieve mcp --all-tools` to add the ones that manage Pensieve itself: `show_search`, `show_visualizer`, `status`, `list_scopes`, `save_scope`, `delete_scope`, `list_sources`, `set_source`, `add_folder`, `remove_folder`, `get_settings`, `update_settings`, `file_types`, `reindex`, `set_thread_state`, `usage_stats`, `token_value`, and `pending_work` and `submit_work`, with which an agent can answer the text jobs (summaries and insights). Until setup is done, every connection also gets the setup tools: `setup_guide`, `setup_scan`, `setup_add_source`, `setup_start`.

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
- **Instructions block** ([`instructions.md`](pensieve/integrations/instructions.md)): a short section that tells the agent two things. For each code lookup, call Pensieve `search` before Grep or Glob. At the start of a session and at natural pauses, call `threads` and offer to close the thread closest to done (the `pensieve-wrap-up` skill). The block goes in `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.gemini/GEMINI.md` or `~/.qwen/QWEN.md`, kept between `<!-- pensieve:start -->` markers. For other agents (Cursor, Windsurf, …), paste it into the agent's rules.
- **Skills** go once into `~/.agents/skills`, the user-level skills folder that Codex, Gemini CLI, opencode, Qwen Code (0.25 and later), Cursor, VS Code, Windsurf and other agents read. Claude Code reads only its own folder, so it gets a link to that copy in `~/.claude/skills`. Older versions of Qwen Code also read only their own folder, so they get a link in `~/.qwen/skills`. Pensieve makes no folder for an agent that is not installed. If an agent's skills folder is a link to a folder that does not exist, Pensieve leaves it and tells you.
- **`pensieve-search` skill** (recommended): query styles, when to fall back to grep, finding similar code before writing new code, recalling past sessions.
- **`pensieve-teammates`**: suggests who knows an area from git blame and drafts the question for you. It never messages anyone.
- **`pensieve-plan`**: plans for new code list prior art to reuse, earlier attempts, people to loop in and knowledge risks.
- **`pensieve-wrap-up`**: gets the threads closest to done and does the steps that close them in git, GitHub and the tracker. It asks before a merge or a close, and it never sets a thread state by hand.
- **`claude-hook`** (recommended): a `PostToolUse` hook in `~/.claude/settings.json`. After each Edit or Write, it sends the changed path to Pensieve. The agent's next search then always finds the new code, even when the file system event comes late. The hook adds about 50 ms to each edit.
- **`claude-repo-check`** (recommended): a `SessionStart` hook. When a session starts in a repo that Pensieve does not index, Claude asks you once whether to index it. If you say yes, Claude runs `pensieve repo index`. If you say no, Claude runs `pensieve repo skip` and uses grep in that repo. Pensieve keeps the answer as the repo's switch in Settings → Sources, so Claude does not ask again. To change it, use that switch or `pensieve repo index`. In a repo that Pensieve indexes, the hook adds one line: the number of loose ends and the thread closest to done. Claude then offers to close it at a natural pause. In Threads mode, the hook asks nothing and only adds that line.
- **`claude-prompt-search`** (recommended): a `UserPromptSubmit` hook. When you send a prompt, Pensieve adds what the prompt needs. In a repo that Pensieve does not index (and in Threads mode), it adds only the thread or the list of threads:
  - a prompt about one piece of work ("where did I leave off on ABC-12", a branch, a PR number): its thread;
  - a standup or "what's still open": the list of your threads;
  - "who knows this code" or "who should review": the people from `people`;
  - "why did we …": the past agent sessions that match;
  - any other prompt: the 5 best code matches, as the `search` tool shows them.

  The hook adds about 300 ms and 400 to 1,500 tokens to each prompt. Slash commands, prompts of 1 or 2 words and prompts that find nothing get nothing.
- **`claude-search-gate`** (recommended): three hooks that make Claude search Pensieve before it greps. In a repo that Pensieve indexes, Claude can't run Grep, Glob, `grep`, `rg`, `ag`, `find` or `git grep` in a turn until it has called a Pensieve tool. The prompt hook's results count as that call. Filtering output (`… | grep x`) stays allowed. If Pensieve isn't running, or the repo isn't indexed, nothing is blocked. One session in 5 runs without blocking (`search_gate_holdout`), so `bench/field.py` can compare Pensieve with grep in your own work. Set it to 0 to block in every session.

### Claude Code plugin

The plugin gives Claude Code the MCP server, the four skills, the instructions block and the four recommended hooks. It does not change your config files. Install Pensieve first, because the plugin starts `~/.local/bin/pensieve`.

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
| **Connections** | Tickets, messages and pages from MCP servers (Jira, Linear, Asana, Slack and others), and the MCP results in your agent sessions | each one off |

**Connections** collect data from MCP servers with read-only tools. Pensieve keeps each item over time, with a list of its changes.

**Issue trackers.** The setup asks you to connect your issue tracker: Jira, Linear or Asana. You sign in with your own account in the browser, to the tracker's own MCP server. A sign-in that ends connected starts a collection at once. Then Pensieve collects every 6 hours. A ticket assigned to you becomes a to-do thread. A ticket that closes closes its thread. You can also connect a tracker later in Settings → Sources → Cloud connectors.

- **Jira:** Atlassian's MCP server (`https://mcp.atlassian.com/v1/mcp`), with an OAuth sign-in. Each collection reads the issues assigned to you that changed since the last collection. It also reads again the status of each ticket on a thread that is not closed, so a ticket closed in Jira closes its thread.
- **Linear:** Linear's MCP server, with an OAuth sign-in or an API key.
- **Asana:** Asana's MCP server, with an OAuth sign-in. Pensieve reads your workspaces, then the tasks assigned to you, then the notes of each task. An Asana task gets the key `ASANA-<the last 6 digits of its id>`, and its status is Done or the name of its section.

Pensieve does not collect the connectors inside Claude (claude.ai connectors). Sign in to the tracker's own MCP server instead.

Pensieve finds your other MCP servers by itself. It never starts an agent of its own:

- **Your agents' servers:** Pensieve reads the MCP config of Codex, Cursor, Gemini CLI, Qwen Code, Windsurf, Claude Desktop and VS Code, and connects to those servers itself, with the credentials in that config.
- **Your own servers:** add a server in Sources. Give a URL with headers (for example `Authorization: Bearer …`), or a command with environment variables. Pensieve connects to it itself, and switches it on at once. Pensieve never shows the header or environment values again. They stay in `~/.pensieve/settings.json`, which only you can read.

- **GitHub through `gh`:** when the `gh` command line is signed in, Pensieve reads the issues and pull requests that involve you, with their comments and reviews. It uses only `gh search` and GET calls of `gh api`.
- **From your agent sessions:** the results that your agents already got from MCP tools. Pensieve fetches nothing new for this source.

**Cloud connectors.** Settings → Sources → Cloud connectors lists Notion, Slack, Google Drive, HubSpot, GitHub, Granola, Linear, Jira and Asana, with the state of each one on this Mac. You do not need Claude Code to add one:

| Connector | How to add it without Claude Code |
|---|---|
| Notion | Sign in to Notion's MCP server in the browser (OAuth), or give an integration token to a local server |
| Linear | Sign in to Linear's MCP server (OAuth, read scope only), or give a personal API key |
| Jira | Sign in to Atlassian's MCP server (OAuth) |
| Asana | Sign in to Asana's MCP server (OAuth) |
| Granola | Sign in to Granola's MCP server (OAuth) |
| GitHub | The `gh` command line, or a token for GitHub's MCP server in read-only mode |
| Slack | A Slack app token for a local server. Slack's own MCP server has no open app sign-up. |
| HubSpot | A private app token for a local server. HubSpot's MCP server needs an app registered in HubSpot. |
| Google Drive | Add your Drive for desktop folder in Files & folders. Google has no MCP server for Drive to sign in to. |

OAuth tokens are in `~/.pensieve/connections/.oauth/`, which only you can read. For each connector, Pensieve knows the read tools, the calls to make, the follow-up call for each item (a Slack thread, a GitHub discussion), and the fields of an item. For other servers, your agent plans the calls: the plan is a job in `pending_work`. Each item keeps its people (author, assignee, owner, attendees, commenters) in the front matter of its Markdown file, with the service and the id of each person there.

A tool is read-only if its name has a read verb (get, list, search, read, fetch and others) and no write verb (create, update, send, delete and others). A connector can also name read tools whose names do not pass this rule (Notion's `API-post-search`). A server's `readOnlyHint: false` or `destructiveHint: true` makes a tool a write tool. Pensieve never calls a write tool. Each connection that is on is collected every 6 hours (`connections_every_hours`); **Collect now** collects at once. The first collection goes back 30 days (`connections_days`). Items are rows of `~/.pensieve/v2/store.db`, each with its changes.

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
| `scopes`, `default_scope` | named index slices for agents (manage in Scopes); the scope of search in the app |
| `appearance` | `system` (or `light`, `dark`) |
| `embed_model` | the search model for the index (manage in Settings → Models); empty: the index's own |
| `editor` | `default`, `vscode`, `cursor` or `zed` |

## Models

Threads mode runs no model. Developer mode runs one model: the search model. You choose it at setup, and change it in Settings → Models.

### Search model

The search model turns text into vectors, so that search finds things by meaning. It always runs on your Mac.

| Model | Notes |
|---|---|
| **EmbeddingGemma 2** (`google/embeddinggemma-2`) | The default for a new index. Its 270M-parameter text part runs on MLX. About twice as fast to index as Qwen3 and 25% smaller, with the same search quality in our tests (`bench/STRATEGIES.md`). Pensieve downloads only its text part: 550 MB the first time. |
| **Qwen3 Embedding 0.6B** (`Qwen/Qwen3-Embedding-0.6B`) | The model of every index made before 0.6.9. A 1.2 GB download the first time. |

A new version of Pensieve deletes the index and what the threads read from transcripts and `gh`, and builds them again on start. Your settings, connections and the states you set by hand stay. An upgrade from a version before 0.16 also runs the setup again, so that you choose a mode and connect your issue tracker. The rebuilt index uses the model in your settings. If you choose another model, Pensieve warns you first. Then it embeds the text it already holds with the new model, in the background. It does not read your files or run git blame again, and search keeps working with the old model. When the new vectors are ready, the backend restarts and uses them.

### Summaries and insights

Session summaries, topic names, insights, team summaries and the collection plans for MCP servers wait in a queue. Threads need none of them. By default, Pensieve runs no text model: the agent that you connect over MCP answers the queue. To write them in the background, choose an engine in Settings → Models → Summaries and insights (`text_engine`):

| Engine | What writes the text |
|---|---|
| **Built-in** (`builtin`) | Qwen 3.5 9B on your Mac, with MLX. Nothing leaves the Mac. Pensieve starts the model when there is work, at background priority, and stops it after 10 minutes without work. It downloads about 5 GB once, or uses the copy that LM Studio downloaded. It needs Apple Silicon and 16 GB of memory. There is only one built-in server: a server that already runs, for example from before a restart, is used again. |
| **Command line** (`cli`) | A command-line agent, run once per job in an empty folder. The Claude Code template uses Haiku with no tools, MCP servers, hooks or saved transcript. Codex, Gemini CLI, Qwen Code and Ollama have templates too. |
| **Model server** (`server`) | A model server that you run, at an OpenAI-compatible URL: LM Studio or Ollama on your Mac, or a hosted API with a key. With no model chosen, Pensieve uses the server's first chat model. Pensieve only connects to the server: it never starts one. |

One job runs at a time, across every Pensieve process, at most `engine_rate` jobs a minute (20 by default). A job that runs longer than 3 minutes is stopped, with every process it started. An agent connected with `?tools=all` answers jobs with `pending_work` and `submit_work`. When an engine is also on, the first answer wins.

Pensieve's own engine runs never come back as your sessions. Every prompt the engine sends opens with a marker line, and each command runs in a `pensieve-ai-…` folder. The indexer skips both, so notes are never written about notes.

Search, maps, activity, keyword topics, your footprint in each repo and teammates' commits do not need the agent.

## Command line

```bash
pensieve                 # backend + Map in the browser (http://localhost:8765)
pensieve --no-open       # backend only (what the Mac app runs)
pensieve mcp             # MCP over stdio (--all-tools to include the settings tools)
pensieve integrate       # agent add-ons: instructions block, skills and the edit hook
pensieve search "retry failed webhook delivery" --kind code   # what an agent's search returns, scoped to this repo
pensieve repo status     # whether Pensieve indexes this repo; also index, skip (stop syncing it) or remove (drop it from the index)
pensieve thread <thread id> -o thread.html   # a thread as one HTML page; --json or --agent for the data
```

A thread follows [`thread.schema.json`](pensieve/thread.schema.json) (`pensieve-thread/2`). Pensieve also reads files in the older `context-card/1` format. Every thread has its **state**, the steps **left**, and the question that the last session asked and nobody answered. Its resources are the supporting context, and each one has a reach ([`resources.py`](pensieve/resources.py)):

| Reach | Resources |
|---|---|
| local | session transcripts, agent memory files, uncommitted or unpushed changes, files outside git, folders. They work only on this Mac. |
| git | the branch on the remote, files at a pushed commit, PRs |
| mcp | tickets, messages, pages from your connections |
| restricted | Claude artifacts: a link that can need access |
| public | public web pages |
| private | direct messages, group messages, private channels. Only your local threads and agents use them. |

On the Threads tab, the eye button shows a thread's page, and the Compact view lists one thread a line (a click opens it in full). A thread follows the work of one session ([`docs/threads.md`](docs/threads.md)). "Fork session" starts an agent that reads the thread first. The threads of one branch sit in a cluster: its header previews them as one page, forks a session from all of them, and has Ship and Discard. The page holds the threads' JSON in a `<script type="application/json" id="pensieve-thread">` element, so an agent reads the same file. Agents save a page with the `thread_page` MCP tool.

Flags: `--port`, `--data` (index location), `--repos PATH…` (extra repos for this run).

On Apple Silicon, the embedding model runs on MLX. On other machines, it runs on sentence-transformers and PyTorch (CUDA or CPU), which makes the install about 650 MB larger. The backend and the Map work on Linux too; the search panel app is macOS only.

## Privacy

Pensieve runs on your Mac. It keeps the index, the settings and the feedback reports in `~/.pensieve`. It sends no usage data and has no account.

Pensieve connects to other hosts only for these jobs:

- **Models:** on first run, it downloads the models from Hugging Face.
- **Updates:** the app and the Map ask GitHub for the latest release.
- **Pull requests:** for a thread, the `gh` command gets the PRs of your branches from GitHub, with your own `gh` login. For the threads that are not done (Open, To-do, Loose ends), Pensieve asks every minute, in one call per repo. It asks about merged or closed PRs once a day.
- **Connections:** if you turn on a connection, Pensieve reads that MCP server with read-only tools.
- **Your agent:** when your agent does Pensieve's pending work, the text of each job goes to that agent and its model provider.

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
