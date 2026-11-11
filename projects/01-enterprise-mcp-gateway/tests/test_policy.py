"""Milestone 4 on the copilot side: the GUARDRAILS policy hook and STATE obey the gateway's policy.

Offline (no OPA_URL) the copilot uses `local_decision`, a copy of policy/gateway.rego. The shared table
policy/decision_cases.json is run against both: here, and against the Rego by `make opa-test P=01`.
With OPA_URL set the hook asks OPA and fails closed (a stdlib HTTP server stands in for OPA here).
"""
import http.server
import json
import os
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

os.environ.setdefault("MEMORY_SQLITE", ":memory:")
os.environ.setdefault("TRACE_FILE", os.devnull)
os.environ.setdefault("PROGRESS_DIR", tempfile.mkdtemp())

from agentcore import GuardrailViolation, Principal, SQLiteMemory
from cs_copilot.agent import guardrails as G
from cs_copilot.agent.build import ROLE_REGIONS, ROLE_SCOPES, build_cs_copilot, principal_for_role
from cs_copilot.agent.perceive import CaseRequestPerceiver
from cs_copilot.agent.reason import scripted_llm
from cs_copilot.agent.state import CurrentStateProvider
from cs_copilot.agent.tools import TOOLS
from mock_systems import get_systems, reset_systems

ROOT = pathlib.Path(__file__).resolve().parents[1]
REP = principal_for_role("cs-rep")         # Central
LEAD = principal_for_role("cs-lead")       # every region
TOOL = {t.name: t for t in TOOLS}
WEST_Q = "Where is order SO-104301 for Pinecrest Hardware?"


def no_opa():
    return mock.patch.dict(os.environ, {"OPA_URL": ""})


class DecisionTableParityTests(unittest.TestCase):
    def test_local_policy_matches_the_shared_decision_table(self):
        cases = json.loads((ROOT / "policy" / "decision_cases.json").read_text())["decision_cases"]
        self.assertGreaterEqual(len(cases), 30)
        for case in cases:
            with self.subTest(case=case["name"]):
                self.assertEqual(G.local_decision(case["input"]), case["expect"])

    def test_reason_codes_come_from_the_one_shared_list(self):
        policy = json.loads((ROOT / "policy" / "data.json").read_text())["gateway_policy"]
        self.assertEqual(G.REASONS, frozenset(policy["reasons"]))
        self.assertFalse(set(policy["reasons"]) & set(policy["gateway_reasons"]))
        generic = G._explain({"reason": "no_such_reason"}, LEAD, "create_case")
        for reason in set(policy["reasons"]) - {"allowed", "invalid_request"}:
            with self.subTest(reason=reason):       # every denial the policy can return has its own text
                self.assertNotEqual(G._explain({"reason": reason, "required_scope": "crm.write"}, LEAD, "create_case"),
                                    generic.replace("no_such_reason", reason))

    def test_policy_data_matches_the_copilot_tools(self):
        self.assertEqual({t.name: t.scope for t in TOOLS}, G.TOOL_SCOPES)
        self.assertEqual({t.name for t in TOOLS if t.side_effect == "write"}, set(G.WRITE_TOOLS))

    def test_roles_match_the_keycloak_clients(self):
        realm = json.loads((ROOT / "keycloak" / "ridgeline-realm.json").read_text())
        clients = {c["clientId"]: c for c in realm["clients"]}
        gateway_scopes = set(G.TOOL_SCOPES.values())
        for role in ROLE_SCOPES:
            client = clients[role]
            self.assertEqual(set(client["defaultClientScopes"]) & gateway_scopes, set(ROLE_SCOPES[role]), role)
            claim = [m["config"] for m in client["protocolMappers"] if m["config"]["claim.name"] == "regions"]
            self.assertEqual(tuple(json.loads(claim[0]["claim.value"])), ROLE_REGIONS[role], role)
        self.assertNotIn("protocolMappers", clients["p01-expiry-test"])
        self.assertEqual(clients["p01-expiry-test"]["defaultClientScopes"], ["gateway-audience"])


class PolicyHookTests(unittest.TestCase):
    def setUp(self):
        reset_systems()
        patcher = no_opa()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.g = G.get_guardrails(5)

    def denied(self, principal, tool, args) -> str:
        with self.assertRaises(GuardrailViolation) as ctx:
            self.g.authorize(principal, TOOL[tool], args)
        self.assertEqual(ctx.exception.rule, "policy")
        return ctx.exception.detail

    def test_rep_reads_its_region_and_is_denied_the_others(self):
        for tool, args in [("search_accounts", {"query": "Summit Contractors"}), ("get_sales_order", {"sales_order": "SO-104233"}),
                           ("track_shipment", {"sales_order": " so-104233 "}), ("get_open_invoices", {"account_id": "ACCT-1002"})]:
            self.g.authorize(REP, TOOL[tool], args)
        for tool, args in [("search_accounts", {"query": "Pinecrest Hardware"}), ("get_sales_order", {"sales_order": "SO-104301"}),
                           ("track_shipment", {"sales_order": "so-104301"}), ("get_open_invoices", {"account_id": "acct-1003"}),
                           ("get_open_invoices", {"account_id": "ACCT-1001"})]:
            detail = self.denied(REP, tool, args)
            self.assertIn("outside your region (cs-rep may see: Central)", detail)
            self.assertIn("CS lead", detail)
            for leak in ("Pinecrest", "West", "East", "Big Box"):
                self.assertNotIn(leak, detail)

    def test_search_spanning_regions_is_denied_whole_for_a_rep(self):
        self.assertGreater(len({a["region"] for a in get_systems().crm.find_accounts("ACCT-10")}), 1)
        self.assertIn("out_of_region", self.denied(REP, "search_accounts", {"query": "ACCT-10"}))
        self.g.authorize(LEAD, TOOL["search_accounts"], {"query": "ACCT-10"})

    def test_lead_reads_every_region(self):
        for tool, args in [("get_sales_order", {"sales_order": "SO-104301"}), ("get_open_invoices", {"account_id": "ACCT-1003"}),
                           ("search_accounts", {"query": "BBO"})]:
            self.g.authorize(LEAD, TOOL[tool], args)

    def test_records_that_do_not_exist_reach_the_tool_as_not_found(self):
        for tool, args in [("get_sales_order", {"sales_order": "SO-199999"}), ("get_open_invoices", {"account_id": "ACCT-9999"}),
                           ("search_accounts", {"query": "Nonexistent Co"}), ("search_accounts", {"query": "  "})]:
            self.g.authorize(REP, TOOL[tool], args)

    def test_unknown_region_and_missing_regions_fail_closed(self):
        get_systems().dataset.accounts["ACCT-1002"]["region"] = ""
        self.assertIn("region_unknown", self.denied(REP, "get_sales_order", {"sales_order": "SO-104233"}))
        self.g.authorize(LEAD, TOOL["get_sales_order"], {"sales_order": "SO-104233"})
        reset_systems()
        nobody = Principal("cs-rep", ROLE_SCOPES["cs-rep"])
        self.assertIn("may see: none", self.denied(nobody, "get_sales_order", {"sales_order": "SO-104233"}))

    def test_policy_error_denies(self):
        with mock.patch.object(G, "decide", side_effect=RuntimeError("boom")):
            self.assertIn("policy error (RuntimeError); denied", self.denied(LEAD, "search_accounts", {"query": "Summit"}))

    def test_load_skill_is_a_local_tool(self):
        local, other = mock.Mock(), mock.Mock()
        local.name, other.name = "load_skill", "load_everything"
        self.assertTrue(G.policy_hook(Principal("anyone"), local, {})[0])
        self.assertFalse(G.policy_hook(LEAD, other, {})[0])


class StateRegionTests(unittest.TestCase):
    def setUp(self):
        reset_systems()
        patcher = no_opa()
        patcher.start()
        self.addCleanup(patcher.stop)

    def snapshot(self, principal, question=WEST_Q):
        return CurrentStateProvider().snapshot(CaseRequestPerceiver().perceive(question, principal))

    def test_context_never_carries_records_outside_the_callers_region(self):
        snap = self.snapshot(REP)
        denied = {"error": "access denied: outside your region"}
        self.assertEqual(snap["accounts"], [{"name": "Pinecrest Hardware", **denied}])
        self.assertEqual(snap["orders"], {"SO-104301": denied})
        self.assertEqual(snap["shipments"], {"SO-104301": denied})
        for key in ("open_orders", "open_invoices"):
            self.assertNotIn(key, snap)
        text = json.dumps(snap)
        for leak in ("ACCT-1003", "Delivered", "SHP-55107", "West", "@example.com"):
            self.assertNotIn(leak, text)

    def test_lead_and_in_region_rep_get_the_full_snapshot(self):
        lead = self.snapshot(LEAD)
        self.assertEqual(lead["accounts"][0]["account_id"], "ACCT-1003")
        self.assertEqual(lead["orders"]["SO-104301"]["status"], "Delivered")
        self.assertIn("ACCT-1003", lead["open_invoices"])
        rep = self.snapshot(REP, "Where is SO-104233 for Summit Contractors?")
        self.assertEqual(rep["orders"]["SO-104233"]["status"], "Shipped")
        self.assertEqual(rep["shipments"]["SO-104233"][0]["shipment_id"], "SHP-55012")

    def test_unknown_order_is_still_reported_as_not_found(self):
        self.assertEqual(self.snapshot(REP, "Where is SO-199999?")["orders"], {"SO-199999": {"error": "not found"}})


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        reset_systems()
        patcher = no_opa()
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_agent(self, script, principal):
        agent = build_cs_copilot(scripted_llm(script))
        agent.memory = SQLiteMemory()
        return agent.run(WEST_Q, principal)

    def test_out_of_region_request_is_denied_and_escalated_without_leaking(self):
        out = self.run_agent("out-of-region-order-denied", REP)
        self.assertEqual(out.status, "escalated")
        self.assertEqual(out.tools_used, [])
        denials = [m["content"] for m in out.history if "denied by policy" in m["content"]]
        self.assertEqual(len(denials), 2)
        history = json.dumps(out.history)
        for leak in ("SHP-55107", "INV-90377", "ACCT-1003", "cartons"):
            self.assertNotIn(leak, history)
        self.assertIn("CS lead", out.answer)

    def test_lead_gets_the_answer(self):
        out = self.run_agent("lead-cross-region-allowed", LEAD)
        self.assertEqual((out.status, out.tools_used), ("answered", ["get_sales_order", "track_shipment"]))
        self.assertIn("5 of 10 cartons", out.answer)


class _FakeOpa(http.server.BaseHTTPRequestHandler):
    answer: tuple[int, bytes] = (200, b"{}")
    seen: list[tuple[str, dict]] = []

    def do_POST(self):  # noqa: N802  (http.server API)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((self.path, body))
        status, payload = type(self).answer
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class OpaModeTests(unittest.TestCase):
    """With OPA_URL set the hook asks OPA for every decision and denies unless it gets a clean allow."""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpa)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        reset_systems()
        _FakeOpa.seen = []
        patcher = mock.patch.dict(os.environ, {"OPA_URL": self.url})
        patcher.start()
        self.addCleanup(patcher.stop)

    def answer(self, body, status=200):
        _FakeOpa.answer = (status, body if isinstance(body, bytes) else json.dumps(body).encode())

    def test_hook_sends_principal_tool_args_then_the_resource_region(self):
        self.answer({"result": {"allow": True, "reason": "allowed"}})
        ok, _ = G.policy_hook(REP, TOOL["get_sales_order"], {"sales_order": "SO-104233"})
        self.assertTrue(ok)
        (path, first), (_, second) = _FakeOpa.seen
        self.assertEqual(path, "/v1/data/ridgeline/gateway/decision")
        self.assertEqual(first, {"input": {"principal": {"client_id": "cs-rep", "scopes": sorted(ROLE_SCOPES["cs-rep"]),
                                                         "regions": ["Central"]},
                                           "tool": "get_sales_order", "args": {"sales_order": "SO-104233"}}})
        self.assertEqual(second["input"]["resource"], {"region": "Central"})

    def test_opa_denial_is_enforced_even_where_the_local_copy_would_allow(self):
        self.answer({"result": {"allow": False, "reason": "out_of_region"}})
        ok, why = G.policy_hook(LEAD, TOOL["get_sales_order"], {"sales_order": "SO-104233"})
        self.assertFalse(ok)
        self.assertIn("out_of_region", why)

    def test_errors_and_malformed_answers_deny(self):
        for status, body in [(500, {"code": "internal_error"}), (200, b"not json"), (200, {}), (200, {"result": True}),
                             (200, {"result": {"allow": "yes", "reason": "allowed"}}),
                             (200, {"result": {"allow": True, "reason": "missing_scope"}}), (200, [])]:
            self.answer(body, status)
            ok, why = G.policy_hook(LEAD, TOOL["search_accounts"], {"query": "Summit"})
            self.assertFalse(ok, body)
            self.assertIn("could not authorize", why)

    def test_unreachable_opa_denies(self):
        with mock.patch.dict(os.environ, {"OPA_URL": "http://127.0.0.1:1"}):
            ok, why = G.policy_hook(LEAD, TOOL["search_accounts"], {"query": "Summit"})
            self.assertFalse(G.may_see(LEAD, "get_sales_order", "Central"))
        self.assertFalse(ok)
        self.assertIn("policy_unavailable", why)


if __name__ == "__main__":
    unittest.main()
