"""GUARDRAILS & SAFETY layer: permissions, approval gates, content filtering, rate limits,
human-in-the-loop, and budgets. Applied at input, before every action, and at output."""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .act import Tool
from .types import Principal


class GuardrailViolation(Exception):
    def __init__(self, rule: str, detail: str):
        super().__init__(f"{rule}: {detail}")
        self.rule, self.detail = rule, detail


# (principal, tool, args) -> (allowed, reason). Project 01 plugs in an OPA client here.
PolicyHook = Callable[[Principal, Tool, dict[str, Any]], tuple[bool, str]]
# (principal, tool, args) -> approved?  Projects plug in an n8n approval webhook here.
ApprovalHook = Callable[[Principal, Tool, dict[str, Any]], bool]

DEFAULT_PII = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "phone": re.compile(r"\b\d{3}[.\- ]\d{3}[.\- ]\d{4}\b"),
}


@dataclass
class Guardrails:
    max_iterations: int = 5
    max_tool_calls: int = 15
    max_cost_usd: float = 0.50
    max_input_chars: int = 20000
    rate_limit_per_minute: int = 60
    policy: PolicyHook | None = None
    approval: ApprovalHook | None = None          # None = writes are denied (fail closed)
    blocked_input_patterns: list[re.Pattern] = field(default_factory=list)
    pii_patterns: dict[str, re.Pattern] = field(default_factory=lambda: dict(DEFAULT_PII))
    _calls: deque = field(default_factory=deque, repr=False)

    def check_input(self, text: str) -> None:
        if len(text) > self.max_input_chars:
            raise GuardrailViolation("input_size", f"{len(text)} chars > {self.max_input_chars}")
        for p in self.blocked_input_patterns:
            if p.search(text):
                raise GuardrailViolation("content_filter", f"input matched {p.pattern!r}")

    def check_budget(self, iterations: int, tool_calls: int, cost_usd: float) -> None:
        if iterations >= self.max_iterations:
            raise GuardrailViolation("max_iterations", f"{iterations} >= {self.max_iterations}")
        if tool_calls >= self.max_tool_calls:
            raise GuardrailViolation("max_tool_calls", f"{tool_calls} >= {self.max_tool_calls}")
        if cost_usd >= self.max_cost_usd:
            raise GuardrailViolation("cost_budget", f"${cost_usd:.4f} >= ${self.max_cost_usd:.2f}")

    def authorize(self, principal: Principal, tool: Tool, args: dict[str, Any]) -> None:
        now = time.monotonic()
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        if len(self._calls) >= self.rate_limit_per_minute:
            raise GuardrailViolation("rate_limit", f"{self.rate_limit_per_minute}/min exceeded")
        if tool.scope and tool.scope not in principal.scopes:
            raise GuardrailViolation("permission", f"{principal.id} lacks scope {tool.scope} for {tool.name}")
        if self.policy:
            ok, reason = self.policy(principal, tool, args)
            if not ok:
                raise GuardrailViolation("policy", reason)
        if tool.side_effect == "write":
            if self.approval is None or not self.approval(principal, tool, args):
                raise GuardrailViolation("approval_gate", f"write tool {tool.name} not approved")
        self._calls.append(now)

    def mask(self, text: str) -> str:
        for label, p in self.pii_patterns.items():
            text = p.sub(f"[{label.upper()} REDACTED]", text)
        return text
