# Threads

A thread follows one goal over time, so you can see how a piece of work changed, pick it up again, or hand it off. This page defines a thread and its parts, the rules that put evidence into a thread, and the events of its life.

## Terms

| Term | Meaning |
|---|---|
| **Goal** | One outcome that someone works toward: "keep placeholder companies out of record linkage", "ship 0.10.0", "find why the nightly gate failed". |
| **Thread** | One goal, followed over time. The goal is the identity of the thread. |
| **Member** | Work done for the goal: a ticket, a pull request (PR), a feature branch, a commit, an agent session, an edited file. |
| **Reference** | Something that only mentions the thread: a standup session that names the ticket, a PR description that links another PR. |
| **Snapshot** | A thread at one point in time: its members and its core (problem, goal, state, next). A context card shows one snapshot. |
| **History** | The snapshots of a thread, oldest first, with the events between them. |
| **Collection** | A set of threads: a person's, a team's or a project's. |

```
member ─▶ snapshot (a card at time t) ─▶ thread (one goal over time) ─▶ collection (many threads)
```

A thread has two ids:

- **Thread id:** it never changes. Hand-set states, notes, published copies and agent references use it.
- **Snapshot id:** a hash of the members at one time. It changes when a member is added or removed.

## Rules

### 1. One goal

A thread has one goal. The steps that serve the goal stay in the thread: a CI fix on its PR, a rebase, a review round. When the goal itself changes, the work becomes a new thread that is forked from the old one.

### 2. Members and references

A member is work done for the goal. A reference only mentions the thread. The card shows references, but references never decide which thread an item belongs to.

### 3. Who owns a member

| Member | Owners |
|---|---|
| Ticket, PR, feature branch | One thread at a time |
| Commit | The thread of its branch |
| Agent session | One or more threads. A session can work toward several goals, one after the other. |
| Edited file | No owner. A file is evidence only (rule 4). |

When two threads claim one ticket, PR or branch, the claim becomes a split or a merge (rule 7). Two threads never share it silently.

### 4. The order of evidence

| Rank | Evidence | Can join | Can split |
|---|---|---|---|
| 1 | Explicit: a person or an agent links or unlinks the item, or a branch name holds the ticket key | Yes | Yes |
| 2 | Structural: the same ticket, PR or feature branch, or a commit on that branch | Yes | No |
| 3 | Activity: a session on the feature branch, or two sessions that edit the same specific files within a few days | Only with rank 2, or with a second rank-3 item | No |
| 4 | Semantic: the distance between the embeddings of two goals | No: it only suggests a link | Yes |

A higher rank wins over a lower rank. An embedding never joins items alone.

### 5. What is never evidence

- A default or long-lived branch (`main`, a release branch, a branch with sessions over more than a few days).
- A file that many threads edit.
- The repo or the folder alone.
- A ticket that a session mentions in passing.

### 6. Continuity

A new snapshot continues a thread when both of these are true:

- It shares a strong member (ticket, PR, feature branch) with the thread, or enough weighted members.
- Its goal is near the goal of the thread (the embeddings of problem and goal).

When the members match and the goal is far, the new snapshot is a fork (rule 7).

### 7. Events

Each event goes into the history of the thread. Old ids and old links continue to point to the thread after an event.

| Event | When |
|---|---|
| **Start** | A snapshot matches no thread. |
| **Continue** | A snapshot matches one thread (rule 6). |
| **Split** | One thread turns out to hold two goals. The part with the larger overlap keeps the thread id. The other part starts a thread that is "split from" it. |
| **Merge** | Two threads turn out to be one goal. The older thread continues. The other thread is "merged into" it. |
| **Fork** | The members match, but the goal moved: a new thread "forked from" the old one. |
| **End** | The goal is done (etched) or left (forgotten). |
| **Revive** | New work on an ended thread, with the same goal. The thread opens again, with no time limit. A different goal starts a new thread. |

### 8. Threads cross repos and people

A ticket can have work in two repos. A teammate's PR joins a thread through its ticket or its branch, not through a folder on your machine.

### 9. Size

A thread is the smallest unit that you hand off or report in a standup. These items are not threads:

- A session that only asked a question and changed nothing. It is a reference.
- A long-lived branch.
- An epic or a project. It is a collection.
- A ticket with no work yet. It is a latent thread: a goal with no members.

## History

The snapshots of a thread form its history, as commits form the history of a branch. The history shows how the goal, the state and the next step changed over time. You can open any snapshot again: a thread that ended can start again from its last snapshot, with its links, its decisions and the commands that get the work.

## How Pensieve links snapshots

1. Pensieve builds the snapshots from the sessions, git, PRs, tickets and connections, as for the Context tab.
2. For each snapshot, it calculates the weighted overlap of members with the snapshots that it built before:

   | Member | Weight |
   |---|---|
   | Ticket key, PR, feature branch | Strong |
   | Commit, session | Medium |
   | Specific edited file | Weak |

3. It finds the best match for each snapshot, not the full connected component. A full component joins everything on a long-lived branch.
4. It compares the embeddings of problem and goal for each match, and applies rule 6.
5. It records the event (rule 7), and keeps the thread id.

The thresholds for overlap and for embedding distance come from a replay of past days (`bench/threads_replay.py`). Pensieve builds the snapshots as of each day, links them, and compares the links with labels of "same work" and "different work". On one Mac, over 30 days and 551 labelled pairs:

| Linker | Wrong joins | Wrong splits | F1 for "same goal" |
|---|---:|---:|---:|
| Members only (overlap 0.4) | 9 | 7 | 0.74 |
| Goals only (similarity 0.8) | 6 | 10 | 0.71 |
| Both (overlap 0.4, similarity 0.6) | 9 | 7 | 0.74 |

Members do the work. The goal check adds nothing on this data, and it stays as a guard for forks. A suggestion at similarity 0.85 finds one missed link, with no wrong suggestion. Most wrong joins come from snapshots that join two goals on one branch before the linker sees them.

## Local history

Pensieve saves each thread's card when it changes (`pensieve/history.py`). It checks every 10 minutes, and it saves a card when:

- the linker records an event (start, fork, split, merge, revive);
- the state of the thread changes;
- a ticket, PR, branch, commit or session comes or goes (edited files alone do not count);
- the embedding of the problem and goal moves 0.05 or more since the last save.

A thread is saved at most once in 30 minutes, except for an event or a state change. A save on a timer alone gives no new information: in the replay, a save at every 12-hour build made 1,365 copies, and the rules above made 29. After 30 days, one save a day is kept for each thread, with every event and state change.

## Examples

- **One goal, three PRs.** A change merges into the wrong branch, a revert undoes it, and a new PR carries the same change. All three PRs are one thread, because the goal is the same.
- **A shared branch.** Two sessions work on a long-lived branch: one changes the logo, one measures search. The branch is not evidence (rule 5), so they are two threads.
- **A PR that another thread owns.** A card about a model fix shows the merged PR of a different feature, which one of its sessions mentions. That PR is a reference on the model fix, and a member of its own thread (rules 2 and 3).
- **A fork.** A session starts on a bug and ends on a new feature. The members match, but the goal moved, so the feature is a new thread, forked from the bug.
