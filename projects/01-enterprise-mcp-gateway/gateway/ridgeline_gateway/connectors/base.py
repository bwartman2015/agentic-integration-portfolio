"""Connector plumbing: the only code in the gateway that talks to a backend system.

Every outbound request goes through `HttpConnector.request`, which pins the host, never follows
redirects, applies the timeout, records upstream status and latency for the call log, and turns
every transport or HTTP failure into a sanitized `ConnectorError`. URLs, response bodies and
credentials never reach the MCP client or the log.
"""
from __future__ import annotations

import contextlib
import contextvars
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx2
from mcp.server.mcpserver.exceptions import ToolError


class NotFound(ToolError):
    """The requested business object does not exist (a normal, expected outcome)."""


class ConnectorError(ToolError):
    """A backend failed. The message is safe to show the caller: system name and reason class only.
    `status` is the upstream HTTP status when there was one (for callers that branch on it)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AuthRejected(ConnectorError):
    """The backend answered 401: it did not accept the gateway's credentials."""


class Forbidden(ConnectorError):
    """The backend answered 403: authenticated, but not permitted (or an org/API limit was reached)."""


MAX_RESPONSE_BYTES = 5 * 1024 * 1024
READ_ONLY_METHODS = frozenset({"GET"})


@dataclass
class UpstreamInfo:
    backend: str = ""
    status: int | None = None
    ms: float = 0.0
    calls: int = 0


_upstream: contextvars.ContextVar[UpstreamInfo | None] = contextvars.ContextVar("upstream", default=None)


def begin_upstream() -> UpstreamInfo:
    info = UpstreamInfo()
    _upstream.set(info)
    return info


def note_backend(backend: str) -> None:
    info = _upstream.get()
    if info is not None:
        info.backend = backend


@contextlib.contextmanager
def side_channel() -> Iterator[None]:
    """Requests made inside are not recorded as the tool's backend call: policy queries and the CRM/ERP
    lookups that resolve a record's region. The call log keeps naming the system the tool is for."""
    token = _upstream.set(None)
    try:
        yield
    finally:
        _upstream.reset(token)


@dataclass
class HttpConnector:
    """Async HTTP access to one system, pinned to the host of `base_url`."""

    system: str            # human name used in error messages, e.g. "Salesforce"
    backend: str           # config value, e.g. "salesforce"
    base_url: str
    timeout_s: float = 10.0
    transport: httpx2.AsyncBaseTransport | None = None   # tests inject httpx2.MockTransport
    allowed_hosts: tuple[str, ...] = ()
    # Writes fail closed at the transport too: gateway connectors may only read. The Salesforce login
    # connector adds POST for the token request; only the operator seed tool builds a write-capable one.
    allowed_methods: frozenset[str] = READ_ONLY_METHODS
    _client: httpx2.AsyncClient | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        host = urlsplit(self.base_url).hostname or ""
        self.allowed_hosts = tuple({host, *self.allowed_hosts})

    @property
    def client(self) -> httpx2.AsyncClient:
        if self._client is None:
            self._client = httpx2.AsyncClient(timeout=self.timeout_s, follow_redirects=False,
                                              transport=self.transport, trust_env=False)
        return self._client

    async def request(self, method: str, url: str, *, expect_404: bool = False, **kwargs: Any) -> httpx2.Response | None:
        """Send one request. Returns None for a 404 when `expect_404`, else raises ConnectorError on failure."""
        if method.upper() not in self.allowed_methods:
            raise ConnectorError(f"{self.system}: the gateway does not send {method.upper()} requests to this system.")
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host not in self.allowed_hosts or (parts.port is not None and parts.port != urlsplit(self.base_url).port):
            raise ConnectorError(f"{self.system}: refused to call a host outside the connector's allowlist.")
        note_backend(self.backend)
        info = _upstream.get()
        started = time.perf_counter()
        status: int | None = None
        too_large = ConnectorError(f"{self.system} returned a response larger than the gateway accepts.")
        try:
            # Streamed, so an oversized body is never held in memory: reject on Content-Length up front,
            # and stop reading as soon as the running total passes the cap (no or wrong Content-Length).
            req = self.client.build_request(method, url, **kwargs)
            stream = await self.client.send(req, stream=True)
            status = stream.status_code
            try:
                declared = stream.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                    too_large.status = status
                    raise too_large
                body, size = [], 0
                async for chunk in stream.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        too_large.status = status
                        raise too_large
                    body.append(chunk)
            finally:
                await stream.aclose()
            # aiter_bytes already decoded any Content-Encoding, so the rebuilt response carries plain bytes.
            headers = [(k, v) for k, v in stream.headers.items()
                       if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
            resp = httpx2.Response(status, headers=headers, content=b"".join(body), request=req)
        except httpx2.TimeoutException:
            raise ConnectorError(f"{self.system} did not respond in time.") from None
        except httpx2.TransportError:
            raise ConnectorError(f"{self.system} is unreachable.") from None
        except httpx2.HTTPError:           # e.g. a body that fails Content-Encoding decoding
            raise ConnectorError(f"{self.system} returned a response the gateway could not read.") from None
        finally:
            if info is not None:
                info.calls += 1
                info.status = status
                info.ms = round(info.ms + (time.perf_counter() - started) * 1000, 2)
        if status == 404 and expect_404:
            return None
        if status == 401:
            raise AuthRejected(f"{self.system} rejected the gateway's credentials.", status)
        if status == 403:
            raise Forbidden(f"{self.system} refused the request (not permitted, or an API limit was reached).", status)
        if status == 429:
            raise ConnectorError(f"{self.system} is rate limiting the gateway; try again later.", status)
        if status is None or not 200 <= status < 300:
            raise ConnectorError(f"{self.system} returned an error (HTTP {status}).", status)
        return resp

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def json(self, method: str, url: str, **kwargs: Any) -> Any:
        resp = await self.request(method, url, **kwargs)
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError:
            raise ConnectorError(f"{self.system} returned a response the gateway could not read.") from None


class CrmConnector(Protocol):
    async def find_accounts(self, query: str) -> list[dict[str, Any]]: ...


class CaseStore(Protocol):
    async def open_case(self, account_id: str, subject: str, summary: str) -> dict[str, Any]: ...


class ErpConnector(Protocol):
    async def fetch_order(self, sales_order: str) -> dict[str, Any] | None: ...


class CarrierConnector(Protocol):
    async def fetch_tracking(self, sales_order: str) -> dict[str, Any] | None: ...


class ArConnector(Protocol):
    async def fetch_open_items(self, account_id: str) -> list[dict[str, Any]] | None: ...
