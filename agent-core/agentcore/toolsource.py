"""MCP (concept 6): a standard plug for tools and data.

An MCP server exposes tools; the agent connects through an MCP client. ToolSource is the
client side, reduced to the two calls an agent needs (list_tools / call_tool), so any
MCP server's tools can be registered into a ToolRegistry and governed like every other Tool.

agent-core stays dependency-free: Project 01 implements McpToolSource with the official
`mcp` Python SDK (ClientSession over streamable HTTP) behind this protocol.
"""
from __future__ import annotations

from typing import Any, Callable, Literal, Protocol

from .act import Tool, ToolRegistry

_JSON_TO_PARAM = {"string": "string", "integer": "integer", "number": "number",
                  "boolean": "boolean", "object": "object", "array": "array"}


class ToolSource(Protocol):
    def list_tools(self) -> list[dict[str, Any]]:
        """Each item: {"name", "description", "inputSchema": JSON Schema object} (MCP tools/list shape)."""
        ...

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


def register_tool_source(registry: ToolRegistry, source: ToolSource, *,
                         scope_for: Callable[[str], str | None],
                         side_effect_for: Callable[[str], Literal["read", "write"]] = lambda n: "read",
                         prefix: str = "", category: str = "mcp") -> list[str]:
    """Wrap every tool the server exposes as a governed agentcore Tool. Scopes and side effects are
    decided HERE (by the client's policy), never trusted from the server's own description."""
    names = []
    for spec in source.list_tools():
        schema = spec.get("inputSchema") or {}
        props = schema.get("properties", {})
        params = {k: _JSON_TO_PARAM.get(v.get("type", "string"), "string") for k, v in props.items()}
        remote = spec["name"]

        def handler(_remote=remote, **kwargs):
            return source.call_tool(_remote, kwargs)

        name = f"{prefix}{remote}"
        registry.register(Tool(name, spec.get("description", ""), handler=handler, params=params,
                               required=list(schema.get("required", [])), scope=scope_for(remote),
                               side_effect=side_effect_for(remote), category=category))  # type: ignore[arg-type]
        names.append(name)
    return names
