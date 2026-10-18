"""Connections: MCP servers whose data Pensieve collects, read-only, and indexes like a folder.

There are three kinds of server. Pensieve finds the first two by itself, whatever the generation model is:
- engine: the MCP servers of Claude Code, when it is on this Mac. This includes its config, its plugins and the
  claude.ai connectors of the account. Pensieve reads the list from Claude Code's startup event. To collect, it runs
  Claude Code headless with only the read tools of one server allowed.
- agent: the servers in the MCP config of the other agents (Codex, Cursor, Gemini CLI, Qwen Code, Windsurf, Claude
  Desktop, VS Code), with the credentials written there. A server that Claude Code also has is left to Claude Code.
- own: the servers in the `mcp_servers` setting, with the credentials given there.
For agent and own servers, Pensieve connects itself, asks the generation model which read calls to make, and makes
them.

Collected items are written as Markdown to ~/.pensieve/connections/<server>/, and the file indexer indexes them. The
raw JSON of each item is in .raw/ and its field changes over time are in .history/ (hidden folders: not indexed).
Pensieve calls only tools that `is_read` accepts. A source is off until the user switches it on."""
import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pensieve import config, connectors, llm, mcp_oauth, settings

DIR = config.DATA_DIR / "connections"
STATE = DIR / "state.json"
SESSIONS = "agent_sessions"  # the source of MCP results found in agent transcripts
GH = "github_cli"  # GitHub issues and pull requests that involve the user, read with the gh command line
_lock = threading.Lock()

READ = {"get", "list", "search", "read", "fetch", "lookup", "find", "query", "describe", "view", "show", "retrieve",
        "browse", "whoami", "count"}
WRITE = {"create", "add", "update", "edit", "delete", "remove", "send", "post", "transition", "set", "put", "patch",
         "move", "upload", "schedule", "trash", "untrash", "archive", "unarchive", "mark", "unmark", "label", "unlabel",
         "apply", "assign", "unassign", "share", "merge", "close", "reopen", "invite", "reply", "forward", "rerun",
         "run", "execute", "exec", "restore", "upsert", "manage", "use", "submit", "authenticate", "complete",
         "recover", "write", "insert", "modify", "rename", "publish", "approve", "reject", "cancel", "start", "stop",
         "trigger", "enable", "disable", "toggle", "react", "pin", "unpin", "star", "unstar", "subscribe",
         "unsubscribe", "comment", "import", "sync", "invoke", "deploy", "rollback", "revoke", "grant", "draft",
         "save", "clear", "reset", "kill", "drop", "purge", "resolve", "convert", "duplicate", "copy", "login",
         "logout", "register", "accept", "decline", "book", "pay", "order", "transfer", "refund", "charge", "notify"}
_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


def words(name: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(name)]


def is_read(name: str, annotations=None) -> bool:
    """A tool that only reads. The name must hold a read verb and no write verb. A server's own hints can only make
    the rule stricter: `readOnlyHint: false` or `destructiveHint: true` makes a tool a write tool."""
    a = annotations if isinstance(annotations, dict) else (annotations.model_dump() if annotations else {})
    if a.get("readOnlyHint") is False or a.get("destructiveHint") is True:
        return False
    w = words(name)
    return not WRITE.intersection(w) and bool(READ.intersection(w))


def read_tool(server: str, name: str, annotations=None) -> bool:
    """`is_read`, and also a tool that the server's connector profile knows as a read tool although its name fails
    the verb rule (Notion's `API-post-search`). The server's own hints can still make it a write tool."""
    if is_read(name, annotations):
        return True
    a = annotations if isinstance(annotations, dict) else (annotations.model_dump() if annotations else {})
    if a.get("readOnlyHint") is False or a.get("destructiveHint") is True:
        return False
    return connectors.known_read(connectors.match(server), name)


PRIVATE = {"private", "dm", "dms", "im", "mpim", "direct"}


def private_tool(name: str) -> bool:
    """A tool that can read direct messages or private channels (slack_search_public_and_private, get_dm_history):
    every item it finds counts as private."""
    return bool(PRIVATE.intersection(words(name)))


CHAT = re.compile(r"slack|teams|chat|discord", re.I)
# How Slack's own MCP server marks a direct or group message in its text results: "Channel: DM (ID: D…)", a
# Participants line, an mpdm- channel, or a D… or G… id in a channel or permalink
_PRIVATE_TEXT = re.compile(r"^\W*Channel:\s*DM\b|^\W*Participants:|\bmpdm-|\(ID: [DG][A-Z0-9]{8,}\)|"
                           r"/archives/[DG][A-Z0-9]{8,}\b", re.M)
_PRIVATE_TYPES = {"im", "mpim", "private", "private_channel", "group"}


def _strings(x, depth=0):
    if depth > 6:
        return
    if isinstance(x, str):
        yield x.replace("\\n", "\n")
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v, depth + 1)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v, depth + 1)


def private_item(d: dict, chat: bool) -> bool:
    """An item from a direct message, a group message or a private channel, by its fields (is_im, is_private,
    channel_type, a D… or G… channel id). A chat server's item (`chat`) is also checked by its text, where Slack's
    MCP server says DM."""
    if any(d.get(k) is True for k in ("is_im", "is_mpim", "is_private", "is_group")):
        return True
    if str(d.get("channel_type") or "").lower() in _PRIVATE_TYPES:
        return True
    ch = d.get("channel")
    cid = ch.get("id") if isinstance(ch, dict) else ch if isinstance(ch, str) else d.get("channel_id")
    if isinstance(cid, str) and re.fullmatch(r"[DG][A-Z0-9]{8,}", cid):
        return True
    if isinstance(ch, dict) and (any(ch.get(k) is True for k in ("is_im", "is_mpim", "is_private", "is_group")) or
                                 str(ch.get("type") or "").lower() in _PRIVATE_TYPES):
        return True
    return chat and any(_PRIVATE_TEXT.search(s) for s in _strings(d))


def slug(server_name: str) -> str:
    """The server part of Claude Code's tool names: 'claude.ai Atlassian' -> 'claude_ai_Atlassian'."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", server_name)


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save(st: dict):
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(STATE)


def _update(fn):
    with _lock:
        st = _load()
        fn(st)
        _save(st)
        return st


def claude_code() -> bool:
    """Whether Claude Code is on this Mac. Its MCP servers are listed and collected from whatever the generation model
    is: Pensieve runs it headless, with only the read-only tools of one server at a time."""
    return bool(llm._exe("claude"))


def _engine_model() -> str:
    """The Claude model for collection: the one in the command-line template when that runs Claude Code, else Haiku."""
    argv = llm._argv(settings.get("cli_command"))
    if not argv or Path(argv[0]).name != "claude":
        return "haiku"
    return argv[argv.index("--model") + 1] if "--model" in argv[:-1] else "haiku"


def servers() -> list[dict]:
    """Every known server: {id, name, origin, status, tools: [{name, read}], collected, items, error}."""
    st = _load()
    out = []
    if claude_code():
        out += [{**r, "id": k, "origin": "engine"} for k, r in st.get("engine", {}).items() if k != "pensieve"]
    seen = {r["id"] for r in out} | {slug(c["name"]) for c in settings.get("mcp_servers")}
    out += [{**r, "id": k, "origin": "agent"} for k, r in st.get("agents", {}).items() if k not in seen]
    for c in settings.get("mcp_servers"):
        k = slug(c["name"])
        r = st.get("own", {}).get(k, {})
        out.append(dict(id=k, name=c["name"], origin="own", status=r.get("status", "unknown"), tools=r.get("tools", []),
                        error=r.get("error"), oauth=bool(c.get("oauth"))))
    gh = st.get("gh")
    if gh:
        out.append(dict(id=GH, name="GitHub (gh)", origin="cli", status=gh["status"], error=gh.get("error"),
                        tools=[dict(name=t, read=True) for t in ("gh search issues", "gh api comments", "gh api reviews")]))
    runs = st.get("runs", {})
    for r in out:
        r.update(runs.get(r["id"], {}))
        r["items"] = _count(r["id"])
    return out


def _count(sid: str) -> int:
    d = DIR / sid
    return sum(1 for _ in d.glob("*.md")) if d.is_dir() else 0


# Headless Claude Code starts before slow MCP servers connect, and lists them as pending with no tools; this makes it
# wait for them.
_WAIT_FOR_MCP = {**os.environ, "MCP_CONNECTION_NONBLOCKING": "false"}


def _claude_init(timeout=90) -> dict:
    """Claude Code's startup event in an empty folder: the MCP servers with their status, and every tool name. The
    process stops at that event, before the model answers."""
    exe = llm._exe("claude")
    if not exe:
        raise llm.Offline("Can't find claude")
    with tempfile.TemporaryDirectory(prefix=llm.OWN_PREFIX) as tmp:
        p = subprocess.Popen([exe, "-p", "--model", _engine_model(), "--output-format", "stream-json", "--verbose",
                              "--tools", "", "--max-turns", "1"], cwd=tmp, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=_WAIT_FOR_MCP)
        timer = threading.Timer(timeout, p.kill)
        timer.start()
        try:
            p.stdin.write("Reply ok")
            p.stdin.close()
            for line in p.stdout:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") == "system" and d.get("subtype") == "init":
                    return d
        finally:
            timer.cancel()
            p.kill()
            p.wait()
    raise RuntimeError("Claude Code ended before its startup event")


def discover_engine() -> list[dict]:
    """Read Claude Code's MCP servers and tools. A server that is still connecting keeps the tools seen last time."""
    if not claude_code():
        return []
    d = _claude_init()
    tools = {}
    for t in d.get("tools", []):
        if t.startswith("mcp__"):
            _, srv, name = t.split("__", 2)
            tools.setdefault(srv, []).append(name)

    def apply(st):
        old = st.get("engine", {})
        new = {}
        for s in d.get("mcp_servers", []):
            k = slug(s["name"])
            names = tools.get(k) or [t["name"] for t in old.get(k, {}).get("tools", [])]
            new[k] = dict(name=s["name"], status=s.get("status", "unknown"),
                          tools=[dict(name=n, read=read_tool(s["name"], n)) for n in names], at=time.time())
        st["engine"] = new
    _update(apply)
    return servers()


@asynccontextmanager
async def _open(cfg: dict, on_url=None):
    """An MCP client session to one of the user's own servers: `url` (streamable HTTP, with `headers`, or with
    `oauth` sign-in) or `command` (stdio, with `args` and `env`). `on_url` gets the sign-in page when the user started
    a sign-in (mcp_oauth)."""
    from mcp import ClientSession
    if cfg.get("url"):
        from mcp.client import streamable_http as h
        auth = mcp_oauth.provider(slug(cfg["name"]), cfg, on_url) if cfg.get("oauth") else None
        if hasattr(h, "streamable_http_client"):  # mcp 2: headers and auth go on the HTTP client
            async with h.create_mcp_http_client(headers=cfg.get("headers") or None, auth=auth) as http, \
                    h.streamable_http_client(cfg["url"], http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as s:
                    await s.initialize()
                    yield s
        else:
            async with h.streamablehttp_client(cfg["url"], headers=cfg.get("headers") or None, auth=auth) as streams:
                async with ClientSession(streams[0], streams[1]) as s:
                    await s.initialize()
                    yield s
    else:
        from mcp.client.stdio import StdioServerParameters, stdio_client
        params = StdioServerParameters(command=cfg["command"], args=cfg.get("args") or [],
                                       env={**os.environ, **(cfg.get("env") or {})})
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                yield s


async def _own_tools(cfg: dict, name: str, on_url=None) -> list[dict]:
    async with _open(cfg, on_url) as s:
        r = await s.list_tools()
        return [dict(name=t.name, read=read_tool(name, t.name, t.annotations)) for t in r.tools]


def _own_status(c: dict, on_url=None, timeout=60) -> dict:
    """Connect to one of the user's own servers and list its tools: the server's record in the state."""
    try:
        tools = asyncio.run(asyncio.wait_for(_own_tools(c, c["name"], on_url), timeout))
        return dict(name=c["name"], status="connected", tools=tools, error=None, at=time.time())
    except Exception as e:
        signin = _cause(e, mcp_oauth.NeedsSignIn)
        return dict(name=c["name"], status="needs-auth" if signin else "failed", tools=[],
                    error=_err(signin or e), at=time.time())


def _cause(e: BaseException | None, kind):
    """The first exception of `kind` in an exception, its groups and its causes."""
    seen = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, kind):
            return e
        if isinstance(e, BaseExceptionGroup):
            for x in e.exceptions:
                if (f := _cause(x, kind)):
                    return f
        e = e.__cause__ or e.__context__
    return None


def discover_own() -> list[dict]:
    """Connect to each server in `mcp_servers` and list its tools."""
    found = {slug(c["name"]): _own_status(c) for c in settings.get("mcp_servers")}
    _update(lambda st: st.__setitem__("own", found))
    return servers()


def sign_in(sid: str) -> dict:
    """Start the OAuth sign-in of one of the user's own servers: {url} of the sign-in page, {done}, or {error}.
    When the user comes back from the sign-in page, the server's tools are listed and saved."""
    c = next((c for c in settings.get("mcp_servers") if slug(c["name"]) == sid and c.get("oauth")), None)
    if not c:
        raise ValueError(f"no server {sid!r} that signs in with OAuth")

    def run(on_url):
        r = _own_status(c, on_url, timeout=mcp_oauth.WAIT_SECONDS + 60)
        _update(lambda st: st.setdefault("own", {}).__setitem__(sid, r))
        if r["status"] != "connected":
            raise RuntimeError(r["error"])
    return mcp_oauth.start(sid, run)


def discover_gh() -> list[dict]:
    """Whether gh can read GitHub as the user. Nothing is listed when gh is not installed."""
    ok, why = connectors.gh_ready()
    if ok or why != "gh is not installed":
        _update(lambda st: st.__setitem__("gh", dict(status="connected" if ok else "needs-auth", error=why or None,
                                                     at=time.time())))
    else:
        _update(lambda st: st.pop("gh", None))
    return servers()


def _server_cfg(c: dict) -> dict | None:
    """One server entry of an agent's config, in the form `_open` takes. Agents name the fields differently."""
    url = c.get("url") or c.get("serverUrl") or c.get("httpUrl")
    if url:
        headers = dict(c.get("headers") or c.get("http_headers") or {})
        token = c.get("bearer_token_env_var")  # Codex: the token is in an environment variable
        if token and os.environ.get(token):
            headers["Authorization"] = "Bearer " + os.environ[token]
        return dict(url=url, headers=headers)
    if c.get("command"):  # the app's PATH misses ~/.local/bin, Homebrew and the like
        return dict(command=llm._exe(c["command"]) or c["command"], args=[str(a) for a in c.get("args") or []],
                    env={k: str(v) for k, v in (c.get("env") or {}).items()})
    return None


def agent_configs() -> dict[str, dict]:
    """The MCP servers in the other agents' config files, by id: {name, agents: [label], cfg}. Pensieve is left out."""
    import tomllib

    from pensieve import integrations
    out = {}
    for item, (label, f, key, _) in integrations.MCP.items():
        if item == "claude-mcp" or not f.exists():  # Claude Code's servers come from its startup event
            continue
        try:
            text = f.read_text()
            table = (tomllib.loads(text).get("mcp_servers") if f.suffix == ".toml" else
                     json.loads(text or "{}").get(key or "mcpServers")) or {}
        except (OSError, ValueError):
            continue
        for name, c in table.items():
            cfg = _server_cfg(c) if isinstance(c, dict) else None
            if not cfg or name == "pensieve" or (cfg.get("args") or [None])[0] == "mcp" and \
                    Path(cfg.get("command") or "").name == "pensieve":
                continue
            r = out.setdefault(slug(name), dict(name=name, agents=[], cfg=cfg))
            if label not in r["agents"]:
                r["agents"].append(label)
    return out


def discover_agents() -> list[dict]:
    """Connect to each server in the other agents' configs that Claude Code does not have, and list its tools."""
    skip = set(_load().get("engine", {})) if claude_code() else set()
    found = {}
    for k, a in agent_configs().items():
        if k in skip:
            continue
        try:
            tools = asyncio.run(asyncio.wait_for(_own_tools(a["cfg"], a["name"]), 60))
            found[k] = dict(name=a["name"], agents=a["agents"], status="connected", tools=tools, error=None, at=time.time())
        except Exception as e:
            found[k] = dict(name=a["name"], agents=a["agents"], status="failed", tools=[], error=_err(e), at=time.time())
    _update(lambda st: st.__setitem__("agents", found))
    return servers()


def _err(e: BaseException) -> str:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:  # anyio wraps errors from the transport
        e = e.exceptions[0]
    return (str(e) or type(e).__name__)[:300]


def discover() -> list[dict]:
    errors = []
    for fn in (discover_engine, discover_agents, discover_own, discover_gh):
        try:
            fn()
        except Exception as e:
            errors.append(_err(e))
    _update(lambda st: st.update(discovered=time.time(), discover_error="; ".join(errors) or None))
    return servers()


PROMPT = """You collect data for the user's personal search index, with the {name} tools only.
Find the items that the user created, owns, is assigned to, took part in, or is mentioned in, and that changed
since {since}. Then fetch the full details of the most useful ones: the description, comments and status.
Make at most {calls} tool calls. Do not summarize, and do not change anything: Pensieve keeps the tool results.
When you are done, reply DONE.{hint}"""


def _since_time(sid: str) -> datetime:
    """Where a collection starts: an hour before the last one, or `connections_days` back for the first one."""
    last = _load().get("runs", {}).get(sid, {}).get("collected")
    return datetime.fromtimestamp(last, timezone.utc) - timedelta(hours=1) if last else \
        datetime.now(timezone.utc) - timedelta(days=settings.get("connections_days"))


def _since(sid: str) -> str:
    return _since_time(sid).strftime("%Y-%m-%dT%H:%MZ")


def _prompt(r: dict, calls: int) -> str:
    h = connectors.hint(connectors.match(r["name"], r["id"]), _since_time(r["id"]))
    return PROMPT.format(name=r["name"], since=_since(r["id"]), calls=calls, hint=f"\n{h}" if h else "")


_SAVED = re.compile(r"Output has been saved to (\S+?\.txt)")


def _result_text(content) -> str:
    if isinstance(content, list):
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    text = content if isinstance(content, str) else ""
    m = _SAVED.search(text)  # Claude Code writes a large result to a file and sends its path instead
    if m and Path(m.group(1)).resolve().is_relative_to(Path.home() / ".claude"):
        try:
            return Path(m.group(1)).read_text()
        except OSError:
            pass
    return text


def _collect_engine(r: dict, calls: int, prompt: str | None = None, only=None) -> list[tuple]:
    """Run Claude Code headless with only the read tools of one server (and of those, only the ones `only` keeps)."""
    ok = [t for t in r["tools"] if t["read"] and (only is None or only(t["name"]))]
    reads = [f"mcp__{r['id']}__{t['name']}" for t in ok]
    if not reads:
        raise RuntimeError("no read-only tools")
    deny = [f"mcp__{r['id']}__{t['name']}" for t in r["tools"] if t not in ok]
    deny += [f"mcp__{k}" for k in _load().get("engine", {}) if k != r["id"]]  # and every other server, whole
    exe = llm._exe("claude")
    if not exe:
        raise llm.Offline("Can't find claude")
    argv = [exe, "-p", "--model", _engine_model(), "--output-format", "stream-json", "--verbose", "--tools", "",
            "--max-turns", str(calls + 2), "--allowedTools", ",".join(reads), "--disallowedTools", ",".join(deny)]
    prompt = prompt or _prompt(r, calls)
    with tempfile.TemporaryDirectory(prefix=llm.OWN_PREFIX) as tmp:
        p = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=900, cwd=tmp, env=_WAIT_FOR_MCP)
    uses, out = {}, []
    for line in p.stdout.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        content = (d.get("message") or {}).get("content")
        for b in content if isinstance(content, list) else []:
            if b.get("type") == "tool_use":
                uses[b["id"]] = (b["name"].split("__", 2)[-1], b.get("input") or {})
            elif b.get("type") == "tool_result" and not b.get("is_error") and b.get("tool_use_id") in uses:
                tool, args = uses[b["tool_use_id"]]
                if any(t["name"] == tool for t in ok):
                    out.append((tool, args, _result_text(b.get("content"))))
        if d.get("type") == "result":
            if d.get("is_error") and not out:
                raise RuntimeError(str(d.get("result") or d.get("subtype"))[:300])
            u = d.get("usage") or {}
            r["_cost"] = dict(cost_usd=d.get("total_cost_usd"), tokens=sum(u.get(k) or 0 for k in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")))
    if p.returncode and not out:
        raise RuntimeError((p.stderr or p.stdout).strip()[-300:] or f"claude exited {p.returncode}")
    return out


PLAN_SCHEMA = {"type": "object", "properties": {"calls": {"type": "array", "items": {
    "type": "object", "properties": {"tool": {"type": "string"}, "args_json": {"type": "string"}},
    "required": ["tool", "args_json"], "additionalProperties": False}}},
    "required": ["calls"], "additionalProperties": False}


async def _collect_own_async(cfg: dict, r: dict, calls: int) -> list[tuple]:
    """Collect from a server that Pensieve connects to itself. When the server's connector profile has a recipe whose
    tools the server has, Pensieve makes those calls; else the generation model plans the calls. Then Pensieve
    makes the follow-up call for each item found: the profile's (a Slack thread, a GitHub discussion), or the
    server's get tool."""
    out = []
    pid = connectors.match(r["name"], r["id"])
    async with _open(cfg) as s:
        tools = [t for t in (await s.list_tools()).tools if read_tool(r["name"], t.name, t.annotations)]
        if not tools:
            raise RuntimeError("no read-only tools")
        names = {t.name for t in tools}

        async def call(tool, args) -> bool:
            if tool not in names:  # never a tool that is not on the read-only list
                return False
            try:
                res = await s.call_tool(tool, args)
            except Exception:
                return False
            if _attr(res, "is_error"):
                return False
            text = "\n".join(getattr(b, "text", "") for b in res.content)
            if _attr(res, "structured_content") and not text:
                text = json.dumps(_attr(res, "structured_content"))
            out.append((tool, args, text))
            return True
        recipe = [(t, connectors.fill(a, _since_time(r["id"]))) for t, a in
                  (connectors.PROFILES[pid]["recipe"] if pid else []) if t in names]
        done = [await call(t, a) for t, a in recipe[:calls]]
        if not any(done):
            menu = "\n".join(f"- {t.name}: {(t.description or '')[:300]} Input schema: "
                             f"{json.dumps(_attr(t, 'input_schema'))[:600]}" for t in tools)
            prompt = (_prompt(r, calls) + f"\n\nThe tools:\n{menu}\n\n"
                      "Plan only the search and list calls whose arguments you know now. Pensieve then fetches the "
                      "details of each item that they find. Give each call's arguments as a JSON object in args_json. "
                      "Do not call any tool yourself: answer only with the plan.")
            plan = await asyncio.to_thread(llm.complete, prompt, max_tokens=1500, schema=PLAN_SCHEMA,
                                           kind="collection plan")
            for c in (plan or {}).get("calls", [])[:calls]:
                try:
                    args = json.loads(c.get("args_json") or "{}")
                except ValueError:
                    continue
                if isinstance(args, dict) and "<" not in json.dumps(args):  # "<" marks a placeholder, not a value
                    await call(c.get("tool"), args)
        found = {}
        for tool, args, text in list(out):
            data = _json(text)
            for i in connectors.parse(pid, tool, args, data) or split(data):
                found.setdefault(str(i.get(_id_key(i))), i)
        getter = _getter(tools)
        for item in list(found.values())[:max(0, calls * 2 - len(out))]:
            nxt = connectors.follow(pid, item, names)
            if not nxt and getter and not (pid and connectors.PROFILES[pid].get("follow")):
                tool, param = getter
                value = item.get(param) or item.get(_id_key(item))
                nxt = (tool, {param: value}) if isinstance(value, (str, int)) else None
            if nxt:
                await call(*nxt)
    return out


def _json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


def _getter(tools) -> tuple[str, str] | None:
    """A read tool that fetches one item by its id: a get, read or fetch verb and one required argument."""
    for t in tools:
        schema = _attr(t, "input_schema") or {}
        req = schema.get("required") or []
        if len(req) == 1 and {"get", "read", "fetch"}.intersection(words(t.name)) and \
                (schema.get("properties", {}).get(req[0], {}).get("type") in ("string", "integer", None)):
            return t.name, req[0]
    return None


def _attr(x, snake: str):
    """A field of an MCP SDK object: snake_case in mcp 2, camelCase in mcp 1."""
    camel = re.sub(r"_(\w)", lambda m: m.group(1).upper(), snake)
    return getattr(x, snake, getattr(x, camel, None))


def collect(sid: str, calls: int = 8) -> dict:
    """Collect from one server now. Returns its run record."""
    r = next((x for x in servers() if x["id"] == sid), None)
    if not r:
        raise ValueError(f"unknown connection {sid!r}")
    t0 = time.time()
    try:
        if r["origin"] == "engine":
            results = _collect_engine(r, calls)
        elif r["origin"] == "cli":
            results = connectors.collect_gh(_since(sid))
        elif r["origin"] == "agent":
            cfg = agent_configs()[sid]["cfg"]
            results = asyncio.run(asyncio.wait_for(_collect_own_async(cfg, r, calls), 900))
        else:
            cfg = next(c for c in settings.get("mcp_servers") if slug(c["name"]) == sid)
            results = asyncio.run(asyncio.wait_for(_collect_own_async(cfg, r, calls), 900))
        n = sum(store(sid, r["name"], tool, args, text) for tool, args, text in results)
        run = dict(collected=t0, error=None, last_items=n, calls=len(results), **r.get("_cost", {}))
    except Exception as e:
        run = dict(collected=_load().get("runs", {}).get(sid, {}).get("collected"), error=_err(e), failed=t0)
    _update(lambda st: st.setdefault("runs", {}).__setitem__(sid, run))
    return run


_USER_WORDS = {"user", "users", "profile", "profiles", "member", "people", "person", "whoami"}
_NOT_USER_WORDS = {"channel", "channels", "message", "messages", "conversation", "conversations", "thread", "group"}


def user_tool(name: str) -> bool:
    """A read tool that looks up people (slack_read_user_profile, slack_search_users, users_info), not messages."""
    w = set(words(name))
    return is_read(name) and bool(_USER_WORDS & w) and not _NOT_USER_WORDS & w


LOOKUP = """Look up user profiles with the {name} tools only. Pensieve keeps the tool results.
First look up the profile of the current user: call the profile tool with no user id.
Then look up the profile of each of these user ids: {ids}.
Make at most {calls} tool calls. Do not summarize, and do not change anything. When you are done, reply DONE."""


def lookup_users(sid: str, ids: list[str], calls: int = 30) -> list[tuple]:
    """The profiles (name, title, picture) of some users of a chat server, with its read-only user tools only:
    [(tool, args, result text)]. The first call has no user id: it gives the user's own profile."""
    r = next((x for x in servers() if x["id"] == sid), None)
    if not r:
        raise ValueError(f"unknown connection {sid!r}")
    ids = [i for i in ids if re.fullmatch(r"[A-Z0-9]{6,}", i)][:calls - 1]
    if r["origin"] == "engine":
        return _collect_engine(r, calls, LOOKUP.format(name=r["name"], ids=", ".join(ids) or "(none)", calls=calls),
                               only=user_tool)
    cfg = agent_configs()[sid]["cfg"] if r["origin"] == "agent" else \
        next(c for c in settings.get("mcp_servers") if slug(c["name"]) == sid)
    return asyncio.run(asyncio.wait_for(_lookup_own(cfg, ids), 300))


async def _lookup_own(cfg: dict, ids: list[str]) -> list[tuple]:
    out = []
    async with _open(cfg) as s:
        tools = [t for t in (await s.list_tools()).tools if is_read(t.name, t.annotations) and user_tool(t.name)]
        pick = None
        for t in tools:  # a tool that reads one user by id: user_id, user or id
            schema = _attr(t, "input_schema") or {}
            param = next((p for p in ("user_id", "userId", "user", "id") if p in schema.get("properties", {})), None)
            if param and {"get", "read", "fetch", "lookup", "view", "show"} & set(words(t.name)):
                pick = (t.name, param, param not in (schema.get("required") or []))
                break
        if not pick:
            return out
        tool, param, optional = pick
        for args in ([{}] if optional else []) + [{param: i} for i in ids]:
            try:
                res = await s.call_tool(tool, args)
            except Exception:
                continue
            if not _attr(res, "is_error"):
                text = "\n".join(getattr(b, "text", "") for b in res.content)
                out.append((tool, args, text or json.dumps(_attr(res, "structured_content") or {})))
    return out


_PREFER = {"own": 0, "cli": 1, "agent": 2, "engine": 3}


def covered(found: list[dict] | None = None) -> dict[str, str]:
    """Switched-on servers that another switched-on server already collects for the same connector: {sid: keeper}.
    One connector is collected once. The keeper is the server added here, then gh, then another agent's config, then
    Claude Code, which costs a model run for each collection. Only a connected server can be the keeper."""
    groups = {}
    for r in found if found is not None else servers():
        pid = connectors.match(r["name"], r["id"])
        if pid and settings.enabled(f"connections/{r['id']}"):
            groups.setdefault(pid, []).append(r)
    out = {}
    for rs in groups.values():
        live = sorted((r for r in rs if r["status"] == "connected"), key=lambda r: _PREFER.get(r["origin"], 9))
        if live:
            out.update({r["id"]: live[0]["id"] for r in rs if r["id"] != live[0]["id"]})
    return out


def due() -> list[str]:
    """Switched-on servers whose last collection is older than `connections_every_hours`. A server that another one
    covers (see `covered`) is never due."""
    every = settings.get("connections_every_hours") * 3600
    if not every:
        return []
    runs = _load().get("runs", {})

    def last(sid):
        r = runs.get(sid, {})
        return max(r.get("collected") or 0, r.get("failed") or 0)
    found = servers()
    skip = covered(found)
    return [r["id"] for r in found if settings.enabled(f"connections/{r['id']}") and r["status"] == "connected"
            and r["id"] not in skip and time.time() - last(r["id"]) > every]


_KEYS = ("key", "identifier", "gid", "number", "ts", "id", "url", "permalink", "webUrl")
_VOLATILE = {"updated", "updated_at", "updatedAt", "modified", "lastModified", "last_modified", "edited_at"}
_NOISE = re.compile(r"avatar|icon|^self$|_links|thumbnail|^expand$|schema|^context$|featureflags|^etag$", re.I)


def _id_key(d: dict):
    return next((k for k in _KEYS if isinstance(d.get(k), (str, int)) and str(d.get(k))), None)


def split(data) -> list[dict]:
    """The items in a tool result: the longest list of objects that have an id, or the object itself."""
    best = []

    def walk(x, depth):
        nonlocal best
        if depth > 4:
            return
        if isinstance(x, list):
            objs = [i for i in x if isinstance(i, dict) and _id_key(i)]
            if len(objs) > len(best):
                best = objs
            for i in x[:50]:
                walk(i, depth + 1)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v, depth + 1)
    walk(data, 0)
    if best:
        return best
    return [data] if isinstance(data, dict) and _id_key(data) else []


def _flat(item: dict) -> dict:
    """Jira and others nest the useful fields under `fields`."""
    f = item.get("fields")
    return {**{k: v for k, v in item.items() if k != "fields"}, **f} if isinstance(f, dict) else item


def _short(v):
    """A nested object as one value: its display name if it has one."""
    if isinstance(v, dict):
        for k in ("displayName", "name", "value", "title", "real_name", "email", "key"):
            if isinstance(v.get(k), str):
                return v[k]
    return v


def _simple(v) -> bool:
    """A field whose change is worth a history line: a value or a named object (status, assignee), not a list."""
    return v is None or isinstance(_short(v), (str, int, float, bool))


def _render(v, depth=0) -> str:
    v = _short(v)
    if isinstance(v, dict):
        if depth > 1:
            return ""
        lines = [f"{k}: {_render(x, depth + 1)}" for k, x in v.items()
                 if not _NOISE.search(k) and x not in (None, "", [], {})]
        return ("\n" + "  " * (depth + 1)).join([""] + lines) if lines else ""
    if isinstance(v, list):
        parts = [_render(x, depth + 1) for x in v[:30]]
        if all(not isinstance(_short(x), (dict, list)) for x in v):
            return ", ".join(p for p in parts if p)
        return "".join(f"\n{'  ' * (depth + 1)}- {p.strip()}" for p in parts if p)
    return str(v)


def _title(d: dict, key: str) -> str:
    for k in ("summary", "title", "name", "subject", "text"):
        if isinstance(d.get(k), str) and d[k].strip():
            return d[k].strip().split("\n")[0][:120]
    return key


def _fname(key_name: str, key: str) -> str:
    raw = key if key_name in ("key", "identifier", "_key") else f"{key_name}-{key}"
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("_")[:100]
    return s if s and s == raw[:100] else f"{s[:80]}-{hashlib.sha1(raw.encode()).hexdigest()[:8]}"


def store(sid: str, label: str, tool: str, args: dict, text: str, session: str | None = None) -> int:
    """Write the items of one tool result to ~/.pensieve/connections/<sid>/. An item seen before is merged with its
    earlier copy, and each changed field goes into its history. An item from a direct message, a group message or a
    private channel (private_tool, private_item) is marked `_private`: it is context on this Mac, and never part of a
    shared card. Returns the number of items written."""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    pid = connectors.match(sid, label, tool)
    items = connectors.parse(pid, tool, args, data) if data is not None else None
    if items is None:
        items = [_flat(i) for i in split(data)] if data is not None else []
    if not items:
        if len(text.strip()) < 40:
            return 0
        h = hashlib.sha1(json.dumps([tool, args], sort_keys=True).encode()).hexdigest()[:12]
        items = [{"id": f"{tool}-{h}", "title": f"{tool} {json.dumps(args)[:100]}",
                  "content": data if isinstance(data, (dict, list)) else text[:50000]}]
    chat, all_private = bool(CHAT.search(f"{sid} {tool}")) or pid == "slack", private_tool(tool)
    for i in items:
        i.update(connectors.normalize(pid, i))
        if all_private or private_item(i, chat):
            i["_private"] = True
    root = DIR / sid
    for sub in ("", ".raw", ".history"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    n = 0
    for item in items:
        kn = "_key" if item.get("_key") else _id_key(item)
        key = str(item[kn])
        name = _fname(kn, key)
        raw_p, hist_p = root / ".raw" / f"{name}.json", root / ".history" / f"{name}.jsonl"
        try:
            old = json.loads(raw_p.read_text())
        except (OSError, ValueError):
            old = {}
        merged = {**old, **item}
        if session:
            merged["_seen_in"] = sorted(set(old.get("_seen_in", [])) | {session})
        if merged == old:
            continue
        changes = {k: [_render(old[k]), _render(v)] for k, v in item.items() if k in old and not _NOISE.search(k)
                   and not k.startswith("_")
                   and k not in _VOLATILE and _simple(v) and _simple(old[k]) and _render(old[k]) != _render(v)
                   and len(_render(v)) < 300}
        if changes:
            with hist_p.open("a") as f:
                f.write(json.dumps({"at": now, "changes": changes}) + "\n")
        raw_p.write_text(json.dumps(merged))
        (root / f"{name}.md").write_text(markdown(label, tool, kn, key, merged, hist_p))
        n += 1
    return n


def markdown(label: str, tool: str, key_name: str, key: str, d: dict, hist_p: Path) -> str:
    url = next((d[k] for k in ("html_url", "webUrl", "url", "permalink", "link") if isinstance(d.get(k), str)
                and d[k].startswith("http") and "://api." not in d[k]), "")
    title = d.get("_title") or _title(d, key)
    front = {"source": label, "tool": tool, "connector": d.get("_connector"), "key": key, "title": title, "url": url,
             "created": d.get("_created"), "updated": d.get("_updated"), "private": bool(d.get("_private")) or None}
    lines = ["---"] + [f"{k}: {json.dumps(v)}" for k, v in front.items() if v]
    if d.get("_people"):  # each person with the service and id, for a people graph to join identities later
        lines.append("people:")
        lines += [f"  - {json.dumps(p)}" for p in d["_people"]]
    head = lines + ["---", "", f"# {title}", "", f"- Source: {label} ({tool})", f"- {key_name.lstrip('_')}: {key}"]
    if url:
        head.append(f"- URL: {url}")
    roles = {}
    for p in d.get("_people") or []:
        roles.setdefault(p["role"], []).append(p.get("name") or p.get("email") or p.get("id"))
    for role, names in roles.items():
        head.append(f"- {role}{'s' if len(names) > 1 else ''}: {', '.join(dict.fromkeys(names))}")
    shown = {key_name, "webUrl", "url"}
    if roles:  # the people lines above name them already
        shown |= {k for ks in connectors.ROLES.values() for k in ks}
    if d.get("_created") or d.get("_updated"):
        head += [f"- {k}: {d['_' + k]}" for k in ("created", "updated") if d.get("_" + k)]
        shown |= {"created", "updated", "created_at", "updated_at", "createdAt", "updatedAt"}
    for k in ("status", "state", "assignee", "reporter", "creator", "owner", "author", "user", "created", "updated",
              "created_at", "updated_at"):
        if k not in shown and d.get(k) not in (None, "", [], {}):
            head.append(f"- {k}: {_render(d[k])}")
            shown.add(k)
    if d.get("_seen_in"):
        head.append(f"- Seen in agent sessions: {', '.join(d['_seen_in'][-5:])}")
    body = [f"{k}: {_render(v)}" for k, v in d.items() if k not in shown and not _NOISE.search(k)
            and not k.startswith("_") and v not in (None, "", [], {}) and _render(v).strip()]
    hist = []
    try:
        for line in hist_p.read_text().splitlines()[-30:]:
            h = json.loads(line)
            hist += [f"- {h['at']}: {k} changed from {a or '(empty)'} to {b or '(empty)'}"
                     for k, (a, b) in h["changes"].items()]
    except (OSError, ValueError):
        pass
    text = "\n".join(head) + "\n\n" + "\n\n".join(body) + ("\n\n## Changes\n\n" + "\n".join(hist) if hist else "")
    return text[:60000] + "\n"


def from_session(session_id: str, results: list[tuple]) -> int:
    """Store the MCP tool results that an agent got in one session: (full tool name, args, result content)."""
    n = 0
    for full, args, content in results:
        _, srv, tool = (full.split("__", 2) + ["", ""])[:3]
        if not tool or srv == "pensieve" or not read_tool(srv, tool):
            continue
        n += store(SESSIONS, "Agent sessions", f"{srv}.{tool}", args, _result_text(content), session=session_id)
    return n


def backfill_sessions() -> int:
    """When the agent-sessions source is switched on: read the MCP results of the sessions indexed before."""
    from pensieve import parsers
    from pensieve.indexer import in_temp
    n = 0
    for source, path in parsers.discover():
        if source != "claude" or path.suffix == ".md":
            continue
        try:
            s = parsers.parse(source, path)
        except Exception:
            continue
        if s.mcp and not in_temp(s.project):
            n += from_session(s.id, s.mcp)
    _update(lambda st: st.__setitem__("sessions_backfilled", time.time()))
    return n


def roots(enabled) -> list[Path]:
    """The folders of switched-on connections, for the file indexer. The folder of a covered server is left out once
    its keeper has collected, so that the index holds each item once."""
    if not DIR.is_dir():
        return []
    runs = _load().get("runs", {})
    skip = {sid for sid, keeper in covered().items() if runs.get(keeper, {}).get("collected")}
    return [p for p in DIR.iterdir() if p.is_dir() and not p.name.startswith(".") and enabled(f"connections/{p.name}")
            and p.name not in skip]


def remove(sid: str):
    import shutil
    shutil.rmtree(DIR / sid, ignore_errors=True)
    mcp_oauth.forget(sid)
    _update(lambda st: st.get("runs", {}).pop(sid, None))
