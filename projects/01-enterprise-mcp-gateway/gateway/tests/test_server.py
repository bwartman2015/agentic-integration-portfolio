"""Gateway contract tests (in-process, official MCP SDK client). Run inside the container:
`make gateway-test P=01`. They need the `mcp` package, which the project's stdlib-only test run lacks."""
from __future__ import annotations

import json
import logging
import unittest
from unittest import mock

from mcp import Client
from starlette.testclient import TestClient

from mock_systems import get_systems, reset_systems
from ridgeline_gateway.server import (SALES_ORDER_PATTERN, GatewayServer, _log_call, configure_logging, mcp,
                                      transport_security)

configure_logging()
_MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def _records(cm) -> list[dict]:
    return [json.loads(r.getMessage()) for r in cm.records]


class ToolContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_systems()

    async def _call(self, name: str, args: dict):
        async with Client(mcp) as client:
            return await client.call_tool(name, args)

    async def test_lists_exactly_one_read_only_tool_with_closed_schema(self):
        async with Client(mcp) as client:
            tools = (await client.list_tools()).tools
        self.assertEqual([t.name for t in tools], ["get_sales_order"])
        schema = tools[0].input_schema
        self.assertEqual(schema["required"], ["sales_order"])
        self.assertEqual(schema["properties"]["sales_order"]["pattern"], SALES_ORDER_PATTERN)
        self.assertIs(schema["additionalProperties"], False)
        self.assertTrue(tools[0].annotations.read_only_hint)
        self.assertFalse(tools[0].annotations.destructive_hint)

    async def test_every_tool_publishes_a_closed_schema_including_later_registrations(self):
        server = GatewayServer(name="t")

        @server.tool()
        def first(a: str) -> str:
            return a

        server.add_tool(lambda b: b, name="second")
        for srv in (mcp, server):
            async with Client(srv) as client:
                for tool in (await client.list_tools()).tools:
                    self.assertIs(tool.input_schema.get("additionalProperties"), False, tool.name)

    async def test_known_order_returns_structured_seeded_data(self):
        result = await self._call("get_sales_order", {"sales_order": "SO-104233"})
        self.assertFalse(result.is_error)
        order = result.structured_content
        self.assertEqual(order["sales_order"], "SO-104233")
        self.assertEqual(order["account_id"], "ACCT-1002")
        self.assertEqual(order["account_name"], "Summit Contractors LLC")
        self.assertEqual(order["delivery_status"][0]["shipment_id"], "SHP-55012")

    async def test_unknown_order_is_a_clear_tool_error(self):
        result = await self._call("get_sales_order", {"sales_order": "SO-999999"})
        self.assertTrue(result.is_error)
        self.assertIn("SO-999999 was not found", result.content[0].text)

    async def test_malformed_id_is_rejected_by_schema(self):
        for bad in ("104233", "SO-1042331", "SO-104233; DROP TABLE orders", ""):
            result = await self._call("get_sales_order", {"sales_order": bad})
            self.assertTrue(result.is_error, bad)
            self.assertIn("pattern", result.content[0].text, bad)

    async def test_gateway_is_strict_and_clients_normalise(self):
        """Decision: the gateway accepts only canonical IDs. The copilot normalises case and whitespace
        in its ACT layer (cs_copilot/agent/tools.py `_norm`) before calling, here and in Milestone 6."""
        for raw in ("so-104233", " SO-104233 "):
            result = await self._call("get_sales_order", {"sales_order": raw})
            self.assertTrue(result.is_error, raw)

    async def test_extra_arguments_are_rejected(self):
        result = await self._call("get_sales_order", {"sales_order": "SO-104233", "extra": "x"})
        self.assertTrue(result.is_error)
        self.assertIn("Unexpected argument(s) for get_sales_order: extra", result.content[0].text)

    async def test_missing_argument_is_rejected(self):
        result = await self._call("get_sales_order", {})
        self.assertTrue(result.is_error)

    async def test_unknown_tool_is_an_error(self):
        result = await self._call("create_case", {"account_id": "ACCT-1002"})
        self.assertTrue(result.is_error)

    async def test_crash_detail_stays_on_the_server(self):
        with mock.patch.object(get_systems().erp, "order", side_effect=RuntimeError("upstream http://erp.internal.example")):
            with self.assertLogs("ridgeline.gateway", logging.INFO) as cm:
                result = await self._call("get_sales_order", {"sales_order": "SO-104233"})
        self.assertTrue(result.is_error)
        self.assertNotIn("erp.internal", result.content[0].text)
        self.assertEqual(_records(cm)[-1]["outcome"], "error")
        self.assertEqual(_records(cm)[-1]["error_type"], "RuntimeError")


class CallLoggingTests(unittest.IsolatedAsyncioTestCase):
    """OBSERVE: every call produces exactly one JSON line, whatever the outcome, without argument values."""

    async def _logged(self, name: str, args: dict) -> dict:
        reset_systems()
        with self.assertLogs("ridgeline.gateway", logging.INFO) as cm:
            async with Client(mcp) as client:
                await client.call_tool(name, args)
        records = [r for r in _records(cm) if r["event"] == "tool_call"]
        self.assertEqual(len(records), 1, records)
        line = json.dumps(records[0])
        for value in args.values():
            if value:
                self.assertNotIn(str(value), line)
        return records[0]

    async def test_every_outcome_is_logged_once(self):
        cases = [
            ("get_sales_order", {"sales_order": "SO-104233"}, "ok"),
            ("get_sales_order", {"sales_order": "SO-999999"}, "not_found"),
            ("get_sales_order", {"sales_order": "bad-id"}, "invalid_arguments"),
            ("get_sales_order", {"sales_order": "SO-104233", "extra": "sentinel-extra"}, "invalid_arguments"),
            ("drop_everything", {"note": "sentinel-arg-value"}, "unknown_tool"),
        ]
        for name, args, outcome in cases:
            with self.subTest(outcome=outcome, args=args):
                rec = await self._logged(name, args)
                self.assertEqual(rec["outcome"], outcome)
                self.assertEqual(rec["ok"], outcome == "ok")
                self.assertIn("latency_ms", rec)
                self.assertIn("request_id", rec)
                self.assertEqual(rec["tool"], "<unknown>" if outcome == "unknown_tool" else name)

    def test_trace_headers_are_logged_only_when_well_formed(self):
        good_tp = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
        cases = [({"traceparent": good_tp, "x-request-id": "req-42"}, good_tp, "req-42"),
                 ({"traceparent": "00-not-a-trace", "x-request-id": "bad id\nwith newline"}, "invalid", "invalid")]
        for headers, tp, rid in cases:
            ctx = mock.Mock(request_id="7", headers=headers)
            with self.assertLogs("ridgeline.gateway", logging.INFO) as cm:
                _log_call("get_sales_order", "ok", 0.0, ctx, None)
            rec = _records(cm)[0]
            self.assertEqual((rec["traceparent"], rec["x_request_id"], rec["request_id"]), (tp, rid, "7"))

    def test_sdk_info_logs_are_silenced(self):
        self.assertGreaterEqual(logging.getLogger("mcp").getEffectiveLevel(), logging.WARNING)


class HttpTransportTests(unittest.TestCase):
    """The ASGI app as uvicorn serves it: health route and DNS-rebinding protection."""

    def _client(self) -> TestClient:
        app = mcp.streamable_http_app(streamable_http_path="/mcp", transport_security=transport_security())
        return TestClient(app, base_url="http://localhost:8765")

    def test_healthz(self):
        with self._client() as http:
            resp = http.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")

    def test_foreign_host_header_is_421(self):
        for host in ("attacker.example", "localhost.attacker.example:8765", "localhost"):
            with self._client() as http:
                resp = http.post("/mcp", headers={**_MCP_HEADERS, "Host": host}, json=_LIST)
            self.assertEqual(resp.status_code, 421, host)

    def test_foreign_origin_is_403(self):
        for origin in ("http://attacker.example", "http://localhost.attacker.example", "null"):
            with self._client() as http:
                resp = http.post("/mcp", headers={**_MCP_HEADERS, "Origin": origin}, json=_LIST)
            self.assertEqual(resp.status_code, 403, origin)


if __name__ == "__main__":
    unittest.main()
