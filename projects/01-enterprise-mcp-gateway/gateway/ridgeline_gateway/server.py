"""Ridgeline MCP gateway (Milestone 3: identity).

An MCP server (official `mcp` SDK, `MCPServer`, formerly FastMCP; streamable HTTP at /mcp) that exposes
Ridgeline's enterprise systems as four read-only tools. Each tool calls a connector (`connectors/`),
which has a `seed` backend (offline synthetic data) and a live backend (Salesforce, SAP sandbox, the
mock carrier and AR APIs), chosen per system in `.env` (see config.py). Every /mcp request must carry a
valid Keycloak access token (auth.py); /healthz and the RFC 9728 metadata stay open. Later milestones add:
  M4 OPA authorization (per-tool scopes)   M5 rate limits, PII masking, audit, approvals

Every tools/call goes through `GatewayServer.call_tool`, the single choke point where identity (M3),
policy (M4) and audit (M5) will sit. Today it rejects unknown tools and unexpected arguments and logs
one JSON line per call for every outcome, including which backend answered and which verified client
called (OBSERVE). Crashes reach the client only as "Error executing tool <name>"; connector failures
as a sanitized message.

Security today: authentication (JWT, fail closed), no per-tool authorization yet (Milestone 4). The
container only publishes on 127.0.0.1, Host and Origin headers are checked against an allowlist
(DNS-rebinding protection), and arguments are validated against closed JSON Schemas. Secrets and
tokens come only from the environment / request and are never logged.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any

from mcp.server.auth.provider import TokenVerifier
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import AuthConfig, AuthConfigError, build_verifier, current_principal
from .config import ConfigError, load_settings
from .connectors import ConnectorError, Connectors, NotFound, begin_upstream, build_connectors

SERVER_NAME = "ridgeline-gateway"
SERVER_VERSION = "0.3.0"
SALES_ORDER_PATTERN = r"^SO-[0-9]{6}$"
ACCOUNT_ID_PATTERN = r"^ACCT-[0-9]{4}$"
ACCOUNT_QUERY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 &.,'-]{1,79}$"
# Trace headers are caller-supplied: only well-formed values are logged, anything else as "invalid".
_TRACE_HEADERS = {
    "traceparent": re.compile(r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"),
    "x-request-id": re.compile(r"^[A-Za-z0-9._-]{1,64}$"),
}
# openWorldHint: the tools reach external systems (Salesforce, SAP, carrier, AR) whose data changes.
_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

log = logging.getLogger("ridgeline.gateway")

_connectors: Connectors | None = None


def get_connectors() -> Connectors:
    """Connectors built from the environment on first use (main() builds them eagerly to fail fast)."""
    global _connectors
    if _connectors is None:
        _connectors = build_connectors(load_settings())
    return _connectors


def set_connectors(connectors: Connectors | None) -> None:
    """Tests swap in connectors over fake transports; None rebuilds from the environment."""
    global _connectors
    _connectors = connectors


class GatewayServer(MCPServer):
    """MCPServer with one governed entry point for every tool call."""

    def enable_auth(self, config: AuthConfig, verifier: TokenVerifier) -> None:
        """Require a verified bearer token on /mcp (applied when the HTTP app is built)."""
        self.settings.auth = config.sdk_settings()
        self._token_verifier = verifier

    def add_tool(self, *args: Any, **kwargs: Any) -> None:
        """Every registration (the @tool decorator calls this too) publishes a closed schema, so
        clients, and OPA in Milestone 4, see exactly the arguments a tool accepts."""
        super().add_tool(*args, **kwargs)
        for tool in self._tool_manager.list_tools():
            tool.parameters["additionalProperties"] = False

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Context | None = None) -> Any:
        started = time.perf_counter()
        upstream = begin_upstream()
        tool = self._tool_manager.get_tool(name)
        outcome, error = "ok", None
        try:
            if tool is None:
                outcome = "unknown_tool"
                raise ToolError("Unknown tool.")
            unexpected = sorted(set(arguments) - set(tool.parameters.get("properties", {})))
            if unexpected:
                outcome = "invalid_arguments"
                raise ToolError(f"Unexpected argument(s) for {name}: {', '.join(unexpected)}")
            return await super().call_tool(name, arguments, context)
        except UnexpectedToolError as exc:
            # The SDK already sends the client only "Error executing tool <name>"; the type stays here.
            outcome, error = "error", type(exc.__cause__).__name__
            raise
        except ToolError as exc:
            if outcome == "ok":
                cause = exc.__cause__
                outcome = ("not_found" if isinstance(cause, NotFound)
                           else "upstream_error" if isinstance(cause, ConnectorError)
                           else "invalid_arguments" if isinstance(cause, ValidationError) else "error")
                if isinstance(cause, ConnectorError):
                    error = type(cause).__name__
            raise
        except Exception as exc:
            outcome, error = "error", type(exc).__name__
            raise
        finally:
            # Unknown tool names are caller-supplied text, so they are not logged verbatim.
            extra = {**current_principal(), "backend": upstream.backend or None, "upstream_status": upstream.status,
                     "upstream_ms": upstream.ms if upstream.calls else None,
                     "upstream_calls": upstream.calls or None}
            _log_call(name if tool is not None else "<unknown>", outcome, started, context, error, extra)


def _log_call(tool: str, outcome: str, started: float, context: Context | None, error: str | None,
              extra: dict[str, Any] | None = None) -> None:
    """OBSERVE: one structured line per tool call, whatever the outcome. Argument values and unknown
    tool names are not logged (they are caller-supplied and can hold customer data). The JSON-RPC
    request id and any trace header are kept so the M5 audit table can join to agent traces."""
    record: dict[str, Any] = {"event": "tool_call", "tool": tool, "outcome": outcome, "ok": outcome == "ok",
                              "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
    record.update({k: v for k, v in (extra or {}).items() if v is not None})
    if error:
        record["error_type"] = error
    if context is not None:
        try:
            record["request_id"] = context.request_id
            headers = context.headers or {}
        except (AttributeError, ValueError, LookupError):
            headers = {}
        for h, pattern in _TRACE_HEADERS.items():
            value = headers.get(h)
            if value:
                record[h.replace("-", "_")] = value if pattern.fullmatch(value) else "invalid"
    log.info(json.dumps(record, sort_keys=True))


@asynccontextmanager
async def _lifespan(_server: MCPServer):
    try:
        yield None
    finally:
        if _connectors is not None:
            await _connectors.aclose()


mcp = GatewayServer(
    lifespan=_lifespan,
    name=SERVER_NAME,
    title="Ridgeline Supply Co. MCP gateway",
    version=SERVER_VERSION,
    instructions=("Ridgeline Supply Co. MCP gateway (synthetic data). Four read-only tools over CRM, ERP, "
                  "carrier and AR systems. Requires a Keycloak access token (OAuth 2.0 client credentials)."),
)


@mcp.tool(title="Search accounts",
          description="Find customer accounts in CRM by name, account number or alias (read-only; synthetic data).",
          annotations=_READ_ONLY)
async def search_accounts(
    query: Annotated[str, Field(description="Account name, number or alias, for example Summit or BBO",
                                pattern=ACCOUNT_QUERY_PATTERN)],
) -> dict[str, Any]:
    return {"query": query, "matches": await get_connectors().crm.find_accounts(query)}


@mcp.tool(title="Get sales order",
          description="Sales order header, lines and delivery status from ERP (read-only; synthetic data).",
          annotations=_READ_ONLY)
async def get_sales_order(
    sales_order: Annotated[str, Field(description="Sales order number, for example SO-104233",
                                      pattern=SALES_ORDER_PATTERN)],
) -> dict[str, Any]:
    order = await get_connectors().erp.fetch_order(sales_order)
    if order is None:
        raise NotFound(f"Sales order {sales_order} was not found in ERP.")
    return order


@mcp.tool(title="Track shipment",
          description="Carrier tracking events, lateness and proof of delivery for a sales order (read-only).",
          annotations=_READ_ONLY)
async def track_shipment(
    sales_order: Annotated[str, Field(description="Sales order number, for example SO-104233",
                                      pattern=SALES_ORDER_PATTERN)],
) -> dict[str, Any]:
    tracking = await get_connectors().carrier.fetch_tracking(sales_order)
    if tracking is None:
        raise NotFound(f"Sales order {sales_order} has no carrier record.")
    return tracking


@mcp.tool(title="Get open invoices",
          description="Open AR invoices for an account, with count and total computed by the gateway (read-only).",
          annotations=_READ_ONLY)
async def get_open_invoices(
    account_id: Annotated[str, Field(description="Account number, for example ACCT-1002",
                                     pattern=ACCOUNT_ID_PATTERN)],
) -> dict[str, Any]:
    invoices = await get_connectors().ar.fetch_open_items(account_id)
    if invoices is None:
        raise NotFound(f"Account {account_id} was not found in AR.")
    # Deterministic code owns the money math (decimal, not float sums); agents only quote these numbers.
    total = sum((Decimal(str(i["amount"])) for i in invoices), Decimal("0")).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return {"account_id": account_id, "invoices": invoices, "count": len(invoices), "total_open": float(total)}


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    """Liveness for Docker's healthcheck. Unauthenticated by design; reveals no data or configuration."""
    return JSONResponse({"status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION})


def transport_security() -> TransportSecuritySettings:
    """Allow only the hosts/origins we publish on (comma-separated env overrides). Compose adds
    `gateway:*` so containers on the project network (the ops tools; n8n and the copilot later) can call it."""
    hosts = os.environ.get("GATEWAY_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*")
    origins = os.environ.get("GATEWAY_ALLOWED_ORIGINS", "http://localhost:*,http://127.0.0.1:*")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[h.strip() for h in hosts.split(",") if h.strip()],
        allowed_origins=[o.strip() for o in origins.split(",") if o.strip()],
    )


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
    # The SDK logs caller-supplied values (tool names, error text) and session ids at INFO, and the
    # HTTP client logs request URLs; the gateway's own JSON line is the record of each call.
    for noisy in ("mcp", "httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error(json.dumps({"event": "startup_failed", "reason": str(exc)}))
        raise SystemExit(2) from None
    try:
        auth = AuthConfig.from_env(dict(os.environ))
    except AuthConfigError as exc:
        log.error(json.dumps({"event": "startup_failed", "reason": str(exc)}))
        raise SystemExit(2) from None
    mcp.enable_auth(auth, build_verifier(auth, settings.http_timeout_s))
    set_connectors(build_connectors(settings))
    log.info(json.dumps({"event": "startup", "version": SERVER_VERSION, "backends": settings.backends(),
                         "auth": {"issuer": auth.issuer, "audience": auth.audience}}))
    mcp.run(
        "streamable-http",
        host=os.environ.get("GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("GATEWAY_INTERNAL_PORT", "8000")),
        streamable_http_path="/mcp",
        transport_security=transport_security(),
    )


if __name__ == "__main__":
    main()
