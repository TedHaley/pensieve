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
    ("threads", "<code>state</code> = <code>open,loose_ends</code>, <code>query</code>, <code>limit</code> = 10",
     "Your threads that are not done, the closest to done first: the fewest steps left. Each thread shows its steps left. Each step says who can do it (you, your agent or the tracker) and the PR, branch or ticket that it acts on. <code>state</code> takes a list of states, or <code>all</code>. <code>query</code> takes words, a ticket key, a branch or a PR number."),
    ("thread", "<code>id</code>, <code>full</code> = false, <code>version</code>",
     "One thread now: its state and why, folder, branch, uncommitted files, PRs, tickets, what changed since the last session, the steps left, where that session stopped, its main files and the command that resumes it. <code>id</code> is a thread id (<code>th_…</code>) or an id from <code>threads</code>. <code>full</code> adds every file and the timeline. <code>version</code> = n gives that saved version (0 is the first, -1 the last). <code>version</code> = <code>list</code> lists the saved versions."),
    ("share_thread", "<code>id</code>, <code>ask</code>",
     "Makes a thread that is fit to send and saves it as one HTML page in <code>~/Downloads</code>. Returns the path of the file and the thread. The agent attaches the file. You approve what it sends."),
    ("report_feedback", "<code>kind</code> = <code>bug</code> | <code>idea</code>, <code>summary</code>, <code>details</code>",
     "Saves a report on your computer. You read it in Settings → Feedback before you send it."),
]

SETUP = [
    ("setup_guide", "none", "What Pensieve is, the user profiles, the source categories and the folders that each scan reads, the search models and the energy note. It reads no folder. The agent then asks you each question in order."),
    ("setup_scan", "<code>profiles</code> or <code>categories</code>", "What can be indexed in those categories, with the size of each item and the recommended items. macOS can ask you for folder access."),
    ("setup_add_source", "<code>kind</code> = <code>files</code> | <code>repos</code> | <code>agents</code>, <code>path</code>", "The id of a source that the scan did not find."),
    ("setup_start", "<code>sources</code> or <code>recommended</code>, <code>content</code>", "Starts indexing. Returns the time left, the energy note and how to search."),
]

WORK = [
    ("pending_work", "<code>limit</code> = 5, <code>kind</code>", "Jobs that wait for your agent: thread notes, session summaries, topic names, insights and collection plans. Each job holds its instructions and its data."),
    ("submit_work", "<code>id</code>, <code>answer</code>", "Saves the answer to one job. Pensieve uses it in its next pass."),
]

ALL = [
    ("show_search", "<code>query</code>", "Opens the search panel with the query."),
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
     "Sets a thread's state by hand, as the button on the thread does: <code>todo</code>, <code>open</code>, <code>loose_ends</code>, <code>complete</code>, <code>forgotten</code>, or <code>auto</code>. An agent does not need it to close a thread: Pensieve sets each state from the evidence."),
]

page("mcp-tools", "MCP tools",
     "Pensieve MCP tools reference: search, read, similar, people, threads and more",
     "Reference for every tool of the Pensieve MCP server: search, read, similar, people, repo, threads, thread, "
     "share_thread and the setup tools, with arguments and return values.",
     "MCP tools reference",
     "This page lists each tool of the Pensieve MCP server, its arguments and what it returns.",
     f"""
<p>An MCP client sends the description of each tool to the model on every turn. Thus Pensieve shows a small set by
default. Other sets appear only when they are necessary. Every tool obeys the <a href="/docs/agents/#scope">scope</a>
of the connection.</p>
{table(["Set", "When the agent sees it", "Tools"], [
    ["Default", "Always", str(len(DEFAULT))],
    ["Setup", "Until setup is done", str(len(SETUP))],
    ["Work queue", "Always", str(len(WORK))],
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
<p>Pensieve has no text model of its own. It keeps the jobs for your agent in a queue, and nothing runs in the
background. Ask the agent to “do Pensieve's pending work”.</p>
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
     "Threads: one record per goal for you and your AI agents | Pensieve",
     "Pensieve threads join agent sessions, branches, PRs, tickets and messages into one record per goal, with "
     "five states. Share a thread as one HTML file that people and agents can read.",
     "Threads",
     "This page tells you how Pensieve makes a thread for each goal, how it sets the state, and how you share a thread.",
     f"""
<h2 id="what-a-thread-holds">What a thread holds</h2>
<p>Every thread has the same core: the <strong>problem</strong>, the <strong>goal</strong>, the <strong>state</strong>
and what is <strong>next</strong>. It also holds the question that waits on you. The rest of the thread is its
resources, the evidence behind the core:</p>
<ul>
  <li>The folders where the work is: the repo, the branch, the worktree, uncommitted files, and how far the branch is ahead or behind.</li>
  <li>The PRs, with their state, CI checks and review.</li>
  <li>The tickets, with their status and assignee.</li>
  <li>The agent sessions, and the command that resumes the last one.</li>
  <li>When you ask, coworkers' recent commits in the same folders, and related tickets and threads.</li>
</ul>
<p>A short note says what is done and what is left. Your agent writes the note when it does Pensieve's pending
work. Until then, the thread has its evidence but no note.</p>

<h2 id="how-threads-join">How Pensieve joins a thread</h2>
<p>Pensieve joins sessions, branches, commits and connection items into one thread by these links:</p>
<ul>
  <li>A ticket key such as <code>ABC-123</code> in a branch name, a commit subject, a session or a ticket.</li>
  <li>The git branch that a session ran on. This applies only to feature branches. A default branch holds many threads.</li>
  <li>For work with no ticket: sessions in the same repo, at most 3 days apart, that edited the same files.</li>
  <li>Session titles that mostly match, within 3 days.</li>
</ul>
<p>Pensieve leaves out sessions in temporary folders and under <code>~/Library</code>. Apps and scripts start those.</p>

<h2 id="states">The five states</h2>
<p>Pensieve sets each state from the thread's evidence. Material work is edits, commits, a branch or a PR.</p>
{table(["State", "Meaning", "Set automatically when"], [
    ['<span class="state" style="--sc:var(--s-todo)">To do</span>', "A ticket that you did not start.", "A ticket that is not done, or with an unknown status, and no edits, commits, branch or PR."],
    ['<span class="state" style="--sc:var(--s-open)">Open</span>', "You work on it now.", "A session runs now. Or the thread has material work and activity in the last 3 days, and it is not done."],
    ['<span class="state" style="--sc:var(--s-loose-ends)">Loose ends</span>', "It is not done and it is quiet. Go back to it.", "It has material work that is not done, with no activity in the last 3 days."],
    ['<span class="state" style="--sc:var(--s-complete)">Complete</span>', "Done.", "A PR is merged, nothing happened after the merge, and no PR is open. Or its own PR is merged and no PR is open. Or its PRs are merged or closed, or its ticket is done, or its work is in the default branch, and nothing is uncommitted. Its work is in the default branch when its branch is merged or squash-merged, or when its commits are there and its note says that nothing is left."],
    ['<span class="state" style="--sc:var(--s-forgotten)">Forgotten</span>', "Not finished, and it will probably stay that way.", "Material work that is not done, quiet for 21 days, with no open PR and no uncommitted work. Or a conversation with no ticket and no edits, after its session ended."],
])}
<p>You can set a state on the thread. An agent closes a thread in git, GitHub or the tracker, and Pensieve sees the
evidence. It does not set the state. A state set by hand stays until the thread has new activity. Then the
automatic state applies again. Pensieve keeps each change by hand, so a thread has a history of its states.</p>

<h2 id="versions">Saved versions</h2>
<p>A thread follows one goal over time. Pensieve saves a version of a thread when it changes, for example when a PR
opens, a commit lands or the next step changes. The version chip on a thread opens its saved versions.
“New session from thread” starts an agent that reads the thread first. Agents get the same data from
<code>thread</code> with <code>version</code>.</p>

<h2 id="share">Share a thread as HTML</h2>
<p>The Share button on a thread opens a preview. Pensieve reads the local resources whole and writes a summary of
each one. Then you can do these steps:</p>
<ol>
  <li>Write what you need from the reader.</li>
  <li>Edit the core and the summaries.</li>
  <li>Clear the resources that do not belong.</li>
  <li>Save the thread as one HTML page in Downloads.</li>
</ol>
<p>The page has no outside files. A person opens it in a browser. The page also holds the thread as JSON in a
<code>&lt;script type="application/json" id="pensieve-thread"&gt;</code> element, so the reader's agent reads the
same file. Agents share a thread with the <code>share_thread</code> MCP tool. Pensieve does not send the file. You
or your agent attach it to a message.</p>

<h3 id="reach">What a shared thread keeps</h3>
<p>Each resource has a reach: who can open it. The reach sets what a shared thread does with the resource.</p>
{table(["Reach", "Resources", "In a shared thread"], [
    ["local", "Session transcripts, agent memory files, uncommitted or unpushed changes, files outside git, folders", "A summary, written from the whole document when you share. No path."],
    ["git", "The branch on the remote, files at a pushed commit, PRs", "A link (a permalink at the commit)"],
    ["mcp", "Tickets, messages and pages from your connections", "A link and its service, with a summary. A message goes as a link only, without its text."],
    ["restricted", "Claude artifacts", "A link marked “may need access”, with a summary"],
    ["public", "Public web pages", "A link"],
    ["private", "Direct messages, group messages, private channels", "Nothing: no link, no text, no summary"],
])}
<p>A shared thread has no local paths, session ids, resume command or secrets. Pensieve replaces text that looks
like a key or a token with <code>[secret]</code>. It also removes sentences about your own computer from the core.</p>

<h2 id="cli">Threads from the command line</h2>
{pre('''pensieve thread <thread id> -o thread.html    # the thread as one HTML page
pensieve thread <thread id> --json            # the thread as JSON
pensieve thread <thread id> --share -o thread.html   # the thread to send''')}
<p>A thread follows the <a href="https://github.com/TedHaley/pensieve/blob/main/pensieve/thread.schema.json">pensieve-thread/2 JSON schema</a>.
Pensieve also reads files in the older context-card/1 format. The <code>threads</code> MCP tool gives the thread ids.</p>

<h2 id="agents">Threads for your agents</h2>
<p>Agents get the same threads over MCP. <code>threads</code> lists the threads that are not done, the closest to
done first, with their steps left. <code>thread</code> gives one thread. At the start of a session and at natural
pauses, the agent offers to close the thread closest to done (the <code>pensieve-wrap-up</code> skill). With the
Claude Code prompt hook, a prompt such as “where did I leave off on ABC-12” gets the thread before the agent
starts.</p>
""")

page("connections", "Connections",
     "Pensieve connections: collect Jira, Slack and Linear over MCP, read-only",
     "Pensieve connections collect tickets, threads and pages from MCP servers such as Jira, Slack and Linear with "
     "read-only tools, keep each change, and add them to your threads.",
     "Connections",
     "This page tells you how Pensieve collects tickets, messages and pages from your MCP servers, and how it keeps the collection read-only.",
     f"""
<p>A connection is an MCP server that Pensieve collects data from. Pensieve writes each item as a Markdown file in
<code>~/.pensieve/connections</code> and indexes it like a document. Each item keeps a list of its changes over time.
Collected tickets and messages also join your <a href="/docs/threads/">threads</a>.</p>
<div class="note"><p>Each connection is off until you switch it on in Settings → Sources.</p></div>

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
items are context for your own threads and agents. A shared thread leaves them out whole.</p>

<h2 id="schedule">When Pensieve collects</h2>
{table(["Setting", "Default", "What it does"], [
    ["<code>connections_every_hours</code>", "6", "How often Pensieve collects from each connection that is on. 0 collects only when you ask."],
    ["<code>connections_days</code>", "30", "How far back the first collection goes."],
], num=(1,))}
<p><strong>Collect now</strong> in Sources collects at once.</p>
<p>See <a href="/docs/privacy/#network">Privacy and data</a> for what goes over the network when Pensieve collects.</p>
""")
