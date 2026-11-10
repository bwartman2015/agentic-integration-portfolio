"""Authorization (Milestone 4): Open Policy Agent decides, the gateway enforces.

Every tool call is checked twice, both times before anything is returned to the caller:
  1. the call: {principal, tool, args} -> does the caller hold the tool's scope? Pricing, credit-limit
     and payment-term changes are always denied.
  2. the data: once the record is resolved, {.., resource: {region}} -> is the record's account in one
     of the caller's regions? A record whose region is unknown is denied.
tools/list is filtered with the same policy (`allowed_tools`); call-time enforcement is the control.

The policy is `policy/gateway.rego`, served by OPA on an internal network. The principal comes only from
the verified token. Fail closed: OPA unreachable, slow, or answering anything but a well-formed decision
means deny. Decisions carry a reason code; the text a caller sees is composed here, never taken from the
policy response.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

import httpx2
from mcp.server.mcpserver.exceptions import ToolError

from .connectors.base import ConnectorError, HttpConnector, side_channel

DECISION_PATH = "/v1/data/ridgeline/gateway/decision"
ALLOWED_TOOLS_PATH = "/v1/data/ridgeline/gateway/allowed_tools"
# Reason codes the policy may return, plus the gateway's own fail-closed codes.
POLICY_REASONS = frozenset({"allowed", "invalid_request", "restricted_change", "unknown_tool", "missing_scope",
                            "region_unknown", "out_of_region"})
POLICY_UNAVAILABLE = "policy_unavailable"      # OPA unreachable, slow or returned an HTTP error
POLICY_ERROR = "policy_error"                  # OPA answered, but not with a well-formed decision
RESOURCE_NOT_CHECKED = "resource_not_checked"  # a tool returned data without asking for the data check
_IDENT = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class PolicyConfigError(RuntimeError):
    """Raised at startup when authorization is not configured (the gateway never runs without policy)."""


@dataclass(frozen=True)
class PolicyConfig:
    url: str                    # OPA base URL on the internal network, e.g. http://opa:8181
    timeout_s: float = 2.0

    @staticmethod
    def from_env(env: Mapping[str, str]) -> "PolicyConfig":
        url = (env.get("OPA_URL") or "").strip().rstrip("/")
        raw_timeout = (env.get("OPA_TIMEOUT_S") or "2").strip()
        errors = []
        try:
            parts = urlsplit(url)
            ok = (parts.scheme in ("http", "https") and bool(parts.hostname) and not parts.username
                  and not parts.password and parts.path in ("", "/") and not parts.query)
            parts.port                                   # raises ValueError on a malformed port
        except ValueError:
            ok = False
        if not ok:
            errors.append("OPA_URL must be an http(s) base URL such as http://opa:8181")
        try:
            timeout = float(raw_timeout)
        except ValueError:
            timeout = -1.0
        if not 0 < timeout <= 10:
            errors.append("OPA_TIMEOUT_S must be a number between 0 and 10")
        if errors:
            raise PolicyConfigError("invalid authorization configuration (the gateway never runs without "
                                    "policy): " + "; ".join(errors))
        return PolicyConfig(url, timeout)


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    required_scope: str | None = None


class AccessDenied(ToolError):
    """The policy refused the call or the data. `reason` is the code that goes to the call log."""

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


def denial_message(decision: Decision, principal: Mapping[str, Any], tool: str) -> str:
    """Clear text for the MCP error. Built from the reason code and verified identity only."""
    who = principal.get("client_id") or "the caller"
    if decision.reason == "missing_scope":
        scope = f"scope {decision.required_scope}" if decision.required_scope else "the required scope"
        return f"Access denied: {who} lacks {scope} for {tool}."
    if decision.reason == "restricted_change":
        return "Access denied: pricing, credit-limit and payment-term changes are human-only."
    if decision.reason == "unknown_tool":
        return f"Access denied: no policy authorizes {tool}."
    if decision.reason == "out_of_region":
        regions = ", ".join(principal.get("regions") or []) or "none"
        return (f"Access denied: this record belongs to an account outside your region "
                f"({who} may see: {regions}). Ask a CS lead.")
    if decision.reason == "region_unknown":
        return ("Access denied: this record is not linked to an account with a known region, "
                "so only a CS lead may read it.")
    return "Access denied: the policy service could not authorize this call, so it was refused."


class Policy(Protocol):
    async def decide(self, principal: Mapping[str, Any], tool: str, args: Mapping[str, Any],
                     resource: Mapping[str, Any] | None = None) -> Decision: ...

    async def allowed_tools(self, principal: Mapping[str, Any], names: list[str]) -> set[str]: ...

    async def aclose(self) -> None: ...


class DenyAllPolicy:
    """What the gateway uses until a policy is configured: nothing is allowed, nothing is listed."""

    async def decide(self, principal: Mapping[str, Any], tool: str, args: Mapping[str, Any],
                     resource: Mapping[str, Any] | None = None) -> Decision:
        return Decision(False, POLICY_UNAVAILABLE)

    async def allowed_tools(self, principal: Mapping[str, Any], names: list[str]) -> set[str]:
        return set()

    async def aclose(self) -> None:
        return None


def _principal_input(principal: Mapping[str, Any]) -> dict[str, Any]:
    return {"client_id": principal.get("client_id"), "scopes": list(principal.get("scopes") or []),
            "regions": list(principal.get("regions") or [])}


@dataclass
class OpaPolicy:
    http: HttpConnector          # POST-only, pinned to the OPA host
    base_url: str

    async def _query(self, path: str, payload: dict[str, Any]) -> Any:
        # side_channel: a policy query is not the tool's backend call, so it stays out of those log fields.
        with side_channel():
            data = await self.http.json("POST", self.base_url + path, json={"input": payload},
                                        headers={"Accept": "application/json"})
        return data.get("result") if isinstance(data, dict) else None

    async def decide(self, principal: Mapping[str, Any], tool: str, args: Mapping[str, Any],
                     resource: Mapping[str, Any] | None = None) -> Decision:
        payload: dict[str, Any] = {"principal": _principal_input(principal), "tool": tool, "args": dict(args)}
        if resource is not None:
            payload["resource"] = dict(resource)
        try:
            result = await self._query(DECISION_PATH, payload)
        except ConnectorError:
            return Decision(False, POLICY_UNAVAILABLE)
        if (not isinstance(result, dict) or not isinstance(result.get("allow"), bool)
                or result.get("reason") not in POLICY_REASONS
                or result["allow"] != (result["reason"] == "allowed")):
            return Decision(False, POLICY_ERROR)
        scope = result.get("required_scope")
        return Decision(result["allow"], result["reason"],
                        scope if isinstance(scope, str) and _IDENT.match(scope) else None)

    async def allowed_tools(self, principal: Mapping[str, Any], names: list[str]) -> set[str]:
        try:
            result = await self._query(ALLOWED_TOOLS_PATH, {"principal": _principal_input(principal)})
        except ConnectorError:
            return set()
        if not isinstance(result, list):
            return set()
        return {t for t in result if isinstance(t, str)} & set(names)

    async def aclose(self) -> None:
        await self.http.aclose()


def build_policy(config: PolicyConfig, transport: httpx2.AsyncBaseTransport | None = None) -> OpaPolicy:
    http = HttpConnector("The policy service", "opa", config.url, config.timeout_s, transport,
                         allowed_methods=frozenset({"POST"}))
    return OpaPolicy(http, config.url)
