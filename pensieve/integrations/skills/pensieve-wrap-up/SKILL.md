---
name: pensieve-wrap-up
description: Close out the user's open threads with Pensieve. Gets the threads closest to done and does the steps that close each one in git, GitHub and the ticket tracker. Use when the user asks to wrap up, close out, tidy or finish their threads, asks "what is left to close", or asks whether the last thread is done.
---

# Wrapping up threads

Pensieve only reads. It derives the state of a thread from its evidence: its branch, PRs, commits and tickets. A
thread closes when that evidence says that the work is done. You close a thread when you change the real systems.
Never set a thread state by hand.

## Steps

1. Call `threads`. It lists the threads that are not done, with the fewest steps left first.
2. For each thread, call `thread(id)` to get its folder, branch, PRs, tickets and the steps left.
3. Do each step as the table below says. Do one thread at a time.
4. After the steps, call `thread(id)` again. Pensieve rebuilds its threads about once a minute.

| Step | Who | What you do |
| --- | --- | --- |
| Fix the failing checks on #n | agent | Fix the code and push. |
| Address the review on #n | agent | Make the changes and push. Reply to each comment. |
| Commit or push on a branch | agent | Commit or push in the folder of the thread. |
| Merge #n | you | Ask the user. If they agree, merge it. |
| Mark #n ready for review, get #n reviewed | you | Tell the user. Do not mark it ready or request a review yourself. |
| Close TICKET-1 | tracker | Ask the user. If they agree, change the status in the tracker. |
| Answer the agent: … | you | Ask the user the question. |
| Work on TICKET-1 | agent | This is new work, not a wrap-up. Tell the user and stop on this thread. |

## Threads that do not close

- **The work was abandoned.** Ask the user. If they agree, close its PR, delete its branch and close its ticket.
  Then Pensieve sees that nothing is waiting, and the thread becomes Forgotten.
- **The work is done, but the thread stays open.** Find the commit, PR or ticket that holds the work. Tell the user
  which link the thread lacks, for example "its work merged in #49, and the thread does not name that PR". This is
  a gap in how Pensieve links evidence. Do not hide it.
- **An idea with no work.** If it has no branch, commit, PR or ticket, Pensieve marks it forgotten by itself. Do
  nothing.

## Rules

- Ask before each action that is hard to undo: a merge, a closed PR, a deleted branch, a ticket status change.
  One yes covers one item.
- Do not call `set_thread_state`.
- Report at the end: the threads that closed, the steps that wait for the user, and the threads that lack a link.
