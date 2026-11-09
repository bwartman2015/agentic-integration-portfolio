"""GUARDRAILS & SAFETY: permissions, policy, approval gates, content filtering, rate limits,
human-in-the-loop and budgets.

Policy: Deny tools outside each agent's allow-list; draft_mapping only writes mappings/<partner_id>.yaml.
Approval: request_order_decision is the human-in-the-loop step (auto-approved to SEND); draft_mapping auto-approved for new files only; activating a mapping is human-only.
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
