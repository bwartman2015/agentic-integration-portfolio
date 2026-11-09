"""ACT layer: tool registry and execution.

Every external action (MCP tool, API call, code execution, database query, file
operation, external service) is a registered Tool with a schema, a required scope
and a side-effect class. Tools are only ever executed through Executor, which the
agent loop calls only after Guardrails.authorize().
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from .types import Principal, ToolResult

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}


@dataclass
class Tool:
    name: str
    description: str
    handler: Callable[..., Any]
    params: dict[str, str] = field(default_factory=dict)      # {"order_id": "string", ...}
    required: list[str] = field(default_factory=list)
    scope: str | None = None                                   # e.g. "erp.read"
    side_effect: Literal["read", "write"] = "read"
    category: Literal["mcp", "api", "code", "database", "file", "external"] = "api"

    def spec(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "params": self.params,
                "required": self.required, "side_effect": self.side_effect}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def catalog(self, principal: Principal) -> list[dict[str, Any]]:
        """Only show the reasoner tools this principal may use (least privilege)."""
        return [t.spec() for t in self._tools.values() if t.scope is None or t.scope in principal.scopes]


def validate_args(tool: Tool, args: dict[str, Any]) -> str | None:
    missing = [p for p in tool.required if p not in args]
    if missing:
        return f"missing required args: {missing}"
    unknown = [a for a in args if a not in tool.params]
    if unknown:
        return f"unknown args: {unknown}"
    for name, typ in tool.params.items():
        if name in args and not isinstance(args[name], _TYPES.get(typ, object)):
            return f"arg {name!r} should be {typ}"
    return None


class Executor:
    def execute(self, tool: Tool, args: dict[str, Any]) -> ToolResult:
        err = validate_args(tool, args)
        if err:
            return ToolResult(tool=tool.name, ok=False, error=err)
        start = time.perf_counter()
        try:
            out = tool.handler(**args)
            return ToolResult(tool=tool.name, ok=True, output=out, latency_ms=(time.perf_counter() - start) * 1000)
        except Exception as e:  # tool failures are data for the reasoner, not crashes
            return ToolResult(tool=tool.name, ok=False, error=f"{type(e).__name__}: {e}",
                              latency_ms=(time.perf_counter() - start) * 1000)
