"""ORCHESTRATION (concept 2): controls who does what.

A manager agent directs micro agents inside an environment. Here a micro agent is exposed
to the manager as a Tool ("agent as tool"), so the manager uses the same seven-layer loop,
the same guardrails and the same tracing to delegate. Each delegation is an act.<agent> span.
Remote micro agents (other processes/teams) plug in the same way through a2a.remote_agent_tool.
"""
from __future__ import annotations

from typing import Any

from .act import Tool
from .types import Principal


def agent_as_tool(agent: Any, *, name: str, description: str, principal: Principal,
                  scope: str | None = None) -> Tool:
    """Wrap a local agentcore.Agent so a manager agent can delegate a sub-task to it.
    `principal` is the micro agent's own service identity (least privilege per specialist)."""
    def delegate(task: str) -> dict[str, Any]:
        out = agent.run(task, principal)
        return {"agent": agent.name, "status": out.status, "answer": out.answer,
                "trace_id": out.trace_id, "tools_used": out.tools_used, "cost_usd": out.cost_usd}
    return Tool(name, description, handler=delegate, params={"task": "string"}, required=["task"],
                scope=scope, side_effect="read", category="external")


MANAGER_PROMPT = """You are a manager agent. You do not do specialist work yourself.
Break the goal into sub-tasks, delegate each to the best specialist tool (each tool is an agent),
check their answers, and combine them into one final answer that credits which specialist found what.
If a specialist escalates, decide whether another specialist can help or escalate yourself."""
