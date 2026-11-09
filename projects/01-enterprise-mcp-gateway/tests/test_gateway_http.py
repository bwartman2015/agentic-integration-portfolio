"""Wire-level MCP contract test against the running gateway container, using only the stdlib.

It speaks raw JSON-RPC over MCP streamable HTTP (initialize -> notifications/initialized -> tools/list
-> tools/call), so it checks protocol compliance independently of the SDK the server is built with.
Start the gateway first (`make up P=01`); otherwise these tests are skipped with the reason.
Set REQUIRE_GATEWAY=1 (CI) to turn that skip into a failure, so a run cannot pass by skipping.
The URL comes from GATEWAY_URL, else http://localhost:${GATEWAY_PORT:-8765}. `make test` does not
read .env, so export GATEWAY_PORT if you changed it there.
"""
from __future__ import annotations

import json
import os
import unittest
import urllib.error
import urllib.request
from typing import Any

BASE = os.environ.get("GATEWAY_URL", f"http://localhost:{os.environ.get('GATEWAY_PORT', '8765')}").rstrip("/")
REQUIRE_GATEWAY = os.environ.get("REQUIRE_GATEWAY") == "1"
PROTOCOL_VERSION = "2025-06-18"


def _gateway_up() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE}/healthz", timeout=1) as resp:
            return resp.status == 200
    except (OSError, urllib.error.URLError):
        return False


class McpHttpSession:
    """Minimal MCP client over streamable HTTP: JSON or SSE responses, session id header."""

    def __init__(self, base: str):
        self.url = f"{base}/mcp"
        self.session_id: str | None = None
        self._next_id = 0

    def _post(self, payload: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read().decode()
        except urllib.error.HTTPError as err:
            return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read().decode()

    @staticmethod
    def _parse(headers: dict[str, str], body: str) -> dict[str, Any]:
        if headers.get("content-type", "").startswith("text/event-stream"):
            events = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
            messages = [json.loads(e) for e in events if e]
            responses = [m for m in messages if "id" in m and ("result" in m or "error" in m)]
            if not responses:
                raise AssertionError(f"no JSON-RPC response in SSE stream: {body[:300]}")
            return responses[-1]
        return json.loads(body)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        status, headers, body = self._post({"jsonrpc": "2.0", "id": self._next_id, "method": method,
                                            "params": params or {}})
        if status != 200:
            raise AssertionError(f"{method}: HTTP {status}: {body[:300]}")
        if method == "initialize":
            self.session_id = headers.get("mcp-session-id")
        return self._parse(headers, body)

    def notify(self, method: str) -> int:
        status, _, _ = self._post({"jsonrpc": "2.0", "method": method})
        return status

    def initialize(self) -> dict[str, Any]:
        resp = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                           "clientInfo": {"name": "p01-contract-test", "version": "0.1.0"}})
        status = self.notify("notifications/initialized")
        if status not in (200, 202):
            raise AssertionError(f"notifications/initialized: HTTP {status}")
        return resp


_UP = _gateway_up()


class GatewayRequiredTest(unittest.TestCase):
    def test_gateway_reachable_when_required(self):
        if not REQUIRE_GATEWAY:
            self.skipTest("set REQUIRE_GATEWAY=1 to fail when the gateway is down")
        self.assertTrue(_UP, f"REQUIRE_GATEWAY=1 but no gateway at {BASE}")


@unittest.skipUnless(_UP, f"gateway not running at {BASE} (start it with `make up P=01`)")
class GatewayHttpContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mcp = McpHttpSession(BASE)
        self.init = self.mcp.initialize()

    def test_initialize_advertises_server_and_tools_capability(self):
        result = self.init["result"]
        self.assertEqual(result["serverInfo"]["name"], "ridgeline-gateway")
        self.assertIn("tools", result["capabilities"])
        self.assertTrue(self.mcp.session_id, "server issued no Mcp-Session-Id")

    def test_tools_list_has_the_hard_coded_tool(self):
        tools = self.mcp.request("tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["get_sales_order"])
        schema = tools[0]["inputSchema"]
        self.assertEqual(schema["required"], ["sales_order"])
        self.assertEqual(schema["properties"]["sales_order"]["pattern"], "^SO-[0-9]{6}$")
        self.assertTrue(tools[0]["annotations"]["readOnlyHint"])

    def test_tools_call_returns_the_order(self):
        result = self.mcp.request("tools/call", {"name": "get_sales_order",
                                                 "arguments": {"sales_order": "SO-104233"}})["result"]
        self.assertFalse(result.get("isError", False))
        self.assertEqual(result["structuredContent"]["account_name"], "Summit Contractors LLC")
        self.assertEqual(json.loads(result["content"][0]["text"])["sales_order"], "SO-104233")

    def test_tools_call_unknown_order_is_error_result(self):
        result = self.mcp.request("tools/call", {"name": "get_sales_order",
                                                 "arguments": {"sales_order": "SO-999999"}})["result"]
        self.assertTrue(result["isError"])
        self.assertIn("not found", result["content"][0]["text"])

    def test_tools_call_extra_argument_is_error_result(self):
        result = self.mcp.request("tools/call", {"name": "get_sales_order",
                                                 "arguments": {"sales_order": "SO-104233", "extra": "x"}})["result"]
        self.assertTrue(result["isError"])

    def _rejected_status(self, header: str, value: str) -> int:
        req = urllib.request.Request(f"{BASE}/mcp", method="POST", data=b"{}",
                                     headers={header: value, "Content-Type": "application/json",
                                              "Accept": "application/json, text/event-stream"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        ctx.exception.close()
        return ctx.exception.code

    def test_foreign_host_header_is_421(self):
        self.assertEqual(self._rejected_status("Host", "attacker.example"), 421)

    def test_foreign_origin_is_403(self):
        self.assertEqual(self._rejected_status("Origin", "http://attacker.example"), 403)


if __name__ == "__main__":
    unittest.main()
