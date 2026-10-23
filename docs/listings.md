# MCP directory listings

This checklist tells where to list the Pensieve MCP server. Each submission publishes text under the account of the person who submits it. Thus the maintainer must do each step. The data is from October 2026.

## Text to paste

Use the same text in each directory.

- **Name:** Pensieve
- **Repository:** https://github.com/TedHaley/pensieve
- **Website:** https://pensieve.tools
- **Short description (98 characters, the registry limit is 100):** Never lose the thread: your agent sessions, git and tickets as threads of work, local on your Mac.
- **Long description:** Pensieve is a free, local Mac app with an MCP server, so you never lose the thread. It keeps a thread for each agent session that wrote something, with its branch, PRs and tickets, the steps left to ship it, and the command that resumes it. Tickets assigned to you from Jira, Linear or Asana become to-dos. In Developer mode it also indexes your documents, git repositories and past agent sessions: agents get the few most relevant files and passages, scoped to the repo that they run in, and see who knows an area of code. MIT license.
- **Categories:** search, developer tools, knowledge and memory
- **Install:** `curl -fsSL https://pensieve.tools/install.sh | sh`, then `pensieve integrate recommended`
- **Client configuration:** the JSON block in the "MCP server" section of the README

## Checklist

| Directory | Submit at | What to give | Who | Status |
| --- | --- | --- | --- | --- |
| Glama | https://glama.ai/mcp/servers, "Add Server" | The repository URL. Then claim the listing with GitHub login. `glama.json` in the repo names the maintainer. | Maintainer | Ready |
| mcp.so | https://mcp.so/submit | Type "MCP Server", the repository URL, the name | Maintainer | Ready |
| MCP Market | https://mcpmarket.com/submit | The repository URL. The free queue takes 4 to 6 weeks. The paid option ($29) takes 24 hours. | Maintainer | Ready |
| Claude plugin directory | https://claude.ai/directory/manage, "Submit new", "Plugin bundle" | Repository `TedHaley/pensieve`, plugin path empty. Run "Validate" first. | Maintainer (paid Claude plan, GitHub connected on claude.ai) | Ready, with reviewer holds (see below) |
| awesome-mcp-servers | Pull request to https://github.com/punkpeye/awesome-mcp-servers | One line in the "Search" or "Knowledge & Memory" section. The list asks for a Glama listing first. | Maintainer | After Glama |
| cursor.directory | https://cursor.directory/mcp, "Submit" | The name, the description, the repository URL, the categories | Maintainer | Ready (form not checked: the site refused the request) |
| Official MCP Registry | `mcp-publisher publish` (https://registry.modelcontextprotocol.io) | `server.json` (draft below) | Maintainer | Blocked: needs a PyPI package |
| PulseMCP | No form. It reads the official MCP Registry. | Nothing | None | Blocked: needs the registry entry. PulseMCP pauses new submissions now. |
| Drio (getdrio.com) | No form found. Its entries mirror the official MCP Registry. | Nothing | None | Blocked: needs the registry entry |
| Smithery | `smithery mcp publish ./pensieve.mcpb` | An `.mcpb` bundle | Maintainer | Blocked: needs an MCPB bundle |
| Claude connectors directory | https://claude.ai/directory/manage, "MCP connector" | A public `https://` URL | None | Not possible: this directory takes only remote servers |

## Claude plugin directory: expected holds

The validator holds these items for a reviewer. A hold does not block the submission.

- The MCP server command is `${HOME}/.local/bin/pensieve`, outside the plugin folder. The plugin needs the Pensieve install first.
- The plugin folder is the repository root. Some files are more than 256 KiB (`uv.lock`, `pensieve/static/app.js`). `site/favicon.ico` is a binary file.
- The hooks send the prompt to `127.0.0.1`. The README section "Privacy" tells what Pensieve sends and where.

## Before the official MCP Registry

The registry takes only a server with a package (npm, PyPI, NuGet, OCI, MCPB) or a public remote URL. Pensieve has none of these today. The install script installs a wheel from inside the app.

The PyPI names `pensieve` and `pensieve-mcp` belong to other projects. `pensieve-tools` is free. To list Pensieve in the registry, do these steps:

1. In `pyproject.toml`, set `name = "pensieve-tools"`. Add the script `pensieve-tools = "pensieve.cli:main"` next to `pensieve`. Then `uvx pensieve-tools mcp` works.
2. Publish the wheel to PyPI. The README is the PyPI description. It holds the line `mcp-name: io.github.TedHaley/pensieve`, which the registry uses to verify the owner.
3. Add `server.json` (below) to the repository root, with the version of the release.
4. Run `mcp-publisher login github`, then `mcp-publisher publish`.

```json
{
  "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
  "name": "io.github.TedHaley/pensieve",
  "title": "Pensieve",
  "description": "Never lose the thread: your agent sessions, git and tickets as threads of work, local on your Mac.",
  "repository": { "url": "https://github.com/TedHaley/pensieve", "source": "github" },
  "websiteUrl": "https://pensieve.tools",
  "version": "0.11.0",
  "packages": [
    {
      "registryType": "pypi",
      "registryBaseUrl": "https://pypi.org",
      "identifier": "pensieve-tools",
      "version": "0.11.0",
      "runtimeHint": "uvx",
      "transport": { "type": "stdio" },
      "packageArguments": [{ "type": "positional", "value": "mcp" }]
    }
  ]
}
```

A server from `uvx` runs without the Mac app. On first run, it downloads the embedding model (about 1.2 GB). It indexes nothing until the user completes setup. The `setup_guide` tool tells the agent the setup steps.

An MCPB bundle is the other route. It also opens Smithery. The registry needs the `.mcpb` file in a GitHub release, with "mcp" in the file name, and its SHA-256 in `server.json`.
