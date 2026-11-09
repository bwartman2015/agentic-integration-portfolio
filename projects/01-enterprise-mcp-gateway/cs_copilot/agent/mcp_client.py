"""MCP client side: McpToolSource implements agentcore.ToolSource with the official `mcp` SDK.

TODO(Milestone 6): connect with mcp.ClientSession over streamable HTTP to the gateway (GATEWAY_URL),
pass the caller's JWT, implement list_tools() / call_tool(), then in build.py use
agentcore.register_tool_source(registry, McpToolSource(...), scope_for=..., side_effect_for=...)
instead of the local stubs in tools.py.
"""
from typing import Any


class McpToolSource:
    def __init__(self, url: str, bearer_token: str):
        self.url, self.token = url, bearer_token

    def list_tools(self) -> list[dict[str, Any]]:
        raise NotImplementedError("McpToolSource.list_tools")

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        raise NotImplementedError("McpToolSource.call_tool")
