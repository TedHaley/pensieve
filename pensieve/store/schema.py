"""The SQL schema of store.db and index.db (docs/design/storage.md).

Each file's schema is a dict of parts, one per aggregate, so that each phase adds its own tables. Every statement is
`IF NOT EXISTS`: a new table needs no migration. A change to a table that exists (a new column) is a step in
MIGRATIONS, and VERSION goes up by one; `schema_version` holds the version of the file."""

STORE_VERSION = 1
INDEX_VERSION = 1

STORE = {
    "sources": """
CREATE TABLE IF NOT EXISTS sources (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL CHECK (kind IN ('mcp', 'agent_sessions', 'gh_cli', 'apps')),
  connector     TEXT,
  name          TEXT NOT NULL,
  origin        TEXT NOT NULL CHECK (origin IN ('engine', 'agent', 'own', 'cli', 'builtin')),
  status        TEXT NOT NULL CHECK (status IN ('connected', 'needs_auth', 'failed', 'unknown')),
  switched      TEXT CHECK (switched IN ('on', 'off')),
  collected_at  INTEGER,
  failed_at     INTEGER,
  error         TEXT
) STRICT;
""",
    "documents": """
CREATE TABLE IF NOT EXISTS documents (
  id         INTEGER PRIMARY KEY,
  source_id  TEXT NOT NULL REFERENCES sources(id),
  key        TEXT NOT NULL,
  kind       TEXT NOT NULL CHECK (kind IN ('ticket', 'pull_request', 'issue', 'message', 'thread', 'page',
                                           'meeting', 'file', 'record', 'other')),
  title      TEXT NOT NULL,
  url        TEXT,
  status     TEXT,
  branch     TEXT,
  created_at INTEGER,
  updated_at INTEGER,
  seen_at    INTEGER,
  private    INTEGER NOT NULL DEFAULT 0 CHECK (private IN (0, 1)),
  text       TEXT NOT NULL,
  raw        TEXT NOT NULL CHECK (json_valid(raw)),
  hash       TEXT NOT NULL,
  UNIQUE (source_id, key)
) STRICT;
CREATE INDEX IF NOT EXISTS documents_time ON documents (coalesce(updated_at, seen_at));
CREATE INDEX IF NOT EXISTS documents_branch ON documents (branch) WHERE branch IS NOT NULL;

CREATE TABLE IF NOT EXISTS document_people (
  doc_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  key     TEXT NOT NULL,
  role    TEXT NOT NULL CHECK (role IN ('author', 'assignee', 'reviewer', 'commenter', 'attendee', 'owner',
                                        'mentioned')),
  name    TEXT,
  PRIMARY KEY (doc_id, key, role)
) STRICT;
CREATE INDEX IF NOT EXISTS document_people_key ON document_people (key);

CREATE TABLE IF NOT EXISTS document_changes (
  doc_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  at      INTEGER NOT NULL,
  field   TEXT NOT NULL,
  old     TEXT,
  new     TEXT,
  PRIMARY KEY (doc_id, at, field)
) STRICT;

CREATE TABLE IF NOT EXISTS document_sessions (
  doc_id      INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  session_id  TEXT NOT NULL,
  seen_at     INTEGER NOT NULL,
  PRIMARY KEY (doc_id, session_id)
) STRICT;
CREATE INDEX IF NOT EXISTS document_sessions_session ON document_sessions (session_id);
""",
    "people": """
CREATE TABLE IF NOT EXISTS identities (
  key         TEXT PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('email', 'gh', 'slack', 'name')),
  name        TEXT,
  title       TEXT,
  avatar_url  TEXT,
  source      TEXT NOT NULL,
  first_seen  INTEGER NOT NULL,
  last_seen   INTEGER NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS persons (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  is_me       INTEGER NOT NULL DEFAULT 0 CHECK (is_me IN (0, 1)),
  created_at  INTEGER NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS person_keys (
  key         TEXT PRIMARY KEY REFERENCES identities(key),
  person_id   TEXT NOT NULL REFERENCES persons(id),
  reason      TEXT NOT NULL CHECK (reason IN ('fact', 'evidence', 'user', 'me')),
  score       REAL,
  evidence    TEXT CHECK (evidence IS NULL OR json_valid(evidence))
) STRICT;
CREATE INDEX IF NOT EXISTS person_keys_person ON person_keys (person_id);

CREATE TABLE IF NOT EXISTS person_aliases (
  old_id     TEXT PRIMARY KEY,
  person_id  TEXT NOT NULL REFERENCES persons(id)
) STRICT;

CREATE TABLE IF NOT EXISTS person_overrides (
  id      INTEGER PRIMARY KEY,
  action  TEXT NOT NULL CHECK (action IN ('merge', 'split', 'me')),
  a       TEXT NOT NULL,
  b       TEXT,
  at      INTEGER NOT NULL
) STRICT;
""",
    "org": """
CREATE TABLE IF NOT EXISTS org_pull_requests (
  url         TEXT PRIMARY KEY,
  repo        TEXT NOT NULL,
  author_key  TEXT,
  created_at  INTEGER,
  updated_at  INTEGER
) STRICT;
CREATE TABLE IF NOT EXISTS org_reviews (
  url           TEXT NOT NULL REFERENCES org_pull_requests(url) ON DELETE CASCADE,
  reviewer_key  TEXT NOT NULL,
  state         TEXT NOT NULL CHECK (state IN ('approved', 'changes_requested', 'commented')),
  at            INTEGER,
  PRIMARY KEY (url, reviewer_key, state)
) STRICT;
CREATE TABLE IF NOT EXISTS org_titles (
  key         TEXT NOT NULL,
  title       TEXT NOT NULL,
  first_seen  INTEGER NOT NULL,
  last_seen   INTEGER NOT NULL,
  builds      INTEGER NOT NULL,
  PRIMARY KEY (key, title)
) STRICT;
CREATE TABLE IF NOT EXISTS org_first_commits (
  key  TEXT PRIMARY KEY,
  at   INTEGER NOT NULL
) STRICT;
""",
    "cards": """
CREATE TABLE IF NOT EXISTS cards (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('ticket', 'branch', 'work', 'goal')),
  goal_of     TEXT REFERENCES cards(id) ON DELETE CASCADE,
  title       TEXT NOT NULL,
  state       TEXT NOT NULL CHECK (state IN ('latent', 'conscious', 'subconscious', 'etched', 'forgotten')),
  first_at    INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL,
  built_at    INTEGER NOT NULL,
  signature   TEXT NOT NULL,
  view        TEXT NOT NULL CHECK (json_valid(view))
) STRICT;
CREATE INDEX IF NOT EXISTS cards_updated ON cards (updated_at);

CREATE TABLE IF NOT EXISTS card_members (
  card_id  TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
  kind     TEXT NOT NULL CHECK (kind IN ('session', 'document', 'pull_request', 'branch', 'commit', 'file',
                                         'ticket_key', 'worktree')),
  ref      TEXT NOT NULL,
  at       INTEGER,
  PRIMARY KEY (card_id, kind, ref)
) STRICT;
CREATE INDEX IF NOT EXISTS card_members_ref ON card_members (kind, ref);

CREATE TABLE IF NOT EXISTS card_notes (
  card_id    TEXT PRIMARY KEY,
  version    INTEGER NOT NULL,
  title      TEXT,
  problem    TEXT,
  goal       TEXT,
  state      TEXT,
  next       TEXT,
  question   TEXT,
  signature  TEXT NOT NULL,
  model      TEXT NOT NULL,
  written_at INTEGER NOT NULL,
  extra      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(extra))
) STRICT;

CREATE TABLE IF NOT EXISTS card_states (
  id       INTEGER PRIMARY KEY,
  card_id  TEXT NOT NULL,
  state    TEXT NOT NULL CHECK (state IN ('latent', 'conscious', 'subconscious', 'etched', 'forgotten', 'auto')),
  by       TEXT NOT NULL CHECK (by IN ('user', 'agent', 'auto')),
  reason   TEXT,
  at       INTEGER NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS card_states_card ON card_states (card_id, at);

CREATE TABLE IF NOT EXISTS card_done (
  card_id  TEXT NOT NULL,
  item     TEXT NOT NULL,
  at       INTEGER NOT NULL,
  why      TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (card_id, item)
) STRICT;

CREATE TABLE IF NOT EXISTS card_done_checks (
  card_id  TEXT PRIMARY KEY,
  key      TEXT NOT NULL,
  at       INTEGER NOT NULL
) STRICT;
""",
    "threads": """
CREATE TABLE IF NOT EXISTS threads (
  id           TEXT PRIMARY KEY,
  goal         TEXT NOT NULL,
  goal_vec     BLOB,
  created_at   INTEGER NOT NULL,
  ended_at     INTEGER,
  updated_at   INTEGER NOT NULL,
  parent       TEXT,
  merged_into  TEXT,
  snapshot     TEXT
) STRICT;
CREATE TABLE IF NOT EXISTS thread_members (
  thread_id  TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  member     TEXT NOT NULL,
  weight     REAL NOT NULL,
  PRIMARY KEY (thread_id, member)
) STRICT;
CREATE TABLE IF NOT EXISTS thread_repos (
  thread_id  TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  repo       TEXT NOT NULL,
  PRIMARY KEY (thread_id, repo)
) STRICT;
CREATE TABLE IF NOT EXISTS thread_cards (
  thread_id  TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  card_id    TEXT NOT NULL,
  PRIMARY KEY (thread_id, card_id)
) STRICT;
CREATE INDEX IF NOT EXISTS thread_cards_card ON thread_cards (card_id);
CREATE TABLE IF NOT EXISTS thread_events (
  id         INTEGER PRIMARY KEY,
  at         INTEGER NOT NULL,
  event      TEXT NOT NULL CHECK (event IN ('start', 'continue', 'fork', 'split', 'merge', 'revive', 'suggest')),
  thread_id  TEXT NOT NULL,
  other      TEXT,
  sim        REAL
) STRICT;
CREATE TABLE IF NOT EXISTS thread_snapshots (
  id         INTEGER PRIMARY KEY,
  thread_id  TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  at         INTEGER NOT NULL,
  snapshot   TEXT NOT NULL,
  state      TEXT NOT NULL CHECK (state IN ('latent', 'conscious', 'subconscious', 'etched', 'forgotten')),
  structure  TEXT NOT NULL,
  pushed     TEXT NOT NULL,
  goal_vec   BLOB,
  card       TEXT NOT NULL CHECK (json_valid(card))
) STRICT;
CREATE INDEX IF NOT EXISTS thread_snapshots_thread ON thread_snapshots (thread_id, at);
CREATE TABLE IF NOT EXISTS thread_snapshot_reasons (
  snapshot_id  INTEGER NOT NULL REFERENCES thread_snapshots(id) ON DELETE CASCADE,
  reason       TEXT NOT NULL CHECK (reason IN ('start', 'fork', 'split', 'merge', 'revive', 'state', 'members',
                                               'push', 'goal')),
  PRIMARY KEY (snapshot_id, reason)
) STRICT;
""",
    "feedback": """
CREATE TABLE IF NOT EXISTS feedback (
  id       TEXT PRIMARY KEY NOT NULL,
  at       INTEGER NOT NULL,
  by       TEXT NOT NULL CHECK (by IN ('user', 'agent')),
  kind     TEXT NOT NULL CHECK (kind IN ('bug', 'idea')),
  text     TEXT NOT NULL,
  status   TEXT NOT NULL CHECK (status IN ('new', 'sent', 'dismissed')),
  details  TEXT NOT NULL,
  source   TEXT NOT NULL,
  version  TEXT NOT NULL,
  context  TEXT NOT NULL CHECK (json_valid(context))
) STRICT;
""",
    "people_extra": """
CREATE TABLE IF NOT EXISTS slack_users (
  id          TEXT PRIMARY KEY NOT NULL,
  name        TEXT NOT NULL,
  title       TEXT NOT NULL,
  email       TEXT NOT NULL,
  avatar_url  TEXT NOT NULL,
  display     TEXT NOT NULL,
  bot         INTEGER NOT NULL CHECK (bot IN (0, 1)),
  is_me       INTEGER NOT NULL CHECK (is_me IN (0, 1)),
  seen_at     INTEGER NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS directory_people (
  id          TEXT PRIMARY KEY NOT NULL,
  name        TEXT NOT NULL,
  login       TEXT NOT NULL,
  slack       TEXT NOT NULL,
  title       TEXT NOT NULL,
  avatar_url  TEXT NOT NULL,
  found_at    INTEGER NOT NULL,
  seen_at     INTEGER NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS directory_emails (
  person_id  TEXT NOT NULL REFERENCES directory_people(id) ON DELETE CASCADE,
  email      TEXT NOT NULL,
  PRIMARY KEY (person_id, email)
) STRICT;
CREATE TABLE IF NOT EXISTS directory_sources (
  person_id  TEXT NOT NULL REFERENCES directory_people(id) ON DELETE CASCADE,
  idx        INTEGER NOT NULL,
  source     TEXT NOT NULL,
  PRIMARY KEY (person_id, idx)
) STRICT;

CREATE TABLE IF NOT EXISTS org_team_pairs (
  a_key       TEXT NOT NULL,
  b_key       TEXT NOT NULL,
  weight      REAL NOT NULL,
  updated_at  INTEGER NOT NULL,
  PRIMARY KEY (a_key, b_key)
) STRICT;
CREATE TABLE IF NOT EXISTS org_manager_titles (
  key         TEXT NOT NULL,
  title       TEXT NOT NULL,
  first_seen  INTEGER NOT NULL,
  last_seen   INTEGER NOT NULL,
  builds      INTEGER NOT NULL,
  PRIMARY KEY (key, title)
) STRICT;
CREATE TABLE IF NOT EXISTS org_state (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  since       INTEGER NOT NULL,
  updated     INTEGER NOT NULL,
  builds      INTEGER NOT NULL,
  last_build  INTEGER NOT NULL,
  teams_at    INTEGER NOT NULL
) STRICT;
""",
    "usage": """
CREATE TABLE IF NOT EXISTS usage_calls (
  id         INTEGER PRIMARY KEY,
  at         INTEGER NOT NULL,
  source     TEXT,
  tool       TEXT NOT NULL,
  client     TEXT,
  conn       TEXT,
  cwd        TEXT,
  scope      TEXT,
  args       TEXT CHECK (args IS NULL OR json_valid(args)),
  ms         REAL,
  ok         INTEGER CHECK (ok IN (0, 1)),
  error      TEXT,
  n_results  INTEGER,
  results    TEXT CHECK (results IS NULL OR json_valid(results)),
  laps       TEXT CHECK (laps IS NULL OR json_valid(laps)),
  out_chars  INTEGER
) STRICT;
CREATE INDEX IF NOT EXISTS usage_calls_at ON usage_calls (at);
CREATE INDEX IF NOT EXISTS usage_calls_tool ON usage_calls (tool, at);
""",
    "agent_jobs": """
CREATE TABLE IF NOT EXISTS agent_jobs (
  id           TEXT PRIMARY KEY NOT NULL,
  kind         TEXT NOT NULL,
  system       TEXT NOT NULL,
  prompt       TEXT NOT NULL,
  schema       TEXT CHECK (schema IS NULL OR json_valid(schema)),
  max_tokens   INTEGER NOT NULL,
  asked_at     INTEGER NOT NULL,
  answer       TEXT,
  answered_at  INTEGER,
  claimed_at   INTEGER,
  claimer      TEXT
) STRICT;
CREATE TABLE IF NOT EXISTS agent_sessions (
  conn     TEXT PRIMARY KEY NOT NULL,
  client   TEXT,
  at       INTEGER NOT NULL,
  answers  INTEGER NOT NULL
) STRICT;
""",
    "migration": """
CREATE TABLE IF NOT EXISTS migration_steps (
  step    TEXT PRIMARY KEY,
  status  TEXT NOT NULL CHECK (status IN ('done', 'failed', 'skipped')),
  count   INTEGER NOT NULL,
  error   TEXT,
  tries   INTEGER NOT NULL,
  at      INTEGER NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS v1_doc_paths (
  path       TEXT PRIMARY KEY,
  source_id  TEXT NOT NULL,
  key        TEXT NOT NULL
) STRICT;
""",
}

INDEX = {
    "docs": """
CREATE TABLE IF NOT EXISTS doc_chunks (
  id      INTEGER PRIMARY KEY,
  doc_id  INTEGER NOT NULL,
  idx     INTEGER NOT NULL,
  text    TEXT NOT NULL,
  vec     BLOB NOT NULL,
  hash    TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS doc_chunks_doc ON doc_chunks (doc_id, idx);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_docs USING fts5 (text, content='doc_chunks', content_rowid='id',
                                                       tokenize='unicode61');
CREATE TABLE IF NOT EXISTS doc_index_state (
  doc_id  INTEGER PRIMARY KEY,
  hash    TEXT NOT NULL,
  at      INTEGER NOT NULL
) STRICT;
""",
    "embed_cache": """
CREATE TABLE IF NOT EXISTS embed_cache (
  hash   TEXT NOT NULL,
  model  TEXT NOT NULL,
  vec    BLOB NOT NULL,
  PRIMARY KEY (hash, model)
) STRICT;
""",
    "person_edges": """
CREATE TABLE IF NOT EXISTS person_edges (
  a       TEXT NOT NULL,
  b       TEXT NOT NULL,
  kind    TEXT NOT NULL CHECK (kind IN ('code', 'pr', 'task', 'thread', 'dm', 'channel')),
  n       INTEGER NOT NULL,
  last_at INTEGER,
  weight  REAL NOT NULL,
  PRIMARY KEY (a, b, kind)
) STRICT;
""",
    "cache": """
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value BLOB NOT NULL, expires_at INTEGER NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
""",
    "avatars": """
CREATE TABLE IF NOT EXISTS avatars (
  hash  TEXT PRIMARY KEY NOT NULL,
  ext   TEXT,
  at    INTEGER NOT NULL
) STRICT;
""",
}

# version -> the statements that take a file from version - 1 to version. Empty while v2 is new.
STORE_MIGRATIONS: dict[int, list[str]] = {}
INDEX_MIGRATIONS: dict[int, list[str]] = {}


def script(parts: dict) -> str:
    return "\n".join(parts.values()) + "\nCREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL) STRICT;\n"
