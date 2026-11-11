"""Copilot container tests (make copilot-test): McpToolSource with the real MCP SDK, no network.

An in-process MCP server stands in for the gateway, so the SDK's own result objects (structured content,
is_error, tool listings) go through McpToolSource; a mock HTTP transport shows what the streamable HTTP
client actually sends (bearer token, trace id). The host tests (tests/test_copilot_gateway.py) cover the
governance; `make ask` covers the live gateway.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from typing import Any
from unittest import mock

os.environ.setdefault("TRACE_FILE", os.devnull)
os.environ.setdefault("PROGRESS_DIR", tempfile.mkdtemp())

import httpx2
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from agentcore.observe import _current_trace
from cs_copilot.agent import mcp_client
from cs_copilot.agent.build import gateway_registry
from cs_copilot.agent.mcp_client import GatewayToolError, McpToolSource

server = MCPServer(name="fake-gateway")


@server.tool(description="Find customer accounts (read-only).")
def search_accounts(query: str) -> dict[str, Any]:
    return {"query": query, "matches": [{"name": "Summit Contractors LLC", "account_id": "ACCT-1002"}]}


@server.tool(description="Sales order header.")
def get_sales_order(sales_order: str) -> dict[str, Any]:
    raise ToolError("Access denied: this record belongs to an account outside your region. Ask a CS lead.")


for _name in ("track_shipment", "get_open_invoices"):
    server.add_tool(lambda sales_order="": {"ok": True}, name=_name)


@server.tool(description="Create a CRM case (the gateway asks a human first).")
def create_case(account_id: str, subject: str, summary: str) -> dict[str, Any]:
    return {"case_id": "CASE-1", "account_id": account_id}


SEEN_HEADERS: list[dict[str, str]] = []


def in_memory_session(url: str, headers: dict[str, str], timeout_s: float) -> Any:
    SEEN_HEADERS.append(headers)

    @asynccontextmanager
    async def session():
        async with Client(server) as client:
            yield client
    return session()


class SdkResultTests(unittest.TestCase):
    def setUp(self):
        self.source = McpToolSource("http://gateway:8000/mcp", lambda: "tok", timeout_s=5, session=in_memory_session)

    def test_listing_and_structured_results_from_the_sdk(self):
        names = {t["name"] for t in self.source.list_tools()}
        self.assertLessEqual({"search_accounts", "get_sales_order", "create_case"}, names)
        schema = next(t for t in self.source.list_tools() if t["name"] == "search_accounts")["inputSchema"]
        self.assertEqual(schema["required"], ["query"])
        out = self.source.call_tool("search_accounts", {"query": "Summit"})
        self.assertEqual(out["matches"][0]["account_id"], "ACCT-1002")

    def test_a_refusal_is_a_gateway_tool_error_with_the_servers_message(self):
        with self.assertRaisesRegex(GatewayToolError, "outside your region"):
            self.source.call_tool("get_sales_order", {"sales_order": "SO-104301"})

    def test_registry_from_the_sdk_listing_takes_scopes_from_policy_data(self):
        reg = gateway_registry(self.source, ["search_accounts", "get_sales_order", "track_shipment",
                                             "get_open_invoices", "create_case"])
        self.assertEqual((reg.get("create_case").scope, reg.get("create_case").side_effect), ("crm.write", "write"))
        self.assertEqual(reg.get("search_accounts").params, {"query": "string"})

    def test_runs_from_inside_a_running_event_loop_too_and_keeps_the_trace_id(self):
        async def inside():
            return self.source.call_tool("search_accounts", {"query": "Summit"})
        token = _current_trace.set("trace-in-loop")
        try:
            self.assertIn("matches", asyncio.run(inside()))
        finally:
            _current_trace.reset(token)
        self.assertEqual(SEEN_HEADERS[-1]["x-request-id"], "trace-in-loop")


class WireHeaderTests(unittest.TestCase):
    def test_streamable_http_requests_carry_the_bearer_token_and_the_trace_id(self):
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(503, json={"error": "stop here"})

        real = httpx2.AsyncClient
        with mock.patch.object(httpx2, "AsyncClient",
                               lambda **kw: real(transport=httpx2.MockTransport(handler), **kw)):
            source = McpToolSource("http://gateway:8000/mcp", lambda: "tok-123", timeout_s=5,
                                   session=mcp_client.sdk_session)
            token = _current_trace.set("trace-abc123")
            try:
                with self.assertRaises(BaseException):
                    source.call_tool("search_accounts", {"query": "Summit"})
            finally:
                _current_trace.reset(token)
        self.assertTrue(seen)
        self.assertEqual(seen[0].headers["authorization"], "Bearer tok-123")
        self.assertEqual(seen[0].headers["x-request-id"], "trace-abc123")
        self.assertEqual(str(seen[0].url), "http://gateway:8000/mcp")


class FetchModelTests(unittest.TestCase):
    def test_pinned_model_and_a_mismatching_download_is_discarded(self):
        import importlib.util
        import io
        import pathlib
        spec = importlib.util.spec_from_file_location("fetch_model", "/app/copilot/fetch_model.py")
        fetch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fetch)
        self.assertRegex(fetch.SHA256, r"^[0-9a-f]{64}$")
        self.assertRegex(fetch.REVISION, r"^[0-9a-f]{40}$")
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"MODELS_DIR": d}), \
                mock.patch("urllib.request.urlopen", lambda url, timeout: io.BytesIO(b"x" * 10)), \
                mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(fetch.main(), 1)
            self.assertEqual(list(pathlib.Path(d).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
