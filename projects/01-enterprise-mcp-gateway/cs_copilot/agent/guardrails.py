"""GUARDRAILS & SAFETY: permissions, policy, approval gates, content filtering, rate limits,
human-in-the-loop and budgets.

Policy: Call OPA (OPA_URL) with {principal, tool, args}; deny on error. Pricing, credit-limit and payment-term changes are always denied.
Approval: create_case goes to an n8n approval webhook (N8N_APPROVAL_URL); deny on timeout.
"""
from typing import Any

from agentcore import Guardrails, Principal, Tool


def policy_hook(principal: Principal, tool: Tool, args: dict[str, Any]) -> tuple[bool, str]:
    """TODO(guardrails milestone): implement the policy above. Fail closed on errors."""
    return True, "allowed (TODO: real policy)"


def approval_hook(principal: Principal, tool: Tool, args: dict[str, Any]) -> bool:
    """TODO(guardrails milestone): implement the approval rule above. Fail closed."""
    return False


def get_guardrails(max_iterations: int) -> Guardrails:
    return Guardrails(max_iterations=max_iterations, max_tool_calls=15, max_cost_usd=0.50,
                      rate_limit_per_minute=60, policy=policy_hook, approval=approval_hook)
