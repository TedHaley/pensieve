# Pensieve

A local knowledge map of every conversation you've had with AI coding agents (Claude Code, Codex, Qwen Code), linked to the code those conversations produced and the people who wrote the code around it.

Everything runs on your machine. Transcripts are embedded with `Qwen3-Embedding-0.6B`, stored in SQLite plus a [turbovec](https://pypi.org/project/turbovec/) index, laid out in 3D with UMAP, and summarized and chatted with by a local model via any OpenAI-compatible server (LM Studio by default).

## Install

```bash
curl -fsSL https://tedhaley.ca/pensieve/install.sh | sh
```

This installs [uv](https://docs.astral.sh/uv/) if needed, installs Pensieve, and starts it. Or, if you already have uv:

```bash
uv tool install git+https://github.com/TedHaley/pensieve
pensieve
```

`pipx install git+https://github.com/TedHaley/pensieve` works too. More at [tedhaley.ca/pensieve](https://tedhaley.ca/pensieve).
To try it without installing: `uvx --from git+https://github.com/TedHaley/pensieve pensieve`.

`pensieve` opens http://localhost:8765. On first run it downloads the embedding model (~1.2 GB), indexes your sessions, then the git repos they ran in. The map fills in live as it goes.

### Optional: a local chat model

Summaries, topic names, insights, and chat need an LLM. Without one, the maps, search, and code views still work and the status pill says **LLM offline**.

1. Install [LM Studio](https://lmstudio.ai), download `qwen/qwen3.5-9b` (or any chat model), and start the server (Developer → Start server, port 1234).
2. Run `pensieve`. Other models or servers: `pensieve --llm-model <name> --llm-url http://localhost:1234/v1`.

## What gets indexed

- **Sessions** from `~/.claude/projects`, `~/.codex/sessions`, and `~/.qwen/projects`. They are read-only; nothing is modified.
- **Repos**: only the git repos your sessions ran in. Pensieve does not scan your disk. Clones and worktrees of the same remote count as one repo. To add others:

  ```bash
  pensieve --repos ~/code/some-repo ~/work   # a repo, or a folder searched 3 levels deep for repos
  ```

- **Index location**: everything Pensieve stores lives in `~/.pensieve` (`--data` to change it). Delete that folder to start over.

## Using it

| | |
|---|---|
| **Code** | Each repo's code as a 3D map with git blame, or *All repositories* in one joint layout. Browse the folder tree with owners, find *who knows about X*, spot conceptually overlapping areas (also across repos), and jump between code and the sessions that touched it. |
| **Map** | Every session (or every conversation *moment*) as a point. Color by code area, topic, project, agent, or recency. Expand a repo to filter by folder; filtering to a repo or folder re-clusters the topics for just those sessions. Filters narrow everything, including chat. |
| **Insights** | Activity, working rhythm, and topic and project breakdowns, plus AI themes, connections, open threads, and unexplored directions for any repo or folder scope. Per repo: your footprint, unexplored areas next to your work and who to talk to, and what each teammate shipped in the last 90 days. |
| **Ask** | Chat grounded in your transcripts (or code), with clickable citations. Questions like "this week" or "yesterday" are scoped automatically. |
| **⌘K** | Search everything: sessions (by title and meaning), topics, projects, folders, files, people, commands. `who: <topic>` finds experts. |

Keyboard: `⌘K` / `/` search · `1 2 3` views · `F` fit · `C` chat · `T` theme · `Esc` close · `?` help.

## Options

| Flag | Env var | Default |
|---|---|---|
| `--port` | `PENSIEVE_PORT` | `8765` |
| `--data` | `PENSIEVE_DATA` | `~/.pensieve` |
| `--repos PATH…` | `PENSIEVE_REPOS` (`:`-separated) | repos your sessions ran in |
| `--llm-url` | `PENSIEVE_LLM_URL` | `http://localhost:1234/v1` |
| `--llm-model` | `PENSIEVE_LLM_MODEL` | `qwen/qwen3.5-9b` |
| | `PENSIEVE_EMBED_MODEL` | `Qwen/Qwen3-Embedding-0.6B` |
| `--no-open` | | opens the browser |

Runs on Apple Silicon (MPS), CUDA, or CPU. CPU indexing is slower but works.

## Development

```bash
git clone https://github.com/TedHaley/pensieve && cd pensieve
uv run pensieve
```

To support another agent's transcripts, add a parser in `pensieve/parsers.py` and a line in `SOURCES` in `pensieve/config.py`.
