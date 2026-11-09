"""GUARDRAILS & SAFETY: permissions, policy, approval gates, content filtering, rate limits,
human-in-the-loop and budgets.

Policy: Deny write_test_file outside tests/ai_generated/; deny any access to secrets or .env; token budget per PR.
Approval: write_test_file auto-approved inside tests/ai_generated/; post_review_comment auto-approved (advisory); merging is always human.
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
