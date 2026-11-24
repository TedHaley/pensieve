# Parity with Pensieve 0.21

[`GOALS.md`](GOALS.md) comes first. A 0.21 feature that breaks a goal there does not get ported.

This file tracks the port of Pensieve 0.21 (Python, `~/Desktop/pensieve`) to Rust in this repository. The goal is
the same features in Threads mode and in Developer mode, behind the same page (`static/app.js`), the same REST API
and the same MCP tools.

## Layout

| Crate | Owns | Ports from `pensieve/` |
|---|---|---|
| `crates/store` | data folder, time, SQLite in WAL mode | `store/db.py` |
| `crates/embed` | EmbeddingGemma 2 (text, image, video, sound) on Metal, the embedding cache | `gemma_mlx.py`, `content.py`, `media.py`, `embedcache.py` |
| `crates/index` | chunks, word index, vectors, map layout, indexing of sessions, files, code and collected documents, file watch, search, scopes, places | `indexer.py`, `files.py`, `docindex.py`, `repos.py`, `segment.py`, `digest.py`, `parsers.py`, `vectors.py`, `layout.py`, `watch.py`, `tokens.py`, `search.py`, `scopes.py`, `places.py`, `actions.py` |
| `crates/people` | identities, people, org chart, find a person | `people.py`, `identity.py`, `org.py`, `orgstore.py`, `directory.py` |
| `crates/connect` | HTTP, OAuth, MCP client, connector profiles, connections, collected documents, Keychain, Jira, Slack, `gh` | `connections.py`, `connectors.py`, `mcp_oauth.py`, `keychain.py`, `sources.py`, `chat_joins.py`, `store/documents.py`, `store/sources.py` |
| `crates/insights` | Insights tab, data map, projects, text jobs and the text engine, stale items | `insights_stats.py`, `datamap.py`, `project_model.py`, `team_projects.py`, `summarize.py`, `engine.py`, `agent_jobs.py`, `llm.py`, `stale.py` |
| app (`src/`) | threads, cards, REST API, MCP server, hooks, add-ons, settings, onboarding, reset, CLI, import from 0.21 | everything else |

While a crate is under construction it is a standalone package (listed in `workspace.exclude`), so that one crate
that does not build does not stop the others. It joins the workspace when the app uses it.

## Rules for every crate

- A crate does no I/O to another crate's tables. The app passes plain inputs in and gets plain outputs back.
- Tests use neutral fixtures. No real repo names, paths or people.
- Parity is measured against 0.21 on the same inputs: its Python functions (`.venv/bin/python`) or its live API
  (`http://127.0.0.1:8765`). Never write to `~/.pensieve`; read a copy.

## Status

- [x] Threads: sessions, branches, PRs, state and steps, page
- [x] Jira: sign-in, collection, ticket steps, to-dos, start a to-do
- [x] embed: text, images, video and sound match 0.21's MLX vectors (cosine ≥ 0.9995 on test texts, ≥ 0.99992 on
  300 real session chunks), and the cache keys match. Speed: a Metal and MPS path for texts gives 119–128 texts/s
  against MLX's 114–125 on a quiet Mac. Under load it gives 0.6–0.95× of MLX. Peak memory is about 3.1 GB (f32),
  against about 1 GB for MLX.
- [x] index and search: joined to the app (`src/dev.rs`, `src/devapi.rs`, `src/devmcp.rs`). Sessions, files, code
  and collected documents are indexed on background threads; a search never waits for them. On a copy of 0.21's
  index, 11 routes answer the same as 0.21. Semantic scores differ by about 0.003. See `INTEGRATION.md`.
- [x] people: joined to the app (`src/dev_people.rs`). A build gets the user's threads, their chat items and
  coworkers, and the index's blame. On a 45-person copy, profiles, relations and find match 0.21.
- [x] connect: built, matches 0.21 on real data, and joined to the app (routes and the worker). Open items are in
  `crates/connect/REPORT.md`.
- [x] insights, data map, projects, text engine: joined to the app (`src/dev_insights.rs`). Stats and projects get
  the Threads tab's cards. The agent pass runs every 30 s, the engine worker answers jobs, and the map is laid out
  when indexing is idle. Team summaries start as in 0.21. The built-in model stops on SIGTERM and SIGINT. Not run:
  the built-in engine. `card_done` is a table in store.db, filled by `import` as 0.21 fills it (from old data
  only); its rows count as items closed.
- [x] app: each card's `chat`, `items`, `seen` and their counts come from the collected documents
  (`src/cardextra.rs`), and a person's Slack threads list the threads that the chat joins.
- [x] app: Developer mode's MCP tools, with 0.21's descriptions and schemas (`static/mcp-tools.json`), the real
  scope, and the prompt-context and search-gate hooks of Developer mode
- [x] app: MCP server for threads (threads, thread, cluster, thread_page, start_todo, set_thread_state,
  report_feedback, open, status, settings), stdio bridge, usage log and Usage tab
- [x] app: hooks (session start, prompt context, search gate, touched) and the Agent add-ons page
- [x] page: live updates (the page needs `/api/points`, `/api/topics` and `/api/session-projects` to start its
  event stream)
- [x] app: CLI (`integrate`, `repo`, `setup`, `search`, `thread`, `transcript`, `open`, `import`), reset and restart in
  place
- [x] app: card fields (kin, claims, related, commits, events, artifacts, review, moved, places), the PR poll (60 s for
  open threads) and the file watch. A warm build takes 86–240 ms. A new turn shows in 0.8 s (median) and 2.9 s
  (max). Of 126 threads that match 0.21, artifacts, review and moved agree on all, and the others agree on 87–97%.
  Not ported: `contributed` for a claim loser, `_adopt` (`shared_sessions`), `_integrated`, `ticket_refs`.
- [x] app: onboarding (`/api/setup`) and the Sources tab over the crates (`src/onboarding.rs`). `/api/setup` and
  the agents, files and repos of `/api/sources` match 0.21. Not run: the content downloads (images, video, sound).
- [x] app: import from 0.21 also copies the collected documents, connections, sign-ins, People tables and the
  embedding cache
- [x] menu bar app: `mac/` builds `Pensieve Lite.app` with the daemon inside (`tools.pensieve.lite`, scheme
  `pensieve-lite://`, port 8790), so it runs beside 0.21

## Differences from 0.21 to settle

- `thread --agent` returns the 0.21 page JSON, not the shorter agent JSON of `cards.render_agent`.
- `pensieve-lite serve` does not open the browser. `pensieve-lite open` does.
- `transcript` reads Claude Code sessions only, until the index crate brings the parsers of the other agents.
- Thread ids are `tl_…` (a hash of session and branch), not `th_…`. Hand-set states of 0.21 do not carry over.
