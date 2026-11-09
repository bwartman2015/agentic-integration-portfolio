"""PLAN: break a goal into ordered steps before acting. Plans are logged in the trace."""
from agentcore import LLMPlanner, LLMProvider, Planner

PLANNING_AGENTS = ['operations-command-center']


def get_planner(agent_name: str, llm: LLMProvider) -> Planner | None:
    return LLMPlanner(llm, max_steps=6) if agent_name in PLANNING_AGENTS else None
