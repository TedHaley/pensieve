# Pensieve

<!-- mcp-name: io.github.TedHaley/pensieve -->

[![Release](https://img.shields.io/github/v/release/TedHaley/pensieve)](https://github.com/TedHaley/pensieve/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform: macOS](https://img.shields.io/badge/platform-macOS%20%28Apple%20Silicon%29-lightgrey.svg)](https://pensieve.tools)

Pensieve makes your coding agents faster and makes them use fewer tokens, and it keeps your work together across
agent sessions as thoughts.

It has four parts:

- **A background indexer.** It indexes your code, documents and agent sessions on your Mac. It runs at background
  priority and starts work only on file events.
- **An MCP server** for your agents: `search`, `read`, `thoughts`, `recall` and `name_thought`.
- **Two skills.** `pensieve-search` tells agents when a search is better than grep. `pensieve-thoughts` tells them how
  to show your thoughts.
- **A menu bar item** that shows the status and opens the settings.

Everything stays on your Mac. Pensieve has one model, EmbeddingGemma, for the embeddings. It has no text model.

## Install

Build it from source with Rust and the Swift command-line tools:

```bash
cargo build --release
sh mac/build.sh
target/release/pensieve install
```

`pensieve install` adds a LaunchAgent, the MCP server for Claude Code and Codex, and the skills.
`pensieve uninstall --purge` removes all of them and the index.

## Thoughts

A thought is one piece of work that Pensieve joins across your agent sessions, with its pull requests, tickets and
files. Ask your agent "where did I leave off?", or use the terminal:

```bash
pensieve thoughts            # a numbered list of your work
pensieve open 2              # continue thought 2 in a new tab
pensieve open 2 --new        # start a new session that reads thought 2
pensieve recall 2            # print the full context of thought 2
pensieve watch               # a list of the newest thoughts that refreshes itself
```

Each thought has a `pensieve://thought/th_…` link. Paste the link into an agent session to continue that work.

## Search

```bash
pensieve search "retry logic for webhooks" --kind code
```

Agents use the same search through MCP.

## Settings

The menu bar item opens the settings window: General, Sources, File Types and Privacy. Pensieve reads images, video
and audio by content only for the files that you use.

## Design

- [`GOALS.md`](GOALS.md): the scope and the principles.
- [`BASELINE.md`](BASELINE.md): the measurements of 0.21, which this version must beat.

## License

MIT
