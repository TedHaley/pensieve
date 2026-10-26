from pages_a import page, pre, table


def tools(rows):
    return table(["Tool", "Arguments", "Returns"], [[f"<code>{n}</code>", a, r] for n, a, r in rows])


DEFAULT = [
    ("search", "<code>query</code>, <code>limit</code> = 8, <code>kind</code>",
     "One line per hit, best first: <code>path:line</code>, the enclosing definition, the line that matched, and an id for <code>read</code> and <code>similar</code>. The top hits also show a few numbered lines. Query syntax: <code>\"exact\"</code>, <code>-word</code>, <code>ext:py</code>, <code>in:&lt;path&gt;</code>, <code>by:&lt;person&gt;</code>, <code>kind:code|doc|session|folder|repo|person</code>, <code>since:3d</code>, <code>until:2026-10-01</code>, <code>project:&lt;name&gt;</code> (sessions)."),
    ("read", "<code>id</code> (one id, or up to 8 ids separated by spaces), <code>max_chars</code> = 12000",
     "The full text of a result. For code, also the repo, branch, lines, owners from git blame, recent commits and the agent sessions that touched it."),
    ("similar", "<code>id</code>, <code>limit</code> = 8",
     "Items that are similar in meaning to a result, in the same line format as <code>search</code>. Use it before you write new code."),
    ("people", "<code>topic</code>, <code>repo</code>, <code>name</code>",
     "With <code>name</code>: one person, with their role, repos, recent PRs and how you work with them. With <code>topic</code>: who wrote the code most related to the topic (git blame), and the matching code. With only <code>repo</code>: the people of the repo, with their commits, main areas and recent commit subjects."),
    ("repo", "<code>repo</code> (default: the repo of the scope), <code>part</code> = <code>overview</code> | <code>gaps</code> | <code>unexplored</code>",
     "<code>overview</code>: where things are in a repo. That is the size, languages, top folders two levels deep, the start of the README, dependencies, the largest files, and topics with their files and authors. <code>gaps</code> (with <code>inactive_days</code> = 180): areas mostly written by people with no recent commit, the active person who knows each one best (<code>ask</code>) and the bus factor. <code>unexplored</code>: areas next to your own work that your agent sessions did not touch, and who owns them."),
    ("projects", "<code>query</code>, <code>limit</code> = 8, <code>project</code>",
     "Your projects: tickets with their branches and PRs, grouped into projects, with the people who work on them."),
    ("list_repos", "none", "The indexed repos in the scope, with their size and the number of agent sessions in each."),
    ("current_scope", "none", "The part of the index that this connection searches."),
    ("open", "<code>id</code>, <code>action</code> = <code>open</code> | <code>reveal</code> | <code>visualize</code>",
     "Opens an item on your computer: code in your editor at the line, the item in Finder, or the item on the Map."),
    ("index_path", "<code>path</code>",
     "Indexes a folder when you ask the agent to: a git repo, a folder of repos, a folder of agent sessions, or documents. Pensieve finds out which kind it is."),
    ("stale", "<code>days</code> = 14",
     "What looks abandoned: worktrees with no commit and no session, merged local branches, and repos with no activity for 3 months. Each item has a verdict and, where it is safe, a command. The agent asks you before it runs a command."),
    ("threads", "<code>state</code> = <code>open,todo,loose_ends</code>, <code>query</code>, <code>limit</code> = 10",
     "Your threads of the last 30 days that are not done, the closest to done first: the fewest steps left. Each thread shows its steps left. Each step says who can do it (you, your agent or the tracker) and the PR, branch or ticket that it acts on. <code>state</code> takes a list of states (<code>open</code>, <code>todo</code>, <code>thoughts</code>, <code>loose_ends</code>, <code>closed</code>, <code>forgotten</code>), or <code>all</code>. <code>query</code> takes words, a ticket key, a branch or a PR number."),
    ("thread", "<code>id</code>, <code>full</code> = false",
     "One thread whole: its state and why, folder, branch, uncommitted files, PRs, tickets, the sessions of the same work, the steps left, where its session stopped, its main files and the command that resumes it. <code>id</code> is a thread id (<code>th_…</code>) from <code>threads</code> or <code>cluster</code>. <code>full</code> adds every file and the timeline."),
    ("cluster", "<code>branch</code>",
     "The threads of one branch, newest first: what each session did on the branch, its share of the branch's lines, and each thread's state. It also gives two lists of steps, each with who can do each step. Ship it: commit, push, open and merge the PR, close the ticket. Discard it: drop the changes, close the PR, delete the branch, cancel the ticket."),
    ("thread_page", "<code>id</code> or <code>branch</code>",
     "Saves the page of one thread, or of every thread of one branch, as one HTML file in <code>~/Downloads</code>. The page also holds the threads as JSON, so an agent can read it as data. Returns the path of the file and the thread ids. Pensieve does not send the file. You approve what your agent sends."),
    ("start_todo", "<code>id</code>, <code>repo</code>",
     "Starts a to-do: makes a worktree from the default branch of the repo, on a branch named from the ticket key. Returns the path, the branch and the prompt for the agent. With no <code>repo</code>, Pensieve uses the repo that it keeps for the ticket's project. If it has none, it returns the repos to choose from."),
    ("report_feedback", "<code>kind</code> = <code>bug</code> | <code>idea</code>, <code>summary</code>, <code>details</code>",
     "Saves a report on your computer. You read it in Settings → Feedback before you send it."),
]

SETUP = [
    ("setup_guide", "none", "What Pensieve is, the two modes, the source categories and the folders that each scan reads, the search models and the energy note. It reads no folder. The agent then asks you each question in order. The last question is whether you track work in Jira, Linear or Asana: the sign-in is in the app, in Settings → Sources → Cloud connectors."),
    ("setup_scan", "<code>profiles</code> or <code>categories</code>", "What can be indexed in those categories, with the size of each item and the recommended items. macOS can ask you for folder access."),
    ("setup_add_source", "<code>kind</code> = <code>files</code> | <code>repos</code> | <code>agents</code>, <code>path</code>", "The id of a source that the scan did not find."),
    ("setup_start", "<code>sources</code> or <code>recommended</code>, <code>content</code>, <code>mode</code> = <code>session</code> | <code>developer</code>", "Finishes setup. <code>session</code> is Threads mode: the agent sources only, with no index and no model. <code>developer</code> also indexes the repos and files. With no <code>mode</code>, it is <code>developer</code> when you chose a repo. In Developer mode, indexing starts at once. Returns the time left, the energy note and how to search."),
]

WORK = [
    ("pending_work", "<code>limit</code> = 5, <code>kind</code>", "Jobs that wait for an answer: session summaries, topic names, insights, team summaries and collection plans. Each job holds its instructions and its data."),
    ("submit_work", "<code>id</code>, <code>answer</code>", "Saves the answer to one job. Pensieve uses it in its next pass."),
]

ALL = [
    ("show_visualizer", "<code>view</code>", "Opens the Map window."),
    ("status", "none", "The size of the index and whether indexing runs now."),
    ("list_scopes", "none", "The named scopes and the current scope."),
    ("save_scope", "<code>name</code>, <code>sources</code>, <code>description</code>", "Makes or replaces a named scope."),
    ("delete_scope", "<code>name</code>", "Removes a named scope."),
    ("get_settings", "none", "The settings, with a description of each."),
    ("update_settings", "<code>changes</code>", "Changes settings, for example <code>{\"editor\": \"cursor\"}</code>."),
    ("list_sources", "none", "The sources by category, with their on and off state and item counts."),
    ("set_source", "<code>id</code>, <code>enabled</code>, <code>map</code>", "Switches a category or one item on or off, or on or off the Map."),
    ("file_types", "none", "For each file type: whether Pensieve reads its content, whether it shows on the Map, and how many files it has."),
    ("add_folder", "<code>path</code>", "Indexes another folder."),
    ("remove_folder", "<code>path</code>", "Stops indexing a folder."),
    ("reindex", "none", "Scans folders and repos now."),
    ("usage_stats", "<code>days</code> = 7", "Pensieve's own MCP calls: for each tool, the number of calls, errors and the p50 and p95 time."),
    ("set_thread_state", "<code>id</code>, <code>state</code>, <code>reason</code>",
     "Sets a thread's state by hand, as you can on the Threads tab: <code>open</code>, <code>todo</code>, <code>loose_ends</code>, <code>closed</code>, <code>forgotten</code>, or <code>auto</code>. <code>thoughts</code> is always automatic. An agent does not need it to close a thread: Pensieve sets each state from the evidence. Use it only when you ask for it."),
]

page("mcp-tools", "MCP tools",
     "Pensieve MCP tools reference: search, read, similar, people, threads and more",
     "Reference for every tool of the Pensieve MCP server: search, read, similar, people, repo, threads, thread, "
     "cluster, start_todo and the setup tools, with arguments and return values.",
     "MCP tools reference",
     "This page lists each tool of the Pensieve MCP server, its arguments and what it returns.",
     f"""
<p>An MCP client sends the description of each tool to the model on every turn. Thus Pensieve shows a small set by
default. Other sets appear only when they are necessary. Every tool obeys the <a href="/docs/agents/#scope">scope</a>
of the connection.</p>
{table(["Set", "When the agent sees it", "Tools"], [
    ["Default", "Always", str(len(DEFAULT))],
    ["Setup", "Until setup is done", str(len(SETUP))],
    ["Work queue", "With <code>pensieve mcp --all-tools</code> or <code>?tools=all</code>", str(len(WORK))],
    ["Settings", "With <code>pensieve mcp --all-tools</code> or <code>?tools=all</code>", str(len(ALL))],
], num=(2,))}

<h2 id="default">Default tools</h2>
{tools(DEFAULT)}

<h3 id="search-output">What a search returns</h3>
<p>The result is short on purpose. The agent reads it again on every later turn. A result looks like this:</p>
{pre('''src/billing/retry.py:42  def schedule_retry(invoice)  ▸ if decline.hard: return None  [code:1832]
    41│ def schedule_retry(invoice):
    42│     if decline.hard: return None
docs/payments.md  Card retries stop after a hard decline …  [file:77]
session "fix card retries" (2026-10-12)  … we skip the queue for hard declines …  [session:4f1c…]''')}
<p>Pensieve adds a note to some hits:</p>
<ul>
  <li><strong>(edited since indexed):</strong> the file changed after Pensieve indexed it, and Pensieve did not index it again before the search.</li>
  <li><strong>(near-copy of …: read both to tell them apart):</strong> another hit has almost the same code.</li>
  <li><strong>(main checkout; differs in your worktree):</strong> the agent works in a linked worktree, and its copy of the file is different.</li>
</ul>
<p>To see what an agent gets, run the same search from a terminal in the repo:</p>
{pre('pensieve search "retry failed webhook delivery" --kind code')}

<h2 id="setup">Setup tools</h2>
<p>Until you finish setup, Pensieve indexes nothing. Every connection then also gets these tools, so the agent that
installed Pensieve can do setup with you in its chat.</p>
{tools(SETUP)}

<h2 id="work">Work queue tools</h2>
<p>Pensieve runs no text model of its own by default. With these two tools, an agent answers Pensieve's text jobs.
They are only in the <code>?tools=all</code> set. In Settings → Models → Summaries and insights, you can choose an engine
that answers the jobs in the background: the built-in model, a command-line agent or a model server that you run.</p>
{tools(WORK)}

<h2 id="settings">Settings tools</h2>
<p>These tools change Pensieve itself. An agent gets them only with <code>pensieve mcp --all-tools</code>, or with
<code>?tools=all</code> over HTTP.</p>
{tools(ALL)}

<h2 id="errors">Errors</h2>
<p>A bad argument, an unknown id or an item outside the scope comes back to the agent as a readable error. Pensieve
logs each call on your computer, in <code>usage.db</code>. Settings → Usage shows the log.</p>
""")

page("threads", "Threads",
     "Threads: one record per agent session and branch for you and your AI agents | Pensieve",
     "Pensieve threads join each agent session with its branch, PRs and tickets. Six states, clusters by branch, "
     "to-dos from your issue tracker, and Ship and Discard to close the work.",
     "Threads",
     "This page tells you how Pensieve makes threads, how it sets the state, and how you close the work.",
     f"""
<h2 id="what-a-thread-holds">What a thread is</h2>
<p>Pensieve makes one thread for each agent session and each branch that the session did work on. Work on a branch
is lines that the session wrote and that are uncommitted there, or that are in commits that the remote's default
branch does not have yet. A commit, a push or a PR is work too. A session that did work on two branches has two
threads. On <code>main</code>, a session's thread is open while its lines are uncommitted or not pushed.</p>
<p>A session that wrote nothing but ran on a feature branch is a review. Its card is on that branch, with the tag
“review”.</p>
<p>Pensieve reads the sessions from your agents' transcript folders, a little at a time, and keeps the facts on disk.
A new message in a session shows on its card at once. Threads need no index and no model, so they work in Threads
mode and in Developer mode.</p>
<p>A ticket that is assigned to you is also a thread: a to-do. It stays a to-do until a session, a branch or a PR
picks it up (see <a href="#todo">To-do threads</a>).</p>
<p>Pensieve leaves out sessions in temporary folders and under <code>~/Library</code>. Apps and scripts start those.</p>

<h3 id="card">The thread card</h3>
<p>Each card shows the title, the state, the time of the last activity, and where the work is: the repo, the branch
and the uncommitted files. Click the card to open it, and click it again to close it. It has these buttons:</p>
{table(["Button", "What it does"], [
    ["Resume session", "Opens Terminal in the session's folder and resumes the session in the agent that wrote it. The copy button copies the command. It also works in Threads mode."],
    ["Fork session", "Opens Terminal and starts a new agent session that reads the thread first. The copy button copies a prompt with the thread in it, for any agent. To change the agent, go to Settings → Agents."],
    ["Ship, Discard", 'Open a modal with the steps left (see <a href="#ship-discard">Ship and Discard</a>). A card in a cluster does not show them, because the cluster header has them.'],
    ["Thread id", "Copies the thread id, for the <code>thread</code> MCP tool."],
    ["Preview (eye)", "Shows the thread as a page."],
])}
<p>An open card has these parts:</p>
<ul>
  <li><strong>Conversation snippet:</strong> the last words of the session. For a to-do, the ticket description.</li>
  <li><strong>Related sessions:</strong> one list, newest first. It has the sessions on the same branch, PR or ticket, and the sessions that edited the same files (“≈ similar context”).</li>
  <li><strong>Data sources in session:</strong> the PRs, tickets and branches.</li>
  <li><strong>Slack:</strong> the Slack messages that name the thread's ticket key, PR link or branch name, up to 10, newest first. Each one says which of them it names. A direct message has the tag “direct” (see <a href="/docs/connections/#slack">Slack</a>).</li>
  <li><strong>Files:</strong> up to 20, collapsed by default. First what the session made for people: a Notion page
  it wrote, a Slack message it sent, a published Artifact. Then what it used as context: a Slack thread, an email or
  a page that the agent read, a link in the conversation, a local document that it read. Code files that it edited are
  not on the list: the branch and its PRs hold them.</li>
  <li><strong>Activity.</strong></li>
</ul>
<p>A card that lost its branch to another thread links to the thread that holds the branch.</p>

<h2 id="how-threads-join">How Pensieve joins tickets to threads</h2>
<p>A thread gets a ticket when the ticket key is in one of these places:</p>
<ul>
  <li>The branch name. The key can be in any case, for a ticket that Pensieve knows: <code>abc-123-fix</code> and <code>feature/ABC_123</code> both match <code>ABC-123</code>.</li>
  <li>The session title.</li>
  <li>A commit subject.</li>
  <li>A PR title.</li>
</ul>
<p>A git command that is only in quoted text or in a heredoc does not count as a branch write. Examples are test
inputs, commit messages and PR bodies.</p>

<h2 id="clusters">Clusters: the threads of one branch</h2>
<p>The threads of one branch sit together in a cluster, newest session first, joined by a tree line.</p>
<p>One session holds the branch: the session with the largest share of the branch's surviving lines. Surviving lines
are the lines that the session wrote and that are still on the branch. On a tie, the later session holds the
branch. Its card says “holds the branch · N% of the branch's lines”, and its Resume session button takes the state
color. The other cards show their share.</p>
<p>A session that does not hold the branch gets its state from its own data sources. If it has none, it gets the
state of the branch that it wrote to. Such a session is never a thought.</p>
<p>The cluster's title is its PR's title. If it has no PR, the title is its ticket's title. If it has neither, the
title is the title of the session that holds the branch. Each card in the cluster keeps its own title.</p>
<p>The cluster header shows the repo, the branch and the uncommitted files. It has these buttons: Resume, Ship,
Discard, Fork from all N, a copy button for the prompt, and a preview. Resume resumes the session that holds the
branch. Fork from all N starts a new agent session that reads all the threads of the branch first. Its prompt names
each session that changed the branch, so that the agent can read their transcripts.</p>

<h2 id="states">The six states</h2>
<p>Pensieve sets each state from the thread's data sources: its PRs, branches and tickets. Material work is edits,
commits, a branch or a PR. By default, the Threads tab shows Open, To-do, Thoughts and Loose ends.</p>
{table(["State", "Meaning", "Set automatically when"], [
    ['<span class="state" style="--sc:var(--s-open)">Open</span>', "You work on it now.", "A session runs now. Or the thread has material work and activity in the last 3 days, and it is not done."],
    ['<span class="state" style="--sc:var(--s-todo)">To-do</span>', "A ticket of yours that nothing picked up.", "A ticket that is assigned to you and is not done, with no session, branch or PR on it yet. It becomes Open when work starts."],
    ['<span class="state" style="--sc:var(--s-thoughts)">Thoughts</span>', "A session with no data source: no PR, branch or ticket that it wrote to.", "A session with no data source, with activity in the last 2 days."],
    ['<span class="state" style="--sc:var(--s-loose-ends)">Loose ends</span>', "Work that is not done and is quiet. Close it out.", "Work that is not done (an open PR, a ticket that is not done, uncommitted files, or work left), with no activity in the last 3 days. Work with a data source stays a loose end, also after a long time."],
    ['<span class="state" style="--sc:var(--s-complete)">Closed</span>', "Done. A closed thread does not open again: new work starts a new thread.", "Each data source is verified done: a PR is merged or closed, a ticket is done, a branch is merged or deleted."],
    ['<span class="state" style="--sc:var(--s-forgotten)">Forgotten</span>', "Not finished, and it will probably stay that way.", "A thought with no activity for 2 days. Or a thread that you set aside by hand."],
])}
<p>You can set a state by hand on the thread. A state set by hand stays until the thread has new activity. Then the
automatic state applies again. Agents never set states. An agent closes a thread in git, GitHub or the tracker,
and Pensieve sees the evidence.</p>

<h2 id="todo">To-do threads</h2>
<p>When you connect your issue tracker (see <a href="/docs/connections/#trackers">Connections</a>), the tickets that
are assigned to you become to-do threads. A to-do card has these buttons: Start session in worktree and Discard.</p>
<p><strong>Start session in worktree</strong> does these steps:</p>
<ol>
  <li>It makes a worktree in <code>&lt;repo&gt;/.claude/worktrees/&lt;branch&gt;</code>, from the default branch. The branch name comes from the ticket key and title.</li>
  <li>It starts your agent in the worktree, with the ticket's title, URL and description.</li>
</ol>
<p>For the first to-do of a ticket project, Pensieve asks which repo to use. It keeps the answer in the
<code>ticket_repos</code> setting. Agents start a to-do with the <code>start_todo</code> MCP tool.</p>

<h2 id="ship-discard">Ship and Discard</h2>
<p>Ship and Discard close the work. Each button opens a modal with the steps left. The buttons show on threads in
these states: Open, To-do, Loose ends and Forgotten.</p>
{table(["Button", "Steps"], [
    ["Ship", "Commit, push, open a PR, review and merge. After the merge, mark the ticket done."],
    ["Discard", "Drop the uncommitted files, close the PR without a merge, delete the branch (local and remote), and mark the ticket cancelled."],
])}
<p>Click <strong>Start shipping</strong> or <strong>Start discarding</strong> in the modal. An agent session starts
in Terminal and does the steps. The agent asks you before each step that leaves your Mac. Pensieve itself only
reads: it sees the result in git, GitHub or the tracker. The button resumes the session that holds the branch, with
the steps as its next message, so the request is in the conversation of the work that it closes. If that session
can't take a message on resume, a new session does the steps. Thoughts and closed threads have no Ship or Discard.</p>

<h2 id="search">Search threads</h2>
<p>The search box on the Threads tab finds threads by the words on the page. Put words in quotes
(<code>"exact words"</code>) to find only the threads that contain them. Put a minus before a word
(<code>-word</code>) to leave out the threads with that word. Threads have no search by meaning.</p>

<h2 id="cli">Threads from the command line</h2>
{pre('''pensieve thread <thread id> -o thread.html    # the thread as one HTML page
pensieve thread <thread id> --json            # the thread as JSON
pensieve thread <thread id> --agent           # the thread as text for an agent''')}
<p>A thread follows the <a href="https://github.com/TedHaley/pensieve/blob/main/pensieve/thread.schema.json">pensieve-thread/2 JSON schema</a>.
Pensieve also reads files in the older context-card/1 format. The <code>threads</code> MCP tool gives the thread ids.</p>

<h2 id="agents">Threads for your agents</h2>
<p>Agents get the same threads over MCP. <code>threads</code> lists the threads that are not done, the closest to
done first, with their steps left. <code>thread</code> gives one thread whole. <code>cluster</code> gives the threads
of one branch, with the steps to ship or discard the work. <code>thread_page</code> saves the page of a thread or a
branch as one HTML file in <code>~/Downloads</code>, with the threads as JSON inside. <code>start_todo</code> starts a
to-do. At the start of a session and at natural pauses, the agent offers to close the thread closest to done (the
<code>pensieve-wrap-up</code> skill). With the Claude Code prompt hook, a prompt such as “where did I leave off on
ABC-12” gets the thread before the agent starts.</p>
""")

page("connections", "Connections",
     "Pensieve connections: collect Jira, Linear and Asana tickets and Slack messages, read-only",
     "Pensieve connections sign in to Jira, Linear and Asana, read Slack with your own read-only app, and collect "
     "tickets, messages and pages from MCP servers with read-only tools. Your tickets become to-do threads, and a "
     "ticket that is done closes its thread.",
     "Connections",
     "This page tells you how Pensieve collects tickets, messages and pages from your MCP servers, and how it keeps the collection read-only.",
     f"""
<p>A connection is an MCP server that Pensieve collects data from. Pensieve keeps each item on your Mac and indexes
it like a document. Each item keeps a list of its changes over time. Collected tickets also join your
<a href="/docs/threads/">threads</a>.</p>
<div class="note"><p>A server that you add or sign in to is on at once. You can switch each connection on or off in
Settings → Sources.</p></div>

<h2 id="trackers">Issue trackers: Jira, Linear and Asana</h2>
<p>Connect your issue tracker at setup, or later in Settings → Sources → Cloud connectors. Click
<strong>Sign in</strong>. The sign-in opens in your browser. Pensieve connects to the tracker's own MCP server and
calls only its read-only tools.</p>
{table(["Tracker", "Server", "Sign-in"], [
    ["Jira", "Atlassian's MCP server (<code>https://mcp.atlassian.com/v1/mcp</code>)", "OAuth"],
    ["Linear", "Linear's MCP server", "OAuth or an API key"],
    ["Asana", "Asana's MCP server", "OAuth"],
])}
<p>The tickets that are assigned to you become <a href="/docs/threads/#todo">to-do threads</a>. A ticket that is done
closes its thread.</p>
<ul>
  <li><strong>Jira:</strong> each collection reads the issues that are assigned to you and that changed after the last collection. It also reads again the status of each ticket on a thread that is not closed. Thus a ticket that you close in Jira closes its thread.</li>
  <li><strong>Asana:</strong> each collection reads your workspaces, then the tasks that are assigned to you, then the notes of each task. An Asana task has no short key, so Pensieve gives it the key <code>ASANA-&lt;the last 6 digits of its id&gt;</code>. Its status is Done, or the name of its section.</li>
</ul>
<p>Pensieve does not connect to the connectors inside Claude. To get your tickets, sign in to the tracker's own MCP
server.</p>

<h2 id="slack">Slack</h2>
<p>Slack's own MCP server has no open sign-up for apps. So you make a small Slack app that can only read, and you give
Pensieve its token:</p>
<ol>
  <li>In Settings → Sources → Cloud connectors → Slack, click <strong>Add</strong>, then <strong>Create the app</strong>. Slack opens with the app's read-only scopes filled in.</li>
  <li>Pick your workspace, then click <strong>Create</strong>.</li>
  <li>Under <strong>OAuth &amp; Permissions</strong>, click <strong>Install to workspace</strong>.</li>
  <li>Copy the <strong>User OAuth Token</strong> (<code>xoxp-…</code>), paste it into Pensieve, and click <strong>Connect</strong>.</li>
</ol>
<p>Some workspaces need an admin to approve the app. Pensieve keeps the token in the macOS Keychain, not in a file.
<strong>Remove token</strong> deletes it from the Keychain.</p>
<p>Each collection reads the messages that you sent, the messages sent to you, and the messages that mention you. It
also reads the whole thread of each recent message in a channel, but never of a direct message. Pensieve calls only
Slack's read methods: <code>auth.test</code>, <code>search.messages</code>, <code>conversations.replies</code> and
<code>users.info</code>.</p>
<p>A message shows on each <a href="/docs/threads/">thread</a> whose ticket key, PR link or branch name it holds. A
branch name counts when it has at least 6 characters and a dash, a slash, an underscore or a digit. A message never
joins two threads into one. In Developer mode, a person's card in People lists the Slack threads that you share, and
the threads of your work that each one names.</p>

<h2 id="kinds">Where the servers come from</h2>
<p>Pensieve finds your MCP servers by itself. It never starts an agent of its own:</p>
<ul>
  <li><strong>Your agents' servers.</strong> Pensieve reads the MCP settings of Claude Code, Codex, Cursor, Gemini CLI, Qwen Code, Windsurf, Claude Desktop and VS Code. It connects to those servers itself, with the credentials in those settings.</li>
  <li><strong>Your own servers.</strong> Add a server in Sources. Give a URL with headers, or a command with environment variables. Pensieve does not show the header or environment values again. They stay in <code>~/.pensieve/settings.json</code>, which only you can read.</li>
  <li><strong>Your agent sessions.</strong> The results that your agents already got from MCP tools. Pensieve fetches nothing new for this source.</li>
</ul>
<p>If Pensieve has no recipe for a server, your agent chooses the search calls as a pending job, and Pensieve makes them.</p>

<h2 id="read-only">How Pensieve keeps it read-only</h2>
<p>Pensieve calls a tool only if the tool is read-only. A tool is read-only when these conditions are true:</p>
<ol>
  <li>Its name has a read verb, such as get, list, search, read or fetch.</li>
  <li>Its name has no write verb, such as create, update, send or delete. The list has almost 100 write verbs.</li>
  <li>The server does not mark it <code>readOnlyHint: false</code> or <code>destructiveHint: true</code>.</li>
</ol>
<p>A server's hints can only make the rule stricter. Pensieve never calls a write tool.</p>

<h2 id="private">Private messages</h2>
<p>Pensieve marks an item as private when it comes from a direct message, a group message or a private channel. It
also marks every item from a tool that can read those, such as a tool with “private” or “dm” in its name. Private
items are context for your own threads and agents. They stay on your Mac: a thread that you share leaves them
out.</p>

<h2 id="schedule">When Pensieve collects</h2>
<p>A sign-in that ends connected starts a collection at once. After that, Pensieve collects every 6 hours. While a
connection collects, the status in the top-right menu shows “Collecting from &lt;name&gt;…”.</p>
{table(["Setting", "Default", "What it does"], [
    ["<code>connections_every_hours</code>", "6", "How often Pensieve collects from each connection that is on. 0 collects only when you ask."],
    ["<code>connections_days</code>", "30", "How far back the first collection goes."],
], num=(1,))}
<p><strong>Collect now</strong> in Sources collects at once.</p>
<p>See <a href="/docs/privacy/#network">Privacy and data</a> for what goes over the network when Pensieve collects.</p>
""")
