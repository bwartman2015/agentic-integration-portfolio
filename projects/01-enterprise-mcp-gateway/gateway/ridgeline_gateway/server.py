"""Ridgeline MCP gateway (Milestone 4: authorization).

An MCP server (official `mcp` SDK, `MCPServer`, formerly FastMCP; streamable HTTP at /mcp) that exposes
Ridgeline's enterprise systems as four read-only tools. Each tool calls a connector (`connectors/`),
which has a `seed` backend (offline synthetic data) and a live backend (Salesforce, SAP sandbox, the
mock carrier and AR APIs), chosen per system in `.env` (see config.py). Every /mcp request must carry a
valid Keycloak access token (auth.py); /healthz and the RFC 9728 metadata stay open. Later milestones add:
  M5 rate limits, PII masking, audit, approvals

Every tools/call goes through `GatewayServer.call_tool`, the single choke point for identity (M3),
policy (M4) and audit (M5). In order, it:
  1. rejects unknown tools;
  2. asks OPA whether this caller may call this tool (scope rule; policy.py), and denies on any error;
  3. rejects unexpected arguments and runs the tool, which reads the record and then asks OPA whether
     the caller may see it (region rule) before returning anything;
  4. refuses a result if the tool never asked for that data check (fail closed for future tools);
  5. logs one JSON line for every outcome, with the verified client, the backend and the policy reason.
tools/list shows a caller only the tools its scopes cover. Crashes reach the client only as "Error
executing tool <name>"; connector failures as a sanitized message; denials as "Access denied: ...".

Security today: authentication (JWT, fail closed) and authorization (OPA, fail closed). The container
only publishes on 127.0.0.1, Host and Origin headers are checked against an allowlist (DNS-rebinding
protection), and arguments are validated against closed JSON Schemas. Secrets and tokens come only
from the environment / request and are never logged.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any

from mcp.server.auth.provider import TokenVerifier
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import Tool as MCPTool
from mcp_types import ToolAnnotations
from pydantic import Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import AuthConfig, AuthConfigError, build_verifier, current_principal
from .config import ConfigError, load_settings
from .connectors import ConnectorError, Connectors, NotFound, begin_upstream, build_connectors
from .connectors.base import side_channel
from .policy import (RESOURCE_NOT_CHECKED, AccessDenied, Decision, DenyAllPolicy, Policy, PolicyConfig,
                     PolicyConfigError, build_policy, denial_message)

SERVER_NAME = "ridgeline-gateway"
SERVER_VERSION = "0.4.0"
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


_policy: Policy = DenyAllPolicy()


def get_policy() -> Policy:
    """The policy decision point. Until main() (or a test) sets one, everything is denied."""
    return _policy


def set_policy(policy: Policy | None) -> None:
    """main() installs the OPA client; tests swap in their own. None goes back to deny-all."""
    global _policy
    _policy = policy if policy is not None else DenyAllPolicy()


@dataclass
class _CallAuth:
    """Authorization state of the tool call in progress (one per call, in a context variable)."""
    principal: dict[str, Any]
    tool: str
    args: dict[str, Any]
    resource_checked: bool = False     # set once the tool has asked for the data (region) check
    hidden: int = 0                    # search results filtered out by the region rule
    policy_ms: float = 0.0
    reasons: list[str] = field(default_factory=list)     # reason code of the denial, for the call log


_call_auth: contextvars.ContextVar[_CallAuth | None] = contextvars.ContextVar("call_auth", default=None)


async def _decide(auth: _CallAuth, resource: dict[str, Any] | None = None) -> Decision:
    started = time.perf_counter()
    try:
        return await get_policy().decide(auth.principal, auth.tool, auth.args, resource)
    finally:
        auth.policy_ms = round(auth.policy_ms + (time.perf_counter() - started) * 1000, 2)


def _current_call() -> _CallAuth:
    auth = _call_auth.get()
    if auth is None:                   # a tool function run outside GatewayServer.call_tool: refuse
        raise AccessDenied(denial_message(Decision(False, RESOURCE_NOT_CHECKED), {}, "this tool"),
                           RESOURCE_NOT_CHECKED)
    return auth


# The only denials that mean "this record is not yours". Anything else is the policy failing, which
# must surface as a denial of the whole call, never as "no such records for you".
_REGION_DENIALS = frozenset({"out_of_region", "region_unknown"})


async def _region_visible(region: Any) -> bool:
    """Data rule for list results: may the caller see a record whose account is in `region`
    (None = unknown)? False hides the record; a policy failure denies the whole call."""
    auth = _current_call()
    decision = await _decide(auth, {"region": region if isinstance(region, str) and region else None})
    if not decision.allow and decision.reason not in _REGION_DENIALS:
        raise AccessDenied(denial_message(decision, auth.principal, auth.tool), decision.reason)
    auth.resource_checked = True
    return decision.allow


async def _require_region(region: Any) -> None:
    auth = _current_call()
    decision = await _decide(auth, {"region": region if isinstance(region, str) and region else None})
    auth.resource_checked = True
    if not decision.allow:
        raise AccessDenied(denial_message(decision, auth.principal, auth.tool), decision.reason)


async def _account_region(account_id: Any) -> str | None:
    """The CRM region of an account, or None when the record has no Ridgeline account or the CRM does
    not know it (SAP sandbox orders, for example). A CRM failure raises, so nothing is returned."""
    if not isinstance(account_id, str) or not re.fullmatch(ACCOUNT_ID_PATTERN, account_id):
        return None
    with side_channel():
        matches = await get_connectors().crm.find_accounts(account_id)
    for match in matches:
        if match.get("account_id") == account_id:
            region = match.get("region")
            return region if isinstance(region, str) and region else None
    return None


async def _require_account(account_id: Any) -> None:
    """Deny unless the caller may see records of this account (resolved to its CRM region)."""
    await _require_region(await _account_region(account_id))


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

    async def list_tools(self) -> list[MCPTool]:
        """tools/list shows a caller only the tools its scopes cover (least privilege in discovery).
        This is a convenience, not the control: every call is authorized again in call_tool."""
        tools = await super().list_tools()
        allowed = await get_policy().allowed_tools(current_principal(), [t.name for t in tools])
        return [t for t in tools if t.name in allowed]

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Context | None = None) -> Any:
        started = time.perf_counter()
        upstream = begin_upstream()
        tool = self._tool_manager.get_tool(name)
        outcome, error = "ok", None
        auth = _CallAuth(current_principal(), name, dict(arguments) if isinstance(arguments, dict) else {})
        auth_token = _call_auth.set(auth)
        try:
            if tool is None:
                outcome = "unknown_tool"
                raise ToolError("Unknown tool.")
            decision = await _decide(auth)
            if not decision.allow:
                raise AccessDenied(denial_message(decision, auth.principal, name), decision.reason)
            unexpected = sorted(set(arguments) - set(tool.parameters.get("properties", {})))
            if unexpected:
                outcome = "invalid_arguments"
                raise ToolError(f"Unexpected argument(s) for {name}: {', '.join(unexpected)}")
            result = await super().call_tool(name, arguments, context)
            if not auth.resource_checked:
                # A tool that returns data without the data check is a bug: refuse instead of leaking.
                raise AccessDenied(denial_message(Decision(False, RESOURCE_NOT_CHECKED), auth.principal, name),
                                   RESOURCE_NOT_CHECKED)
            return result
        except AccessDenied as exc:
            outcome = "denied"
            auth.reasons = [exc.reason]
            raise
        except UnexpectedToolError as exc:
            # The SDK already sends the client only "Error executing tool <name>"; the type stays here.
            outcome, error = "error", type(exc.__cause__).__name__
            raise
        except ToolError as exc:
            if isinstance(exc.__cause__, AccessDenied):
                # Raised inside the tool by the data check; the SDK wrapped it. Send the denial itself.
                outcome = "denied"
                auth.reasons = [exc.__cause__.reason]
                raise exc.__cause__ from None
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
            _call_auth.reset(auth_token)
            # Unknown tool names are caller-supplied text, so they are not logged verbatim.
            extra = {**auth.principal, "backend": upstream.backend or None, "upstream_status": upstream.status,
                     "upstream_ms": upstream.ms if upstream.calls else None,
                     "upstream_calls": upstream.calls or None,
                     "policy_reason": auth.reasons[0] if outcome == "denied" and auth.reasons else None,
                     "policy_ms": auth.policy_ms if tool is not None else None,
                     "hidden_out_of_region": auth.hidden or None}
            _log_call(name if tool is not None else "<unknown>", outcome, started, context, error, extra)


def _log_call(tool: str, outcome: str, started: float, context: Context | None, error: str | None,
              extra: dict[str, Any] | None = None) -> None:
    """OBSERVE: one structured line per tool call, whatever the outcome. Argument values and unknown
    tool names are not logged (they are caller-supplied and can hold customer data). A denial logs
    outcome "denied" with the policy's reason code. The JSON-RPC request id and any trace header are
    kept so the M5 audit table can join to agent traces."""
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
        await _policy.aclose()


mcp = GatewayServer(
    lifespan=_lifespan,
    name=SERVER_NAME,
    title="Ridgeline Supply Co. MCP gateway",
    version=SERVER_VERSION,
    instructions=("Ridgeline Supply Co. MCP gateway (synthetic data). Four read-only tools over CRM, ERP, "
                  "carrier and AR systems. Requires a Keycloak access token (OAuth 2.0 client credentials). "
                  "Each tool needs its scope, and records are visible only for accounts in the caller's regions."),
)


@mcp.tool(title="Search accounts",
          description="Find customer accounts in CRM by name, account number or alias (read-only; synthetic data).",
          annotations=_READ_ONLY)
async def search_accounts(
    query: Annotated[str, Field(description="Account name, number or alias, for example Summit or BBO",
                                pattern=ACCOUNT_QUERY_PATTERN)],
) -> dict[str, Any]:
    matches = await get_connectors().crm.find_accounts(query)
    auth = _current_call()
    if not matches:
        auth.resource_checked = True             # nothing matched: there is nothing to protect
    by_region: dict[Any, bool] = {}
    visible = []
    for match in matches:
        region = match.get("region")
        key = region if isinstance(region, str) and region else None
        if key not in by_region:
            by_region[key] = await _region_visible(key)
        if by_region[key]:
            visible.append(match)
    auth.hidden = len(matches) - len(visible)
    result: dict[str, Any] = {"query": query, "matches": visible}
    if auth.hidden:
        # Says that records exist without showing them, so the copilot can route the rep to a CS lead.
        result["hidden_out_of_region"] = auth.hidden
    return result


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
    await _require_account(order.get("account_id"))
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
    # The carrier does not know the customer: the order's account (from ERP) decides who may see it.
    with side_channel():
        order = await get_connectors().erp.fetch_order(sales_order)
    await _require_account(order.get("account_id") if order else None)
    return tracking


@mcp.tool(title="Get open invoices",
          description="Open AR invoices for an account, with count and total computed by the gateway (read-only).",
          annotations=_READ_ONLY)
async def get_open_invoices(
    account_id: Annotated[str, Field(description="Account number, for example ACCT-1002",
                                     pattern=ACCOUNT_ID_PATTERN)],
) -> dict[str, Any]:
    # The account is the input, so its region is checked before AR is asked: a caller outside the
    # account's region causes no AR read. An account with no known region still reaches AR first, so
    # an account that does not exist stays "not found" for everyone.
    region = await _account_region(account_id)
    if region is not None:
        await _require_region(region)
    invoices = await get_connectors().ar.fetch_open_items(account_id)
    if invoices is None:
        raise NotFound(f"Account {account_id} was not found in AR.")
    if region is None:
        await _require_region(None)
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
    try:
        policy = PolicyConfig.from_env(dict(os.environ))
    except PolicyConfigError as exc:
        log.error(json.dumps({"event": "startup_failed", "reason": str(exc)}))
        raise SystemExit(2) from None
    mcp.enable_auth(auth, build_verifier(auth, settings.http_timeout_s))
    set_policy(build_policy(policy))
    set_connectors(build_connectors(settings))
    log.info(json.dumps({"event": "startup", "version": SERVER_VERSION, "backends": settings.backends(),
                         "auth": {"issuer": auth.issuer, "audience": auth.audience},
                         "policy": {"engine": "opa", "timeout_s": policy.timeout_s}}))
    mcp.run(
        "streamable-http",
        host=os.environ.get("GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("GATEWAY_INTERNAL_PORT", "8000")),
        streamable_http_path="/mcp",
        transport_security=transport_security(),
    )


if __name__ == "__main__":
    main()
