"""PLAN layer: break a goal into ordered, inspectable sub-tasks before acting."""
from __future__ import annotations

import json
from typing import Any, Protocol

from .reason import LLMProvider, extract_json
from .types import Percept, Plan, Step

PLANNER_PROMPT = """You are a planning module. Break the goal into the fewest ordered steps that use catalog tools.
Reply with JSON only: {"goal": "...", "steps": [{"description": "...", "tool": "<name>", "args": {...}}]}"""


class Planner(Protocol):
    def make_plan(self, percept: Percept, history: list[dict[str, str]], catalog: list[dict[str, Any]]) -> Plan: ...


class LLMPlanner:
    def __init__(self, llm: LLMProvider, max_steps: int = 6):
        self.llm, self.max_steps = llm, max_steps
        self.last_response = None

    def make_plan(self, percept, history, catalog):
        msg = json.dumps({"goal": percept.content, "entities": percept.entities,
                          "progress_so_far": history[-6:], "tool_catalog": catalog})
        resp = self.llm.complete(PLANNER_PROMPT, [{"role": "user", "content": msg}])
        self.last_response = resp
        d = extract_json(resp.text)
        steps = [Step(description=s.get("description", ""), tool=s.get("tool"), args=s.get("args") or {})
                 for s in d.get("steps", [])][: self.max_steps]
        return Plan(goal=d.get("goal", percept.content), steps=steps)
