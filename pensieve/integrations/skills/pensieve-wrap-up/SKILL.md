---
name: pensieve-wrap-up
description: Close out the user's open threads with Pensieve, by shipping or discarding their work. Gets the threads closest to done and does the steps that close each one in git, GitHub and the issue tracker. Use when the user asks to wrap up, ship, close out, tidy, discard or finish their threads, asks "what is left to close", or asks whether the last thread is done.
---

# Closing out threads

Pensieve only reads. It derives the state of a thread from its evidence: its branch, PRs, commits and tickets. A
thread closes when that evidence says that the work is done. You close a thread when you change the real systems.
Never set a thread state by hand.

A thread closes in one of two ways:

- **Ship:** commit, push, open a PR, get it reviewed and merged, then mark the ticket done.
- **Discard:** drop the uncommitted files, close the PR without merging, delete the branch, and mark the ticket
  cancelled.

## Steps

1. Call `threads`. It lists the threads that are not done, with the fewest steps left first.
2. For each thread, call `thread(id)` to get its folder, branch, PRs, tickets and the steps left.
3. When several sessions wrote to one branch, call `cluster(branch)`. It gives each session's share of the branch
   and the two step lists: to ship the branch and to discard it. Work on the branch as one piece of work.
4. Ask the user which way to close it, unless they said so already. Then do each step as the table below says. Do
   one thread or one branch at a time.
5. After the steps, call `thread(id)` again. Pensieve rebuilds its threads about once a minute.

| Step | Who | What you do |
| --- | --- | --- |
| Commit N changed files on a branch | agent | Commit in the folder of the thread. |
| Push N commits on a branch | agent | Push the branch. |
| Open a pull request for a branch | agent | Open it as a draft. Show the user the title and body first. |
| Fix the failing checks on #n | agent | Fix the code and push. |
| Address the review on #n | agent | Make the changes and push. Reply to each comment. |
| Mark #n ready for review, get #n reviewed | you | Tell the user. Do not mark it ready or request a review yourself. |
| Merge #n | you | Ask the user. If they agree, merge it. |
| Delete branch X after the merge | you | Ask the user. If they agree, delete it. |
| Close TICKET-1: its pull request merged | tracker | Ask the user. If they agree, change the status in the tracker. |
| Discard N uncommitted files on a branch | agent | Show the user the files. Discard them only after a yes. |
| Close #n without merging | you | Ask the user. If they agree, close it. |
| Delete branch X (local and remote) | agent | Ask the user. If they agree, delete both. |
| Mark TICKET-1 as cancelled (won't do) | tracker | Ask the user. If they agree, change the status in the tracker. |
| Answer the agent: … | you | Ask the user the question. |
| Work on TICKET-1 | agent | This is new work, not a close-out. Tell the user and stop on this thread. |

## Threads that do not close

- **A to-do** is a ticket of the user's with no work yet. It is not a close-out. To start it, call
  `start_todo(id)`. To drop it, mark the ticket cancelled after the user agrees.
- **The work is done, but the thread stays open.** Find the commit, PR or ticket that holds the work. Tell the user
  which link the thread lacks, for example "its work merged in #49, and the thread does not name that PR". This is
  a gap in how Pensieve links evidence. Do not hide it.
- **An idea with no work.** If it has no branch, commit, PR or ticket, it is a thought, and Pensieve marks it
  forgotten after 2 quiet days. Do nothing.

## Rules

- Ask before each action that is hard to undo or that leaves the Mac: a push, a PR, a merge, a closed PR, a deleted
  branch, a ticket status change. One yes covers one item.
- Do not call `set_thread_state`.
- Report at the end: the threads that closed, the steps that wait for the user, and the threads that lack a link.
