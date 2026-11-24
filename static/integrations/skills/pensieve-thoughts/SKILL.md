---
name: pensieve-thoughts
description: Show the user their earlier work across agent sessions, with the pull requests, tickets and files of each piece of work. Use when the user asks what they worked on, where they left off, what is still open, which work a PR, ticket or file belongs to, or wants to continue or open earlier work. Also use when the user pastes a pensieve://thought/ or pensieve://session/ link.
---

# Showing thoughts with Pensieve

A thought is one piece of work that Pensieve joined across the user's agent sessions. The `thoughts` tool of the
Pensieve MCP server returns thoughts as JSON cards. Do not show the JSON. Write each card again so that the user knows
the work at one look.

## Get the cards

1. Call `thoughts`. Give `query` when the user names the work ("the retry logic for webhooks"). Give no `query` for
   "what did I work on" or "where did I leave off".
2. If the first card does not fit the question, call `thoughts` one more time with words from the cards.
3. If no card fits, tell the user that Pensieve has no thought for it. Do not make one up.

## Show a list of thoughts

Use the words that the user knows. The user wrote `asked` and `left_off`, and they know the session titles, the PR
numbers and the ticket keys. They do not know the thought id or the session ids.

Leave out the thought of the session that runs now, unless the user asks for it. The user knows where that work is.

Give each thought a number, from 1, in the order of the cards. Write at most 4 lines for each thought:

```
**2. Retry logic for webhooks** · app · 3 sessions · 2 days ago
Stopped: the backoff works, and the test for the limit of 5 tries fails.
Next: correct the test, then open the PR.
Links: acme/app#7, ABC-12
```

- **The first line** has the number, a name of 3 to 6 words, the project, the number of sessions, and when the work
  was last active. If `named` is true, use `name`: the user or an agent gave it. If `named` is false, `name` is a
  session title or the start of a prompt. Use it only when it tells what the work is. If it does not, make the name
  from `asked` and `session_titles`.
- **Stopped** is one sentence from `left_off` and `ended_with`. If `left_off` is a short answer such as "yes", use
  `ended_with`.
- **Next** is the next step that `ended_with` or `left_off` names. Leave the line out if they name none.
- **Links** has the pull requests as `repo#number` with their `url`, then the tickets by `key`. Leave the line out if
  the card has none. Do not show the files in a list.

After the last thought, write one line: `Say a number to continue it here, or "open 2" to continue it in a new tab.`

Remember which thought id each number has. The user refers to the thoughts by number after that.

If the search gave `matched`, show that prompt for the thought. It is why the thought matched.

## Show one thought

When the user asks for one piece of work, or for the details of a number, show the full card:

- the first line as in the list;
- **Asked:** one sentence from `asked`, in the user's words;
- **Stopped** and **Next** as in the list;
- **Links:** the pull requests, the tickets, then at most 3 files;
- the `resume` command in a code block, if `resume` is not null.

## Do not guess

- Show only links that the card holds. Do not make a URL from a ticket key or a PR number.
- The card is as of the last session. If the user asks for the state of a PR or a ticket now, read it with `gh` or
  the tracker's tool.
- `ended_with` is the end of the agent's last reply. It does not prove that the work is done.

## Continue a thought in this session

When the user says a number, or asks to continue a thought here, call `recall` with the card's `id`. It returns each
session's part in time order, with the prompts, the files and the end of the last reply. Read it, then tell the user
in two sentences where the work is, and continue from there.

When the user pastes a link, call `recall` with the link:

- `pensieve://thought/th_…` gives the whole thought, from all its sessions.
- `pensieve://session/<agent>:<id>` gives all the parts of that one session, with the thought of each part. It also
  gives the command that resumes that session in a terminal.

Then tell the user in two sentences where the work is, and continue from there.

## Open a thought in a new tab

When the user says "open 2", or asks for a new tab or a new session for a thought, run this command in the shell:

```
pensieve open <id>
```

- It opens a new tab in the app that the terminal runs in, and resumes the newest session of the thought there.
- If the user asks for a new session and not the old one, add `--new`. The new session reads the thought with
  `recall`, which gives it the work of all sessions.
- If the command opens no tab, it prints the command that continues the thought. Show that command in a code block.
- If you have no shell, show the `resume` command of the card (or `start` for a new session) in a code block.

Tell the user in one line that the tab is open. Do not call `recall` for a thought that you open in a new tab.

## Name a thought

Pensieve has no language model, so its names are session titles or the user's first words. You can give a better
name, and Pensieve keeps it.

- If the user gives a thought a name, call `name_thought` with the card's `id` and that name.
- After `recall`, if the card's `named` is false and its `name` does not tell what the work is, call `name_thought`
  with 3 to 6 words in the user's words. You know the full context then.
- Do not give names to thoughts that you know only from their cards.

## In a terminal

`pensieve thoughts [query]` prints the same cards as a numbered list. `pensieve open <number>` continues
one of them in a new tab, and `pensieve recall <number>` prints its context. `pensieve watch` shows the newest
thoughts in a list that refreshes itself. A click on a thought resumes it in a new tab, and `[fork]` starts a new
session that reads the thought.
