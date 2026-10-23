# Threads

A thread follows one goal over time, so you can see how a piece of work changed, pick it up again, or hand it off. This page defines a thread and its parts, the rules that put evidence into a thread, and the events of its life.

## Terms

| Term | Meaning |
|---|---|
| **Goal** | One outcome that someone works toward: "keep placeholder companies out of record linkage", "ship 0.10.0", "find why the nightly gate failed". |
| **Thread** | One goal, followed over time. The goal is the identity of the thread. |
| **Member** | Work done for the goal: a ticket, a pull request (PR), a feature branch, a commit, an agent session, an edited file. |
| **Reference** | Something that only mentions the thread: a standup session that names the ticket, a PR description that links another PR. |
| **Snapshot** | A thread at one point in time: its members and its core (problem, goal, state, next). A thread page shows one snapshot: the thread now, or a saved version. |
| **History** | The snapshots of a thread, oldest first, with the events between them. |
| **Collection** | A set of threads: a person's, a team's or a project's. |

```
member ─▶ snapshot (a version at time t) ─▶ thread (one goal over time) ─▶ collection (many threads)
```

A thread has two ids:

- **Thread id:** it never changes. Hand-set states, notes, published copies and agent references use it.
- **Snapshot id:** a hash of the members at one time. It changes when a member is added or removed.

## Rules

### 1. One goal

A thread has one goal. The steps that serve the goal stay in the thread: a CI fix on its PR, a rebase, a review round. When the goal itself changes, the work becomes a new thread that is forked from the old one.

### 2. Members and references

A member is work done for the goal. A reference only mentions the thread. The thread shows references, but references never decide which thread an item belongs to.

### 3. Who owns a member

| Member | Owners |
|---|---|
| Ticket, PR, feature branch | One thread at a time |
| Commit | The thread of its branch |
| Agent session | One or more threads. A session can work toward several goals, one after the other (rule 10). |
| Edited file | No owner. A file is evidence only (rule 4). |

When two threads claim one ticket, PR or branch, the claim becomes a split or a merge (rule 7). Two threads never share it silently.

A branch that several sessions wrote to goes to the session with the most of its surviving lines. On a tie, the later session holds it. The other sessions keep their own threads, and the branch's state is their state: a session that contributed is open while the branch is open, and never a thought. Each of these threads lists its kin: the sessions of the same work, oldest first, with what each did on the shared branch, PR or ticket (created, committed, pushed, uncommitted edits, or nothing). The holder of the branch takes the activity of its kin, so a new session on the branch keeps the branch's thread fresh.

A session writes to a branch when it commits or pushes on it, and also when lines it wrote are still in the uncommitted diff of a checkout of that branch.

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
| **End** | The goal is done (Closed) or left (Forgotten). A thread that has nothing left and no outside evidence is Closing for 7 days first. |
| **Revive** | New work on a Closed thread starts a new thread, with the same goal. A Forgotten thread opens again. A different goal starts a new thread. |

### 8. Threads cross repos and people

A ticket can have work in two repos. A teammate's PR joins a thread through its ticket or its branch, not through a folder on your machine.

### 9. Size

A thread is the smallest unit that you hand off or report in a standup. These items are not threads:

- A session that only asked a question and changed nothing. It is a reference.
- A long-lived branch.
- An epic or a project. It is a collection.
- A ticket with no work yet. It is a thread in the To do state: a goal with no members.

### 10. A goal in a session becomes a thread when data outside the session supports it

A long session can work toward several goals. Each goal that has its own PR, ticket or commit is a thread. A goal that only the session talks about is a line on the thread of the main goal ("Also in these sessions"). It becomes a thread when its first PR, ticket or commit appears.

The main goal is the newest goal that a session did work toward: it changed code, made a commit or PR, ran a job or made a decision. A session that only explained code or answered questions does not set the main goal. A step that serves a goal (a change it needs, a revert, a CI fix, a review round) is part of that goal (rule 1). Each PR, commit and ticket belongs to the goal that it was made for.

`bench/goals.py` measures this rule on labelled examples: is the main goal right, and do the PRs, tickets and commits land in the right goals?

## Starting a thread again

To start a thread again, start a new agent session from it (Fork session). Pensieve writes the thread to a file and opens the agent in the work's folder. The prompt tells the agent to read the thread, to get the live state of its branch, PRs and tickets, and to report before it changes anything. A cluster (the threads of one branch) forks a session that reads them all, or a session that does the steps left to wrap the branch up.

A to-do (a ticket of yours with no session, branch or PR) starts in a worktree: `<repo>/.claude/worktrees/<branch>`, on a branch named from the ticket's key and title, made from the repo's default branch. The agent starts there with the ticket's title, URL and description, and reads the ticket's page before it plans. The repo of a ticket project is asked once and kept in the `ticket_repos` setting.

## What threads read

Threads read the transcript folders of the agents, git and gh, and the tickets, PRs and messages that connections collected when there are any (`pensieve/transcripts.py`). They read no index and no model. A transcript is read once, and only its new lines after that; the facts persist on disk, so a restart reads nothing twice.

## Examples

- **One goal, three PRs.** A change merges into the wrong branch, a revert undoes it, and a new PR carries the same change. All three PRs are one thread, because the goal is the same.
- **A shared branch.** Two sessions work on a long-lived branch: one changes the logo, one measures search. The branch is not evidence (rule 5), so they are two threads.
- **A PR that another thread owns.** A thread about a model fix shows the merged PR of a different feature, which one of its sessions mentions. That PR is a reference on the model fix, and a member of its own thread (rules 2 and 3).
- **A fork.** A session starts on a bug and ends on a new feature. The members match, but the goal moved, so the feature is a new thread, forked from the bug.
