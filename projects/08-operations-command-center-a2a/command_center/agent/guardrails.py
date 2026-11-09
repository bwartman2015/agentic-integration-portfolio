"""GUARDRAILS & SAFETY: permissions, policy, approval gates, content filtering, rate limits,
human-in-the-loop and budgets.

Policy: Only delegate to agents listed in a2a/registry.json; each delegation carries the case id; resolution packages require findings from >= 2 specialists.
Approval: record_resolution auto-approved to save a package; executing the recommendation (credit memo, claim, dispute) is done by humans in the owning systems.
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
