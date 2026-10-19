# Storage v2

Storage v2 keeps all of Pensieve's data in two SQLite files with typed tables, so that cards, people and search read
rows by key instead of scanning files. The main parts are the data store (`store.db`), the derived index (`index.db`),
the vector index (`vectors/*.tv`), the typed models (`pensieve/models.py`) and the store layer (`pensieve/store/`).

## Goals

- One database engine (SQLite) and one way to read and write data.
- Each fact is stored once. Every index is derived from stored rows and can be built again.
- Clear data models: each table has typed columns, and each row type has a Python model.
- Cards update within 2 seconds of a change in their sessions, documents, commits or pull requests.
- Search never waits for the indexer.

## Why SQLite

Benchmark on this project's workloads (synthetic data at real sizes, 16 cores, warm file cache). Scripts and raw
results: the storage benchmark of 2026-10-18.

| Workload | Files (v1) | SQLite | DuckDB | Postgres | Delta | LanceDB |
|---|---|---|---|---|---|---|
| Scan items of the last 30 days, 100k items | 13–39 s | 0.07 s | 0.05 s | 0.12 s | 0.05 s | 0.05 s |
| Re-sync 100k items, no change | 9.5 s | 0.14 s | 0.06 s | 0.14 s | 0.05 s | 0.06 s |
| Upsert one item | 0.23 ms | 0.10 ms | 3.4 ms | 0.28 ms | 26 ms | 51 ms |
| Read 10 items by id | 2 ms | 0.026 ms | 0.53 ms | 0.15 ms | 9.5 ms | 2.3 ms |
| A second process reads while one writes | yes | yes | no (file lock) | yes | yes | yes |

DuckDB locks its file against a second process and does not update its full-text index on writes. Postgres is a
server that the app would have to run. Delta and LanceDB are slow for single-row writes and need compaction. SQLite is
in the process, fast for the small writes that the indexer makes, and lets agents read while the indexer writes.

Vectors stay in turbovec: 0.26 ms for a top-20 search over 300k vectors at recall 0.91, against 270 ms for sqlite-vec,
4.6 ms for LanceDB (recall 0.79 with a filter) and 39 ms for DuckDB (recall 0.10 with a filter). The best hybrid
query (vectors, a word and a JSON filter) is SQLite for the filter and turbovec for the vectors: 23 ms, recall 0.90.

## Layout

```
~/.pensieve/
  settings.json          configuration (unchanged)
  server.log, *.log      logs (unchanged)
  v2/
    store.db             data that Pensieve collected or that the user decided; a reset keeps it
    index.db             data derived from disk and from store.db; a reset deletes it
    vectors/             turbovec indexes: code.tv, files.tv, sessions.tv, docs.tv; derived from vectors in index.db
    git/                 shadow git repos for blame (derived)
    avatars/             pictures (derived, a cache)
```

`settings.json` stays a file, because the Mac app and the configuration code read it before a database opens.

## Rules for every table

- Each table is `STRICT`. SQLite then rejects a value of the wrong type.
- Times are `INTEGER` Unix seconds, UTC. No ISO strings and no floats in columns.
- Booleans are `INTEGER` with `CHECK (x IN (0, 1))`.
- A closed set of values (a kind, a state, a role) has a `CHECK (x IN (...))` and a Python `StrEnum` of the same values.
- JSON is `TEXT` with `CHECK (json_valid(x))`, and only for open data from an outside service (`raw`), never for data
  that Pensieve reads by field. A field that Pensieve reads gets its own column.
- Every table has a primary key. Foreign keys are on (`PRAGMA foreign_keys = ON`).
- Each row type has a frozen dataclass in `pensieve/models.py` with the same fields and types.
- Only `pensieve/store/` runs SQL. Other modules call its typed functions.

## Identifiers

| Type | Form | Example |
|---|---|---|
| `SourceId` | slug of a connection, or a fixed name | `slack`, `claude_ai_Slack`, `agent_sessions`, `github_cli` |
| `DocId` | `INTEGER` row id in `documents` | `412` |
| `IdentityKey` | `<kind>:<value>`, lower case | `email:sam@example.com`, `gh:slee`, `slack:U012ABC`, `name:sam lee` |
| `PersonId` | `p` + 10 hex characters, stable once given | `p3f9a2c01be` |
| `CardId` | `t:<ticket>`, `b:<repo>@<branch>`, `s:<session>`, `p:<pr url>` | `t:ABC-123` |
| `ChunkId` | `INTEGER` row id of a chunk table; also the turbovec id | `90311` |
| Search hit id | `<kind>:<ref>` | `doc:412`, `code:<repo>:<path>`, `file:<path>`, `session:<id>` |

## store.db: collected and user data

### Sources and documents

```sql
CREATE TABLE sources (            -- a place that Pensieve collects documents from
  id            TEXT PRIMARY KEY,                 -- SourceId
  kind          TEXT NOT NULL CHECK (kind IN ('mcp', 'agent_sessions', 'gh_cli', 'apps')),
  connector     TEXT,                             -- slack, jira, github, linear, notion, hubspot, granola, drive
  name          TEXT NOT NULL,
  origin        TEXT NOT NULL CHECK (origin IN ('engine', 'agent', 'own', 'cli', 'builtin')),
  status        TEXT NOT NULL CHECK (status IN ('connected', 'needs_auth', 'failed', 'unknown')),
  switched      TEXT CHECK (switched IN ('on', 'off')),   -- NULL: the user did not choose; on once it has data
  collected_at  INTEGER,
  failed_at     INTEGER,
  error         TEXT
) STRICT;

CREATE TABLE documents (          -- one item from a source: a ticket, a pull request, a message, a page
  id         INTEGER PRIMARY KEY,                 -- DocId
  source_id  TEXT NOT NULL REFERENCES sources(id),
  key        TEXT NOT NULL,                       -- the item's id in its service: ABC-123, owner/repo#12, C01:1712.5
  kind       TEXT NOT NULL CHECK (kind IN ('ticket', 'pull_request', 'issue', 'message', 'thread', 'page',
                                           'meeting', 'file', 'record', 'other')),
  title      TEXT NOT NULL,
  url        TEXT,
  status     TEXT,                                -- as the service names it: open, in progress, merged
  branch     TEXT,                                -- head branch of a pull request
  created_at INTEGER,
  updated_at INTEGER,                             -- the service's update time
  seen_at    INTEGER,                             -- when an agent session last saw it, else when it was collected;
                                                  -- used when updated_at is NULL
  private    INTEGER NOT NULL DEFAULT 0 CHECK (private IN (0, 1)),
  text       TEXT NOT NULL,                       -- the readable text that search indexes
  raw        TEXT NOT NULL CHECK (json_valid(raw)), -- the service's item, as it came
  hash       TEXT NOT NULL,                       -- of raw: an upsert with the same hash changes nothing
  UNIQUE (source_id, key)
) STRICT;
CREATE INDEX documents_time ON documents (coalesce(updated_at, seen_at));
CREATE INDEX documents_branch ON documents (branch) WHERE branch IS NOT NULL;

CREATE TABLE document_people (    -- the people on a document, as identity keys
  doc_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  key     TEXT NOT NULL,                          -- IdentityKey
  role    TEXT NOT NULL CHECK (role IN ('author', 'assignee', 'reviewer', 'commenter', 'attendee', 'owner',
                                        'mentioned')),
  name    TEXT,
  PRIMARY KEY (doc_id, key, role)
) STRICT;

CREATE TABLE document_changes (   -- the history of a document's fields
  doc_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  at      INTEGER NOT NULL,
  field   TEXT NOT NULL,
  old     TEXT,
  new     TEXT,
  PRIMARY KEY (doc_id, at, field)
) STRICT;

CREATE TABLE document_sessions (  -- the agent sessions that saw a document
  doc_id      INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  session_id  TEXT NOT NULL,
  seen_at     INTEGER NOT NULL,
  PRIMARY KEY (doc_id, session_id)
) STRICT;
CREATE INDEX document_sessions_session ON document_sessions (session_id);
CREATE INDEX document_people_key ON document_people (key);
```

The Markdown of a document is made from its row when an agent or a card asks for it. It is not stored.

An upsert runs on the writer thread: it reads the stored row, merges the new copy into it, and writes only when the
hash of the merged `raw` changed. The sessions that saw an item are rows of `document_sessions`, not part of `raw`, so
a new session does not change the hash.

### People

```sql
CREATE TABLE identities (         -- one key of a person, as one service shows it
  key         TEXT PRIMARY KEY,                   -- IdentityKey
  kind        TEXT NOT NULL CHECK (kind IN ('email', 'gh', 'slack', 'name')),
  name        TEXT,
  title       TEXT,
  avatar_url  TEXT,
  source      TEXT NOT NULL,                      -- git, github, slack, directory, task
  first_seen  INTEGER NOT NULL,
  last_seen   INTEGER NOT NULL
) STRICT;

CREATE TABLE persons (
  id          TEXT PRIMARY KEY,                   -- PersonId
  name        TEXT NOT NULL,
  is_me       INTEGER NOT NULL DEFAULT 0 CHECK (is_me IN (0, 1)),
  created_at  INTEGER NOT NULL
) STRICT;

CREATE TABLE person_keys (        -- which identities are one person, and why
  key         TEXT PRIMARY KEY REFERENCES identities(key),
  person_id   TEXT NOT NULL REFERENCES persons(id),
  reason      TEXT NOT NULL CHECK (reason IN ('fact', 'evidence', 'user', 'me')),
  score       REAL,                               -- for 'evidence'
  evidence    TEXT CHECK (evidence IS NULL OR json_valid(evidence))
) STRICT;

CREATE TABLE person_aliases (     -- older ids of a person, so that links keep working
  old_id     TEXT PRIMARY KEY,
  person_id  TEXT NOT NULL REFERENCES persons(id)
) STRICT;

CREATE TABLE person_overrides (   -- the user's own decisions; they always win
  id      INTEGER PRIMARY KEY,
  action  TEXT NOT NULL CHECK (action IN ('merge', 'split', 'me')),
  a       TEXT NOT NULL,                          -- IdentityKey
  b       TEXT,                                   -- IdentityKey, for merge
  at      INTEGER NOT NULL
) STRICT;

CREATE TABLE org_pull_requests (  -- evidence for the org chart, kept across builds
  url         TEXT PRIMARY KEY,
  repo        TEXT NOT NULL,
  author_key  TEXT,
  created_at  INTEGER,
  updated_at  INTEGER
) STRICT;
CREATE TABLE org_reviews (
  url           TEXT NOT NULL REFERENCES org_pull_requests(url) ON DELETE CASCADE,
  reviewer_key  TEXT NOT NULL,
  state         TEXT NOT NULL CHECK (state IN ('approved', 'changes_requested', 'commented')),
  at            INTEGER,
  PRIMARY KEY (url, reviewer_key, state)
) STRICT;
CREATE TABLE org_titles (
  key         TEXT NOT NULL,                      -- IdentityKey
  title       TEXT NOT NULL,
  first_seen  INTEGER NOT NULL,
  last_seen   INTEGER NOT NULL,
  builds      INTEGER NOT NULL,
  PRIMARY KEY (key, title)
) STRICT;
CREATE TABLE org_first_commits (
  key  TEXT PRIMARY KEY,
  at   INTEGER NOT NULL
) STRICT;
```

### Cards

```sql
CREATE TABLE cards (              -- one piece of the user's work
  id          TEXT PRIMARY KEY,                   -- CardId
  kind        TEXT NOT NULL CHECK (kind IN ('ticket', 'branch', 'work', 'goal')),
  goal_of     TEXT REFERENCES cards(id) ON DELETE CASCADE,  -- for a goal split from another card
  title       TEXT NOT NULL,
  state       TEXT NOT NULL CHECK (state IN ('latent', 'conscious', 'subconscious', 'etched', 'forgotten')),
  first_at    INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL,                   -- the newest member's time
  built_at    INTEGER NOT NULL,                   -- when the builder last wrote this row
  signature   TEXT NOT NULL,                      -- of the members: a change marks the note stale
  view        TEXT NOT NULL CHECK (json_valid(view))  -- the whole card as the page and agents read it
) STRICT;
CREATE INDEX cards_updated ON cards (updated_at);

CREATE TABLE card_members (       -- what a card is made of
  card_id  TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
  kind     TEXT NOT NULL CHECK (kind IN ('session', 'document', 'pull_request', 'branch', 'commit', 'file',
                                         'ticket_key', 'worktree')),
  ref      TEXT NOT NULL,                         -- session id, DocId, PR url, repo@branch, sha, path, key, path
  at       INTEGER,                               -- the member's own time
  PRIMARY KEY (card_id, kind, ref)
) STRICT;
CREATE INDEX card_members_ref ON card_members (kind, ref);

CREATE TABLE card_notes (         -- the AI note of a card
  card_id    TEXT PRIMARY KEY,                    -- no foreign key: see below
  version    INTEGER NOT NULL,
  title      TEXT,
  problem    TEXT,
  goal       TEXT,
  state      TEXT,
  next       TEXT,
  question   TEXT,
  signature  TEXT NOT NULL,                       -- the card's signature when the note was written
  model      TEXT NOT NULL,
  written_at INTEGER NOT NULL,
  extra      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(extra))  -- the rest of the model's answer
) STRICT;

CREATE TABLE card_states (        -- states set by the user or an agent; the newest row wins
  id       INTEGER PRIMARY KEY,
  card_id  TEXT NOT NULL,
  state    TEXT NOT NULL CHECK (state IN ('latent', 'conscious', 'subconscious', 'etched', 'forgotten', 'auto')),
  by       TEXT NOT NULL CHECK (by IN ('user', 'agent', 'auto')),
  reason   TEXT,
  at       INTEGER NOT NULL
) STRICT;
CREATE INDEX card_states_card ON card_states (card_id, at);

CREATE TABLE card_done (          -- items of a note that a later commit did
  card_id  TEXT NOT NULL,
  item     TEXT NOT NULL,
  at       INTEGER NOT NULL,
  why      TEXT NOT NULL DEFAULT '',              -- "done in 1a2b3c4 (subject)"
  PRIMARY KEY (card_id, item)
) STRICT;

CREATE TABLE threads (            -- one goal followed over time (docs/threads.md)
  id           TEXT PRIMARY KEY,
  goal         TEXT NOT NULL,
  goal_vec     BLOB,                              -- float32 embedding of the goal
  created_at   INTEGER NOT NULL,
  ended_at     INTEGER,
  updated_at   INTEGER NOT NULL,
  parent       TEXT,                              -- the thread it forked or split from
  merged_into  TEXT
) STRICT;
CREATE TABLE thread_members (     -- the linker's members of a thread and their weights (threads.members)
  thread_id  TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  member     TEXT NOT NULL,                       -- ticket:ABC-1, pr:<url>, branch:<repo>@<branch>, ...
  weight     REAL NOT NULL,
  PRIMARY KEY (thread_id, member)
) STRICT;
CREATE TABLE thread_repos (
  thread_id  TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  repo       TEXT NOT NULL,
  PRIMARY KEY (thread_id, repo)
) STRICT;
CREATE TABLE thread_cards (
  thread_id  TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  card_id    TEXT NOT NULL,
  PRIMARY KEY (thread_id, card_id)
) STRICT;
CREATE TABLE thread_events (
  id         INTEGER PRIMARY KEY,
  at         INTEGER NOT NULL,
  event      TEXT NOT NULL CHECK (event IN ('start', 'continue', 'fork', 'split', 'merge', 'revive', 'suggest')),
  thread_id  TEXT NOT NULL,
  other      TEXT,
  sim        REAL
) STRICT;
CREATE TABLE thread_snapshots (   -- a saved card, at one time
  id         INTEGER PRIMARY KEY,
  thread_id  TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  at         INTEGER NOT NULL,
  snapshot   TEXT NOT NULL,                       -- the snapshot id (a hash of the members)
  state      TEXT NOT NULL CHECK (state IN ('latent', 'conscious', 'subconscious', 'etched', 'forgotten')),
  structure  TEXT NOT NULL,                       -- a hash of the members other than files
  pushed     TEXT NOT NULL,                       -- the remote commits of the card's folders, space-separated
  goal_vec   BLOB,
  card       TEXT NOT NULL CHECK (json_valid(card))  -- context-card/1, a format for other programs
) STRICT;
CREATE INDEX thread_snapshots_thread ON thread_snapshots (thread_id, at);
CREATE TABLE thread_snapshot_reasons (
  snapshot_id  INTEGER NOT NULL REFERENCES thread_snapshots(id) ON DELETE CASCADE,
  reason       TEXT NOT NULL CHECK (reason IN ('start', 'fork', 'split', 'merge', 'revive', 'state', 'members',
                                               'push', 'goal')),
  PRIMARY KEY (snapshot_id, reason)
) STRICT;
```

Three exceptions to the rules above, and why:

- `cards.view` is JSON that Pensieve writes. The view holds the card's folders, PR checks, events and sessions. Its
  shape changes often, and Pensieve reads it only whole: the page, the MCP process and the history read it without a
  build. The fields that SQL filters or joins on are columns (`state`, `updated_at`) or rows (`card_members`).
- `card_notes.extra` is the rest of the model's answer (evidence, other goals) and the goals that Pensieve split from
  it. It is outside data: what the model wrote.
- `card_notes` has no foreign key to `cards`. A note outlives its card's row: a card leaves the 30-day window and
  comes back, and a split goal's note is written before its card.

The linker of `pensieve/threads.py` keeps each thread's members, weights, repos and events, so these are tables too.

### Other user data

```sql
CREATE TABLE feedback (id INTEGER PRIMARY KEY, at INTEGER NOT NULL,
  by TEXT NOT NULL CHECK (by IN ('user', 'agent')), kind TEXT NOT NULL CHECK (kind IN ('bug', 'idea')),
  text TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('new', 'sent', 'dismissed'))) STRICT;
CREATE TABLE usage_calls (...) STRICT;   -- the columns of usage.db, which moves here
CREATE TABLE agent_jobs (...) STRICT;    -- the columns of agent_jobs.db, which moves here
CREATE TABLE schema_version (version INTEGER NOT NULL) STRICT;
```

## index.db: derived data

The tables of today's `pensieve.db` move here with STRICT types and integer times: `sessions`, `session_chunks`
(today `chunks`), `session_files`, `files`, `file_chunks`, `repos`, `code_chunks`, `commits`, `checkout_files`,
`worktrees`, `data_points`, `data_topics`, `topics`. New tables:

```sql
CREATE TABLE doc_chunks (         -- documents, chunked for search
  id      INTEGER PRIMARY KEY,                    -- ChunkId, the id in vectors/docs.tv
  doc_id  INTEGER NOT NULL,                       -- DocId in store.db
  idx     INTEGER NOT NULL,
  text    TEXT NOT NULL,
  vec     BLOB NOT NULL,                          -- float16
  hash    TEXT NOT NULL                           -- sha1 of the text: a chunk that did not change keeps its row
) STRICT;
CREATE INDEX doc_chunks_doc ON doc_chunks (doc_id, idx);

CREATE TABLE doc_index_state (    -- the hash of each document when the indexer chunked it
  doc_id  INTEGER PRIMARY KEY,
  hash    TEXT NOT NULL,
  at      INTEGER NOT NULL
) STRICT;

CREATE TABLE embed_cache (        -- a vector for each distinct text, so that the same text is embedded once
  hash   TEXT NOT NULL,                           -- sha1 of the text
  model  TEXT NOT NULL,
  vec    BLOB NOT NULL,
  PRIMARY KEY (hash, model)
) STRICT;

CREATE TABLE person_edges (       -- the People graph, built from store.db and the repos
  a       TEXT NOT NULL,                          -- PersonId
  b       TEXT NOT NULL,
  kind    TEXT NOT NULL CHECK (kind IN ('code', 'pr', 'task', 'thread', 'dm', 'channel')),
  n       INTEGER NOT NULL,
  last_at INTEGER,
  weight  REAL NOT NULL,
  PRIMARY KEY (a, b, kind)
) STRICT;

CREATE TABLE projects (...) STRICT;          -- the Projects tab, one row per project, members in project_members
CREATE TABLE cache (key TEXT PRIMARY KEY, value BLOB NOT NULL, expires_at INTEGER NOT NULL) STRICT;  -- gh and git
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
```

Full-text indexes: `fts_code_words` (word tokens: names and words) is the main code index, and `fts_code` (trigram)
stays only for substring queries of 3 or more characters. `fts_docs`, `fts_files` and `fts_sessions` use word tokens.

## Connections and locks

- Each thread has its own SQLite connection to each file. In WAL mode, a reader never waits for the writer.
- One writer thread per file takes write jobs from a queue and commits them in batches of up to 50 ms.
- The process-wide lock (`store.lock`) is removed. Search, cards and the MCP server read without a lock.
- The MCP command-line process opens the same files. WAL allows a second process.

The code: `pensieve/store/db.py` has the `Db` class of one file. `db.read(fn)`, `db.query(sql)` and `db.one(sql)` run
on the calling thread's connection, which is `query_only`. `db.write(fn)` sends `fn(conn)` to the writer and waits
for its commit; `db.write(fn, wait=False)` returns a future. Each job runs in its own savepoint, so a job that fails
rolls back only its own changes. `db.after_commit` callbacks get the results of each committed batch.
`pensieve/store/schema.py` has the schema of each file as one part for each aggregate, and a list of migrations for
changes to tables that exist. `pensieve/store/rows.py` turns models into rows and back. There is one repository
module for each aggregate (`sources.py`, `documents.py`, `cards.py`, ...). The tables `usage_calls`, `agent_jobs`
and `projects` come in phase 4, with their modules.

## Cards within 2 seconds

Today a card build reads every session, document and commit of 30 days, every 60 seconds. In v2 the build is
incremental:

1. A write that can change a card adds an event to an in-memory queue: a session indexed, a document upserted, a
   commit loaded, a pull request state changed, a card state set.
2. The card builder reads the event's refs, finds the cards that have them (`card_members_ref`), and builds only those
   cards again: their members, title, state and signature. A ref that no card has starts a new card.
3. The builder writes the changed rows of `cards` and `card_members` and sends a `card` event with the card ids to
   the web page, which updates only those cards.
4. A card whose signature changed gets a new AI note after 20 seconds without more changes, so that a running
   session does not start a note for each turn.
5. Pull request states of open PRs come from `gh` every 5 minutes, and at once for a card that changed.

Target: a new session turn, document or commit shows on its card within 2 seconds. The note follows within about 30
seconds.

## Phases

Each phase is one or more commits on the branch `storage-v2`, with its own measurement.

| Phase | Work | Measure |
|---|---|---|
| 0 | Fixes in v1: a lock and an atomic write for card notes; commit in `Store.meta`; `Files.roots()` without a folder scan every 5 s; count thread snapshots without reading whole files | no lost note updates in a test with 10 writers |
| 1 | `models.py`, `store/` (schemas, connections, writer queue), the v2 layout and `schema_version` | read and write tests per table |
| 2 | Documents: connections write `documents`; the indexer chunks documents from the table; search ids `doc:<id>` | 30-day scan and re-sync times at 10k and 100k documents |
| 3 | Cards: tables, the incremental builder, notes, states, threads | time from a change to the card on the page |
| 4 | People, org evidence, projects, caches, feedback, usage, agent jobs | People and Projects build times |
| 5 | Search: word index for code, trigram for substrings only | `bench/`: no loss of accuracy; latency p50 and p95 |
| 6 | Vectors: `embed_cache`; in-memory vector arrays that change with the rows (no full read every 30–120 s) | CPU while idle; search p95 while indexing |
| 7 | Cutover to v2 and removal of v1 code and files | a first start on a copy of real data |

## Migration

Pensieve is early, so v2 starts clean in `~/.pensieve/v2/` and indexes again. It keeps only what cannot be made
again, and what costs time or money to make again.

On the first start of v2:

1. Make `~/.pensieve/v2/` and the two databases.
2. Import from v1, each import small and allowed to fail:
   - the user's decisions: person merges and splits, "This is me", card states set by the user, feedback;
   - card notes, so that the AI does not write them again;
   - org evidence;
   - usage history.
3. Fill `embed_cache` from the vectors of v1 (`code_chunks`, `file_chunks`, `chunks`), by the hash of each chunk's
   text. The new index then embeds only text that changed. Without this, the first index of a large repo takes hours.
4. Index again: sessions, files, repos. Collect the connections again and read the agent sessions again.
5. Search works for each source as soon as its first index is done.
6. When v2 finishes its first full index, delete the v1 data: `pensieve.db`, the `.tv` files, `connections/`,
   `people/`, `projects/`, `threads/`, `git/`, `work_notes.json`, `work_done.json`, `usage.db`, `agent_jobs.db` and
   the empty `index.db`.

Until step 6, an older app still works with the v1 data. After step 6 there is no way back.

What users see: one slower start after the update, with the progress in the status menu, and search results that
fill in as indexing goes. Settings, logs and the Mac app stay as they are.

What changes for agents: search hits for connection items have the id `doc:<id>` instead of a file path. `read` and
`similar` accept both forms during v2's first release.

## Not in scope

- One table for all chunk kinds. The kinds have different columns; this can come later.
- A server database or a search server.
- Sync between Macs (see `docs/design/peer-sharing.md`).
