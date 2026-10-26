# Threads

A thread follows the work of one agent session on one branch, so you can see where the work stands, pick it up again, or close it out. This page defines a thread and its parts, the rules that put evidence into a thread, and the states of its life.

## Terms

| Term | Meaning |
|---|---|
| **Session** | One agent transcript. Pensieve reads it from the agent's transcript folder. |
| **Data source** | A place that a session writes to and that can be checked as done: a pull request (PR), a branch, a ticket. |
| **Thread** | One session's work on one branch (rule 2), with the data sources it wrote to there. A session that did no work on a branch but wrote a PR or a ticket is one thread. |
| **Thought** | A session with no data source: no PR, branch or ticket that it wrote to. |
| **To-do** | A ticket assigned to you that is not done, with no session, branch or PR on it yet. |
| **Cluster** | The threads of one branch, newest session first. Its title is its PR's title, then its ticket's, then the holder's session title. |
| **Holder** | The session of a cluster that holds the branch (rule 4). |

```
session ─▶ thread (its work on one branch) ─▶ cluster (the threads of one branch)
```

Each thread has an id that stays the same: of its session and its branch, or of its session alone when it has no branch. Hand-set states and the `thread` MCP tool use it. The id button on a card copies it.

## Rules

### 1. One session and one branch, one thread

A session has one thread for each branch that it did work on. A session that did work on two branches has a thread in each branch's cluster. A thread holds only what its own session wrote to on its branch. Several threads can hold the same data source.

### 2. What a session writes to

A session did work on a branch when lines that it wrote with its edit tools are uncommitted in a checkout of that branch, or are in the branch's commits that the remote's default branch does not have yet. It needs 3 of its lines there, or all of them when it wrote fewer. A commit or a push on the branch is work on it too. A session writes to a PR when it opens or merges the PR with `gh`. A PR goes in the thread of its head branch.

On a default branch (`main`), each checkout is its own branch. A session's thread there is open while its lines are uncommitted or not pushed. It closes when they reach the remote (landed) or leave the checkout (discarded). The main branch itself never closes: its cluster has only the threads that still have work there.

A session that wrote nothing but ran on a feature branch (a review) has a thread there. Its card has the tag "review" in place of a share of the branch's lines.

Git commands that only appear in quoted text or heredocs (test inputs, commit messages, PR bodies) are not writes.

### 3. Tickets

A thread joins a ticket by its key (`ABC-123`) in:

- the branch name, in any case (`abc-123-fix`, `feature/ABC_123`), for a ticket that Pensieve knows;
- the session title;
- commit subjects;
- PR titles.

### 4. Who holds a branch

When several sessions wrote to one branch, their threads are a cluster. Each session claims the lines that it wrote with its edit tools. A line counts when it is still on the branch's current version of the file. A line belongs to the latest session that wrote it.

The session with the largest share of the surviving lines holds the branch. On a tie, the later session holds it. Its card says "holds the branch · N% of the branch's lines". Its Resume session button takes the state color.

The other sessions show their share. A session that does not hold the branch takes its state from its own data sources. When it has none, it takes the state of the branch that it contributed to. It is never a thought.

A card that lost its branch links to the thread that holds it.

### 5. What is never evidence

- A long-lived branch other than the default branch (a release branch, a branch with sessions over more than a few days).
- A file that many threads edit.
- The repo or the folder alone.
- A ticket that a session mentions in passing.

### 6. States

A thread has one of six states, in this order:

| State | When |
|---|---|
| **Open** | A session is running, or the thread has work in the last 3 days and is not done. |
| **To-do** | A ticket assigned to you, not done, with no session, branch or PR on it yet. |
| **Thoughts** | A session with no data source, active in the last 2 days. |
| **Loose ends** | Work that is not done, with no activity in the last 3 days. |
| **Closed** | Every data source is verified done: the PR is merged or closed, the ticket is done, the branch is merged or deleted. |
| **Forgotten** | A thought quiet for 2 days, or a thread that you set aside by hand. Work with a data source that is not done stays a loose end. |

A closed thread does not open again: new work starts a new thread. You can set a state by hand. It holds until the thread has new activity. Agents never set states: they close threads in git, GitHub or the tracker.

### 7. Threads cross repos and people

A ticket can have work in two repos. A teammate's PR joins a thread through its ticket or its branch, not through a folder on your machine.

### 8. Size

A thread is the smallest unit that you hand off or report in a standup. These items are not threads:

- A session that only asked a question and changed nothing. It is a thought.
- A long-lived branch.
- An epic or a project.

## Ship and Discard

**Ship** and **Discard** open a modal with the steps left. A card has them, except a card inside a cluster: the cluster header has them.

- **Ship:** commit, push, open a PR, review and merge, mark the ticket done after the merge.
- **Discard:** drop uncommitted files, close the PR without merging, delete the branch (local and remote), mark the ticket cancelled.

The modal's button (Start shipping or Start discarding) starts an agent session in Terminal. The agent does the steps and asks before each step that leaves the Mac.

## Starting a thread again

To continue the session of a thread, use Resume session. Pensieve reads the session's folder from its transcript. A cluster header has **Resume**: it resumes the holder's session.

To start a new agent session from a thread, use Fork session. Pensieve writes the thread to a file and opens the agent in the work's folder. The prompt tells the agent to read the thread, to get the live state of its branch, PRs and tickets, and to report before it changes anything. It also names every session that changed the branch, so the agent can read their transcripts with `pensieve transcript <id>`. A cluster header has **Fork from all N**: it starts a session that reads all the threads of the branch.

A to-do (a ticket of yours with no session, branch or PR) has **Start session in worktree**. It makes `<repo>/.claude/worktrees/<branch>`, on a branch named from the ticket's key and title, from the repo's default branch. The agent starts there with the ticket's title, URL and description, and reads the ticket's page before it plans. The first to-do of a ticket project asks which repo to use. Pensieve keeps the answer in the `ticket_repos` setting. The `start_todo` MCP tool does the same for an agent.

## What threads read

Threads read the transcript folders of the agents, git and gh, and the tickets that connections collected when there are any (`pensieve/transcripts.py`). They read no index and no model. A transcript is read once, and only its new lines after that; the facts persist on disk, so a restart reads nothing twice.

A new turn in a session shows on its card at once: the card's time moves, and the card is running. A new session gets a thought card at once. The next build replaces these cards with whole ones, also while a full build runs (`cardbuild.Builder.hot`).

A Slack message that a connection collected shows on each thread whose ticket key, PR link or branch name it holds (`pensieve/chat_joins.py`). A message never joins two threads into one. A direct message or a private channel shows only on this Mac.

## Examples

- **One session, two repos.** A session works on a feature branch in one repo, then changes the header of another repo on `main` without a commit. It has two threads: one in the feature branch's cluster, and one in that checkout's `main` cluster. The `main` thread closes when its changes are pushed or discarded.
- **A shared branch.** Two sessions work on a long-lived branch: one changes the logo, one measures search. The branch is not evidence (rule 5), so they are two threads, and not a cluster.
- **A feature branch with two sessions.** One session writes most of a feature, and a later session fixes a test on the same branch. They are a cluster. The first session holds the branch, because most of the surviving lines are its lines (rule 4).
- **A question.** A session explains how a module works and changes nothing. It is a thought. After 2 quiet days it is forgotten.
