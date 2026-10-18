from pages_a import page, pre, table


def tools(rows):
    return table(["Tool", "Arguments", "Returns"], [[f"<code>{n}</code>", a, r] for n, a, r in rows])


DEFAULT = [
    ("search", "<code>query</code>, <code>limit</code> = 8, <code>kind</code>",
     "One line per hit, best first: <code>path:line</code>, the enclosing definition, the line that matched, and an id for <code>read</code> and <code>similar</code>. The top hits also show a few numbered lines. Query syntax: <code>\"exact\"</code>, <code>-word</code>, <code>ext:py</code>, <code>in:&lt;path&gt;</code>, <code>by:&lt;person&gt;</code>, <code>kind:code|doc|session|folder|repo|person</code>."),
    ("read", "<code>id</code> (one id, or up to 8 ids separated by spaces), <code>max_chars</code> = 12000",
     "The full text of a result. For code, also the repo, branch, lines, owners from git blame, recent commits and the agent sessions that touched it."),
    ("similar", "<code>id</code>, <code>limit</code> = 8",
     "Items that are similar in meaning to a result, in the same line format as <code>search</code>. Use it before you write new code."),
    ("overview", "<code>repo</code> (default: the repo of the scope)",
     "Where things are in a repo: size, languages, top folders two levels deep, the start of the README, dependencies, the largest files, and topics (files grouped by meaning) with their files and authors."),
    ("who_knows", "<code>topic</code>, <code>repo</code>",
     "<code>people</code>: who wrote the code most related to the topic (git blame). <code>code</code>: the matching code, with id, repo, path, line and score."),
    ("team", "<code>repo</code>, <code>days</code> = 90",
     "For each person: commits, main areas and recent commit subjects in that period."),
    ("unexplored", "<code>repo</code>",
     "Areas next to your own work that your agent sessions did not touch, and who owns them."),
    ("knowledge_gaps", "<code>repo</code>, <code>inactive_days</code> = 180",
     "Areas mostly written by people with no commit in <code>inactive_days</code>: who wrote each one, the active person who knows it best (<code>ask</code>) and the bus factor."),
    ("list_repos", "none", "The indexed repos in the scope, with their size and the number of agent sessions in each."),
    ("current_scope", "none", "The part of the index that this connection searches."),
    ("open", "<code>id</code>, <code>action</code> = <code>open</code> | <code>reveal</code> | <code>visualize</code>",
     "Opens an item on your computer: code in your editor at the line, the item in Finder, or the item on the Map."),
    ("index_path", "<code>path</code>",
     "Indexes a folder when you ask the agent to: a git repo, a folder of repos, a folder of agent sessions, or documents. Pensieve finds out which kind it is."),
    ("stale", "<code>days</code> = 14",
     "What looks abandoned: worktrees with no commit and no session, merged local branches, and repos with no activity for 3 months. Each item has a verdict and, where it is safe, a command. The agent asks you before it runs a command."),
    ("tasks", "<code>state</code> = <code>conscious,subconscious</code>, <code>limit</code> = 10, <code>query</code>",
     "Your context cards of the last 30 days, one line each, newest first. <code>state</code> takes a list of states, or <code>all</code>. <code>query</code> takes words, a ticket key, a branch or a PR number."),
    ("task", "<code>id</code> (a task id or a thread id), <code>full</code> = false",
     "One task as a card: its state and why, folder, branch, uncommitted files, PRs, tickets, what changed since the last session, what is left, where that session stopped and the command that resumes it."),
    ("set_task_state", "<code>id</code>, <code>state</code>, <code>reason</code>",
     "Sets a task's state: <code>conscious</code>, <code>subconscious</code>, <code>latent</code>, <code>etched</code>, <code>forgotten</code>, or <code>auto</code>. You see the change on the Context tab."),
    ("share_card", "<code>id</code>, <code>ask</code>",
     "Makes a card that is fit to send and saves it as one HTML page in <code>~/Downloads</code>. Returns the path of the file and the card. The agent attaches the file. You approve what it sends."),
    ("thread_history", "<code>thread</code>, <code>n</code>",
     "The saved cards of a thread, oldest first. With <code>n</code>, the whole card of that save (<code>n</code> = -1 is the last)."),
    ("report_feedback", "<code>kind</code> = <code>bug</code> | <code>idea</code>, <code>summary</code>, <code>details</code>",
     "Saves a report on your computer. You read it in Settings → Feedback before you send it."),
]

SETUP = [
    ("setup_guide", "none", "What Pensieve is, the user profiles, the source categories and the folders that each scan reads, the generation models and the energy note. It reads no folder. The agent then asks you each question in order."),
    ("setup_scan", "<code>profiles</code> or <code>categories</code>", "What can be indexed in those categories, with the size of each item and the recommended items. macOS can ask you for folder access."),
    ("setup_add_source", "<code>kind</code> = <code>files</code> | <code>repos</code> | <code>agents</code>, <code>path</code>", "The id of a source that the scan did not find."),
    ("setup_start", "<code>sources</code> or <code>recommended</code>, <code>ai</code>, <code>content</code>", "Starts indexing. Returns the time left, the energy note and how to search."),
]

WORK = [
    ("pending_work", "<code>limit</code> = 5, <code>kind</code>", "Jobs that wait for your agent: task notes, session summaries, topic names and insights. Each job holds its instructions and its data."),
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
]

page("mcp-tools", "MCP tools",
     "Pensieve MCP tools reference: search, read, similar, who_knows and more",
     "Reference for every tool of the Pensieve MCP server: search, read, similar, overview, who_knows, "
     "knowledge_gaps, tasks, task, share_card and the setup tools, with arguments and return values.",
     "MCP tools reference",
     "This page lists each tool of the Pensieve MCP server, its arguments and what it returns.",
     f"""
<p>An MCP client sends the description of each tool to the model on every turn. Thus Pensieve shows a small set by
default. Other sets appear only when they are necessary. Every tool obeys the <a href="/docs/agents/#scope">scope</a>
of the connection.</p>
{table(["Set", "When the agent sees it", "Tools"], [
    ["Default", "Always", "19"],
    ["Setup", "Until setup is done", "4"],
    ["Work queue", "While the generation model is your agent", "2"],
    ["Settings", "With <code>pensieve mcp --all-tools</code> or <code>?tools=all</code>", "15"],
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
<p>If you choose your agent as the generation model, Pensieve keeps its jobs in a queue. Nothing runs in the
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

page("context-cards", "Context cards",
     "Context cards: one card per task for you and your AI agents | Pensieve",
     "Pensieve context cards join agent sessions, branches, PRs, tickets and messages into one card per task, with "
     "five states. Share a card as one HTML file that people and agents can read.",
     "Context cards",
     "This page tells you how Pensieve makes a context card for each task, how it sets the state, and how you share a card.",
     f"""
<h2 id="what-a-card-holds">What a card holds</h2>
<p>Every card has the same core: the <strong>problem</strong>, the <strong>goal</strong>, the <strong>state</strong>
and what is <strong>next</strong>. It also holds the question that waits on you. The rest of the card is its
resources, the evidence behind the core:</p>
<ul>
  <li>The folders where the work is: the repo, the branch, the worktree, uncommitted files, and how far the branch is ahead or behind.</li>
  <li>The PRs, with their state, CI checks and review.</li>
  <li>The tickets, with their status and assignee.</li>
  <li>The agent sessions, and the command that resumes the last one.</li>
  <li>When you ask, coworkers' recent commits in the same folders, and related tickets and threads.</li>
</ul>
<p>A short AI note says what is done and what is left. The note needs a generation model. Without one, the card has
its evidence but no note.</p>

<h2 id="how-tasks-join">How Pensieve joins a task</h2>
<p>Pensieve joins sessions, branches, commits and connection items into one task by these links:</p>
<ul>
  <li>A ticket key such as <code>ABC-123</code> in a branch name, a commit subject, a session or a ticket.</li>
  <li>The git branch that a session ran on. This applies only to feature branches. A default branch holds many tasks.</li>
  <li>For work with no ticket: sessions in the same repo, at most 3 days apart, that edited the same files.</li>
  <li>Session titles that mostly match, within 3 days.</li>
</ul>
<p>Pensieve leaves out sessions in temporary folders and under <code>~/Library</code>. Apps and scripts start those.</p>

<h2 id="states">The five states</h2>
<p>Pensieve sets each state from the task's evidence. Material work is edits, commits, a branch or a PR.</p>
{table(["State", "Meaning", "Set automatically when"], [
    ['<span class="state" style="--sc:var(--s-conscious)">Conscious</span>', "You work on it now.", "A session runs now. Or the task has material work and activity in the last 3 days, and it is not done."],
    ['<span class="state" style="--sc:var(--s-sub)">Subconscious</span>', "It is not done and it is quiet. Go back to it.", "It has material work that is not done, with no activity in the last 3 days."],
    ['<span class="state" style="--sc:var(--s-latent)">Latent</span>', "A ticket that you did not start.", "A ticket that is not done, or with an unknown status, and no edits, commits, branch or PR."],
    ['<span class="state" style="--sc:var(--s-etched)">Etched</span>', "Done.", "A PR is merged, nothing happened after the merge, and no PR is open. Or its own PR is merged and no PR is open. Or its PRs are merged or closed, or its ticket is done, and nothing is uncommitted."],
    ['<span class="state" style="--sc:var(--s-forgotten)">Forgotten</span>', "Not finished, and it will probably stay that way.", "Material work that is not done, quiet for 21 days, with no open PR and no uncommitted work. Or a conversation with no ticket and no edits, after its session ended."],
])}
<p>You can set a state on the card. An agent can set one with <code>set_task_state</code>. A state set by hand stays
until the task has new activity. Then the automatic state applies again. Pensieve keeps each change by hand, so a
task has a history of its states.</p>

<h2 id="threads">Threads</h2>
<p>Each card belongs to a thread: one goal, followed over time. Pensieve saves a thread's card when it changes, for
example when a PR opens, a commit lands or the next step changes. The thread chip on a card opens its saved cards.
“New session from card” starts an agent that reads the card first. Agents get the same data from
<code>thread_history</code>.</p>

<h2 id="share">Share a card as HTML</h2>
<p>The Share button on a card opens a preview. Pensieve reads the local resources whole and writes a summary of
each one. Then you can do these steps:</p>
<ol>
  <li>Write what you need from the reader.</li>
  <li>Edit the core and the summaries.</li>
  <li>Clear the resources that do not belong.</li>
  <li>Save the card as one HTML page in Downloads.</li>
</ol>
<p>The page has no outside files. A person opens it in a browser. The page also holds the card as JSON in a
<code>&lt;script type="application/json" id="context-card"&gt;</code> element, so the reader's agent reads the same
file. Agents share a card with the <code>share_card</code> MCP tool. Pensieve does not send the file. You or your
agent attach it to a message.</p>

<h3 id="reach">What a shared card keeps</h3>
<p>Each resource has a reach: who can open it. The reach sets what a shared card does with the resource.</p>
{table(["Reach", "Resources", "In a shared card"], [
    ["local", "Session transcripts, agent memory files, uncommitted or unpushed changes, files outside git, folders", "A summary, written from the whole document when you share. No path."],
    ["git", "The branch on the remote, files at a pushed commit, PRs", "A link (a permalink at the commit)"],
    ["mcp", "Tickets, messages and pages from your connections", "A link and its service, with a summary. A message goes as a link only, without its text."],
    ["restricted", "Claude artifacts", "A link marked “may need access”, with a summary"],
    ["public", "Public web pages", "A link"],
    ["private", "Direct messages, group messages, private channels", "Nothing: no link, no text, no summary"],
])}
<p>A shared card has no local paths, session ids, resume command or secrets. Pensieve replaces text that looks like
a key or a token with <code>[secret]</code>. It also removes sentences about your own computer from the core.</p>

<h2 id="cli">Cards from the command line</h2>
{pre('''pensieve card <task id> -o card.html    # the card as one HTML page
pensieve card <task id> --json          # the card as JSON
pensieve card <task id> --share -o card.html   # the card to send''')}
<p>A card follows the <a href="https://github.com/TedHaley/pensieve/blob/main/pensieve/context-card.schema.json">context-card/1 JSON schema</a>.
The <code>tasks</code> MCP tool gives the task ids.</p>

<h2 id="agents">Cards for your agents</h2>
<p>Agents get the same cards over MCP. <code>tasks</code> lists them by state. <code>task</code> gives one card.
When an agent finishes a task, it marks it etched with <code>set_task_state</code>. With the Claude Code prompt
hook, a prompt such as “where did I leave off on ABC-12” gets the card before the agent starts.</p>
""")

page("connections", "Connections",
     "Pensieve connections: collect Jira, Slack and Linear over MCP, read-only",
     "Pensieve connections collect tickets, threads and pages from MCP servers such as Jira, Slack and Linear with "
     "read-only tools, keep each change, and add them to your context cards.",
     "Connections",
     "This page tells you how Pensieve collects tickets, messages and pages from your MCP servers, and how it keeps the collection read-only.",
     f"""
<p>A connection is an MCP server that Pensieve collects data from. Pensieve writes each item as a Markdown file in
<code>~/.pensieve/connections</code> and indexes it like a document. Each item keeps a list of its changes over time.
Collected tickets and threads also join your <a href="/docs/context-cards/">context cards</a>.</p>
<div class="note"><p>Each connection is off until you switch it on in Settings → Sources.</p></div>

<h2 id="kinds">Where the servers come from</h2>
<p>Pensieve finds your MCP servers by itself:</p>
<ul>
  <li><strong>Claude Code's servers.</strong> If Claude Code is on your computer, Sources lists its MCP servers. This includes the claude.ai connectors of your account. To collect, Pensieve runs Claude Code headless with the Haiku model and only the read tools of one server. Every other tool is blocked.</li>
  <li><strong>Your other agents' servers.</strong> Pensieve reads the MCP settings of Codex, Cursor, Gemini CLI, Qwen Code, Windsurf, Claude Desktop and VS Code. It connects to those servers itself, with the credentials in those settings. If Claude Code also has a server, Pensieve leaves that server to Claude Code.</li>
  <li><strong>Your own servers.</strong> Add a server in Sources. Give a URL with headers, or a command with environment variables. Pensieve does not show the header or environment values again. They stay in <code>~/.pensieve/settings.json</code>, which only you can read.</li>
  <li><strong>Your agent sessions.</strong> The results that your agents already got from MCP tools. Pensieve fetches nothing new for this source.</li>
</ul>
<p>For the servers that Pensieve connects to itself, the generation model chooses the search calls, and Pensieve makes them.</p>

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
items are context for your own cards and agents. A shared card leaves them out whole.</p>

<h2 id="schedule">When Pensieve collects</h2>
{table(["Setting", "Default", "What it does"], [
    ["<code>connections_every_hours</code>", "6", "How often Pensieve collects from each connection that is on. 0 collects only when you ask."],
    ["<code>connections_days</code>", "30", "How far back the first collection goes."],
], num=(1,))}
<p><strong>Collect now</strong> in Sources collects at once.</p>
<p>See <a href="/docs/privacy/#network">Privacy and data</a> for what goes over the network when Pensieve collects.</p>
""")
