"""GUARDRAILS & SAFETY: permissions, policy, approval gates, content filtering, rate limits,
human-in-the-loop and budgets.

Policy: Call OPA (OPA_URL) with {principal, tool, args}; deny on error. Pricing, credit-limit and payment-term changes are always denied.
Approval: create_case goes to an n8n approval webhook (N8N_APPROVAL_URL); deny on timeout.

Milestone 0 implements the policy as a local deterministic rule set behind the same hook
(OPA replaces it in Milestone 4). The approval hook denies every write until the n8n webhook
lands in Milestone 5, so writes fail closed.
"""
from __future__ import annotations

import re
from typing import Any

from agentcore import Guardrails, Principal, Tool

# Tools this agent may ever call (deny by default: a new gateway tool is blocked until listed here).
ALLOWED_TOOLS = {"search_accounts", "get_sales_order", "track_shipment", "get_open_invoices", "create_case",
                 "load_skill"}
# Pricing, credit-limit and payment-term changes are human-only, whatever the tool is called.
RESTRICTED = re.compile(r"pric|discount|rebate|credit[_ -]?limit|payment[_ -]?terms?", re.I)


def policy_hook(principal: Principal, tool: Tool, args: dict[str, Any]) -> tuple[bool, str]:
    """Local policy (OPA in Milestone 4). Fails closed: any error denies."""
    try:
        if RESTRICTED.search(tool.name) or (tool.side_effect == "write" and any(RESTRICTED.search(k) for k in args)):
            return False, f"{tool.name}: pricing, credit-limit and payment-term changes are human-only"
        if tool.name not in ALLOWED_TOOLS:
            return False, f"{tool.name} is not on the cs-copilot tool allow-list"
        return True, "allowed by local policy"
    except Exception as e:  # noqa: BLE001  fail closed on any policy error
        return False, f"policy error ({type(e).__name__}); denied"


def approval_hook(principal: Principal, tool: Tool, args: dict[str, Any]) -> bool:
    """Human approval for writes. Until the n8n webhook (Milestone 5) there is no approver, so deny."""
    return False


def get_guardrails(max_iterations: int) -> Guardrails:
    return Guardrails(max_iterations=max_iterations, max_tool_calls=15, max_cost_usd=0.50,
                      max_input_chars=4000, rate_limit_per_minute=60, policy=policy_hook, approval=approval_hook)
