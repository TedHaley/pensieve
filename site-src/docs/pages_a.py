from html import escape


def pre(text):
    """A code block; lines that start with # are comments."""
    out = []
    for line in text.strip("\n").split("\n"):
        e = escape(line)
        if line.lstrip().startswith("#") and not line.lstrip().startswith("#!"):
            e = f'<span class="c">{e}</span>'
        elif "   # " in line:
            a, _, b = line.partition("   # ")
            e = f'{escape(a)}   <span class="c"># {escape(b)}</span>'
        out.append(e)
    return "<pre><code>" + "\n".join(out) + "</code></pre>"


def table(head, rows, num=()):
    th = "".join(f'<th{" class=n" if i in num else ""}>{h}</th>' for i, h in enumerate(head))
    trs = "".join("<tr>" + "".join(f'<td{" class=n" if i in num else ""}>{c}</td>' for i, c in enumerate(r)) + "</tr>"
                  for r in rows)
    return f'<div class="tbl"><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>'


PAGES = []


def page(slug, nav, title, desc, h1, lede, body, crumb="Docs"):
    PAGES.append(dict(slug=slug, nav=nav, title=title, desc=desc, h1=h1, lede=lede, body=body, crumb=crumb))


page("", "Overview",
     "Pensieve docs: threads and an MCP server for AI coding agents",
     "Pensieve documentation. A free, local Mac app and MCP server that gives you and your AI coding agents the right "
     "context: threads, code search, who knows what, and knowledge gaps.",
     "Pensieve documentation",
     "This page tells you what Pensieve is, what its parts do, and where to start.",
     f"""
<p>Pensieve keeps the context of your work in one place on your Mac. It indexes your files, git repos, AI agent
sessions, tickets and messages. You search it from a panel. Your coding agents search it over MCP. Each person's
context is different, so Pensieve builds it from your own work and keeps it on your Mac.</p>
<p>Pensieve is free and open source under the MIT license. It is for developers and their AI agents.</p>

<div class="cards">
  <a href="/docs/install/"><b><i style="--c:var(--ink)"></i>Install</b><span>One command for the app and the backend, then one command for your agents.</span></a>
  <a href="/docs/agents/"><b><i style="--c:var(--c-agents)"></i>Connect your agent</b><span>Claude Code, Codex, Cursor, Gemini CLI, Qwen Code, Windsurf, Claude Desktop and VS Code.</span></a>
  <a href="/docs/mcp-tools/"><b><i style="--c:var(--c-agents)"></i>MCP tools</b><span>Each tool, its arguments and what it returns.</span></a>
  <a href="/docs/benchmark/"><b><i style="--c:var(--c-owners)"></i>Benchmark</b><span>The method, the results and what we did not measure.</span></a>
</div>

<h2 id="parts">The main parts</h2>

<h3 id="threads">Threads</h3>
<p>Pensieve makes one thread for each goal of your work. A thread joins the agent sessions, the branch, the PRs,
the tickets and the messages of that goal. It says the problem, the goal, the state and what is next. It also
gives the steps left to close it, and the command that resumes the last agent session. Each thread has one of five
states: <span class="state" style="--sc:var(--s-todo)">To do</span>,
<span class="state" style="--sc:var(--s-open)">Open</span>,
<span class="state" style="--sc:var(--s-loose-ends)">Loose ends</span>,
<span class="state" style="--sc:var(--s-complete)">Complete</span> or
<span class="state" style="--sc:var(--s-forgotten)">Forgotten</span>. You can share a thread as one HTML file.
<a href="/docs/threads/">Read about threads</a>.</p>

<h3 id="mcp-server">MCP server</h3>
<p>Pensieve is an MCP server. Your coding agent asks it one question and gets the few files and lines that match.
Each hit shows the path, the line, the enclosing definition and the line that matched. Each agent connection works
in a scope. By default, the scope is the repo that the agent started in.
<a href="/docs/agents/">Connect your agent</a>, or read the <a href="/docs/mcp-tools/">MCP tools reference</a>.</p>

<h3 id="search">Search</h3>
<p>Search finds items by meaning and by exact words. It blends a vector search with a full-text search. Then it
gives more weight to exact text and to names that a file defines. A search takes about 0.1 to 0.2 seconds. On a Mac,
press <kbd>Control</kbd>+<kbd>Shift</kbd> to open the search panel.</p>
{table(["Query", "Finds"], [
    ["<code>pricing model</code>", "Items about that subject, by meaning"],
    ["<code>\"rate limit\"</code>", "Items that contain those exact words (case-insensitive)"],
    ["<code>-draft</code>", "Leaves out items with that word"],
    ["<code>kind:code</code>", "Code only. Also <code>kind:doc</code>, <code>kind:session</code>, <code>kind:folder</code>, <code>kind:repo</code>, <code>kind:person</code>"],
    ["<code>by:sam terraform</code>", "Only what that person wrote (code by git blame, documents by author)"],
    ["<code>ext:pdf</code>", "One file type"],
    ["<code>in:my-app</code>", "The path or the repo contains this text"],
    ["<code>since:3d</code>, <code>until:2026-10-01</code>", "Only what changed in that time: a session by its last activity, code by its last commit, a file by its last change. Use a date, a time back (<code>12h</code>, <code>3d</code>, <code>2w</code>, <code>1m</code>), <code>today</code> or <code>yesterday</code>. <code>until:</code> includes that day."],
    ["<code>project:my-app</code>", "Agent sessions of that project only"],
    ["<code>kind:session since:yesterday</code>", "With no other words: the sessions that match, newest first"],
])}

<h3 id="who-knows">Who knows what</h3>
<p>Pensieve reads git blame for each indexed repo. Ask about a subject, not a file, and it names the people who
wrote the most related code. Agents get the same data from the <code>people</code> tool.</p>

<h3 id="knowledge-gaps">Knowledge gaps</h3>
<p>A knowledge gap is an area of a repo that people who are no longer active wrote most of. For each area, Pensieve
shows who wrote it, which active person knows it best, and its bus factor. The bus factor is the number of active
people who hold 10% or more of the lines. By default, a person with no commit in 180 days is not active.</p>

<h3 id="connections">Connections</h3>
<p>Connections collect your tickets, threads and pages from MCP servers such as Jira, Slack and Linear. Pensieve
calls only read-only tools. Each connection is off until you switch it on.
<a href="/docs/connections/">Read about connections</a>.</p>

<h2 id="what-gets-indexed">What Pensieve indexes</h2>
<p>You choose each source at setup. You can change your choice in Settings → Sources.</p>
<ul>
  <li><strong>AI agent sessions:</strong> Claude Code, Codex and Qwen Code transcripts.</li>
  <li><strong>Files and folders:</strong> documents in <code>~/Documents</code>, <code>~/Desktop</code>, <code>~/Downloads</code> and the folders that you add.</li>
  <li><strong>Git repositories:</strong> committed, staged, modified and untracked files, and linked worktrees.</li>
  <li><strong>Apps:</strong> Obsidian vaults and, with your permission, Apple Notes.</li>
  <li><strong>Connections:</strong> items from MCP servers. Each one is off by default.</li>
</ul>
<p>Pensieve never indexes files that look like secrets, such as <code>.env</code> files, keys and credentials.</p>

<h2 id="models">Models</h2>
<p>Pensieve runs one model: the search model. It turns text into vectors, and it always runs on your computer. The
default for a new index is EmbeddingGemma 2.</p>
<p>Thread notes, summaries and insights are written in the background. Setup asks what writes them: a command-line
agent (Claude Code with Haiku), the built-in Qwen 3.5 9B on your Mac, or a model server that you run, such as LM
Studio or Ollama. The built-in model runs only when there is work, and only one copy runs. Search, agent context,
ownership and knowledge gaps need none of them.</p>
""")

page("install", "Install",
     "Install Pensieve on Mac or Linux: the MCP server for AI coding agents",
     "Install Pensieve with one command, connect Claude Code, Codex, Cursor and other agents with pensieve integrate "
     "recommended, and choose what to index. Requirements for Mac and Linux.",
     "Install Pensieve",
     "This page tells you how to install Pensieve, connect your agents and choose what to index.",
     f"""
<h2 id="requirements">Requirements</h2>
{table(["Computer", "What you get"], [
    ["Mac with Apple Silicon, macOS 14 or later", "The app (search panel, menu bar, Threads tab, Map) and the <code>pensieve</code> backend. This is the main platform."],
    ["Intel Mac", "The backend and the Map in your browser. If the Xcode command-line tools are installed, the script also builds the app from source. The release download is for Apple Silicon only."],
    ["Linux", "The backend, the MCP server and the Map in your browser. The search panel app is macOS only."],
])}
<p>On Apple Silicon, the search model runs on MLX. On other computers, it runs on sentence-transformers and
PyTorch (CUDA or CPU). This makes the install about 650 MB larger.</p>

<h2 id="install">Install</h2>
<ol>
  <li>Open Terminal and run the install script:
{pre("curl -fsSL https://pensieve.tools/install.sh | sh")}</li>
  <li>Connect the coding agents on your computer:
{pre("pensieve integrate recommended")}</li>
  <li>Restart each agent that is open. An agent loads its MCP servers when it starts.</li>
  <li>Do the setup steps in the app, or ask your agent to do them (see <a href="#setup">Choose what to index</a>).</li>
</ol>
<p>You can also give the two commands to your coding agent. When an agent runs the install script, the script
installs only and does not open the app.</p>

<h3 id="what-the-script-does">What the install script does</h3>
<ul>
  <li><strong>On an Apple Silicon Mac:</strong> it downloads the latest <code>Pensieve.dmg</code> from GitHub Releases and copies the app to Applications. Then it installs the <code>pensieve</code> backend from the package inside the app. You do not need git, the Xcode tools or Python.</li>
  <li><strong>On Linux and Intel Macs:</strong> it installs <a href="https://docs.astral.sh/uv/">uv</a> if it is not there. Then it installs the backend from source with git, starts the backend and opens the Map at <code>http://localhost:8765</code>.</li>
  <li>The <code>pensieve</code> command goes in <code>~/.local/bin</code>.</li>
</ul>
<p>Two environment variables change the script. <code>PENSIEVE_NO_START=1</code> never starts Pensieve.
<code>PENSIEVE_NO_APP=1</code> skips the Mac app.</p>

<h3 id="what-integrate-does">What <code>pensieve integrate recommended</code> does</h3>
<p>The command finds the agents on your computer. It gives each one Pensieve as an MCP server, a short
“search before you grep” instructions block, and the <code>pensieve-search</code> skill. For Claude Code, it also
adds three hooks. <a href="/docs/agents/">Connect your agent</a> lists each change, file by file.</p>

<h2 id="setup">Choose what to index</h2>
<p>Pensieve indexes nothing until you finish setup. This is also true after a reset. When the app opens, it shows
these steps:</p>
<ol>
  <li>Read what Pensieve does.</li>
  <li>Choose how you use your computer: software development, AI coding agents, documents and office work, or research and notes.</li>
  <li>Let Pensieve look for sources in the folders that those choices need. macOS can ask you to allow access to some folders.</li>
  <li>Choose what to index. Add folders or repos that Pensieve did not find.</li>
  <li>Choose whether Pensieve reads images, video and audio by what they show or say. These are off by default.</li>
  <li>Choose the search model.</li>
  <li>Start indexing.</li>
</ol>
<p>To do setup from a terminal, use these commands:</p>
{pre('''pensieve setup                  # list the sources that Pensieve found, with their sizes
pensieve setup recommended      # index the sources marked [x]
pensieve setup recommended images audio   # also read images and sound by content''')}
<p>An agent can also do setup in its chat. Until setup is done, every MCP connection gets the tools
<code>setup_guide</code>, <code>setup_scan</code>, <code>setup_add_source</code> and <code>setup_start</code>.</p>

<h2 id="first-index">The first index</h2>
<p>The first index uses a lot of energy. The search model runs until it finishes, at about 500 code files a minute
on Apple Silicon. A repo with 35,000 files takes a little more than one hour. Search works while Pensieve indexes.
Settings → Sources shows what is syncing now.</p>
<div class="note"><p>If you use a laptop, connect it to power before the first index.</p></div>

<h2 id="by-hand">Install by hand</h2>
{pre('''uv tool install git+https://github.com/TedHaley/pensieve   # the backend: `pensieve`
git clone https://github.com/TedHaley/pensieve && pensieve/mac/build.sh --install   # the Mac app (needs the Xcode tools)''')}

<h2 id="update">Update</h2>
<p>The Mac app finds new releases on GitHub and installs them. You can also run the install script again.</p>

<h2 id="uninstall">Uninstall</h2>
<ol>
  <li>Remove what <code>pensieve integrate</code> added. Name each add-on that you installed:
{pre("pensieve integrate claude claude-mcp claude-hook --remove")}</li>
  <li>Remove the backend, the index and the app:
{pre("rm -rf ~/.local/share/uv/tools/pensieve ~/.local/bin/pensieve ~/.pensieve ~/Applications/Pensieve.app")}</li>
</ol>
<p>If the app is in <code>/Applications</code>, remove it from there.</p>
""")
