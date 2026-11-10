"""Authorization (Milestone 4): OPA decides, the gateway enforces and fails closed.

Two groups. `OpaClientTests` drive the client over a fake transport (what the gateway sends, and that every
bad answer means deny). Everything else runs the gateway against a real OPA serving the committed policy
(see opa_fixture.py): scope rule, region rule, tools/list filtering, logging, HTTP end to end."""
from __future__ import annotations

import json
import logging
import unittest
from typing import Any
from unittest import mock

import httpx2
from mcp import Client
from starlette.testclient import TestClient

from mock_systems import get_systems, reset_systems
from opa_fixture import CRM_ONLY, LEAD, NO_SCOPES, REP, act_as, real_policy, start_opa, use_policy
from ridgeline_gateway import server
from ridgeline_gateway.config import load_settings
from ridgeline_gateway.connectors import build_connectors
from ridgeline_gateway.policy import (POLICY_ERROR, POLICY_UNAVAILABLE, Decision, DenyAllPolicy, PolicyConfig,
                                      PolicyConfigError, build_policy, denial_message)
from ridgeline_gateway.server import GatewayServer, configure_logging, mcp, set_connectors, set_policy, transport_security
from test_auth import CONFIG, INIT, MCP_HEADERS, FakeKeycloak, reset_auth
from ridgeline_gateway.auth import build_verifier

configure_logging()
READ_TOOLS = ["get_open_invoices", "get_sales_order", "search_accounts", "track_shipment"]
CALLS = [("search_accounts", {"query": "Summit"}), ("get_sales_order", {"sales_order": "SO-104233"}),
         ("track_shipment", {"sales_order": "SO-104233"}), ("get_open_invoices", {"account_id": "ACCT-1002"})]
# Pinecrest Hardware (ACCT-1003, SO-104301) is a West account; cs-rep is a Central rep.
WEST_CALLS = [("get_sales_order", {"sales_order": "SO-104301"}), ("track_shipment", {"sales_order": "SO-104301"}),
              ("get_open_invoices", {"account_id": "ACCT-1003"})]


def opa_answering(handler) -> Any:
    return build_policy(PolicyConfig("http://opa:8181", 2.0), transport=httpx2.MockTransport(handler))


class OpaClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_principal_tool_args_and_resource_only_when_given(self):
        seen: list[httpx2.Request] = []

        def opa(req: httpx2.Request) -> httpx2.Response:
            seen.append(req)
            return httpx2.Response(200, json={"result": {"allow": True, "reason": "allowed"}})

        policy = opa_answering(opa)
        caller = {**REP, "token": "must-not-be-forwarded"}
        self.assertEqual(await policy.decide(caller, "get_sales_order", {"sales_order": "SO-104233"}),
                         Decision(True, "allowed"))
        await policy.decide(caller, "get_sales_order", {"sales_order": "SO-104233"}, {"region": None})
        first, second = (json.loads(r.content) for r in seen)
        self.assertEqual(first, {"input": {"principal": REP, "tool": "get_sales_order",
                                           "args": {"sales_order": "SO-104233"}}})
        self.assertEqual(second["input"]["resource"], {"region": None})
        self.assertEqual((seen[0].method, str(seen[0].url)),
                         ("POST", "http://opa:8181/v1/data/ridgeline/gateway/decision"))

    async def test_denial_carries_reason_and_scope(self):
        policy = opa_answering(lambda r: httpx2.Response(200, json={"result": {
            "allow": False, "reason": "missing_scope", "required_scope": "crm.write"}}))
        self.assertEqual(await policy.decide(REP, "create_case", {}), Decision(False, "missing_scope", "crm.write"))

    async def test_any_failure_or_malformed_answer_denies(self):
        def unreachable(req: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("refused", request=req)

        def slow(req: httpx2.Request) -> httpx2.Response:
            raise httpx2.ReadTimeout("slow", request=req)

        unavailable = [unreachable, slow, lambda r: httpx2.Response(500, json={"code": "internal_error"}),
                       lambda r: httpx2.Response(401, json={"code": "unauthorized"}),
                       lambda r: httpx2.Response(200, content=b"not json"),
                       lambda r: httpx2.Response(302, headers={"location": "http://attacker.example/"})]
        for handler in unavailable:
            decision = await opa_answering(handler).decide(LEAD, "search_accounts", {})
            self.assertEqual(decision, Decision(False, POLICY_UNAVAILABLE))
        malformed = [{}, {"result": None}, {"result": True}, {"result": {"allow": "true", "reason": "allowed"}},
                     {"result": {"allow": 1, "reason": "allowed"}}, {"result": {"allow": True}},
                     {"result": {"allow": True, "reason": "because"}},
                     {"result": {"allow": True, "reason": "missing_scope"}},      # allow must agree with reason
                     {"result": {"allow": False, "reason": "allowed"}}, {"result": [{"allow": True}]}, []]
        for body in malformed:
            decision = await opa_answering(lambda r, b=body: httpx2.Response(200, json=b)).decide(LEAD, "search_accounts", {})
            self.assertEqual(decision, Decision(False, POLICY_ERROR), body)

    async def test_allowed_tools_fails_closed_and_never_adds_tools(self):
        names = ["search_accounts", "get_sales_order"]
        for body in ({"result": ["search_accounts", "create_case", 7]},):
            policy = opa_answering(lambda r, b=body: httpx2.Response(200, json=b))
            self.assertEqual(await policy.allowed_tools(REP, names), {"search_accounts"})
        for handler in (lambda r: httpx2.Response(500), lambda r: httpx2.Response(200, json={}),
                        lambda r: httpx2.Response(200, json={"result": "search_accounts"})):
            self.assertEqual(await opa_answering(handler).allowed_tools(REP, names), set())

    async def test_unconfigured_gateway_denies_everything(self):
        policy = DenyAllPolicy()
        self.assertFalse((await policy.decide(LEAD, "search_accounts", {})).allow)
        self.assertEqual(await policy.allowed_tools(LEAD, READ_TOOLS), set())

    def test_config_validation(self):
        cfg = PolicyConfig.from_env({"OPA_URL": "http://opa:8181/"})
        self.assertEqual((cfg.url, cfg.timeout_s), ("http://opa:8181", 2.0))
        for env in ({}, {"OPA_URL": ""}, {"OPA_URL": "opa:8181"}, {"OPA_URL": "ftp://opa"},
                    {"OPA_URL": "http://user:pw@opa:8181"}, {"OPA_URL": "http://opa:8181/v1/data"},
                    {"OPA_URL": "http://opa:port"}, {"OPA_URL": "http://opa:8181", "OPA_TIMEOUT_S": "abc"},
                    {"OPA_URL": "http://opa:8181", "OPA_TIMEOUT_S": "60"}):
            with self.assertRaises(PolicyConfigError, msg=env):
                PolicyConfig.from_env(env)

    def test_denial_messages_are_clear_and_built_only_from_codes(self):
        self.assertEqual(denial_message(Decision(False, "missing_scope", "crm.write"), REP, "create_case"),
                         "Access denied: cs-rep lacks scope crm.write for create_case.")
        self.assertIn("outside your region (cs-rep may see: Central)",
                      denial_message(Decision(False, "out_of_region"), REP, "get_open_invoices"))
        self.assertIn("only a CS lead", denial_message(Decision(False, "region_unknown"), REP, "get_sales_order"))
        self.assertIn("human-only", denial_message(Decision(False, "restricted_change"), LEAD, "update_pricing"))
        for reason in (POLICY_UNAVAILABLE, POLICY_ERROR, "invalid_request", "anything-else"):
            self.assertIn("could not authorize", denial_message(Decision(False, reason), REP, "search_accounts"))


class _Gateway(unittest.IsolatedAsyncioTestCase):
    """The gateway on seed backends against the real policy; each test picks who is calling."""

    def setUp(self) -> None:
        reset_systems()
        set_connectors(build_connectors(load_settings({})))
        use_policy(self, principal=None)

    async def call(self, name: str, args: dict, srv: GatewayServer = mcp):
        async with Client(srv) as client:
            return await client.call_tool(name, args)

    async def logged(self, name: str, args: dict, srv: GatewayServer = mcp) -> tuple[Any, dict]:
        with self.assertLogs("ridgeline.gateway", logging.INFO) as cm:
            result = await self.call(name, args, srv)
        records = [json.loads(r.getMessage()) for r in cm.records]
        calls = [r for r in records if r["event"] == "tool_call"]
        self.assertEqual(len(calls), 1, records)
        return result, calls[0]

    async def listed(self, srv: GatewayServer = mcp) -> list[str]:
        async with Client(srv) as client:
            return sorted(t.name for t in (await client.list_tools()).tools)


class ScopeRuleTests(_Gateway):
    async def test_client_without_scopes_is_denied_every_tool_with_a_clear_error(self):
        """Milestone 4 acceptance: unauthorized tool -> clear MCP error. Also the M3 promise about p01-expiry-test."""
        act_as(self, NO_SCOPES)
        scope_of = {"search_accounts": "crm.read", "get_sales_order": "erp.read", "track_shipment": "shipping.read",
                    "get_open_invoices": "ar.read"}
        for name, args in CALLS:
            result, rec = await self.logged(name, args)
            self.assertTrue(result.is_error, name)
            self.assertEqual(result.content[0].text,
                             f"Access denied: p01-expiry-test lacks scope {scope_of[name]} for {name}.")
            self.assertIsNone(result.structured_content)
            self.assertEqual((rec["outcome"], rec["ok"], rec["policy_reason"]), ("denied", False, "missing_scope"))
            self.assertNotIn("backend", rec, "a denied call must not reach a backend")
            self.assertIn("policy_ms", rec)
            self.assertNotIn(str(next(iter(args.values()))), json.dumps(rec))

    async def test_partial_scopes_reach_only_their_tools(self):
        act_as(self, CRM_ONLY)
        self.assertFalse((await self.call("search_accounts", {"query": "Summit"})).is_error)
        for name, args in CALLS[1:]:
            result = await self.call(name, args)
            self.assertTrue(result.is_error, name)
            self.assertIn("Access denied: p01-crm-only-test lacks scope", result.content[0].text)

    async def test_denial_comes_before_argument_validation(self):
        act_as(self, NO_SCOPES)
        for args in ({"sales_order": "bad-id"}, {"sales_order": "SO-104233", "extra": "x"}, {}):
            result, rec = await self.logged("get_sales_order", args)
            self.assertIn("Access denied", result.content[0].text)
            self.assertEqual(rec["outcome"], "denied")

    async def test_non_ascii_argument_names_are_refused_by_policy_before_anything_runs(self):
        act_as(self, LEAD)
        for key in ("pr\u0130ce", "di\u017fcount", "credit limit"):
            result, rec = await self.logged("get_sales_order", {"sales_order": "SO-104233", key: 1})
            self.assertTrue(result.is_error, key)
            self.assertEqual((rec["outcome"], rec["policy_reason"]), ("denied", "invalid_request"))
            self.assertNotIn("backend", rec)

    async def test_caller_without_identity_is_denied(self):
        act_as(self, {})
        result, rec = await self.logged("search_accounts", {"query": "Summit"})
        self.assertIn("could not authorize", result.content[0].text)
        self.assertEqual(rec["policy_reason"], "invalid_request")

    async def test_restricted_and_unmapped_tools_are_denied_even_for_a_lead(self):
        srv = GatewayServer(name="t")
        ran = []

        @srv.tool()
        def update_pricing(account_id: str) -> str:
            ran.append(account_id)
            return "changed"

        @srv.tool()
        def delete_account(account_id: str) -> str:
            ran.append(account_id)
            return "deleted"

        act_as(self, LEAD)
        result, rec = await self.logged("update_pricing", {"account_id": "ACCT-1002"}, srv)
        self.assertEqual(result.content[0].text,
                         "Access denied: pricing, credit-limit and payment-term changes are human-only.")
        self.assertEqual(rec["policy_reason"], "restricted_change")
        result, rec = await self.logged("delete_account", {"account_id": "ACCT-1002"}, srv)
        self.assertEqual(result.content[0].text, "Access denied: no policy authorizes delete_account.")
        self.assertEqual(rec["policy_reason"], "unknown_tool")
        self.assertEqual(ran, [])


class RegionRuleTests(_Gateway):
    async def test_rep_reads_records_of_accounts_in_its_region(self):
        act_as(self, REP)
        for name, args in CALLS:
            result, rec = await self.logged(name, args)
            self.assertFalse(result.is_error, (name, result.content))
            self.assertEqual((rec["outcome"], rec["regions"]), ("ok", ["Central"]))
            self.assertNotIn("policy_reason", rec)

    async def test_rep_is_denied_records_of_accounts_outside_its_region(self):
        act_as(self, REP)
        for name, args in WEST_CALLS:
            result, rec = await self.logged(name, args)
            self.assertTrue(result.is_error, name)
            self.assertEqual(result.content[0].text, "Access denied: this record belongs to an account outside "
                             "your region (cs-rep may see: Central). Ask a CS lead.")
            self.assertIsNone(result.structured_content)
            for leak in ("Pinecrest", "INV-90377", "SHP-55107", "West"):
                self.assertNotIn(leak, str(result.content), name)
            self.assertEqual((rec["outcome"], rec["policy_reason"]), ("denied", "out_of_region"))

    async def test_lead_reads_every_region(self):
        act_as(self, LEAD)
        for name, args in CALLS + WEST_CALLS:
            self.assertFalse((await self.call(name, args)).is_error, (name, args))

    async def test_search_results_are_filtered_by_region(self):
        act_as(self, REP)
        result, rec = await self.logged("search_accounts", {"query": "Pinecrest"})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, {"query": "Pinecrest", "matches": [], "hidden_out_of_region": 1})
        self.assertEqual((rec["outcome"], rec["hidden_out_of_region"]), ("ok", 1))
        everything = get_systems().crm.find_accounts("ACCT-10")
        self.assertGreater(len({a["region"] for a in everything}), 1)
        mine = (await self.call("search_accounts", {"query": "ACCT-10"})).structured_content
        self.assertEqual(mine["matches"], [a for a in everything if a["region"] == "Central"])
        self.assertEqual(mine["hidden_out_of_region"], len(everything) - len(mine["matches"]))
        act_as(self, LEAD)
        full = (await self.call("search_accounts", {"query": "ACCT-10"})).structured_content
        self.assertEqual(full, {"query": "ACCT-10", "matches": everything})

    async def test_no_match_is_an_empty_result_not_a_denial(self):
        act_as(self, REP)
        result = await self.call("search_accounts", {"query": "Nonexistent Co"})
        self.assertEqual(result.structured_content, {"query": "Nonexistent Co", "matches": []})

    async def test_unknown_region_fails_closed_for_reps_but_not_for_leads(self):
        """A record with no Ridgeline account (every SAP sandbox order) has no region."""
        order = get_systems().erp.order("SO-104233")
        for account_id in (None, "ACCT-9999", "0001000042"):
            erp = mock.AsyncMock(return_value={**order, "account_id": account_id, "account_name": None})
            with mock.patch.object(server.get_connectors().erp, "fetch_order", erp):
                act_as(self, REP)
                for name in ("get_sales_order", "track_shipment"):
                    result, rec = await self.logged(name, {"sales_order": "SO-104233"})
                    self.assertTrue(result.is_error, (name, account_id))
                    self.assertIn("not linked to an account with a known region", result.content[0].text)
                    self.assertEqual(rec["policy_reason"], "region_unknown")
                act_as(self, LEAD)
                for name in ("get_sales_order", "track_shipment"):
                    self.assertFalse((await self.call(name, {"sales_order": "SO-104233"})).is_error, (name, account_id))

    async def test_account_without_a_region_in_crm_is_unknown(self):
        get_systems().dataset.accounts["ACCT-1002"]["region"] = ""
        act_as(self, REP)
        result = await self.call("get_open_invoices", {"account_id": "ACCT-1002"})
        self.assertIn("not linked to an account with a known region", result.content[0].text)
        found = (await self.call("search_accounts", {"query": "Summit"})).structured_content
        self.assertEqual((found["matches"], found["hidden_out_of_region"]), ([], 1))

    async def test_token_without_regions_sees_no_records(self):
        act_as(self, {**REP, "regions": []})
        for name, args in CALLS[1:]:
            result = await self.call(name, args)
            self.assertIn("may see: none", result.content[0].text, name)
        self.assertEqual((await self.call("search_accounts", {"query": "Summit"})).structured_content["matches"], [])

    async def test_not_found_stays_not_found(self):
        act_as(self, REP)
        result, rec = await self.logged("get_sales_order", {"sales_order": "SO-999999"})
        self.assertIn("SO-999999 was not found", result.content[0].text)
        self.assertEqual(rec["outcome"], "not_found")
        result = await self.call("get_open_invoices", {"account_id": "ACCT-9999"})
        self.assertIn("was not found in AR", result.content[0].text)

    async def test_out_of_region_caller_causes_no_ar_read(self):
        act_as(self, REP)
        ar = server.get_connectors().ar
        with mock.patch.object(ar, "fetch_open_items", wraps=ar.fetch_open_items) as fetch:
            result, rec = await self.logged("get_open_invoices", {"account_id": "ACCT-1003"})   # Pinecrest: West
            self.assertEqual((result.is_error, rec["policy_reason"]), (True, "out_of_region"))
            fetch.assert_not_called()
            self.assertFalse((await self.call("get_open_invoices", {"account_id": "ACCT-1002"})).is_error)
            fetch.assert_called_once()

    async def test_crm_failure_while_resolving_the_region_returns_no_data(self):
        act_as(self, REP)

        async def down(_query: str):
            from ridgeline_gateway.connectors import ConnectorError
            raise ConnectorError("Salesforce is unreachable.")

        with mock.patch.object(server.get_connectors().crm, "find_accounts", side_effect=down):
            result, rec = await self.logged("get_open_invoices", {"account_id": "ACCT-1002"})
        self.assertTrue(result.is_error)
        self.assertIn("Salesforce is unreachable.", result.content[0].text)
        self.assertNotIn("INV-90388", str(result.content))
        self.assertEqual(rec["outcome"], "upstream_error")

    async def test_region_lookups_do_not_change_the_logged_backend(self):
        ar_items = get_systems().ar.open_items("ACCT-1002")

        def ar_api(req: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json={"account_id": "ACCT-1002", "invoices": ar_items})

        settings = load_settings({"AR_BACKEND": "http", "AR_API_TOKEN": "t" * 20})
        set_connectors(build_connectors(settings, transport=httpx2.MockTransport(ar_api)))
        act_as(self, REP)
        result, rec = await self.logged("get_open_invoices", {"account_id": "ACCT-1002"})
        self.assertFalse(result.is_error)
        self.assertEqual((rec["backend"], rec["upstream_calls"], rec["upstream_status"]), ("ar_api", 1, 200))

    async def test_a_tool_that_skips_the_data_check_returns_nothing(self):
        srv = GatewayServer(name="t")

        @srv.tool()
        def get_open_invoices(account_id: str) -> dict:
            return {"account_id": account_id, "invoices": ["INV-SECRET"]}

        act_as(self, LEAD)
        result, rec = await self.logged("get_open_invoices", {"account_id": "ACCT-1003"}, srv)
        self.assertTrue(result.is_error)
        self.assertNotIn("INV-SECRET", str(result.content))
        self.assertEqual((rec["outcome"], rec["policy_reason"]), ("denied", "resource_not_checked"))


class FailClosedTests(_Gateway):
    async def test_opa_down_denies_every_call_and_lists_nothing(self):
        set_policy(build_policy(PolicyConfig("http://127.0.0.1:1", 0.5)))
        act_as(self, LEAD)
        for name, args in CALLS:
            result, rec = await self.logged(name, args)
            self.assertTrue(result.is_error, name)
            self.assertEqual(result.content[0].text,
                             "Access denied: the policy service could not authorize this call, so it was refused.")
            self.assertEqual((rec["outcome"], rec["policy_reason"]), ("denied", "policy_unavailable"))
            self.assertNotIn("backend", rec)
        self.assertEqual(await self.listed(), [])

    async def test_opa_failing_between_the_two_checks_returns_no_data(self):
        answers = iter([{"result": {"allow": True, "reason": "allowed"}}])

        def flaky(req: httpx2.Request) -> httpx2.Response:
            body = next(answers, None)
            return httpx2.Response(200, json=body) if body else httpx2.Response(503)

        set_policy(opa_answering(flaky))
        act_as(self, LEAD)
        result, rec = await self.logged("get_open_invoices", {"account_id": "ACCT-1002"})
        self.assertTrue(result.is_error)
        self.assertNotIn("INV-90388", str(result.content))
        self.assertEqual(rec["policy_reason"], "policy_unavailable")

    async def test_opa_failing_during_a_search_is_a_denial_not_hidden_records(self):
        for body in (None, {"result": {"allow": False, "reason": "invalid_request"}}, {"result": "nonsense"}):
            answers = iter([{"result": {"allow": True, "reason": "allowed"}}])

            def flaky(req: httpx2.Request, body=body, answers=answers) -> httpx2.Response:
                first = next(answers, None)
                if first:
                    return httpx2.Response(200, json=first)
                return httpx2.Response(200, json=body) if body else httpx2.Response(503)

            set_policy(opa_answering(flaky))
            act_as(self, REP)
            result, rec = await self.logged("search_accounts", {"query": "Summit"})
            self.assertTrue(result.is_error, body)
            self.assertIn("could not authorize", result.content[0].text)
            self.assertIsNone(result.structured_content)
            self.assertEqual(rec["outcome"], "denied")
            self.assertIn(rec["policy_reason"], ("policy_unavailable", "policy_error", "invalid_request"))
            self.assertNotIn("hidden_out_of_region", rec)

    async def test_gateway_without_a_policy_denies(self):
        set_policy(None)
        act_as(self, LEAD)
        self.assertTrue((await self.call("search_accounts", {"query": "Summit"})).is_error)
        self.assertEqual(await self.listed(), [])

    async def test_opa_api_refuses_reads_and_writes_of_policy_and_data(self):
        base = start_opa()
        async with httpx2.AsyncClient() as http:
            self.assertEqual((await http.get(f"{base}/health")).status_code, 200)
            for method, path in (("GET", "/v1/data"), ("GET", "/v1/policies"), ("PUT", "/v1/data/gateway_policy"),
                                 ("PUT", "/v1/policies/x"), ("DELETE", "/v1/policies/x"), ("POST", "/v1/query"),
                                 ("POST", "/v1/data/gateway_policy")):
                resp = await http.request(method, base + path, content=b"{}" if method != "GET" else None)
                self.assertEqual(resp.status_code, 401, (method, path))
        self.assertTrue((await real_policy().decide(LEAD, "search_accounts", {})).allow)


class ToolListingTests(_Gateway):
    """tools/list shows a caller only what its scopes cover (call-time enforcement is the real control)."""

    def _server_with_writes(self) -> GatewayServer:
        srv = GatewayServer(name="t")
        for name in (*READ_TOOLS, "create_case", "update_pricing", "delete_account"):
            srv.add_tool(lambda: "x", name=name)
        return srv

    async def test_rep_sees_the_read_tools_and_no_create_style_tool(self):
        act_as(self, REP)
        self.assertEqual(await self.listed(), READ_TOOLS)
        self.assertEqual(await self.listed(self._server_with_writes()), READ_TOOLS)

    async def test_lead_also_sees_create_case_but_never_restricted_or_unmapped_tools(self):
        act_as(self, LEAD)
        self.assertEqual(await self.listed(self._server_with_writes()), sorted([*READ_TOOLS, "create_case"]))

    async def test_out_of_scope_tools_are_hidden(self):
        act_as(self, CRM_ONLY)
        self.assertEqual(await self.listed(), ["search_accounts"])
        act_as(self, {**REP, "scopes": ["erp.read", "shipping.read"]})
        self.assertEqual(await self.listed(self._server_with_writes()), ["get_sales_order", "track_shipment"])
        for principal in (NO_SCOPES, {}):
            act_as(self, principal)
            self.assertEqual(await self.listed(self._server_with_writes()), [])

    async def test_a_hidden_tool_is_still_denied_when_called_directly(self):
        act_as(self, REP)
        srv = self._server_with_writes()
        result = await self.call("create_case", {}, srv)
        self.assertEqual(result.content[0].text, "Access denied: cs-rep lacks scope crm.write for create_case.")


class HttpAuthorizationTests(unittest.TestCase):
    """End to end over HTTP: the identity the policy sees comes from the verified token, nothing else."""

    def setUp(self) -> None:
        reset_systems()
        set_connectors(build_connectors(load_settings({})))
        use_policy(self, principal=None)
        self.idp = FakeKeycloak()
        mcp.enable_auth(CONFIG, build_verifier(CONFIG, transport=httpx2.MockTransport(self.idp)))
        self.addCleanup(reset_auth)
        app = mcp.streamable_http_app(streamable_http_path="/mcp", transport_security=transport_security())
        self.http = TestClient(app, base_url="http://localhost:8765")
        self.http.__enter__()
        self.addCleanup(self.http.__exit__, None, None, None)

    def rpc(self, token: str, method: str, params: dict | None = None) -> dict:
        headers = {**MCP_HEADERS, "Authorization": f"Bearer {token}"}
        init = self.http.post("/mcp", headers=headers, json=INIT)
        self.assertEqual(init.status_code, 200)
        headers.update({"Mcp-Session-Id": init.headers["mcp-session-id"], "MCP-Protocol-Version": "2025-06-18"})
        self.http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        resp = self.http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": method,
                                                             "params": params or {}})
        self.assertEqual(resp.status_code, 200)
        data = [line[5:] for line in resp.text.splitlines() if line.startswith("data:")]
        return json.loads(data[-1] if data else resp.text)["result"]

    def call(self, token: str, name: str, args: dict) -> dict:
        return self.rpc(token, "tools/call", {"name": name, "arguments": args})

    def test_scopes_and_regions_come_from_the_token(self):
        rep = self.idp.mint()
        self.assertFalse(self.call(rep, "get_open_invoices", {"account_id": "ACCT-1002"}).get("isError", False))
        denied = self.call(rep, "get_open_invoices", {"account_id": "ACCT-1003"})
        self.assertTrue(denied["isError"])
        self.assertIn("outside your region", denied["content"][0]["text"])
        lead = self.idp.mint(azp="cs-lead", sub="service-account-cs-lead", regions=["*"],
                             scope="crm.read erp.read shipping.read ar.read crm.write")
        self.assertFalse(self.call(lead, "get_open_invoices", {"account_id": "ACCT-1003"}).get("isError", False))

    def test_token_without_scopes_or_regions(self):
        nothing = self.idp.mint(azp="p01-expiry-test", sub="service-account-p01-expiry-test", scope="",
                                drop=("regions",))
        self.assertEqual(self.rpc(nothing, "tools/list")["tools"], [])
        denied = self.call(nothing, "search_accounts", {"query": "Summit"})
        self.assertEqual(denied["content"][0]["text"],
                         "Access denied: p01-expiry-test lacks scope crm.read for search_accounts.")
        no_region = self.idp.mint(drop=("regions",))
        self.assertEqual(sorted(t["name"] for t in self.rpc(no_region, "tools/list")["tools"]), READ_TOOLS)
        self.assertIn("may see: none", self.call(no_region, "get_sales_order", {"sales_order": "SO-104233"})
                      ["content"][0]["text"])

    def test_identity_cannot_be_supplied_in_arguments(self):
        rep = self.idp.mint()
        for extra in ({"regions": ["*"]}, {"principal": {"regions": ["*"]}}, {"resource": {"region": "Central"}}):
            result = self.call(rep, "get_open_invoices", {"account_id": "ACCT-1003", **extra})
            self.assertTrue(result["isError"], extra)
            self.assertNotIn("INV-90377", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
