"""GUARDRAILS & SAFETY: permissions, policy, approval gates, content filtering, rate limits,
human-in-the-loop and budgets.

Policy: Call OPA (OPA_URL) with {principal, tool, args}; deny on error. Pricing, credit-limit and payment-term changes are always denied.
Approval: create_case needs a human approval (an n8n workflow, run by the gateway); deny on timeout.

Milestone 4: the policy is the gateway's, `policy/gateway.rego`, served by OPA. The copilot asks the same
question the gateway asks: {principal, tool, args} plus the region of the record the call would read.
  - OPA_URL set: every tool call is decided by OPA (`opa_decision`); any error, timeout or malformed
    answer denies.
  - OPA_URL unset (the offline demo, tests and scripted evals): `local_decision`, a deterministic copy of
    the same rules that reads the same `policy/data.json`. `policy/decision_cases.json` is run against
    both the Rego (make opa-test) and this copy (tests/test_policy.py), so they cannot drift.
Restricted argument names are matched at any depth (Milestone 5).

Two backends (Milestone 6):
  - Gateway (`get_guardrails(gateway=True)`, chosen by build.py with the tools). Tools are the gateway's MCP tools. The copilot still
    checks scope and restricted changes before a call (defense in depth, and no wasted gateway calls), but
    the region rule is the gateway's: only it knows each record's account. `create_case` is approved by a
    human in the gateway's n8n workflow, so this hook lets it through to that approval; any other write is
    refused here.
  - In-process (offline demo, tests, scripted evals): the copilot resolves a record's region from the
    mock systems itself, and a write runs only if an approver is installed. None is by default, so writes
    fail closed; the scripted evals install one.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import urllib.error
import urllib.request
from functools import partial
from typing import Any, Callable

from agentcore import Guardrails, Principal, Tool
from mock_systems import get_systems

POLICY_DIR = pathlib.Path(__file__).resolve().parents[2] / "policy"
_POLICY: dict[str, Any] = json.loads((POLICY_DIR / "data.json").read_text())["gateway_policy"]
TOOL_SCOPES: dict[str, str] = _POLICY["tool_scopes"]
WRITE_TOOLS: frozenset[str] = frozenset(_POLICY["write_tools"])
# Pricing, credit-limit and payment-term changes are human-only, whatever the tool is called.
RESTRICTED = re.compile(_POLICY["restricted_pattern"])
# Tool and argument names must be plain ASCII identifiers, in the Rego and here, so the two regex
# engines (Go RE2 in OPA, Python re) can never disagree about Unicode case folding.
NAME = re.compile(_POLICY["name_pattern"].rstrip("$") + r"\Z")
# agent-core's own tools, which never reach an enterprise system and are not gateway tools.
LOCAL_TOOLS = frozenset({"load_skill"})
# Writes whose human approval the gateway runs itself (n8n). Any other write is refused by the copilot.
GATEWAY_APPROVED_WRITES = frozenset({"create_case"})
DECISION_PATH = "/v1/data/ridgeline/gateway/decision"
# One reason-code list for the Rego, the gateway and this module: policy/data.json.
REASONS: frozenset[str] = frozenset(_POLICY["reasons"])
_MISSING = object()


def _deny(reason: str, **extra: str) -> dict[str, Any]:
    return {"allow": False, "reason": reason, **extra}


def _arg_keys(args: dict[str, Any]) -> set[Any]:
    """Every argument name at any depth (`arg_keys` in the Rego). Iterative, so depth cannot crash it."""
    keys: set[Any] = set()
    stack: list[Any] = [args]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            keys.update(value)
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return keys


def local_decision(request: dict[str, Any]) -> dict[str, Any]:
    """`decision` in policy/gateway.rego, rule for rule (first match wins). Default deny."""
    principal = request.get("principal")
    tool, args, resource = request.get("tool"), request.get("args", {}), request.get("resource", _MISSING)
    if (not isinstance(principal, dict) or not isinstance(principal.get("client_id"), str)
            or not isinstance(principal.get("scopes"), list) or not isinstance(tool, str)
            or not isinstance(args, dict) or not (resource is _MISSING or isinstance(resource, dict))
            or not isinstance(principal.get("regions", []), list) or not NAME.fullmatch(tool)):
        return _deny("invalid_request")
    arg_keys = _arg_keys(args)
    if not all(isinstance(k, str) and NAME.fullmatch(k) for k in arg_keys):
        return _deny("invalid_request")
    scopes = {s for s in principal["scopes"] if isinstance(s, str)}
    regions = {r for r in principal.get("regions", []) if isinstance(r, str)}
    if RESTRICTED.search(tool) or (tool in WRITE_TOOLS and any(RESTRICTED.search(k) for k in arg_keys)):
        return _deny("restricted_change")
    if tool not in TOOL_SCOPES:
        return _deny("unknown_tool")
    if TOOL_SCOPES[tool] not in scopes:
        return _deny("missing_scope", required_scope=TOOL_SCOPES[tool])
    if resource is not _MISSING and "*" not in regions:
        region = resource.get("region")
        if not isinstance(region, str) or not region:
            return _deny("region_unknown")
        if region not in regions:
            return _deny("out_of_region")
    return {"allow": True, "reason": "allowed"}


def opa_decision(request: dict[str, Any], url: str, timeout_s: float = 2.0) -> dict[str, Any]:
    """Ask OPA. Fail closed: unreachable, slow, an HTTP error or a malformed decision all deny."""
    try:
        req = urllib.request.Request(url.rstrip("/") + DECISION_PATH, data=json.dumps({"input": request}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            result = json.loads(resp.read()).get("result")
    except (OSError, ValueError, AttributeError, urllib.error.URLError):
        return _deny("policy_unavailable")
    if (not isinstance(result, dict) or not isinstance(result.get("allow"), bool)
            or result.get("reason") not in REASONS or result["allow"] != (result["reason"] == "allowed")):
        return _deny("policy_error")
    return result


def decide(request: dict[str, Any]) -> dict[str, Any]:
    url = os.environ.get("OPA_URL", "").strip()
    return opa_decision(request, url) if url else local_decision(request)


def principal_input(principal: Principal) -> dict[str, Any]:
    return {"client_id": principal.id, "scopes": sorted(principal.scopes), "regions": list(principal.attr("regions"))}


def may_see(principal: Principal, tool_name: str, region: str | None) -> bool:
    """Data rule for one record: may this caller see, through this tool, a record of an account in
    `region` (None = unknown)? STATE uses it too, so injected context obeys the same policy as tools."""
    return decide({"principal": principal_input(principal), "tool": tool_name, "args": {},
                   "resource": {"region": region}})["allow"]


def resource_regions(tool_name: str, args: dict[str, Any]) -> list[str | None] | None:
    """Regions of the accounts whose records this call would return; None when there is no record to
    protect (it does not exist, so the tool itself reports "not found")."""
    dataset = get_systems().dataset

    def region_of(account_id: Any) -> str | None:
        return (dataset.accounts.get(account_id) or {}).get("region") or None

    if tool_name == "search_accounts":
        try:
            hits = get_systems().crm.find_accounts(str(args.get("query", "")))
        except ValueError:
            return None
        return sorted({h.get("region") or None for h in hits}, key=str) or None
    if "sales_order" in args:
        order = dataset.orders.get(str(args["sales_order"]).strip().upper())
        return None if order is None else [region_of(order.get("account_id"))]
    if "account_id" in args:
        account_id = str(args["account_id"]).strip().upper()
        return None if account_id not in dataset.accounts else [region_of(account_id)]
    return None


def _explain(decision: dict[str, Any], principal: Principal, tool_name: str) -> str:
    reason = decision.get("reason")
    if reason == "restricted_change":
        return f"{tool_name}: pricing, credit-limit and payment-term changes are human-only"
    if reason == "unknown_tool":
        return f"{tool_name} is not a tool the gateway policy authorizes"
    if reason == "missing_scope":
        return f"{principal.id} lacks scope {decision.get('required_scope')} for {tool_name}"
    if reason == "out_of_region":
        regions = ", ".join(principal.attr("regions")) or "none"
        return (f"out_of_region: this record belongs to an account outside your region ({principal.id} may "
                f"see: {regions}); do not retry, hand the request to a CS lead")
    if reason == "region_unknown":
        return ("region_unknown: this record is not linked to an account with a known region; only a CS "
                "lead may read it")
    return f"the policy service could not authorize {tool_name} ({reason}); denied"


def policy_hook(principal: Principal, tool: Tool, args: dict[str, Any], gateway: bool = False) -> tuple[bool, str]:
    """Policy decision for one tool call. Fails closed: any error denies. `gateway`: the region rule is
    left to the gateway, which knows each record's account."""
    try:
        if tool.name in LOCAL_TOOLS:
            return True, "local tool"
        request = {"principal": principal_input(principal), "tool": tool.name, "args": dict(args)}
        decision = decide(request)
        if decision["allow"] and not gateway:
            # Data rule, in-process backend only (the gateway applies it itself, per record). A search
            # that would return any account outside the caller's regions is denied whole; the gateway
            # filters the matches instead.
            for region in resource_regions(tool.name, args) or []:
                decision = decide({**request, "resource": {"region": region}})
                if not decision["allow"]:
                    break
        if decision["allow"]:
            return True, "allowed by policy"
        return False, _explain(decision, principal, tool.name)
    except Exception as e:  # noqa: BLE001  fail closed on any policy error
        return False, f"policy error ({type(e).__name__}); denied"


# Human approval for writes. The gateway owns the real approval flow (n8n, Milestone 5): with the gateway
# backend, `create_case` goes to it and waits there for a person (and is refused on rejection or timeout).
# The in-process `create_case` runs only if an approver is installed, and none is by default (fail
# closed). The scripted evals install one to exercise what the agent does after "approved", "rejected"
# and "timed out".
Approver = Callable[[Principal, Tool, dict[str, Any]], bool]
_approver: Approver | None = None


def set_approver(approver: Approver | None) -> None:
    global _approver
    _approver = approver


def approval_hook(principal: Principal, tool: Tool, args: dict[str, Any], gateway: bool = False) -> bool:
    """Gateway backend: pass `create_case` on to the gateway's human approval, refuse other writes.
    In-process: true only when an installed approver says yes. No approver, or any error, denies."""
    if gateway:
        return tool.name in GATEWAY_APPROVED_WRITES and tool.category == "mcp"
    if _approver is None:
        return False
    try:
        return _approver(principal, tool, args) is True
    except Exception:  # noqa: BLE001  timeout, unreachable approver, anything: fail closed
        return False


def scripted_approver(decision: str | None) -> Approver | None:
    """The approver a scripted eval installs: "approve", "reject", or "timeout" (the approver never
    answers, which the hook treats as a denial). None installs nothing."""
    if decision is None:
        return None
    if decision not in ("approve", "reject", "timeout"):
        raise ValueError(f"unknown scripted approval {decision!r}")

    def approver(principal: Principal, tool: Tool, args: dict[str, Any]) -> bool:
        if decision == "timeout":
            raise TimeoutError("no human decision in time")
        return decision == "approve"
    return approver


def mask_pii(text: str) -> str:
    """The PII mask used for answers, also applied to progress files and traces at rest (build.py)."""
    return _MASKER.mask(text)


_MASKER = Guardrails()


def get_guardrails(max_iterations: int, gateway: bool = False) -> Guardrails:
    return Guardrails(max_iterations=max_iterations, max_tool_calls=15, max_cost_usd=0.50,
                      max_input_chars=4000, rate_limit_per_minute=60,
                      policy=partial(policy_hook, gateway=gateway), approval=partial(approval_hook, gateway=gateway))
