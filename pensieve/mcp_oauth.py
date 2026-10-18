"""OAuth sign-in for remote MCP servers that Pensieve connects to itself (Notion, Linear, Granola and others).

The MCP SDK does the OAuth work: discovery, dynamic client registration, PKCE, the token exchange and refresh.
Pensieve gives it three things:
- a token store: ~/.pensieve/connections/.oauth/<server>.json, readable only by the user;
- a redirect handler: during a sign-in that the user started, it hands the sign-in page's URL to the web UI. During a
  collection, nobody is there to sign in, so it raises NeedsSignIn and the server shows as needing sign-in;
- a callback handler: it waits for the browser to come back to /api/connectors/callback with the code."""
import asyncio
import json
import os
import threading
import time
from urllib.parse import parse_qs, urlparse

from pensieve import config

DIR = config.DATA_DIR / "connections" / ".oauth"
REDIRECT = f"http://localhost:{config.PORT}/api/connectors/callback"
WAIT_SECONDS = 600
_pending: dict[str, dict] = {}  # OAuth state -> {sid, event, code, error}
_lock = threading.Lock()


class NeedsSignIn(Exception):
    pass


def _path(sid: str):
    return DIR / f"{sid}.json"


def _read(sid: str) -> dict:
    try:
        return json.loads(_path(sid).read_text())
    except (OSError, ValueError):
        return {}


def _write(sid: str, d: dict):
    DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DIR, 0o700)
    p = _path(sid)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def signed_in(sid: str) -> bool:
    return bool(_read(sid).get("tokens"))


def forget(sid: str):
    _path(sid).unlink(missing_ok=True)


class _Store:
    """The SDK's TokenStorage. The SDK does not keep a token's expiry across restarts, so the store keeps the time
    and hands back an expired token without its access token: the SDK then refreshes it."""

    def __init__(self, sid: str):
        self.sid = sid

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        d = _read(self.sid)
        t = d.get("tokens")
        if not t:
            return None
        if d.get("expires_at") and time.time() > d["expires_at"] - 60:
            t = {**t, "access_token": ""}
        return OAuthToken.model_validate(t)

    async def set_tokens(self, tokens):
        d = _read(self.sid)
        d["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        d["expires_at"] = time.time() + tokens.expires_in if tokens.expires_in else None
        _write(self.sid, d)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        c = _read(self.sid).get("client")
        if not c or REDIRECT not in [str(u) for u in c.get("redirect_uris") or []]:  # Pensieve's port changed
            return None
        return OAuthClientInformationFull.model_validate(c)

    async def set_client_info(self, info):
        d = _read(self.sid)
        d["client"] = info.model_dump(mode="json", exclude_none=True)
        _write(self.sid, d)


def provider(sid: str, cfg: dict, on_url=None):
    """An httpx Auth for one server. `on_url(url)` gets the sign-in page; without it, a sign-in raises NeedsSignIn."""
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata
    state_of = {}

    async def redirect(url: str):
        if not on_url:
            raise NeedsSignIn("needs sign-in")
        state = (parse_qs(urlparse(url).query).get("state") or [""])[0]
        with _lock:
            _pending[state] = dict(sid=sid, event=threading.Event(), code=None, error=None, at=time.time())
        state_of["state"] = state
        on_url(url)

    async def callback():
        p = _pending.get(state_of.get("state", ""))
        if not p:
            raise NeedsSignIn("the sign-in did not start")
        try:
            if not await asyncio.to_thread(p["event"].wait, WAIT_SECONDS):
                raise NeedsSignIn("the sign-in timed out")
            if p["error"] or not p["code"]:
                raise NeedsSignIn(f"the sign-in failed: {p['error'] or 'no code'}")
        finally:
            with _lock:
                _pending.pop(state_of["state"], None)
        try:
            from mcp.shared.auth import AuthorizationCodeResult  # mcp 2
            return AuthorizationCodeResult(code=p["code"], state=state_of["state"], iss=p.get("iss") or None)
        except ImportError:  # mcp 1: a (code, state) tuple
            return p["code"], state_of["state"]

    class Provider(OAuthClientProvider):
        async def _perform_authorization_code_grant(self):
            # The SDK asks for every scope that the server lists (Linear: read and write). A connector that names a
            # scope asks only for it, so the token itself can only read.
            if cfg.get("scope"):
                self.context.client_metadata.scope = cfg["scope"]
            return await super()._perform_authorization_code_grant()

    meta = OAuthClientMetadata(client_name="Pensieve", redirect_uris=[REDIRECT], grant_types=[
        "authorization_code", "refresh_token"], response_types=["code"], token_endpoint_auth_method="none",
        scope=cfg.get("scope") or None)
    return Provider(server_url=cfg["url"], client_metadata=meta, storage=_Store(sid),
                    redirect_handler=redirect, callback_handler=callback)


def finish(state: str, code: str | None, error: str | None, iss: str | None = None) -> bool:
    """The browser came back from the sign-in page. `iss` is the issuer that the page names (RFC 9207), which the SDK
    checks. Returns False when no sign-in waits for this state."""
    with _lock:
        p = _pending.get(state)
    if not p:
        return False
    p["code"], p["error"], p["iss"] = code, error, iss
    p["event"].set()
    return True


def start(sid: str, run, timeout: float = 30) -> dict:
    """Start a sign-in in a thread: `run(on_url)` connects to the server and lists its tools. Returns
    {url} when the user must sign in in the browser, {done: True} when the server let Pensieve in without it, or
    {error}."""
    got, out = threading.Event(), {}

    def on_url(url):
        out["url"] = url
        got.set()

    def go():
        try:
            run(on_url)
            out.setdefault("done", True)
        except Exception as e:  # shown to the user
            out.setdefault("error", str(e)[:300] or type(e).__name__)
        got.set()
    threading.Thread(target=go, daemon=True, name=f"oauth-{sid}").start()
    got.wait(timeout)
    return {k: out[k] for k in ("url", "done", "error") if k in out} or {"error": "the server did not answer"}
