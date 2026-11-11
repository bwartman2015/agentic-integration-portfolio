"""MCP client side (Milestone 6): McpToolSource implements agentcore.ToolSource with the official `mcp`
SDK, over streamable HTTP to the Ridgeline gateway (GATEWAY_URL, e.g. http://gateway:8000/mcp).

Every call carries:
  Authorization: Bearer <token>   a Keycloak access token for the human role the copilot acts for
                                  (client credentials, like the `ops` tools); the gateway verifies it and
                                  decides with OPA. The copilot never sees another role's token.
  x-request-id: <trace id>        the agent's trace id (agentcore.current_trace_id), so every gateway audit
                                  row of a run joins to that run's trace.
What the gateway says about its tools (descriptions, annotations) is never trusted for governance: build.py
registers them with scopes and side effects from the copilot's own policy data (policy/data.json).
A gateway error (denied, not found, approval rejected) is raised as GatewayToolError, which the agent loop
returns to THINK as data, like any failed tool call.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from agentcore import current_trace_id

from .aio import run_sync

TRACE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")          # the gateway accepts only this shape
_JWT_SHAPE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
CLIENT_SECRET_ENV = {"cs-rep": "CS_REP_CLIENT_SECRET", "cs-lead": "CS_LEAD_CLIENT_SECRET"}


class GatewayToolError(RuntimeError):
    """The gateway refused or failed a call. The message is the gateway's own (no secrets, no data)."""


class TokenError(RuntimeError):
    """Keycloak did not issue a token. Messages never contain the secret or a token."""


class KeycloakTokens:
    """OAuth 2.0 client credentials for one role, cached until shortly before the token expires."""

    def __init__(self, client_id: str, secret: str, token_url: str, timeout_s: float = 10.0,
                 opener: Callable[..., Any] = urllib.request.urlopen):
        self.client_id, self._secret, self.token_url = client_id, secret, token_url
        self.timeout_s, self._open = timeout_s, opener
        self._token, self._expires = "", 0.0

    @staticmethod
    def for_role(role: str, env: dict[str, str] | None = None) -> "KeycloakTokens":
        env = dict(os.environ) if env is None else env
        if role not in CLIENT_SECRET_ENV:
            raise TokenError(f"unknown role {role!r}; choose one of {', '.join(CLIENT_SECRET_ENV)}")
        secret, url = env.get(CLIENT_SECRET_ENV[role], ""), env.get("OIDC_TOKEN_URL", "")
        if not secret or not url:
            raise TokenError(f"{CLIENT_SECRET_ENV[role]} and OIDC_TOKEN_URL must be set (run via `make ask`)")
        return KeycloakTokens(role, secret, url)

    def __call__(self) -> str:
        if self._token and time.monotonic() < self._expires:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self._secret}".encode()).decode()
        req = urllib.request.Request(self.token_url, method="POST",
                                     data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
                                     headers={"Authorization": f"Basic {basic}", "Accept": "application/json",
                                              "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self._open(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise TokenError(f"Keycloak refused the {self.client_id} client (HTTP {exc.code})") from None
        except (OSError, ValueError) as exc:
            raise TokenError(f"Keycloak could not be reached ({type(exc).__name__})") from None
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not _JWT_SHAPE.match(token):
            raise TokenError("Keycloak returned an unusable token response")
        lifetime = data.get("expires_in") if isinstance(data.get("expires_in"), (int, float)) else 60
        self._token, self._expires = token, time.monotonic() + max(0.0, float(lifetime) - 30)
        return token


def request_headers(token: str, trace_id: str | None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if trace_id and TRACE_ID.match(trace_id):
        headers["x-request-id"] = trace_id
    return headers


@asynccontextmanager
async def sdk_session(url: str, headers: dict[str, str], timeout_s: float) -> AsyncIterator[Any]:
    """An MCP client session with the official SDK (only the copilot container has it installed)."""
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    async with httpx2.AsyncClient(headers=headers, timeout=timeout_s) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            yield client


def _field(obj: Any, *names: str) -> Any:
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if value is not None:
            return value
    return None


class McpToolSource:
    """agentcore.ToolSource for the gateway. One short MCP session per call: the loop is synchronous and
    a write may wait minutes for a human, so no connection is held between calls."""

    def __init__(self, url: str, token: Callable[[], str], timeout_s: float | None = None,
                 session: Callable[[str, dict[str, str], float], Any] = sdk_session):
        self.url, self._token, self._session = url, token, session
        # Longer than the gateway's approval wait, so a write waiting for a human is not cut off here.
        self.timeout_s = timeout_s or float(os.environ.get("APPROVAL_TIMEOUT_S", "120")) + 30

    def _headers(self) -> dict[str, str]:
        return request_headers(self._token(), current_trace_id())

    def list_tools(self) -> list[dict[str, Any]]:
        headers = self._headers()          # token and trace id are taken here, in the caller's context

        async def go() -> list[dict[str, Any]]:
            async with self._session(self.url, headers, self.timeout_s) as client:
                listed = await client.list_tools()
            return [{"name": _field(t, "name"), "description": _field(t, "description") or "",
                     "inputSchema": _field(t, "input_schema", "inputSchema") or {}}
                    for t in _field(listed, "tools") or []]
        return run_sync(go())

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        headers = self._headers()

        async def go() -> Any:
            async with self._session(self.url, headers, self.timeout_s) as client:
                return await client.call_tool(name, arguments)
        result = run_sync(go())
        texts = [t for t in (_field(b, "text") for b in _field(result, "content") or []) if isinstance(t, str)]
        if _field(result, "is_error", "isError"):
            raise GatewayToolError(" ".join(texts)[:500] or f"the gateway refused {name}")
        structured = _field(result, "structured_content", "structuredContent")
        if structured is not None:
            return structured
        try:
            return json.loads(texts[0]) if texts else {}
        except ValueError:
            return {"text": " ".join(texts)}


def gateway_url() -> str:
    """Set (GATEWAY_URL) only where the copilot runs against the gateway: the copilot container."""
    return os.environ.get("GATEWAY_URL", "").strip()
