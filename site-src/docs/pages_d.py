from pages_a import page, pre, table

BENCH = "https://github.com/TedHaley/pensieve/tree/main/bench"
STRAT = "https://github.com/TedHaley/pensieve/blob/main/bench/STRATEGIES.md"

page("benchmark", "Benchmark",
     "Pensieve benchmark: MCP code search vs grep for Claude Code agents",
     "How Pensieve was measured against grep with Claude Sonnet in Claude Code: 72 held-out tasks in four repos, "
     "tasks solved, tool calls, cost and time. The method, the results and the limits.",
     "Benchmark",
     "This page tells you how we measured Pensieve against grep, what the results are, and what we did not measure.",
     f"""
<div class="stats">
  <div><b>−47%</b><span>tool calls per lookup: 1.81 against 3.42</span></div>
  <div><b>−23%</b><span>cost per lookup: $0.039 against $0.051</span></div>
  <div><b>−29%</b><span>time per lookup: 6.8 s against 9.5 s</span></div>
  <div><b>90%</b><span>tasks solved, the same as with grep (129 / 144)</span></div>
</div>
<p>These four numbers are from the latest batch (2026-10-13), with clean Claude Code settings. Pensieve solves as many
tasks as grep, with about half the tool calls. An earlier batch, with Pensieve 0.5.6, solved 91% against 85% with
grep. The two later clean batches did not repeat this gain. Both results are below.</p>

<h2 id="method">Method</h2>
<p>The script is <a href="{BENCH}/agent_search.py"><code>bench/agent_search.py</code></a>. It does these steps:</p>
<ol>
  <li>It makes tasks from indexed code. Each task describes one piece of code. The answer is the file that holds it.</li>
  <li>It writes each task in one of these styles: a plain description, a name that the code defines, an error string from the code, or a mix of a name and words.</li>
  <li>It splits the tasks into a dev set, which we used to tune Pensieve, and a test set, which we did not use to tune.</li>
  <li>It runs Claude Sonnet headless through Claude Code (<code>claude -p</code>) on each test task, once in each setup.</li>
  <li>It records whether the answer names the right file, the tool calls, the tokens, the time, and the cost that Claude Code reports.</li>
</ol>
{table(["Setup", "Tools that the agent has"], [
    ["grep", "Read, Grep and Glob"],
    ["guided", "Read, Grep and Glob, plus Pensieve's <code>search</code>, <code>read</code> and <code>similar</code>, plus the “search before you grep” instructions block. This is the recommended install."],
    ["only", "Pensieve's tools and Read, with no Grep or Glob. Only in the later batches."],
])}
{table(["Item", "Value"], [
    ["Model", "Claude Sonnet, in Claude Code"],
    ["Tasks", "72 held-out (test) tasks"],
    ["Repos", "4 repos, from 504 to 34,847 files"],
    ["Runs", "Each task twice in each setup: 144 runs per setup"],
    ["Order", "All setups of a batch run interleaved, in one batch"],
])}
<p>Cost depends on the state of Claude's prompt cache. The same tokens can cost 4 times more with a cold cache. Thus
we compare cost only between setups in one batch. Calls, tokens and time can be compared between batches.</p>

<h2 id="results">Results: first batch (Pensieve 0.5.6)</h2>
{table(["", "grep", "guided (Pensieve)", "Change"], [
    ["Tasks solved", "123 / 144 (85%)", "<strong>131 / 144 (91%)</strong>", "+6 points"],
    ["Tool calls per lookup", "3.44", "<strong>1.97</strong>", "−43%"],
    ["Cost per lookup", "$0.050", "<strong>$0.041</strong>", "−18%"],
    ["Tokens per lookup", "67k", "<strong>57k</strong>", "−16%"],
    ["Time per lookup (mean)", "10.9 s", "<strong>8.6 s</strong>", "−21%"],
], num=(1, 2, 3))}
<p>In 82 of the 144 guided runs, the agent answered with one tool call or none. Pensieve solved as many tasks or
more, and cost less, in every repo:</p>
{table(["Repo", "Solved, grep", "Solved, guided", "Cost change", "Calls, grep", "Calls, guided"], [
    ["504 files", "24 / 26", "24 / 26", "−15%", "2.85", "1.65"],
    ["Medium repo 1", "34 / 40", "36 / 40", "−6%", "2.65", "1.77"],
    ["Medium repo 2", "20 / 26", "24 / 26", "−14%", "2.54", "1.46"],
    ["34,847 files", "45 / 52", "47 / 52", "−28%", "4.81", "2.54"],
], num=(1, 2, 3, 4, 5))}
<p>The two medium repos have 1,783 and 2,383 files.</p>

<h2 id="clean-runs">Results: later batches with clean settings</h2>
<p>The first batch loaded the Claude Code settings of the computer that ran it. Later, the script loads only the
settings of the repo (<code>--setting-sources project,local</code>). These are the last of those batches, run on
2026-10-13 with the same 72 tasks:</p>
{table(["Setup", "Solved", "Tool calls", "Cost per lookup", "Time (mean)"], [
    ["grep", "129 / 144", "3.42", "$0.051", "9.5 s"],
    ["guided", "129 / 144", "1.81", "$0.039", "6.8 s"],
    ["only", "130 / 144", "1.70", "$0.038", "6.5 s"],
], num=(1, 2, 3, 4))}
<ul>
  <li><strong>Accuracy is level with grep.</strong> The 6-point gain of the first batch did not come back with clean settings. This task set suits grep: most descriptions use words that are in the code.</li>
  <li><strong>Cost and time are lower.</strong> Guided was 23 to 25% cheaper and 25 to 29% faster than grep in the two clean batches.</li>
  <li><strong>6 tasks fail in every setup.</strong> Their description fits a near-copy of the answer as well as the answer.</li>
</ul>

<h2 id="search-only">Search alone, with no agent</h2>
<p>A second set measures one search, with no agent. It has 2,377 queries in the same four repos. Each query has one
right file. “@1” is the share of queries where the right file is first. The grep row is one case-insensitive grep,
with files ranked by the number of matches.</p>
{table(["Query kind", "Queries", "Pensieve 0.6.4 @1", "grep @1"], [
    ["Question in plain words", "334", "47.9%", "0%"],
    ["Name plus words", "333", "70.9%", "0%"],
    ["Name", "219", "94.1%", "51.3%"],
    ["Exact string", "714", "84.6%", "85.2%"],
    ["String that 2 or more files contain", "404", "37.1%", "39.7%"],
    ["Message as it shows in a log", "373", "56.3%", "0%"],
    ["<strong>All</strong>", "2,377", "<strong>65.9%</strong>", "37.1%"],
], num=(1, 2, 3))}
<p>In the top 8 results, which is what the <code>search</code> tool shows, Pensieve has the right file for 91.6% of
queries. Grep has it for 53.1%. On the held-out queries only, @1 is 66.7% against 35.2%. A search took 73 ms at the
median, on an Apple M4 Max. Grep is level with Pensieve on exact strings.
<a href="{STRAT}">STRATEGIES.md</a> lists every ranking method that we tried, with its numbers.</p>

<h2 id="overview-tool">Repo overviews</h2>
<p>We asked “Give me a high-level overview of this repository” in five repos, three times each. A second Claude run
checked each claim against the code. Only confirmed errors count.</p>
{table(["Setup", "Wrong claims (15 answers)", "Tool calls", "Cost", "Time (median)"], [
    ["grep, repo with no CLAUDE.md", "10", "4.1", "$0.056", "13.0 s"],
    ["Repo overview tool, repo with no CLAUDE.md", "<strong>4</strong>", "1.4", "$0.034", "12.0 s"],
    ["grep, repo with its CLAUDE.md", "3", "1.5", "$0.035", "10.0 s"],
    ["Repo overview tool, repo with its CLAUDE.md", "2", "1.2", "$0.044", "11.9 s"],
], num=(1, 2, 3, 4))}
<p>If a repo has good notes for the agent, grep needs no help. The overview tool helps most when a repo has no
notes. The test used the <code>overview</code> tool. Today, <code>repo</code> gives the same overview.</p>

<h2 id="shared-threads">Shared threads</h2>
<p>This test asks whether a shared thread helps a teammate's agent. The agent worked in a fresh clone, with no
access to the owner's computer. It answered questions about 4 real pieces of work, 3 times each. A judge counted
the facts from a list that the answer got right, and the wrong claims.</p>
{table(["Setup", "Facts right", "Wrong claims"], [
    ["No shared thread", "6 / 60 (11%)", "10"],
    ["With the shared thread", "52 / 60 (85%)", "0"],
], num=(1, 2))}
<p>The script is <a href="{BENCH}/workflows.py"><code>bench/workflows.py</code></a>. This test is small: 4 pieces
of work and 12 runs in each setup.</p>

<h2 id="limits">What we did not measure</h2>
<ul>
  <li><strong>Other models and agents.</strong> Every agent run used Claude Sonnet in Claude Code. We did not measure Codex, Cursor, Gemini CLI, Qwen Code, Windsurf, Claude Desktop or VS Code.</li>
  <li><strong>Other repos.</strong> The tasks come from four repos on one developer's computer. We do not publish their names.</li>
  <li><strong>Full coding tasks.</strong> Each task asks the agent to find one file. We did not measure whether Pensieve helps an agent fix a bug or ship a feature.</li>
  <li><strong>Generated tasks.</strong> A model wrote the tasks from the code. Real questions from real sessions are noisier. We have only about 18 of them, too few to report.</li>
  <li><strong>Independent runs.</strong> Nobody outside the project has run the benchmark yet.</li>
  <li><strong>Linux and Intel.</strong> All runs were on Apple Silicon.</li>
  <li><strong>The prompt hook.</strong> The optional <code>claude-prompt-search</code> hook cut tool calls by 33 to 46% in three batches. Its accuracy was level with guided, and its cost was the same. The results above do not use it.</li>
</ul>

<h2 id="data">Data and scripts</h2>
<p>The scripts are in <a href="{BENCH}"><code>bench/</code></a>. The run files hold paths and code from the four
repos, so they are not public. To get your own numbers, run the benchmark on your repos:</p>
{pre('''uv run python bench/agent_search.py gen --repo ~/code/my-app -n 40 --tasks tasks-v2.jsonl
uv run python bench/exact_tasks.py      # adds name, error-string and mixed tasks
uv run python bench/agent_search.py run --repo ~/code/my-app --tasks tasks-v2.jsonl --arms grep,guided''')}
<div class="note warn"><p>Use a copy of an index (<code>PENSIEVE_DATA</code>), not <code>~/.pensieve</code>. Run a server on the copy for the agent runs.</p></div>
""")

page("privacy", "Privacy and data",
     "Pensieve privacy: a local index, and what goes over the network",
     "Pensieve indexes and searches on your computer. This page lists where Pensieve keeps data and each time it "
     "uses the network: model downloads, updates, git fetch, connections and AI models.",
     "Privacy and data",
     "This page tells you where Pensieve keeps your data, and each time that Pensieve or your agent uses the network.",
     f"""
<h2 id="local">What stays on your computer</h2>
<ul>
  <li>Indexing and search run on your computer. The search model always runs locally.</li>
  <li>Pensieve keeps everything in <code>~/.pensieve</code>: the index, the settings, the collected connection items and the feedback reports. To start again, remove that folder.</li>
  <li>The backend listens on <code>127.0.0.1</code> only, port 8765 by default. Other computers cannot connect to it.</li>
  <li>The app and the backend have no analytics and no account.</li>
  <li>Pensieve never indexes files that look like secrets: password-manager kits, keys, <code>.env</code> files and credentials. The <code>exclude_files</code> setting lists the patterns.</li>
  <li>Pensieve keeps its git data in <code>~/.pensieve/git</code>. It does not write to your repos.</li>
</ul>

<h2 id="network">When Pensieve uses the network</h2>
{table(["When", "What goes over the network", "To"], [
    ["Install", "The app download and the Python packages", "GitHub Releases, the Python package index"],
    ["First run, and when you switch models", "Model downloads: the search model (550 MB), and the media parts if you switch them on", "Hugging Face"],
    ["Update checks", "A request for the latest release", "GitHub"],
    ["Every 30 minutes", "<code>git fetch</code> of the default branch of each indexed repo, to show how far your branch is behind. It does not change your working tree.", "Each repo's own remote"],
    ["When a thread shows PRs", "<code>gh pr list</code> and <code>gh pr view</code>, if the GitHub CLI is installed, with your own login", "GitHub"],
    ["Connections that you switch on", "Read calls to those MCP servers.", "Each server"],
    ["When your agent does Pensieve's pending work", "The text of each job (a thread note, a summary, an insight) goes to your agent.", "Your agent's model provider"],
])}

<h2 id="agents">Your agent</h2>
<p>Pensieve is a tool that your agent calls. If you use a cloud agent, such as Claude Code or Codex, what the agent
reads from Pensieve goes to its provider, the same as a file that it reads with grep. Pensieve sends nothing to the
agent until the agent calls a tool. The Claude Code prompt hook is the exception: it adds context to each prompt in
an indexed repo.</p>

<h2 id="sharing">Shared threads and feedback</h2>
<ul>
  <li><strong>Shared threads:</strong> Pensieve saves a shared thread as a file in Downloads. It does not send it. Before you save, you see the thread and can edit it. A shared thread has no local paths, session ids, resume command, secrets or private messages (see <a href="/docs/threads/#reach">What a shared thread keeps</a>).</li>
  <li><strong>Feedback:</strong> reports from you and from agents stay in <code>~/.pensieve/feedback.jsonl</code>. Settings → Feedback lists them. Send to GitHub opens an issue with the text filled in, and you read it before you post it.</li>
</ul>

<h2 id="private-messages">Private messages</h2>
<p>Items from direct messages, group messages and private channels are context for your own threads and agents only.
A shared thread leaves them out whole. See <a href="/docs/connections/#private">Connections</a>.</p>

<h2 id="site">This website</h2>
<p>pensieve.tools counts page views with <a href="https://www.goatcounter.com/">GoatCounter</a>, which sets no
cookies. The app does not use it.</p>
""")

FAQ = [
    ("Is Pensieve free?", "Yes. Pensieve is free and open source under the MIT license. The code is on <a href=\"https://github.com/TedHaley/pensieve\">GitHub</a>. There is no account and no paid plan."),
    ("Does Pensieve need Apple Silicon?", "The app download needs an Apple Silicon Mac with macOS 14 or later. On Linux and Intel Macs, the same install command installs the backend, the MCP server and the Map in your browser. The search panel is macOS only. See <a href=\"/docs/install/#requirements\">Requirements</a>."),
    ("Does it work on Linux?", "Yes, without the Mac app. The backend indexes, searches and serves MCP to your agents. The Map opens in your browser. The search model runs on PyTorch (CUDA or CPU). <code>pensieve integrate</code> connects the agents whose settings are in your home folder: Claude Code, Codex, Cursor, Gemini CLI, Qwen Code and Windsurf."),
    ("Which agents can use it?", "Claude Code, Codex, Cursor, Gemini CLI, Qwen Code, Windsurf, Claude Desktop and VS Code, with one command. Any other MCP client can connect over stdio or HTTP. See <a href=\"/docs/agents/\">Connect your agent</a>."),
    ("Does anything leave my computer?", "Your files, code and sessions do not leave because of Pensieve. Indexing and search run locally. Pensieve downloads models, finds updates, fetches your repos' default branches, and collects from connections that you switch on. If your agent is a cloud agent, what it reads goes to its provider. See <a href=\"/docs/privacy/\">Privacy and data</a>."),
    ("Do I need an AI model?", "No. Pensieve has no text model of its own. Your agent writes the notes on threads and the written insights, with its own model. Search, agent context, ownership and knowledge gaps do not need it."),
    ("How do I make my agent use Pensieve and not grep?", "Run <code>pensieve integrate recommended</code>. It adds the MCP server, a short “search before you grep” instructions block, and the <code>pensieve-search</code> skill. Without instructions, agents did not call Pensieve in our first test. With them, the agent used Pensieve in all 144 runs of the last benchmark batch."),
    ("Is it better than grep?", "It finds the right file with fewer tool calls, at lower cost. In our benchmark with Claude Sonnet, the cost per lookup was 18 to 25% lower. Accuracy was 6 points higher in the first batch and level in the later batches. See the <a href=\"/docs/benchmark/\">benchmark</a> for the method and the limits."),
    ("How long does the first index take?", "About 500 code files a minute on Apple Silicon. A repo with 35,000 files takes a little more than one hour. Search works while Pensieve indexes. After the first index, a change shows in search about half a second after you save."),
    ("Can I limit an agent to one project?", "Yes. By default, an agent's scope is the repo that it started in. You can make named scopes in Settings → Scopes, and connect an agent with <code>pensieve mcp --scope &lt;name&gt;</code>. See <a href=\"/docs/agents/#scope\">Scopes</a>."),
    ("Does Pensieve read my private messages?", "Only if you switch on a connection that can read them. Pensieve marks those items private. They help your own threads and agents. A shared thread leaves them out whole."),
    ("For Claude Code, which do I use: the plugin or pensieve integrate?", "Use one of them, not both. The plugin does not change your settings files. <code>pensieve integrate</code> also connects your other agents. See <a href=\"/docs/agents/claude-code/\">Pensieve for Claude Code</a>."),
    ("How do I uninstall it?", "See <a href=\"/docs/install/#uninstall\">Uninstall</a>."),
    ("How do I report a bug?", "Use Settings → Feedback in the app, or open an issue on <a href=\"https://github.com/TedHaley/pensieve/issues/new/choose\">GitHub</a>. Agents can call the <code>report_feedback</code> tool. Reports stay on your computer until you send them."),
]

page("faq", "FAQ",
     "Pensieve FAQ: Mac and Linux, privacy, agents, MCP and grep",
     "Answers about Pensieve: price, Apple Silicon and Linux, which AI agents work with it, privacy, the "
     "models, scopes, and how it compares with grep.",
     "Frequently asked questions",
     "This page answers the questions that people ask most about Pensieve.",
     "".join(f'<h2 id="q{i}">{q}</h2><p>{a}</p>' for i, (q, a) in enumerate(FAQ, 1)),
     )
