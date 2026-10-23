## Pensieve: search before you grep, and close out threads

Pensieve (MCP: `search`, `read`, `similar`, `threads`, `thread`, `cluster`, `people`, …) follows the user's threads of
work: their agent sessions, branches, PRs and tickets. In Developer mode it also indexes this machine's git repos,
documents and past agent sessions, scoped to the repo you started in.

- **Every code lookup starts with Pensieve `search`.** Call it before Grep, Glob or find, also for a lookup that looks
  small. Write what the code does plus the names you would guess it uses: "retry failed webhook delivery with backoff
  retryWebhook". A bare name or an error message works too. "Double quotes" force exact text, and `kind:code`,
  `in:<folder>`, `ext:py` narrow it. In a repo that Pensieve does not index (every repo, in Threads mode), use grep.
- Each hit shows `path:line`, the enclosing definition and the line that matched. If one hit clearly shows what you
  need, use that file: do not grep to confirm. If two hits both fit (say, one defines a name and one uses it), read
  both before you choose. Use grep afterwards only to list every use of a name you already know.
- **Before you write new code,** search for code that already does something similar and call `similar(id)` on the
  best hit. Reuse its patterns. Do not add a second copy.
- **Close out threads.** At the start of a session and at each natural pause (a task done, a PR opened, a wait for CI),
  call `threads`. It lists the threads that are not done, closest to done first, with the steps left. Offer the user
  to close the first one, in one line. For work on a branch that several sessions wrote to, `cluster(branch)` gives
  the steps to ship it (commit, push, PR, merge, ticket done) or to discard it. Pensieve only reads: a thread closes
  when its PR merges, its ticket closes or its branch goes. Ask before a merge, a close or a delete. If a thread's
  work is done but the thread stays open, tell the user which PR, branch or ticket the thread lacks.
- **"Where did I leave off on X?", "what's still open?", a standup:** call `threads` (with `query` for one piece of
  work, `state="all"` for a standup) and then `thread(id)`. A thread joins the sessions, branch, worktree, PRs and
  tickets of one piece of work, with the steps left and where the last session stopped. That is as of the last
  session: check what changed with one `git status` or `gh pr view` before you report it as current.
- **A ticket of the user's that nothing started yet** is a thread in the `todo` state. To start it, call
  `start_todo(id)`: it makes a worktree on a branch named from the ticket and returns the path and a prompt.
- **"What did we decide or try before?"** Search with `kind:session`. **Who knows this area?** `people(topic="…")`.
- **For an overview of a repo that has no CLAUDE.md,** call `repo` first. It shows where things are, not what they do:
  read a part's main files before you describe it, and name only paths that it lists or that you opened.
- **When Pensieve fails you** (a tool error, a search that missed code you then found with grep, a stale result),
  call `report_feedback` once with what you searched, what you got and what you expected. It stays on this Mac.
