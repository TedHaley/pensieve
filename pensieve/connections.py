"""Connections: MCP servers whose data Pensieve collects, read-only, and indexes like a folder.

There are two kinds of server:
- engine: the MCP servers of the insights engine when it is Claude Code (`ai: cli` with a `claude` command). This
  includes its config, its plugins and the claude.ai connectors of the account. Pensieve reads the list from the
  engine's startup event. To collect, it runs the engine with only the read tools of one server allowed.
- own: the servers in the `mcp_servers` setting, with the credentials given there. Use them with any engine, also
  the built-in one. Pensieve connects to them itself, asks the engine which read calls to make, and makes them.

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

from pensieve import config, llm, settings

DIR = config.DATA_DIR / "connections"
STATE = DIR / "state.json"
SESSIONS = "agent_sessions"  # the source of MCP results found in agent transcripts
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


def engine() -> str | None:
    """'claude' when the insights engine is Claude Code, 'codex' for Codex, else None (no MCP servers of its own)."""
    s = settings.load()
    if s["ai"] != "cli" or not s["cli_command"].strip():
        return None
    name = Path(llm._argv(s["cli_command"])[0]).name
    return name if name in ("claude", "codex") else None


def _engine_model() -> str:
    argv = llm._argv(settings.get("cli_command"))
    return argv[argv.index("--model") + 1] if "--model" in argv[:-1] else "haiku"


def servers() -> list[dict]:
    """Every known server: {id, name, origin, status, tools: [{name, read}], collected, items, error}."""
    st = _load()
    out = []
    if engine() == "claude":
        out += [{**r, "id": k, "origin": "engine"} for k, r in st.get("engine", {}).items() if k != "pensieve"]
    for c in settings.get("mcp_servers"):
        k = slug(c["name"])
        r = st.get("own", {}).get(k, {})
        out.append(dict(id=k, name=c["name"], origin="own", status=r.get("status", "unknown"), tools=r.get("tools", []),
                        error=r.get("error")))
    runs = st.get("runs", {})
    for r in out:
        r.update(runs.get(r["id"], {}))
        r["items"] = _count(r["id"])
    return out


def _count(sid: str) -> int:
    d = DIR / sid
    return sum(1 for _ in d.glob("*.md")) if d.is_dir() else 0


def _claude_init(timeout=90) -> dict:
    """Claude Code's startup event in an empty folder: the MCP servers with their status, and every tool name. The
    process stops at that event, before the model answers."""
    exe = llm._exe("claude")
    if not exe:
        raise llm.Offline("Can't find claude")
    with tempfile.TemporaryDirectory(prefix=llm.OWN_PREFIX) as tmp:
        p = subprocess.Popen([exe, "-p", "--model", _engine_model(), "--output-format", "stream-json", "--verbose",
                              "--tools", "", "--max-turns", "1"], cwd=tmp, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
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
    """Read the engine's MCP servers and tools. A server that is still connecting keeps the tools seen last time."""
    if engine() != "claude":
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
                          tools=[dict(name=n, read=is_read(n)) for n in names], at=time.time())
        st["engine"] = new
    _update(apply)
    return servers()


@asynccontextmanager
async def _open(cfg: dict):
    """An MCP client session to one of the user's own servers: `url` (streamable HTTP, with `headers`) or `command`
    (stdio, with `args` and `env`)."""
    from mcp import ClientSession
    if cfg.get("url"):
        from mcp.client import streamable_http as h
        if hasattr(h, "streamable_http_client"):  # mcp 2: headers go on the HTTP client
            async with h.create_mcp_http_client(headers=cfg.get("headers") or None) as http, \
                    h.streamable_http_client(cfg["url"], http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as s:
                    await s.initialize()
                    yield s
        else:
            async with h.streamablehttp_client(cfg["url"], headers=cfg.get("headers") or None) as streams:
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


async def _own_tools(cfg: dict) -> list[dict]:
    async with _open(cfg) as s:
        r = await s.list_tools()
        return [dict(name=t.name, read=is_read(t.name, t.annotations)) for t in r.tools]


def discover_own() -> list[dict]:
    """Connect to each server in `mcp_servers` and list its tools."""
    found = {}
    for c in settings.get("mcp_servers"):
        k = slug(c["name"])
        try:
            found[k] = dict(name=c["name"], status="connected", tools=asyncio.run(asyncio.wait_for(_own_tools(c), 60)),
                            error=None, at=time.time())
        except Exception as e:
            found[k] = dict(name=c["name"], status="failed", tools=[], error=_err(e), at=time.time())
    _update(lambda st: st.__setitem__("own", found))
    return servers()


def _err(e: BaseException) -> str:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:  # anyio wraps errors from the transport
        e = e.exceptions[0]
    return (str(e) or type(e).__name__)[:300]


def discover() -> list[dict]:
    errors = []
    for fn in (discover_engine, discover_own):
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
When you are done, reply DONE."""


def _since(sid: str) -> str:
    last = _load().get("runs", {}).get(sid, {}).get("collected")
    t = datetime.fromtimestamp(last, timezone.utc) - timedelta(hours=1) if last else \
        datetime.now(timezone.utc) - timedelta(days=settings.get("connections_days"))
    return t.strftime("%Y-%m-%dT%H:%MZ")


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


def _collect_engine(r: dict, calls: int) -> list[tuple]:
    reads = [f"mcp__{r['id']}__{t['name']}" for t in r["tools"] if t["read"]]
    if not reads:
        raise RuntimeError("no read-only tools")
    deny = [f"mcp__{r['id']}__{t['name']}" for t in r["tools"] if not t["read"]]
    deny += [f"mcp__{k}" for k in _load().get("engine", {}) if k != r["id"]]  # and every other server, whole
    exe = llm._exe("claude")
    if not exe:
        raise llm.Offline("Can't find claude")
    argv = [exe, "-p", "--model", _engine_model(), "--output-format", "stream-json", "--verbose", "--tools", "",
            "--max-turns", str(calls + 2), "--allowedTools", ",".join(reads), "--disallowedTools", ",".join(deny)]
    prompt = PROMPT.format(name=r["name"], since=_since(r["id"]), calls=calls)
    with tempfile.TemporaryDirectory(prefix=llm.OWN_PREFIX) as tmp:
        p = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=900, cwd=tmp)
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
                if any(t["name"] == tool and t["read"] for t in r["tools"]):
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
    out = []
    async with _open(cfg) as s:
        tools = [t for t in (await s.list_tools()).tools if is_read(t.name, t.annotations)]
        if not tools:
            raise RuntimeError("no read-only tools")
        menu = "\n".join(f"- {t.name}: {(t.description or '')[:300]} Input schema: {json.dumps(_attr(t, 'input_schema'))[:600]}"
                         for t in tools)
        prompt = (PROMPT.format(name=r["name"], since=_since(r["id"]), calls=calls) + f"\n\nThe tools:\n{menu}\n\n"
                  "Plan only the search and list calls whose arguments you know now. Pensieve then fetches the "
                  "details of each item that they find. Give each call's arguments as a JSON object in args_json. "
                  "Do not call any tool yourself: answer only with the plan.")
        plan = await asyncio.to_thread(llm.complete, prompt, max_tokens=1500, schema=PLAN_SCHEMA)
        names = {t.name for t in tools}

        async def call(tool, args):
            try:
                res = await s.call_tool(tool, args)
            except Exception:
                return
            if not _attr(res, "is_error"):
                text = "\n".join(getattr(b, "text", "") for b in res.content)
                if _attr(res, "structured_content") and not text:
                    text = json.dumps(_attr(res, "structured_content"))
                out.append((tool, args, text))
        for c in (plan or {}).get("calls", [])[:calls]:
            if c.get("tool") not in names:  # never a tool that is not on the read-only list
                continue
            try:
                args = json.loads(c.get("args_json") or "{}")
            except ValueError:
                continue
            if isinstance(args, dict) and "<" not in json.dumps(args):  # "<" marks a placeholder, not a value
                await call(c["tool"], args)
        found = list({str(i.get(_id_key(i))): i for _, _, text in list(out) for i in split(_json(text))}.values())
        getter = _getter(tools)
        for item in found[:max(0, calls * 2 - len(out))]:
            if getter:
                tool, param = getter
                value = item.get(param) or item.get(_id_key(item))
                if isinstance(value, (str, int)):
                    await call(tool, {param: value})
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
        else:
            cfg = next(c for c in settings.get("mcp_servers") if slug(c["name"]) == sid)
            results = asyncio.run(asyncio.wait_for(_collect_own_async(cfg, r, calls), 900))
        n = sum(store(sid, r["name"], tool, args, text) for tool, args, text in results)
        run = dict(collected=t0, error=None, last_items=n, calls=len(results), **r.get("_cost", {}))
    except Exception as e:
        run = dict(collected=_load().get("runs", {}).get(sid, {}).get("collected"), error=_err(e), failed=t0)
    _update(lambda st: st.setdefault("runs", {}).__setitem__(sid, run))
    return run


def due() -> list[str]:
    """Switched-on servers whose last collection is older than `connections_every_hours`."""
    every = settings.get("connections_every_hours") * 3600
    if not every:
        return []
    runs = _load().get("runs", {})

    def last(sid):
        r = runs.get(sid, {})
        return max(r.get("collected") or 0, r.get("failed") or 0)
    return [r["id"] for r in servers() if settings.enabled(f"connections/{r['id']}") and r["status"] == "connected"
            and time.time() - last(r["id"]) > every]


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
    raw = key if key_name in ("key", "identifier") else f"{key_name}-{key}"
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("_")[:100]
    return s if s and s == raw[:100] else f"{s[:80]}-{hashlib.sha1(raw.encode()).hexdigest()[:8]}"


def store(sid: str, label: str, tool: str, args: dict, text: str, session: str | None = None) -> int:
    """Write the items of one tool result to ~/.pensieve/connections/<sid>/. An item seen before is merged with its
    earlier copy, and each changed field goes into its history. Returns the number of items written."""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    items = [_flat(i) for i in split(data)] if data is not None else []
    if not items:
        if len(text.strip()) < 40:
            return 0
        h = hashlib.sha1(json.dumps([tool, args], sort_keys=True).encode()).hexdigest()[:12]
        items = [{"id": f"{tool}-{h}", "title": f"{tool} {json.dumps(args)[:100]}",
                  "content": data if isinstance(data, (dict, list)) else text[:50000]}]
    root = DIR / sid
    for sub in ("", ".raw", ".history"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    n = 0
    for item in items:
        kn = _id_key(item)
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
    url = next((d[k] for k in ("webUrl", "url", "permalink", "html_url", "link") if isinstance(d.get(k), str)
                and d[k].startswith("http")), "")
    head = [f"# {_title(d, key)}", "", f"- Source: {label} ({tool})", f"- {key_name}: {key}"]
    if url:
        head.append(f"- URL: {url}")
    shown = {key_name, "webUrl", "url"}
    for k in ("status", "assignee", "reporter", "creator", "owner", "author", "user", "created", "updated",
              "created_at", "updated_at"):
        if d.get(k) not in (None, "", [], {}):
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
        if not tool or srv == "pensieve" or not is_read(tool):
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
    """The folders of switched-on connections, for the file indexer."""
    if not DIR.is_dir():
        return []
    return [p for p in DIR.iterdir() if p.is_dir() and not p.name.startswith(".") and enabled(f"connections/{p.name}")]


def remove(sid: str):
    import shutil
    shutil.rmtree(DIR / sid, ignore_errors=True)
    _update(lambda st: st.get("runs", {}).pop(sid, None))
