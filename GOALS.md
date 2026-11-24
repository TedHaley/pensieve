# Goals

Pensieve makes your coding agents faster and makes them use fewer tokens. It also keeps your work together across
agent sessions, as thoughts.

It has four parts: a background indexer, a service for agents (MCP), two skills, and a menu bar item for the settings.
One skill tells agents when to use the search. The other skill tells agents how to show your thoughts. The menu
bar item opens a settings window. Apart from it, you use Pensieve through your agents and the `pensieve` command.

## Scope

| Kept | Removed |
|---|---|
| `crates/store`, `crates/embed`, `crates/index` | `crates/people`, `crates/connect`, `crates/insights` |
| Indexing of code, documents and agent sessions | Threads, to-dos, and calls to GitHub and Jira |
| Thoughts: work joined across sessions, with the pull requests, tickets and files that the sessions name | The page (`static/`), the REST API for the page |
| MCP tools: `search`, `read`, `thoughts`, `recall`, `name_thought` | The app window of 0.21 |
| The CLI: `install`, `status`, `uninstall`, `search`, `thoughts`, `watch`, `open`, `recall`, `name` | Hooks: search gate, prompt context, session start lines |
| Two skills for agents: `pensieve-search`, `pensieve-thoughts` | |
| The menu bar item (`mac/`): the status, and a settings window (General, Sources, File Types, Privacy) | |
| The weekly usage profile | The text engine and every text LLM |

[`PARITY.md`](PARITY.md) records the 0.21 port. It is history now. Only the kept parts stay in the build.

## Principles

1. **Quiet.** The only interface is the menu bar item. It shows the status and opens the settings window.
   `pensieve install` adds a LaunchAgent, the MCP server and the skills. Pensieve never shows a notification.
2. **Pull, not push.** Agents call Pensieve when the skill tells them that it is better than grep. Pensieve adds no hooks
   and puts no context into prompts.
3. **Events, not polls.** File system events and transcript appends start the work. One safety sweep runs every 6
   hours. No loop wakes more often than once a minute when the Mac is idle.
4. **Idle and power aware.** Embedding and other heavy work run at background QoS. They run only when the Mac is idle,
   and on AC power or with more than 50% battery. A search always comes first.
5. **Index less, by evidence.** A usage profile, built every week from the agent transcripts, decides what to embed.
   Areas that agents do not use get a name-only index. The profile only adds and reorders sources. It never removes
   a source that the user added.
6. **One model, on MLX.** The embedding model is the only model. It runs on MLX, which uses about 1 GB where the f32
   candle path uses about 3.1 GB.
7. **Measured.** Each change gets a measurement against the baseline. Two numbers decide: the energy that Pensieve uses,
   and the tokens and time that agents use for the same tasks with and without Pensieve.

## Measures

| Measure | How |
|---|---|
| CPU seconds per hour, idle and live | `ps` CPU time of the daemon and its child processes, sampled every 10 s |
| Energy Impact | The `POWER` column of `top`, per process, mean over the hour |
| Memory | The `MEM` column of `top`, which includes GPU memory |
| Energy per indexed chunk | CPU seconds and Energy Impact of a first index of a fixed repo, divided by its chunks |
| Agent tokens and time per task | The same tasks with and without Pensieve, in a scratch agent setup |

`powermetrics` gives energy in mWh, but it needs root. Use it when root is available, and record which tool gave each
number.

## Baseline: Pensieve 0.21

See [`BASELINE.md`](BASELINE.md).

## Open work

- [x] Remove the parts in the "Removed" column from the build.
- [x] `pensieve install`, `status` and `uninstall`, with a LaunchAgent at background priority. Tested on a scratch
  home folder without `launchctl`. The LaunchAgent is not yet tested in a real login session.
- [x] Loops wake on file and transcript events. The safety sweep runs every 6 hours, and no loop waits less than 60 s.
- [x] Video and sound decoder (`media/pensieve-media.swift`). `cargo build` compiles it into the pensieve
  binary, and `install` and `serve` write it to `bin/` in the data folder. When the settings switch on images, video or
  audio, the daemon downloads that part of the model and reads those files again by content.
- [x] Images, video and audio are read by content only when in use (`crates/index/src/fileuse.rs`): 2 opens (Spotlight
  open count), 2 edits at least an hour apart, or 1 agent session. The other files in the folder of a file in use are
  in use too. The setting `media_by_use: false` reads all of them.
- [x] Images, video and audio are on by default. The daemon downloads the image part (about 340 MB) or the sound part
  (about 610 MB) only when a file of that kind is in use.
- [ ] Embedding on MLX: link the prebuilt `libmlx.dylib` and `mlx.metallib` from the `mlx` Python wheel. A build of
  MLX from source needs the Metal compiler, which comes only with Xcode.
- [ ] A `powermetrics` run of the baseline, for energy in mWh.
- [ ] A benchmark of agent tokens and time with and without it.
- [ ] The usage profile, weekly, from the transcripts, with the rules in principle 5.
- [ ] Gate heavy work on idle time and power state.
