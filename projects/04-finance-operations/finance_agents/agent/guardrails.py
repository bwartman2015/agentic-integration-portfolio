"""GUARDRAILS & SAFETY: permissions, policy, approval gates, content filtering, rate limits,
human-in-the-loop and budgets.

Policy: Segregation of duties: agents hold only *.read and *.propose scopes, never post/pay; deny proposals when the period is closed; amounts must balance.
Approval: Proposals are written to the clerk review queue (auto-approved to CREATE a proposal); posting is a human action outside the agent's reach.
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
