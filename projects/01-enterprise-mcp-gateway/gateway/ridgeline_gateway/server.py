"""Ridgeline MCP gateway (Milestone 5: guardrails, audit, approvals).

An MCP server (official `mcp` SDK, `MCPServer`, formerly FastMCP; streamable HTTP at /mcp) that exposes
Ridgeline's enterprise systems as four read-only tools and one write tool (`create_case`). Each tool
calls a connector (`connectors/`), which has a `seed` backend (offline synthetic data) and a live backend
(Salesforce, SAP sandbox, the mock carrier and AR APIs), chosen per system in `.env` (see config.py).
Every /mcp request must carry a valid Keycloak access token (auth.py); /healthz and the RFC 9728
metadata stay open.

Every tools/call goes through `GatewayServer.call_tool`, the single choke point. In order, it:
  1. applies the caller's rate limit (ratelimit.py);
  2. rejects unknown tools;
  3. asks OPA whether this caller may call this tool (scope rule, restricted changes; policy.py), and
     denies on any error;
  4. rejects unexpected arguments and runs the tool, which reads the record and then asks OPA whether
     the caller may see it (region rule) before returning anything. A write tool then asks a human
     (approvals.py) and writes only after an explicit approval;
  5. refuses a result if the tool never asked for that data check (fail closed for future tools);
  6. masks contact emails and phone numbers unless the caller's token holds the PII scope (pii.py);
  7. writes one audit row (audit.py) and one JSON log line for every outcome, with the verified client
     and the reason. A read that cannot be audited is not returned.
tools/list shows a caller only the tools its scopes cover. Crashes reach the client only as "Error
executing tool <name>"; connector failures as a sanitized message; denials as "Access denied: ...".

Other routes: POST /approvals/callback (n8n reports a human decision; one-time token per request),
GET /approvals/pending (operator token; the approval links), GET /audit (token with the audit scope).

Security today: authentication (JWT), authorization (OPA), approvals, audit and rate limits, all fail
closed. The container only publishes on 127.0.0.1, Host and Origin headers are checked against an
allowlist (DNS-rebinding protection), and arguments are validated against closed JSON Schemas. Secrets
and tokens come only from the environment / request and are never logged.
"""
from __future__ import annotations

import asyncio
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

import anyio
from mcp.server.auth.provider import TokenVerifier
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import Tool as MCPTool
from mcp_types import ToolAnnotations
from pydantic import Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from .approvals import (DENIAL_REASONS, ApprovalConfig, ApprovalConfigError, Approver, NoApprover,
                        build_approver)
from .audit import (AUDIT_UNAVAILABLE, AuditConfig, AuditConfigError, AuditSink, AuditUnavailable,
                    PostgresAudit, UnavailableAudit)
from .auth import AuthConfig, AuthConfigError, build_verifier, current_principal
from .config import ConfigError, load_settings
from .connectors import ConnectorError, Connectors, NotFound, begin_upstream, build_connectors
from .connectors.base import side_channel
from .pii import audit_args, mask_json_text, mask_value
from .policy import (AUDIT_SCOPE, PII_SCOPE, RATE_LIMITED, RESOURCE_NOT_CHECKED, AccessDenied, Decision,
                     DenyAllPolicy, Policy, PolicyConfig, PolicyConfigError, build_policy, denial_message)
from .ratelimit import RateLimitConfig, RateLimitConfigError, RateLimiter

SERVER_NAME = "ridgeline-gateway"
SERVER_VERSION = "0.5.0"
SALES_ORDER_PATTERN = r"^SO-[0-9]{6}$"
ACCOUNT_ID_PATTERN = r"^ACCT-[0-9]{4}$"
ACCOUNT_QUERY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 &.,'-]{1,79}$"
# Free text a human approver will read in a web form: letters, digits and plain punctuation only (no
# angle brackets, quotes or braces), so it cannot carry markup or template syntax anywhere downstream.
CASE_SUBJECT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 .,:;'()#/&-]{2,119}$"
CASE_SUMMARY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 .,:;'()#/&%$@+!?-]{2,499}$"
MAX_CALLBACK_BYTES = 4096
# Trace headers are caller-supplied: only well-formed values are logged, anything else as "invalid".
_TRACE_HEADERS = {
    "traceparent": re.compile(r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"),
    "x-request-id": re.compile(r"^[A-Za-z0-9._-]{1,64}$"),
}
# openWorldHint: the tools reach external systems (Salesforce, SAP, carrier, AR) whose data changes.
_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)

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


_audit: AuditSink = UnavailableAudit()


def get_audit() -> AuditSink:
    """The audit store. Until main() (or a test) sets one, nothing can be recorded, so reads are refused."""
    return _audit


def set_audit(audit: AuditSink | None) -> None:
    global _audit
    _audit = audit if audit is not None else UnavailableAudit()


_approver: Approver = NoApprover()


def get_approver() -> Approver:
    """The human-approval service for write tools. Until one is set, every write is refused."""
    return _approver


def set_approver(approver: Approver | None) -> None:
    global _approver
    _approver = approver if approver is not None else NoApprover()


_limiter = RateLimiter()


def get_limiter() -> RateLimiter:
    return _limiter


def set_limiter(limiter: RateLimiter | None) -> None:
    global _limiter
    _limiter = limiter if limiter is not None else RateLimiter()


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
    outcome: str = "ok"
    error: str | None = None           # exception type name for crashes and upstream failures
    write: bool = False                # set by a write tool: its approval record is its audit trail
    approval_id: str | None = None
    pii_masked: int = 0                # values masked in the result (caller lacks the PII scope)
    trace_id: str | None = None        # the caller's well-formed trace header (x-request-id / traceparent)


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
        auth = _CallAuth(current_principal(), name, dict(arguments) if isinstance(arguments, dict) else {})
        auth.trace_id = caller_trace_id(_trace_fields(context))
        auth_token = _call_auth.set(auth)
        result: Any = None
        failure: BaseException | None = None
        try:
            result = await self._governed_call(tool, name, arguments, context, auth)
        except asyncio.CancelledError as exc:
            # The client cancelled the request or went away. The call is still recorded (shielded
            # below), then the cancellation goes on.
            auth.outcome = "cancelled"
            failure = exc
        except Exception as exc:  # noqa: BLE001  re-raised below, after the call has been recorded
            failure = exc
        finally:
            _call_auth.reset(auth_token)
        # Unknown tool names and their arguments are caller-supplied text, so they are not recorded verbatim.
        known = tool is not None
        record = _call_record(name if known else "<unknown>", auth, started, context, upstream, known)
        row = {**record, "reason": record.get("policy_reason"), "args": audit_args(auth.args) if known else {},
               "trace_id": record.get("traceparent") or record.get("x_request_id")}
        try:
            # Shielded: a cancelled call is recorded too ("every call, allowed or denied, is audited").
            with anyio.CancelScope(shield=True):
                await get_audit().write(row)
        except AuditUnavailable:
            record["audited"] = False
            if failure is None and not auth.write:
                # A read that cannot be recorded is not returned. (An approved write is already recorded
                # in the approvals table, and has happened: it is reported, with audited=false logged.)
                record.update(outcome="denied", ok=False, policy_reason=AUDIT_UNAVAILABLE)
                failure = AccessDenied(denial_message(Decision(False, AUDIT_UNAVAILABLE), auth.principal, name),
                                       AUDIT_UNAVAILABLE)
        log.info(json.dumps(record, sort_keys=True))
        if failure is not None:
            raise failure
        return result

    async def _governed_call(self, tool: Any, name: str, arguments: dict[str, Any], context: Context | None,
                             auth: _CallAuth) -> Any:
        try:
            if not get_limiter().allow(str(auth.principal.get("client_id") or "<anonymous>")):
                raise AccessDenied(denial_message(Decision(False, RATE_LIMITED), auth.principal, name), RATE_LIMITED)
            if tool is None:
                auth.outcome = "unknown_tool"
                raise ToolError("Unknown tool.")
            decision = await _decide(auth)
            if not decision.allow:
                raise AccessDenied(denial_message(decision, auth.principal, name), decision.reason)
            unexpected = sorted(set(arguments) - set(tool.parameters.get("properties", {})))
            if unexpected:
                auth.outcome = "invalid_arguments"
                raise ToolError(f"Unexpected argument(s) for {name}: {', '.join(unexpected)}")
            result = await super().call_tool(name, arguments, context)
            if not auth.resource_checked:
                # A tool that returns data without the data check is a bug: refuse instead of leaking.
                raise AccessDenied(denial_message(Decision(False, RESOURCE_NOT_CHECKED), auth.principal, name),
                                   RESOURCE_NOT_CHECKED)
            if PII_SCOPE not in (auth.principal.get("scopes") or []):
                auth.pii_masked = _mask_result(result)
            return result
        except AccessDenied as exc:
            auth.outcome = "denied"
            auth.reasons = [exc.reason]
            raise
        except UnexpectedToolError as exc:
            # The SDK already sends the client only "Error executing tool <name>"; the type stays here.
            auth.outcome, auth.error = "error", type(exc.__cause__).__name__
            raise
        except ToolError as exc:
            if isinstance(exc.__cause__, AccessDenied):
                # Raised inside the tool by the data check; the SDK wrapped it. Send the denial itself.
                auth.outcome = "denied"
                auth.reasons = [exc.__cause__.reason]
                raise exc.__cause__ from None
            if auth.outcome == "ok":
                cause = exc.__cause__
                auth.outcome = ("not_found" if isinstance(cause, NotFound)
                                else "upstream_error" if isinstance(cause, ConnectorError)
                                else "invalid_arguments" if isinstance(cause, ValidationError) else "error")
                if isinstance(cause, ConnectorError):
                    auth.error = type(cause).__name__
            raise
        except Exception as exc:
            auth.outcome, auth.error = "error", type(exc).__name__
            raise


def _mask_result(result: Any) -> int:
    """Mask PII in a tool result in place (structured content and its text form). Returns the number
    of values masked. Any surprise in the result's shape raises, so nothing unmasked is returned."""
    masked_total = 0
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        result.structured_content, masked_total = mask_value(structured)
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            block.text, n = mask_json_text(text)
            masked_total = max(masked_total, n)
    return masked_total


def caller_trace_id(fields: dict[str, Any]) -> str | None:
    """The caller's trace id for records that outlive the call (approval requests): a well-formed
    traceparent, else a well-formed x-request-id, else None. A malformed header is never stored."""
    return next((fields[k] for k in ("traceparent", "x_request_id") if fields.get(k) and fields[k] != "invalid"), None)


def _trace_fields(context: Context | None) -> dict[str, Any]:
    """The JSON-RPC request id and any well-formed trace header, so audit rows join to agent traces."""
    fields: dict[str, Any] = {}
    if context is not None:
        try:
            fields["request_id"] = str(context.request_id)
            headers = context.headers or {}
        except (AttributeError, ValueError, LookupError):
            headers = {}
        for h, pattern in _TRACE_HEADERS.items():
            value = headers.get(h)
            if value:
                fields[h.replace("-", "_")] = value if pattern.fullmatch(value) else "invalid"
    return fields


def _call_record(tool: str, auth: _CallAuth, started: float, context: Context | None, upstream: Any,
                 known_tool: bool) -> dict[str, Any]:
    """OBSERVE: the record of one tool call, whatever the outcome. It is written to the audit table and
    logged as one JSON line. Argument values are not in it (the audit row stores them masked); a denial
    has outcome "denied" and the reason code."""
    record: dict[str, Any] = {"event": "tool_call", "tool": tool, "outcome": auth.outcome,
                              "ok": auth.outcome == "ok",
                              "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
    extra = {**auth.principal, "backend": upstream.backend or None, "upstream_status": upstream.status,
             "upstream_ms": upstream.ms if upstream.calls else None, "upstream_calls": upstream.calls or None,
             "policy_reason": auth.reasons[0] if auth.outcome == "denied" and auth.reasons else None,
             "policy_ms": auth.policy_ms if known_tool else None, "hidden_out_of_region": auth.hidden or None,
             "pii_masked": auth.pii_masked or None, "approval_id": auth.approval_id,
             "error_type": auth.error}
    record.update({k: v for k, v in extra.items() if v is not None})
    record.update(_trace_fields(context))
    return record


def _log_call(tool: str, outcome: str, started: float, context: Context | None, error: str | None,
              extra: dict[str, Any] | None = None) -> None:
    """One structured line for an event outside call_tool (kept for callers that log a call directly)."""
    record: dict[str, Any] = {"event": "tool_call", "tool": tool, "outcome": outcome, "ok": outcome == "ok",
                              "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
    record.update({k: v for k, v in (extra or {}).items() if v is not None})
    if error:
        record["error_type"] = error
    record.update(_trace_fields(context))
    log.info(json.dumps(record, sort_keys=True))


@asynccontextmanager
async def _lifespan(_server: MCPServer):
    try:
        yield None
    finally:
        if _connectors is not None:
            await _connectors.aclose()
        await _policy.aclose()
        await _approver.aclose()
        await _audit.aclose()


mcp = GatewayServer(
    lifespan=_lifespan,
    name=SERVER_NAME,
    title="Ridgeline Supply Co. MCP gateway",
    version=SERVER_VERSION,
    instructions=("Ridgeline Supply Co. MCP gateway (synthetic data). Four read-only tools over CRM, ERP, "
                  "carrier and AR systems, and create_case, a write that runs only after a human approves it. "
                  "Requires a Keycloak access token (OAuth 2.0 client credentials). Each tool needs its scope, "
                  "records are visible only for accounts in the caller's regions, contact details are masked "
                  "without the PII scope, and every call is rate limited and audited."),
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


@mcp.tool(title="Create case",
          description=("Create a CRM case for an account (a write). It runs only after a human approves it in the "
                       "approval workflow: the call waits for that decision and is refused on rejection or timeout."),
          annotations=_WRITE)
async def create_case(
    account_id: Annotated[str, Field(description="Account number, for example ACCT-1002", pattern=ACCOUNT_ID_PATTERN)],
    subject: Annotated[str, Field(description="Short case subject, for example Late delivery SO-104233",
                                  pattern=CASE_SUBJECT_PATTERN)],
    summary: Annotated[str, Field(description="What happened and the proposed next step (plain text)",
                                  pattern=CASE_SUMMARY_PATTERN)],
) -> dict[str, Any]:
    auth = _current_call()
    auth.write = True
    with side_channel():
        matches = await get_connectors().crm.find_accounts(account_id)
    account = next((m for m in matches if m.get("account_id") == account_id), None)
    if account is None:
        raise NotFound(f"Account {account_id} was not found in CRM.")
    region = account.get("region")
    await _require_region(region if isinstance(region, str) and region else None)
    # Agents propose; a human approves. Nothing is written unless the approver says "Approve".
    details = {"trace_id": auth.trace_id, "account_id": account_id, "account_name": re.sub(r"[^A-Za-z0-9 .,&'()-]", "", str(account.get("name") or ""))[:80],
               "subject": subject, "summary": summary}
    try:
        approval = await get_approver().request(auth.principal, auth.tool, details,
                                                on_request_id=lambda rid: setattr(auth, "approval_id", rid))
    except AuditUnavailable:
        raise AccessDenied(denial_message(Decision(False, AUDIT_UNAVAILABLE), auth.principal, auth.tool),
                           AUDIT_UNAVAILABLE) from None
    auth.approval_id = approval.request_id
    if not approval.approved:
        reason = DENIAL_REASONS[approval.status]
        raise AccessDenied(denial_message(Decision(False, reason), auth.principal, auth.tool), reason)
    case = await get_connectors().cases.open_case(account_id, subject, summary)
    return {**case, "approval": {"request_id": approval.request_id, "approved_by": approval.approver}}


def _bearer(request: Request) -> str:
    scheme, _, value = (request.headers.get("authorization") or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


@mcp.custom_route("/approvals/callback", methods=["POST"])
async def approvals_callback(request: Request) -> JSONResponse:
    """n8n reports a human decision. No bearer token: the proof is the one-time token that was given
    only to this request's n8n execution. Unknown, expired or repeated callbacks change nothing."""
    raw = await request.body()
    try:
        body = json.loads(raw) if len(raw) <= MAX_CALLBACK_BYTES else None
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid request"}, status_code=400)
    accepted = get_approver().resolve(body.get("request_id"), body.get("token"), body.get("decision"),
                                      body.get("approver"))
    log.info(json.dumps({"event": "approval_callback", "accepted": accepted}))
    if not accepted:
        return JSONResponse({"ok": False, "error": "no pending approval for this request"}, status_code=404)
    return JSONResponse({"ok": True})


@mcp.custom_route("/approvals/pending", methods=["GET"])
async def approvals_pending(request: Request) -> JSONResponse:
    """Operator only (APPROVAL_OPERATOR_TOKEN, which no agent client holds): the requests waiting for a
    human, with their approval links. `make approvals P=01` prints this."""
    token = _bearer(request)
    if not token or not get_approver().operator_token_ok(token):
        return JSONResponse({"error": "operator token required"}, status_code=401)
    return JSONResponse({"pending": get_approver().pending()})


@mcp.custom_route("/audit", methods=["GET"])
async def audit_report(request: Request) -> JSONResponse:
    """The audit report: totals by outcome and reason, recent calls and approval requests. Needs a valid
    Keycloak token that holds the audit scope (`audit_scope` in policy/data.json)."""
    verifier = mcp._token_verifier
    token = _bearer(request)
    access = await verifier.verify_token(token) if verifier is not None and token else None
    if access is None:
        return JSONResponse({"error": "a valid access token is required"}, status_code=401)
    if AUDIT_SCOPE not in (access.scopes or []):
        return JSONResponse({"error": f"scope {AUDIT_SCOPE} is required"}, status_code=403)
    raw_limit = request.query_params.get("limit", "50")
    limit = int(raw_limit) if raw_limit.isdigit() else 50
    try:
        return JSONResponse(await get_audit().report(limit))
    except AuditUnavailable:
        return JSONResponse({"error": "the audit store is unavailable"}, status_code=503)


# Which database the audit log is in, as the database itself says (db/stamp.sql), read at startup:
# main | test | local | unknown. The wire tests read it from /healthz and refuse to run against main.
_database_environment = "unknown"


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    """Liveness for Docker's healthcheck. Unauthenticated by design; reveals no data, and of the
    configuration only the database's environment label (no host, name or credentials)."""
    return JSONResponse({"status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION,
                         "database": _database_environment})


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


async def close_orphaned_approvals(audit: AuditSink) -> list[str]:
    """Startup: approval requests left `pending` by a gateway that stopped while a human was deciding
    are closed as `unavailable`. Nothing was written for them, and no process can resolve them now (a
    late form submission reaches no waiting request; the gateway answers it with 404). Failure is
    logged, not fatal: the audit store is checked again on every call."""
    try:
        closed = await audit.close_orphaned_approvals()
    except AuditUnavailable as exc:
        log.warning(json.dumps({"event": "orphaned_approvals_not_checked", "reason": str(exc)}))
        return []
    if closed:
        log.info(json.dumps({"event": "orphaned_approvals_closed", "count": len(closed), "request_ids": closed}))
    return closed


async def read_environment(audit: AuditSink) -> str:
    """The database's environment stamp, or "unknown" (never stamped, or unreadable: logged, not fatal)."""
    try:
        return await audit.environment() or "unknown"
    except AuditUnavailable as exc:
        log.warning(json.dumps({"event": "database_environment_unknown", "reason": str(exc)}))
        return "unknown"


async def _startup_checks(config: AuditConfig) -> str:
    # Its own short-lived connection: the server's pool is created later, on the server's event loop.
    sink = PostgresAudit(config)
    try:
        await close_orphaned_approvals(sink)
        return await read_environment(sink)
    finally:
        await sink.aclose()


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
    try:
        audit_config = AuditConfig.from_env(dict(os.environ))
        approval_config = ApprovalConfig.from_env(dict(os.environ))
        rate_config = RateLimitConfig.from_env(dict(os.environ))
    except (AuditConfigError, ApprovalConfigError, RateLimitConfigError) as exc:
        log.error(json.dumps({"event": "startup_failed", "reason": str(exc)}))
        raise SystemExit(2) from None
    mcp.enable_auth(auth, build_verifier(auth, settings.http_timeout_s))
    set_policy(build_policy(policy))
    set_connectors(build_connectors(settings))
    global _database_environment
    _database_environment = asyncio.run(_startup_checks(audit_config))
    audit = PostgresAudit(audit_config)
    set_audit(audit)
    set_approver(build_approver(approval_config, audit, settings.http_timeout_s))
    set_limiter(RateLimiter(rate_config))
    log.info(json.dumps({"event": "startup", "version": SERVER_VERSION, "backends": settings.backends(),
                         "auth": {"issuer": auth.issuer, "audience": auth.audience},
                         "policy": {"engine": "opa", "timeout_s": policy.timeout_s},
                         "audit": {"store": "postgres", "source": audit_config.source, "host": audit_config.host,
                                   "environment": _database_environment},
                         "approvals": {"engine": "n8n", "timeout_s": approval_config.timeout_s},
                         "rate_limit": {"per_minute": rate_config.per_minute,
                                        "overrides": len(rate_config.overrides)}}))
    mcp.run(
        "streamable-http",
        host=os.environ.get("GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("GATEWAY_INTERNAL_PORT", "8000")),
        streamable_http_path="/mcp",
        transport_security=transport_security(),
    )


if __name__ == "__main__":
    main()
