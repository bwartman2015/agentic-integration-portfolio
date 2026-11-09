"""Ridgeline MCP gateway (Milestone 1: server skeleton).

An MCP server (official `mcp` SDK, `MCPServer`, formerly FastMCP; streamable HTTP at /mcp) that exposes
enterprise systems as tools. Milestone 1 has one hard-coded, read-only tool, `get_sales_order`, backed
by the seeded mock ERP in `mock_systems/` (synthetic data). Later milestones add:
  M2 connectors (Salesforce, SAP sandbox, carrier, AR)   M3 Keycloak JWT validation
  M4 OPA authorization                                   M5 rate limits, PII masking, audit, approvals

Every tools/call goes through `GatewayServer.call_tool`, the single choke point where identity (M3),
policy (M4) and audit (M5) will sit. Today it rejects unknown arguments and logs one JSON line per call
for every outcome (OBSERVE). Crashes reach the client only as "Error executing tool <name>" (the SDK
keeps the exception text server-side).

Security today: NO authentication yet (Milestone 3). The container only publishes on 127.0.0.1, Host
and Origin headers are checked against an allowlist (DNS-rebinding protection), and arguments are
validated against the tool's JSON Schema (`additionalProperties: false`).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from mock_systems import get_systems

SERVER_NAME = "ridgeline-gateway"
SERVER_VERSION = "0.1.0"
SALES_ORDER_PATTERN = r"^SO-[0-9]{6}$"
# Trace headers are caller-supplied: only well-formed values are logged, anything else as "invalid".
_TRACE_HEADERS = {
    "traceparent": re.compile(r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"),
    "x-request-id": re.compile(r"^[A-Za-z0-9._-]{1,64}$"),
}

log = logging.getLogger("ridgeline.gateway")


class NotFound(ToolError):
    """The requested business object does not exist (a normal, expected outcome)."""


class GatewayServer(MCPServer):
    """MCPServer with one governed entry point for every tool call."""

    def add_tool(self, *args: Any, **kwargs: Any) -> None:
        """Every registration (the @tool decorator calls this too) publishes a closed schema, so
        clients, and OPA in Milestone 4, see exactly the arguments a tool accepts."""
        super().add_tool(*args, **kwargs)
        for tool in self._tool_manager.list_tools():
            tool.parameters["additionalProperties"] = False

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Context | None = None) -> Any:
        started = time.perf_counter()
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
                           else "invalid_arguments" if isinstance(cause, ValidationError) else "error")
            raise
        except Exception as exc:
            outcome, error = "error", type(exc).__name__
            raise
        finally:
            # Unknown tool names are caller-supplied text, so they are not logged verbatim.
            _log_call(name if tool is not None else "<unknown>", outcome, started, context, error)


def _log_call(tool: str, outcome: str, started: float, context: Context | None, error: str | None) -> None:
    """OBSERVE: one structured line per tool call, whatever the outcome. Argument values and unknown
    tool names are not logged (they are caller-supplied and can hold customer data). The JSON-RPC
    request id and any trace header are kept so the M5 audit table can join to agent traces."""
    record: dict[str, Any] = {"event": "tool_call", "tool": tool, "outcome": outcome, "ok": outcome == "ok",
                              "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
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


mcp = GatewayServer(
    name=SERVER_NAME,
    title="Ridgeline Supply Co. MCP gateway",
    version=SERVER_VERSION,
    instructions=("Ridgeline Supply Co. MCP gateway (synthetic data). Milestone 1: one read-only ERP tool, "
                  "get_sales_order; no authentication yet."),
)


@mcp.tool(
    title="Get sales order",
    description="Sales order header, lines and delivery status from ERP (read-only; synthetic data).",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                                openWorldHint=False),
)
def get_sales_order(
    sales_order: Annotated[str, Field(description="Sales order number, for example SO-104233",
                                      pattern=SALES_ORDER_PATTERN)],
) -> dict[str, Any]:
    systems = get_systems()
    if sales_order not in systems.dataset.orders:
        raise NotFound(f"Sales order {sales_order} was not found in ERP.")
    return systems.erp.order(sales_order)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    """Liveness for Docker's healthcheck. Unauthenticated by design; reveals no data."""
    return JSONResponse({"status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION})


def transport_security() -> TransportSecuritySettings:
    """Allow only the hosts/origins we publish on (comma-separated env overrides). Containers that call
    the gateway as http://gateway:8000 (Milestones 5-6) need `gateway:*` added to GATEWAY_ALLOWED_HOSTS."""
    hosts = os.environ.get("GATEWAY_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*")
    origins = os.environ.get("GATEWAY_ALLOWED_ORIGINS", "http://localhost:*,http://127.0.0.1:*")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[h.strip() for h in hosts.split(",") if h.strip()],
        allowed_origins=[o.strip() for o in origins.split(",") if o.strip()],
    )


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
    # The SDK logs caller-supplied values (tool names, error text) and session ids at INFO;
    # the gateway's own JSON line is the record of each call.
    for noisy in ("mcp", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    mcp.run(
        "streamable-http",
        host=os.environ.get("GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("GATEWAY_INTERNAL_PORT", "8000")),
        streamable_http_path="/mcp",
        transport_security=transport_security(),
    )


if __name__ == "__main__":
    main()
