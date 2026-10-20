from pages_a import page, pre, table

H = "#"
INSTR_LINK = '<a href="https://github.com/TedHaley/pensieve/blob/main/pensieve/integrations/instructions.md">instructions block</a>'

CLIENTS = [
    dict(slug="claude-code", name="Claude Code", id="claude-mcp", instr="claude", instr_file="~/.claude/CLAUDE.md",
         cfg="<code>~/.claude.json</code> (through <code>claude mcp add --scope user</code>)",
         skills="a link to <code>~/.agents/skills</code> in <code>~/.claude/skills</code>",
         title="Pensieve MCP server for Claude Code: install and setup",
         desc="Connect Pensieve to Claude Code as an MCP server with one command or the Claude Code plugin. Search "
              "code, past sessions and threads before you grep."),
    dict(slug="codex", name="Codex", id="codex-mcp", instr="codex", instr_file="~/.codex/AGENTS.md",
         cfg="<code>~/.codex/config.toml</code>", skills="<code>~/.agents/skills</code>",
         title="Pensieve MCP server for OpenAI Codex CLI: setup",
         desc="Add Pensieve to the OpenAI Codex CLI as an MCP server in config.toml, with an AGENTS.md block that "
              "tells Codex to search before it greps."),
    dict(slug="cursor", name="Cursor", id="cursor-mcp", instr=None, instr_file=None,
         cfg="<code>~/.cursor/mcp.json</code>", skills="<code>~/.agents/skills</code>",
         title="Pensieve MCP server for Cursor: mcp.json setup",
         desc="Add Pensieve to Cursor as an MCP server in ~/.cursor/mcp.json, and add the search-before-grep rule. "
              "Local code search and threads for the Cursor agent."),
    dict(slug="gemini-cli", name="Gemini CLI", id="gemini-mcp", instr="gemini", instr_file="~/.gemini/GEMINI.md",
         cfg="<code>~/.gemini/settings.json</code>", skills="<code>~/.agents/skills</code>",
         title="Pensieve MCP server for Gemini CLI: settings.json setup",
         desc="Add Pensieve to Gemini CLI as an MCP server in ~/.gemini/settings.json, with a GEMINI.md block that "
              "tells the agent to search before it greps."),
    dict(slug="qwen-code", name="Qwen Code", id="qwen-mcp", instr="qwen", instr_file="~/.qwen/QWEN.md",
         cfg="<code>~/.qwen/settings.json</code>",
         skills="<code>~/.agents/skills</code> (Qwen Code 0.25 and later), or a link in <code>~/.qwen/skills</code> for older versions",
         title="Pensieve MCP server for Qwen Code: settings.json setup",
         desc="Add Pensieve to Qwen Code as an MCP server in ~/.qwen/settings.json, with a QWEN.md block. Pensieve "
              "also indexes your Qwen Code sessions."),
    dict(slug="windsurf", name="Windsurf", id="windsurf-mcp", instr=None, instr_file=None,
         cfg="<code>~/.codeium/windsurf/mcp_config.json</code>", skills="<code>~/.agents/skills</code>",
         title="Pensieve MCP server for Windsurf: mcp_config.json setup",
         desc="Add Pensieve to Windsurf as an MCP server in ~/.codeium/windsurf/mcp_config.json, and add the "
              "search-before-grep rule for the Windsurf agent."),
    dict(slug="claude-desktop", name="Claude Desktop", id="claude-desktop-mcp", instr=None, instr_file=None,
         cfg="<code>~/Library/Application Support/Claude/claude_desktop_config.json</code>", skills=None,
         title="Pensieve MCP server for Claude Desktop: setup on Mac",
         desc="Add Pensieve to the Claude Desktop app as a local MCP server in claude_desktop_config.json. Search "
              "your files, repos, agent sessions and threads from Claude."),
    dict(slug="vs-code", name="VS Code", id="vscode-mcp", instr=None, instr_file=None,
         cfg="<code>~/Library/Application Support/Code/User/mcp.json</code>", skills="<code>~/.agents/skills</code>",
         title="Pensieve MCP server for VS Code: mcp.json setup",
         desc="Add Pensieve to VS Code agent mode as an MCP server in the user mcp.json file. Local code search, "
              "threads and who knows what for GitHub Copilot and other agents."),
]

STD_JSON = '''{
  "mcpServers": {
    "pensieve": {
      "command": "/Users/you/.local/bin/pensieve",
      "args": ["mcp"]
    }
  }
}'''

SNIPPET = {
    "claude-code": None,
    "codex": f'''{H} pensieve:start
[mcp_servers.pensieve]
command = "/Users/you/.local/bin/pensieve"
args = ["mcp"]
{H} pensieve:end''',
    "vs-code": '''{
  "servers": {
    "pensieve": {
      "type": "stdio",
      "command": "/Users/you/.local/bin/pensieve",
      "args": ["mcp"]
    }
  }
}''',
}

EXTRA = {
    "claude-code": f"""
<h2 id="plugin">Or install the Claude Code plugin</h2>
<p>The plugin gives Claude Code the MCP server, the four skills, the instructions block and the four recommended
hooks. It does not change your settings files. Install Pensieve first, because the plugin starts
<code>~/.local/bin/pensieve</code>. Then type these commands in Claude Code:</p>
{pre('''/plugin marketplace add TedHaley/pensieve
/plugin install pensieve@pensieve''')}
<ul>
  <li>Use the plugin or <code>pensieve integrate</code> for Claude Code, not both. When the plugin is installed, <code>pensieve integrate</code> skips the Claude Code MCP server, instructions block, hooks and skill link.</li>
  <li>The plugin's tools have names that start with <code>mcp__plugin_pensieve_pensieve__</code>.</li>
  <li>If the backend uses another port, set <code>PENSIEVE_PORT</code> in the environment that starts Claude Code.</li>
</ul>

<h2 id="hooks">Hooks for Claude Code</h2>
<p><code>pensieve integrate recommended</code> adds four hooks to <code>~/.claude/settings.json</code>:</p>
{table(["Hook", "Event", "What it does"], [
    ["<code>claude-hook</code>", "PostToolUse", "After each Edit or Write, it sends the changed path to Pensieve. The next search finds the new code. It adds about 50 ms to each edit."],
    ["<code>claude-repo-check</code>", "SessionStart", "In a repo that Pensieve does not index, Claude asks you once whether to index it. Pensieve keeps your answer. In a repo that Pensieve indexes, it adds one line: the number of loose ends and the thread closest to done. Claude then offers to close it at a natural pause."],
    ["<code>claude-prompt-search</code>", "UserPromptSubmit", "Pensieve adds what the prompt needs: a thread, your list of threads, who knows the code, past sessions, or the 5 best code matches. It adds about 300 ms and 400 to 1,500 tokens."],
    ["<code>claude-search-gate</code>", "UserPromptSubmit, PostToolUse, PreToolUse", "In a repo that Pensieve indexes, Claude can't run Grep, Glob, <code>grep</code>, <code>rg</code>, <code>ag</code>, <code>find</code> or <code>git grep</code> in a turn until it calls a Pensieve tool. The prompt hook's results count as that call. Filtering output stays allowed. If Pensieve isn't running, nothing is blocked. One session in 5 runs without blocking, as a control group."],
])}
<p>Slash commands, prompts of 1 or 2 words, and repos that Pensieve does not index get nothing from the prompt hook.</p>

<h2 id="manual">Connect by hand</h2>
{pre('''claude mcp add --scope user pensieve -- pensieve mcp      # every project, scope "auto"
claude mcp add pensieve -- pensieve mcp --scope payments     # this folder only, a named scope
claude mcp add --transport http pensieve "http://127.0.0.1:8765/mcp?scope=payments"''')}
<p>Without <code>--scope user</code>, <code>claude mcp add</code> connects only the folder that you run it in.
Claude Code shows the tools as <code>mcp__pensieve__search</code>, <code>mcp__pensieve__read</code> and more.</p>
""",
    "codex": f"""
<p>Pensieve writes its entry between the lines <code>{H} pensieve:start</code> and <code>{H} pensieve:end</code>.
When you remove it, the rest of <code>config.toml</code> stays the same. If <code>config.toml</code> already has a
server named <code>pensieve</code> that Pensieve did not add, Pensieve does not change it.</p>
<p>Pensieve indexes your Codex sessions from <code>~/.codex/sessions</code>. Thus <code>search</code> with
<code>kind:session</code> also finds earlier Codex work.</p>
""",
    "qwen-code": """
<p>Pensieve indexes your Qwen Code sessions from <code>~/.qwen/projects</code>. Thus <code>search</code> with
<code>kind:session</code> also finds earlier Qwen Code work.</p>
""",
    "claude-desktop": """
<p>Claude Desktop does not start the server inside a repo. Thus the scope <code>auto</code> is everything that
Pensieve indexes. To limit it, add <code>"--scope", "payments"</code> to <code>args</code>, with the name of a
scope from Settings → Scopes.</p>
<p>The path above is the macOS path. <code>pensieve integrate</code> looks only there.</p>
""",
    "vs-code": """
<p>VS Code uses the key <code>servers</code>, not <code>mcpServers</code>, and each server has a
<code>type</code>. The path above is the macOS path. <code>pensieve integrate</code> looks only there. On Linux,
add the same entry to the user <code>mcp.json</code> of VS Code by hand.</p>
""",
}


def client_body(c):
    rules = (f"<p>The {INSTR_LINK} goes in <code>{c['instr_file']}</code>, between "
             "<code>&lt;!-- pensieve:start --&gt;</code> markers.</p>") if c["instr"] else (
        f"<p>{c['name']} has no global instructions file that Pensieve writes to. Copy the {INSTR_LINK} into the "
        f"rules of {c['name']}. It tells the agent to call <code>search</code> before it greps.</p>")
    snippet = SNIPPET.get(c["slug"], STD_JSON)
    rows = [["MCP server", f"<code>{c['id']}</code>", c["cfg"]]]
    if c["instr"]:
        rows.append(["Instructions block", f"<code>{c['instr']}</code>", f"<code>{c['instr_file']}</code>"])
    if c["skills"]:
        rows.append(["Skills", "<code>pensieve-search</code>", c["skills"]])
    manual = "" if snippet is None else f"""
<h2 id="manual">Connect by hand</h2>
<p>Add this entry to {c['cfg']}. Use the full path of your <code>pensieve</code> command. To find it, run
<code>which pensieve</code>.</p>
{pre(snippet)}
"""
    ids = c['id'] + ((' ' + c['instr']) if c['instr'] else '')
    return f"""
<h2 id="connect">Connect {c['name']}</h2>
<ol>
  <li>Install Pensieve (see <a href="/docs/install/">Install</a>).</li>
  <li>Run one of these commands:
{pre(f'''pensieve integrate recommended   # every agent on this computer
pensieve integrate {ids} pensieve-search   # only {c['name']}''')}</li>
  <li>Restart {c['name']}. It loads its MCP servers when it starts.</li>
  <li>Ask {c['name']} a question about your code, for example “where do we retry failed webhooks?”.</li>
</ol>

<h2 id="what-changes">What Pensieve adds</h2>
{table(["Add-on", "Id", "File"], rows)}
{rules}
{manual}
{EXTRA.get(c['slug'], '')}
<h2 id="remove">Remove</h2>
{pre(f"pensieve integrate {ids} --remove")}
<p>Pensieve removes only the entries that it added.</p>
<p>See also: <a href="/docs/agents/#scope">scopes</a>, the <a href="/docs/mcp-tools/">MCP tools</a>, and the
<a href="/docs/benchmark/">benchmark</a>.</p>
"""


AGENT_ROWS = [[f'<a href="/docs/agents/{c["slug"]}/">{c["name"]}</a>', f"<code>{c['id']}</code>", c["cfg"],
               f"<code>{c['instr_file']}</code>" if c["instr"] else "Copy into the agent's rules"] for c in CLIENTS]

page("agents", "Connect your agent",
     "Connect Pensieve to your AI coding agent over MCP",
     "Connect Pensieve to Claude Code, Codex, Cursor, Gemini CLI, Qwen Code, Windsurf, Claude Desktop or VS Code as "
     "an MCP server. One command, scopes, skills, hooks and the instructions block.",
     "Connect your agent",
     "This page tells you how to give your coding agents Pensieve as an MCP server, and what each add-on does.",
     f"""
<h2 id="one-command">One command for every agent</h2>
{pre("pensieve integrate recommended")}
<p>This command finds the agents on your computer. An agent counts as installed when its settings folder exists.
For each agent, the command adds:</p>
<ul>
  <li>Pensieve as an MCP server, for every project.</li>
  <li>The instructions block, where the agent has a global instructions file. It says to search with Pensieve before grep, and to offer to close the thread closest to done.</li>
  <li>The <code>pensieve-search</code> skill.</li>
  <li>For Claude Code, three hooks (see <a href="/docs/agents/claude-code/#hooks">Claude Code</a>).</li>
</ul>
<p>Restart each agent that is open. To see what is available and what is installed, run
<code>pensieve integrate</code> with no items.</p>

<h2 id="clients">Each agent</h2>
{table(["Agent", "MCP id", "MCP settings file", "Instructions"], AGENT_ROWS)}
<p>Any other MCP client can use Pensieve too. It speaks stdio through <code>pensieve mcp</code>, and streamable HTTP
at <code>http://127.0.0.1:8765/mcp</code>. <code>pensieve mcp</code> starts the backend if it is not running.</p>
{pre('{"mcpServers": {"pensieve": {"command": "pensieve", "args": ["mcp"]}}}')}

<h2 id="scope">Scopes</h2>
<p>Each agent connection works in a scope: a part of the index. Every tool obeys the scope. Pensieve refuses an item
outside it.</p>
<ul>
  <li><strong>auto</strong> is the default for <code>pensieve mcp</code>. It uses the folder that the agent started in. If a named scope includes that repo, the agent gets that scope. If not, it gets the repo, its worktrees and the agent sessions in it. Outside a repo, <code>auto</code> is everything.</li>
  <li><strong>Named scopes</strong> are lists of sources, for example the repos and documents of one project. Make them in Settings → Scopes. A scope only filters the index, so nothing is indexed twice.</li>
  <li><strong>HTTP connections</strong> cannot see the agent's folder. Give a scope in the URL: <code>?scope=payments</code>. With no scope, an HTTP connection gets everything.</li>
</ul>
{pre('''pensieve mcp --scope payments     # stdio, one named scope
pensieve mcp --all-tools          # also the tools that change Pensieve's settings''')}
<p>In a repo that Pensieve does not index, <code>search</code> tells the agent to use grep. It does not return code
from your other repos.</p>

<h2 id="add-ons">Add-ons</h2>
<p>Agents grep by habit, also with Pensieve connected. The add-ons make them use it. Install them from
Settings → Agents → Agent add-ons, or with <code>pensieve integrate</code>.</p>
{table(["Id", "What it does", "Recommended"], [
    ["<code>claude-mcp</code>, <code>codex-mcp</code>, <code>cursor-mcp</code>, <code>gemini-mcp</code>, <code>qwen-mcp</code>, <code>windsurf-mcp</code>, <code>claude-desktop-mcp</code>, <code>vscode-mcp</code>", "Pensieve as the agent's MCP server, for every project. In each settings file, Pensieve changes only its own entry.", "Yes"],
    ["<code>claude</code>, <code>codex</code>, <code>gemini</code>, <code>qwen</code>", f"The {INSTR_LINK} in the agent's global instructions file. It tells the agent to call Pensieve <code>search</code> before Grep or Glob for each code lookup. It also tells the agent to call <code>threads</code> at the start of a session and at natural pauses, and to offer to close the thread closest to done.", "Yes"],
    ["<code>pensieve-search</code>", "A skill: query styles, when to use grep, similar code before new code, past sessions.", "Yes"],
    ["<code>pensieve-teammates</code>", "A skill: who knows an area, from git blame, and a draft question. It never sends a message.", "No"],
    ["<code>pensieve-plan</code>", "A skill: plans for new code list code to reuse, earlier attempts, people to ask and knowledge risks.", "No"],
    ["<code>pensieve-wrap-up</code>", "A skill: gets the threads closest to done and does the steps that close them in git, GitHub and the tracker. It asks before a merge or a close.", "No"],
    ["<code>claude-hook</code>, <code>claude-repo-check</code>, <code>claude-prompt-search</code>, <code>claude-search-gate</code>", 'Claude Code hooks (see <a href="/docs/agents/claude-code/#hooks">Claude Code</a>). The search gate makes Claude search Pensieve before it greps.', "Yes"],
])}
<p>Skills go once into <code>~/.agents/skills</code>, the user skills folder that many agents read. Claude Code reads
only its own folder, so it gets a link in <code>~/.claude/skills</code>. Pensieve makes no folder for an agent that
is not installed.</p>
{pre('''pensieve integrate cursor-mcp codex                  # only these
pensieve integrate pensieve-teammates pensieve-plan  # the optional skills
pensieve integrate claude --remove                   # removes exactly what was added''')}
""")

for c in CLIENTS:
    page(f"agents/{c['slug']}", c["name"], c["title"], c["desc"], f"Pensieve for {c['name']}",
         f"This page tells you how to connect {c['name']} to Pensieve over MCP, and what changes on your computer.",
         client_body(c), crumb="Connect your agent")
