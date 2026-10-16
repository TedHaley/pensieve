# Peer sharing: let a team see each other's sessions

Status: **proposal, not started.** Parked to come back to later.

## Goal

Pensieve only sees your own agent sessions. Git blame shows who *committed* code, but not who is working
in an area right now. Sharing sessions across a team would surface in-progress work ("Yuhong had three
sessions in `src/pipeline/utils` this week") before a PR exists, and make "who should I talk to" much
better. All of this without a central server.

## What gets shared: session cards, not transcripts

Raw transcripts stay on the owner's machine. They contain pasted secrets, customer data, half-formed
thoughts, and file contents from repos a teammate might not be able to read. Each session is instead
published as a small **card** built from data Pensieve already produces:

| Field | Source | Why |
|---|---|---|
| Title + LLM summary | `summarize.py` | readable without the transcript |
| Embedding of the summary (+ model id) | `indexer.py` | peers' sessions appear on your map, in search, and in "who knows X" |
| Code areas touched (`repo/a/b`) | `repos.session_scope()` | "who's working near me right now" |
| Canonical repo remote | `repos.Repos.locate()` | access control (below) |
| Author, agent, date | sessions table | who to reach out to |

A peer can *request* the full transcript, and the owner approves or declines it.

### Access follows the repo

You only receive cards for repos whose canonical remote you also have cloned locally. If you can read
the code, you can see who has been working on it with agents. This needs no separate permission system,
and nobody sees sessions about repos they can't access.

## Transport options

1. **Shared folder** (Syncthing, Dropbox, network drive). Each person's Pensieve writes
   `cards/<email>.jsonl` and reads everyone else's. No networking code, and genuinely P2P with
   Syncthing. A card can be taken back because rewriting the file replaces it.
2. **Live federation over Tailscale or the LAN.** Each instance exposes a read-only
   `/api/peer/cards`, and peers are configured with `--peers alice.tailnet:8765`. Results are cached
   locally. Data stays with its owner and is always current, but an offline peer's cards go stale.
   Tailscale provides identity and NAT traversal.
3. **A git repo or orphan branch.** Everyone already has auth, but anything pushed lives in history
   forever, so a card can't be taken back. Rejected for that reason.
4. **P2P libraries** (iroh, libp2p). Discovery and hole punching are overkill until there are many
   teams on different networks.

**Recommendation:** define the card format and a pluggable sync directory (option 1) first; add the
Tailscale endpoint (option 2) next.

## Must-haves

- **Opt-in sharing**: per repo, plus a "don't share" toggle on each session. Before anything leaves the
  machine, run a secret scan (key and token patterns) on the summary, and show a preview of exactly
  what will be shared.
- **Embedding compatibility**: cards record their embedding model; mismatches are re-embedded locally
  from the summary text (cheap) or dropped. Alternatively, ship text only and always re-embed.
- **Company policy**: for work repos, sessions may contain customer data. Check with security before
  enabling sharing for a team, even for summaries.

## First milestone

- Card export and import with a shared-folder sync (`--share-dir PATH`).
- The repo-based access rule.
- Opt-in UI with a preview.
- Peer points on the Map and Code views in a distinct color; peers in Insights team cards and in
  "who knows X".
- Test between two `--data` directories on one machine before any network transport.
