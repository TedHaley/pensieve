# index: what the app must wire

The crate ports Developer mode's index and search: sessions, files, code, collected documents, vectors, the map
layout, the file watch, search, scopes, places and items. Code search scores the same as 0.21 on 410 bench tasks
(R@1 0.949, MRR 0.967, top-1 file the same on all 410). Chunk boundaries and session parses are identical to 0.21.
Semantic order can differ within about 0.005, because the crate ranks exactly where 0.21 ranks by turbovec's 4-bit
scores.

## Opening

- `Index::open(path, Arc<dyn Embed>, Config)`. index.db has 0.21's tables, so a copy of 0.21's index.db opens as is.
  All vectors load into memory at start (float16 copies plus 4-bit codes; no .tv files).
- `Embed`: `embed(texts, query)`, `dim()`, optional `embed_image` and `embed_media`. With `query = false` the crate
  adds the `title: … | text: …` format itself, so the adapter to crates/embed must use `Kind::Raw`. With
  `query = true` the adapter adds the query prompt (`Kind::Query`).
- `Config::new(settings_json, home)`. The app sets `connections_with_data`. A call that changes a setting returns the
  new value: the app saves it, then calls `set_config`.

## Routes and tools (methods on `Index`)

- Sessions: `api_points(level, session_scope)`, `api_topics`, `api_topics_scoped`, `api_session` (Null = 404),
  `api_similar`, `api_search`.
- Find: `api_find(q, limit, kinds, scope, cwd)`.
- Scopes: `api_scopes`, `save_scope`, `delete_scope`, `api_scopes_resolve`.
- Items: `read_item` (/api/item), `open_item` (/api/open).
- Repos: `api_repos`, `api_repos_upstream`, `api_repos_pull`, `api_repo_check`, `api_repo_points`,
  `api_repo_overlaps`, `api_repo_owners`, `knowledge_gaps`, `api_repo_people`, `api_code`, `api_experts`,
  `api_sessioncode`, `api_team`, `api_territory`, `data_points_code`.
- Files and places: `api_files_points`, `api_file`, `api_filetypes`, `put_filetype`, `api_places`,
  `places_index_folder`.
- MCP: `mcp_search`, `mcp_read`, `mcp_similar`, `mcp_current_scope`, `mcp_list_scopes`, `mcp_who_knows`,
  `mcp_list_repos`, `mcp_overview`, `mcp_repo`, `mcp_reindex`, `mcp_file_types`, `mcp_index_path`; add_folder and
  remove_folder return `{"folders": …}`, which is also the setting to save.

## Background work

- Sessions: `sync_sessions(budget)` again while `sessions.behind`; `sync_session_paths(watch.take_sessions())`, again
  1.5 s later when `deferred`; `backfill_usage` once; `on_session_indexed` (also feeds `connect::from_session`).
- Files: `files_sync(false)` each pass (30 s limit inside); `files_sync_paths(watch.take_files(), SETTLE_SECONDS)`;
  `set_files_content_ready(kinds)`.
- Documents: `docs_sync(want, fetch)` with (id, hash) pairs from connect.db and a fetch closure.
- Repos: `repos_refresh(watch.take_repos())`; `repos_sync(false, None)` every few minutes; `check_upstream(true)`
  every 30 minutes; drain `take_commit_events()`.
- Map, when idle: `reproject_sessions`, `files_reproject`, `repos_reproject` while each `*_needs_layout()`.
- Search: `folder_index_tick(sweeping)` every few seconds; `set_doc_markdown(f)`.
- Watch: `watch_configure(checkouts)` at start and when sources change; `watch.set_wake(f)`; drain with `take_*`.

## Left to the app

The usage log, the scope checks of mcp_server (`_check_repo`, `_mark_user`), naming topics with the text engine,
and the Data-map topics that overview uses.

## Differences from 0.21

- PDF text comes from pdf-extract, not pypdf (lengths within 1.5%). Panics on bad pages are caught.
- `/api/repo-check` gave 1,724 files for one repo where live 0.21 gave 0 (hypothesis: git returns nothing inside the
  live server's process; not checked).
- The map layout uses a fixed seed. 0.21's UMAP has none, so topic counts can differ (silhouette is flat for k 4–8).
- `migrate.compat_id` and the copy from `code_files` to `checkout_files` are not ported.
