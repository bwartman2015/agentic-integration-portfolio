"""Wire-level MCP contract test against the running gateway container, using only the stdlib.

It speaks raw JSON-RPC over MCP streamable HTTP (initialize -> notifications/initialized -> tools/list
-> tools/call), so it checks protocol compliance independently of the SDK the server is built with.
Start the gateway first (`make up P=01`); otherwise these tests are skipped with the reason.
Set REQUIRE_GATEWAY=1 (CI) to turn that skip into a failure, so a run cannot pass by skipping.
The URL comes from GATEWAY_URL, else http://localhost:${GATEWAY_PORT:-8765}. `make test` does not
read .env, so export GATEWAY_PORT if you changed it there.

The data assertions compare against the seeded dataset (`mock_systems/`), which is what the default
Compose stack serves: seed CRM/ERP in the gateway, carrier and AR over HTTP from the mock APIs. With
live CRM/ERP backends configured, set GATEWAY_LIVE_BACKENDS=1 to skip those two comparisons and use
`make live-check P=01` instead.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from typing import Any

BASE = os.environ.get("GATEWAY_URL", f"http://localhost:{os.environ.get('GATEWAY_PORT', '8765')}").rstrip("/")
REQUIRE_GATEWAY = os.environ.get("REQUIRE_GATEWAY") == "1"
LIVE_CRM_ERP = os.environ.get("GATEWAY_LIVE_BACKENDS") == "1"
TOOLS = ["search_accounts", "get_sales_order", "track_shipment", "get_open_invoices"]

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mock_systems import get_systems  # noqa: E402  (stdlib-only seeded dataset, for comparisons)
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

    def _call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return self.mcp.request("tools/call", {"name": name, "arguments": args})["result"]

    def test_tools_list_has_the_four_read_only_tools(self):
        tools = {t["name"]: t for t in self.mcp.request("tools/list")["result"]["tools"]}
        self.assertEqual(sorted(tools), sorted(TOOLS))
        for t in tools.values():
            self.assertTrue(t["annotations"]["readOnlyHint"], t["name"])
            self.assertIs(t["inputSchema"]["additionalProperties"], False, t["name"])
        self.assertEqual(tools["get_sales_order"]["inputSchema"]["properties"]["sales_order"]["pattern"], "^SO-[0-9]{6}$")

    def test_every_read_tool_returns_data(self):
        """Milestone 2 acceptance on the default stack: all read tools return data."""
        for name, args in [("search_accounts", {"query": "Summit"}), ("get_sales_order", {"sales_order": "SO-104233"}),
                           ("track_shipment", {"sales_order": "SO-104233"}), ("get_open_invoices", {"account_id": "ACCT-1002"})]:
            with self.subTest(tool=name):
                result = self._call(name, args)
                self.assertFalse(result.get("isError", False), result)
                self.assertTrue(result["structuredContent"], name)

    def test_carrier_and_ar_over_http_match_the_seeded_systems(self):
        systems = get_systems()
        self.assertEqual(self._call("track_shipment", {"sales_order": "SO-104301"})["structuredContent"],
                         systems.carrier.tracking("SO-104301"))
        items = systems.ar.open_items("ACCT-1001")
        self.assertEqual(self._call("get_open_invoices", {"account_id": "ACCT-1001"})["structuredContent"],
                         {"account_id": "ACCT-1001", "invoices": items, "count": len(items),
                          "total_open": round(sum(i["amount"] for i in items), 2)})

    @unittest.skipIf(LIVE_CRM_ERP, "GATEWAY_LIVE_BACKENDS=1: CRM/ERP are live, not seeded")
    def test_seeded_crm_alias_search(self):
        matches = self._call("search_accounts", {"query": "BBO"})["structuredContent"]["matches"]
        self.assertEqual([m["account_id"] for m in matches], ["ACCT-1001"])

    @unittest.skipIf(LIVE_CRM_ERP, "GATEWAY_LIVE_BACKENDS=1: CRM/ERP are live, not seeded")
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
