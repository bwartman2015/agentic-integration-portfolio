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
import httpx2

from ridgeline_gateway.config import load_settings
from ridgeline_gateway.connectors import build_connectors
from opa_fixture import use_policy
from ridgeline_gateway.server import (ACCOUNT_ID_PATTERN, SALES_ORDER_PATTERN, GatewayServer, _log_call,
                                      configure_logging, mcp, set_connectors, transport_security)

configure_logging()
_MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def _records(cm) -> list[dict]:
    return [json.loads(r.getMessage()) for r in cm.records]


TOOLS = ["search_accounts", "get_sales_order", "track_shipment", "get_open_invoices"]


def use_seed_backends() -> None:
    set_connectors(build_connectors(load_settings({})))


class ToolContractTests(unittest.IsolatedAsyncioTestCase):
    """Tool contracts, called as cs-lead (every scope, every region) so that policy never interferes.
    Authorization itself is covered in test_policy.py."""

    def setUp(self) -> None:
        reset_systems()
        use_seed_backends()
        use_policy(self)

    async def _call(self, name: str, args: dict):
        async with Client(mcp) as client:
            return await client.call_tool(name, args)

    async def test_lists_the_four_read_only_tools_with_closed_schemas(self):
        async with Client(mcp) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
        self.assertEqual(sorted(tools), sorted(TOOLS))
        expected = {"search_accounts": ("query", None), "get_sales_order": ("sales_order", SALES_ORDER_PATTERN),
                    "track_shipment": ("sales_order", SALES_ORDER_PATTERN),
                    "get_open_invoices": ("account_id", ACCOUNT_ID_PATTERN)}
        for name, (param, pattern) in expected.items():
            schema = tools[name].input_schema
            self.assertEqual(schema["required"], [param], name)
            self.assertIs(schema["additionalProperties"], False, name)
            if pattern:
                self.assertEqual(schema["properties"][param]["pattern"], pattern, name)
            self.assertTrue(tools[name].annotations.read_only_hint, name)
            self.assertFalse(tools[name].annotations.destructive_hint, name)
        self.assertNotIn("create_case", tools, "writes stay off the gateway until approvals (Milestone 5)")

    async def test_seed_results_match_the_copilot_shapes(self):
        systems = get_systems()
        r = await self._call("search_accounts", {"query": "BBO"})
        self.assertEqual(r.structured_content, {"query": "BBO", "matches": systems.crm.find_accounts("BBO")})
        self.assertEqual([m["account_id"] for m in r.structured_content["matches"]], ["ACCT-1001"])
        r = await self._call("track_shipment", {"sales_order": "SO-104301"})
        self.assertEqual(r.structured_content, systems.carrier.tracking("SO-104301"))
        r = await self._call("get_open_invoices", {"account_id": "ACCT-1002"})
        items = systems.ar.open_items("ACCT-1002")
        self.assertEqual(r.structured_content, {"account_id": "ACCT-1002", "invoices": items, "count": len(items),
                                                "total_open": round(sum(i["amount"] for i in items), 2)})
        self.assertEqual(r.structured_content["total_open"], 8370.0)

    async def test_not_found_for_every_tool(self):
        cases = [("search_accounts", {"query": "Nonexistent Co"}, None),
                 ("track_shipment", {"sales_order": "SO-999999"}, "has no carrier record"),
                 ("get_open_invoices", {"account_id": "ACCT-9999"}, "was not found in AR")]
        for name, args, message in cases:
            r = await self._call(name, args)
            if message is None:
                self.assertEqual(r.structured_content["matches"], [])
            else:
                self.assertTrue(r.is_error, name)
                self.assertIn(message, r.content[0].text)

    async def test_input_patterns_for_new_tools(self):
        for name, args in [("search_accounts", {"query": "x"}), ("search_accounts", {"query": "a{b}"}),
                           ("search_accounts", {"query": "A" * 81}), ("get_open_invoices", {"account_id": "acct-1002"}),
                           ("track_shipment", {"sales_order": "SO-1"})]:
            r = await self._call(name, args)
            self.assertTrue(r.is_error, (name, args))

    async def test_every_tool_publishes_a_closed_schema_including_later_registrations(self):
        server = GatewayServer(name="t")

        @server.tool()
        def first(a: str) -> str:
            return a

        server.add_tool(lambda b: b, name="second")
        async with Client(mcp) as client:
            for tool in (await client.list_tools()).tools:
                self.assertIs(tool.input_schema.get("additionalProperties"), False, tool.name)
        # tools/list hides tools the policy does not cover, so the new server is checked at the registry.
        registered = server._tool_manager.list_tools()
        self.assertEqual(sorted(t.name for t in registered), ["first", "second"])
        for tool in registered:
            self.assertIs(tool.parameters.get("additionalProperties"), False, tool.name)

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
        use_seed_backends()
        use_policy(self)
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

    async def test_backend_and_upstream_fields(self):
        rec = await self._logged("get_open_invoices", {"account_id": "ACCT-1002"})
        self.assertEqual(rec["backend"], "seed")
        self.assertNotIn("upstream_status", rec)

        def boom(r: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(503, json={"detail": "down"})
        settings = load_settings({"AR_BACKEND": "http", "AR_API_TOKEN": "t" * 20})
        set_connectors(build_connectors(settings, transport=httpx2.MockTransport(boom)))
        use_policy(self)
        try:
            with self.assertLogs("ridgeline.gateway", logging.INFO) as cm:
                async with Client(mcp) as client:
                    result = await client.call_tool("get_open_invoices", {"account_id": "ACCT-1002"})
        finally:
            use_seed_backends()
        self.assertTrue(result.is_error)
        self.assertIn("AR API returned an error (HTTP 503).", result.content[0].text)
        rec = [r for r in _records(cm) if r["event"] == "tool_call"][0]
        self.assertEqual((rec["outcome"], rec["backend"], rec["upstream_status"], rec["error_type"]),
                         ("upstream_error", "ar_api", 503, "ConnectorError"))

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
