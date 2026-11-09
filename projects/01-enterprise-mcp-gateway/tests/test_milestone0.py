"""Milestone 0: every layer runs end to end on agent-core with a scripted LLM, against seeded mocks."""
import json
import os
import pathlib
import tempfile
import unittest

os.environ.setdefault("MEMORY_SQLITE", ":memory:")
os.environ.setdefault("TRACE_FILE", os.devnull)
os.environ.setdefault("PROGRESS_DIR", tempfile.mkdtemp())

from agentcore import (FakeLLM, GuardrailViolation, InMemorySink, Percept, Principal, SQLiteMemory, Tool,
                       ToolResult, Tracer, load_cases, run_evals)
from cs_copilot.agent import tools as T
from cs_copilot.agent.build import build_cs_copilot, principal_for_role
from cs_copilot.agent.guardrails import get_guardrails
from cs_copilot.agent.knowledge import known_citations
from cs_copilot.agent.perceive import CaseRequestPerceiver
from cs_copilot.agent.reason import scripted_llm
from cs_copilot.agent.state import CurrentStateProvider
from cs_copilot.agent.verify import EvidenceBackedAnswerVerifier
from mock_systems import build_dataset, get_systems, reset_systems

ROOT = pathlib.Path(__file__).resolve().parents[1]
REP = principal_for_role("cs-rep")
LEAD = principal_for_role("cs-lead")
DEMO_Q = "Summit Contractors says SO-104233 hasn't arrived and they were billed. What's going on?"


def traced(llm):
    """Build the real cs-copilot with an in-memory trace sink and a fresh in-memory long-term memory."""
    agent = build_cs_copilot(llm)
    sink = InMemorySink()
    agent.tracer = Tracer(sink)
    agent.memory = SQLiteMemory()
    return agent, sink


def context_of(llm: FakeLLM, call: int = 0) -> dict:
    return json.loads(llm.calls[call]["messages"][0]["content"])


class SeedDataTests(unittest.TestCase):
    def test_same_seed_same_data_and_planted_scenarios_present(self):
        a, b, c = build_dataset(7), build_dataset(7), build_dataset(8)
        self.assertEqual(a, b)
        self.assertNotEqual(a.orders, c.orders)
        for ds in (a, c):
            self.assertEqual(ds.shipments["SHP-55012"]["status"], "EXCEPTION")
            self.assertEqual(ds.shipments["SHP-55107"]["proof_of_delivery"]["cartons_delivered"], 5)
            self.assertEqual(ds.invoices["INV-90412"]["amount"], 20000.0)

    def test_only_synthetic_contacts(self):
        for acct in build_dataset().accounts.values():
            self.assertTrue(acct["primary_contact"]["email"].endswith("@example.com"))


class PerceiveTests(unittest.TestCase):
    def setUp(self):
        self.p = CaseRequestPerceiver()

    def test_extracts_ids_and_account_aliases(self):
        pc = self.p.perceive("BBO asks about  so-104290,\n INV-90412 and PO-7190", REP)
        self.assertEqual(pc.kind, "cs_request")
        self.assertEqual(pc.content, "BBO asks about so-104290, INV-90412 and PO-7190")
        self.assertEqual(pc.entities, {"sales_order": ["SO-104290"], "po_number": ["PO-7190"],
                                       "invoice": ["INV-90412"], "account": ["Big Box Outdoor"]})
        self.assertIs(pc.principal, REP)

    def test_accepts_inbound_case_payload(self):
        pc = self.p.perceive({"subject": "Late order", "description": "SO-104233 not here", "account": "Summit Contractors"}, REP)
        self.assertEqual(pc.source, "case")
        self.assertEqual(pc.entities["account"], ["Summit Contractors LLC"])
        self.assertEqual(pc.entities["sales_order"], ["SO-104233"])

    def test_rejects_empty_or_wrong_type(self):
        for raw in ("   ", {"subject": ""}, 42):
            with self.assertRaises(ValueError):
                self.p.perceive(raw, REP)

    def test_oversized_input_is_rejected_by_the_input_guardrail(self):
        agent, _ = traced(FakeLLM([]))
        out = agent.run("where is my order " * 400, REP)
        self.assertEqual(out.status, "rejected")
        self.assertIn("input_size", out.answer)


class StateTests(unittest.TestCase):
    def setUp(self):
        reset_systems()
        self.state = CurrentStateProvider()

    def percept(self, principal):
        return CaseRequestPerceiver().perceive("Where is SO-104233 for Summit Contractors?", principal)

    def test_snapshot_is_filtered_by_scope(self):
        crm_only = self.state.snapshot(self.percept(Principal("r", frozenset({"crm.read"}))))
        self.assertEqual(crm_only["accounts"][0]["account_id"], "ACCT-1002")
        for key in ("orders", "open_orders", "shipments", "open_invoices"):
            self.assertNotIn(key, crm_only)
        self.assertIn("erp.read", crm_only["not_visible_without_scope"])
        full = self.state.snapshot(self.percept(REP))
        self.assertEqual(full["orders"]["SO-104233"]["status"], "Shipped")
        self.assertEqual(full["shipments"]["SO-104233"][0]["business_days_late"], 3)
        self.assertEqual(full["open_invoices"]["ACCT-1002"], {"count": 1, "total_open": 8370.0})

    def test_snapshot_is_live(self):
        pc = self.percept(REP)
        self.assertEqual(self.state.snapshot(pc)["orders"]["SO-104233"]["status"], "Shipped")
        get_systems().dataset.orders["SO-104233"]["status"] = "Delivered"
        self.assertEqual(self.state.snapshot(pc)["orders"]["SO-104233"]["status"], "Delivered")

    def test_state_is_reread_every_iteration(self):
        agent, sink = traced(scripted_llm("wismo-basic"))
        out = agent.run("Where is order SO-104233 for Summit Contractors?", REP)
        self.assertEqual(sum(s.name == "remember.state" for s in sink.spans), out.iterations)


class ToolTests(unittest.TestCase):
    def setUp(self):
        reset_systems()

    def test_handlers_return_seeded_data(self):
        self.assertEqual(T.search_accounts("bbo")["matches"][0]["account_id"], "ACCT-1001")
        order = T.get_sales_order(" so-104233 ")
        self.assertEqual((order["order_total"], order["account_name"]), (8370.0, "Summit Contractors LLC"))
        self.assertEqual(T.track_shipment("SO-104301")["shipments"][0]["proof_of_delivery"]["cartons_delivered"], 5)
        inv = T.get_open_invoices("ACCT-1003")
        self.assertEqual(inv["total_open"], round(sum(i["amount"] for i in inv["invoices"]), 2))

    def test_unknown_ids_raise_so_errors_reach_think(self):
        with self.assertRaises(KeyError):
            T.get_sales_order("SO-199999")
        with self.assertRaises(KeyError):
            T.get_open_invoices("ACCT-9999")


class GuardrailTests(unittest.TestCase):
    def setUp(self):
        reset_systems()

    def test_rep_without_crm_write_is_denied_and_handler_never_runs(self):
        agent, _ = traced(scripted_llm("demo-rep-create-case"))
        out = agent.run("Open a case for Summit Contractors about the late delivery of SO-104233.", REP)
        self.assertTrue(any("denied by permission" in m["content"] for m in out.history))
        self.assertNotIn("create_case", out.tools_used)
        self.assertEqual(get_systems().crm.cases, [])

    def test_lead_with_crm_write_still_fails_closed_at_approval(self):
        agent, _ = traced(scripted_llm("demo-lead-create-case"))
        out = agent.run("Open a case for Summit Contractors about the late delivery of SO-104233.", LEAD)
        self.assertTrue(any("denied by approval_gate" in m["content"] for m in out.history))
        self.assertEqual(get_systems().crm.cases, [])

    def test_policy_denies_pricing_credit_and_unlisted_tools(self):
        g = get_guardrails(5)
        p = Principal("lead", frozenset({"crm.write", "crm.read"}))
        for tool, args in [(Tool("update_pricing", "x", handler=print, scope="crm.write", side_effect="write"), {}),
                           (Tool("create_case", "x", handler=print, scope="crm.write", side_effect="write"),
                            {"credit_limit": 1}),
                           (Tool("delete_account", "x", handler=print, scope="crm.read"), {})]:
            with self.assertRaises(GuardrailViolation) as ctx:
                g.authorize(p, tool, args)
            self.assertEqual(ctx.exception.rule, "policy")

    def test_iteration_cap_escalates(self):
        loop = [{"type": "tool", "tool": "search_accounts", "args": {"query": "Summit Contractors"}}] * 10
        agent, _ = traced(FakeLLM(loop))
        out = agent.run("Where is SO-104233 for Summit Contractors?", REP)
        self.assertEqual(out.status, "escalated")
        self.assertIn("max_iterations", out.answer)


class HappyPathTests(unittest.TestCase):
    def setUp(self):
        reset_systems()

    def test_end_to_end_traces_every_layer_and_persists(self):
        progress = tempfile.mkdtemp()
        os.environ["PROGRESS_DIR"] = progress
        try:
            llm = scripted_llm("demo-late-and-billed")
            agent, sink = traced(llm)
        finally:
            os.environ["PROGRESS_DIR"] = tempfile.mkdtemp()
        out = agent.run(DEMO_Q, REP)
        self.assertEqual(out.status, "answered")
        self.assertEqual(out.tools_used, ["search_accounts", "get_sales_order", "track_shipment", "get_open_invoices"])
        self.assertTrue({"perceive", "remember", "harness", "plan", "think", "act", "guardrails"}
                        <= {s.layer for s in sink.spans})
        self.assertTrue(any(s.name == "think.goal_check" and s.attrs["achieved"] for s in sink.spans))
        self.assertIn("[EMAIL REDACTED]", out.answer)
        self.assertNotIn("@example.com", out.answer)
        saved = json.loads((pathlib.Path(progress) / f"{out.trace_id}.json").read_text())
        self.assertEqual(saved["status"], "answered")

    def test_rag_citations_and_skills_reach_the_context(self):
        llm = scripted_llm("demo-late-and-billed")
        agent, _ = traced(llm)
        agent.run(DEMO_Q, REP)
        ctx = context_of(llm)
        self.assertIn("customer-service-policy.md#0", [c["cite"] for c in ctx["retrieved_context"]])
        self.assertIn("where-is-my-order", [s["name"] for s in ctx["active_skills"]])
        self.assertIn("SO-104233", ctx["current_state"]["orders"])

    def test_memory_written_after_a_run_is_recalled_in_the_next(self):
        script = scripted_llm("policy-direct").responses * 2
        agent, sink = traced(FakeLLM(script))
        agent.run("What's our return window for unopened items?", REP)
        agent.run("What's the return window for unopened items again?", REP)
        recalls = [s.attrs["hits"] for s in sink.spans if s.name == "remember.recall"]
        self.assertEqual(recalls[0], 0)
        self.assertGreater(recalls[1], 0)


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.v = EvidenceBackedAnswerVerifier(known_citations())
        self.pc = CaseRequestPerceiver().perceive("Where is SO-104233 for Summit Contractors?", REP)
        self.ev = [ToolResult("get_sales_order", True, {"sales_order": "SO-104233", "order_total": 8370.0,
                                                        "account_name": "Summit Contractors LLC"}),
                   ToolResult("track_shipment", True, {"shipments": [{"shipment_id": "SHP-55012"}]})]

    def test_accepts_evidence_backed_answer(self):
        ok, why = self.v.check(self.pc, "SO-104233 for Summit Contractors ($8,370.00) is on SHP-55012.", self.ev)
        self.assertTrue(ok, why)

    def test_rejects_invented_ids_of_any_length(self):
        for fake in ("SHP-99999", "SHP-550129", "INV-9038", "CASE-1"):
            ok, why = self.v.check(self.pc, f"SO-104233 for Summit Contractors is on SHP-55012, see {fake}.", self.ev)
            self.assertFalse(ok, fake)
            self.assertIn(fake, why)

    def test_a_citation_never_backs_an_order_claim(self):
        cite = "[customer-service-policy.md#0]"
        ok, why = self.v.check(self.pc, f"SO-104233 for Summit Contractors was delivered yesterday {cite}.", [])
        self.assertFalse(ok)
        self.assertIn("not backed by a tool result", why)
        ghost = CaseRequestPerceiver().perceive("Where is SO-199999?", REP)
        self.assertFalse(self.v.check(ghost, f"SO-199999 shipped on time {cite}.", [])[0])

    def test_not_found_lookup_backs_a_cannot_find_answer(self):
        ghost = CaseRequestPerceiver().perceive("Where is SO-199999?", REP)
        miss = [ToolResult("get_sales_order", False, error="KeyError: 'no sales order SO-199999'")]
        self.assertTrue(self.v.check(ghost, "I couldn't find SO-199999 in the ERP.", miss)[0])
        denied = [ToolResult("get_sales_order", False, error="denied by permission: SO-199999", denied=True)]
        self.assertFalse(self.v.check(ghost, "I couldn't find SO-199999 in the ERP.", denied)[0])

    def test_rejects_amounts_not_in_tool_results(self):
        ok, why = self.v.check(self.pc, "SO-104233 for Summit Contractors on SHP-55012: they owe $99,999.00.", self.ev)
        self.assertFalse(ok)
        self.assertIn("99999.0", why)

    def test_rejects_unaddressed_entities(self):
        ok, why = self.v.check(self.pc, "It is on SHP-55012.", self.ev)
        self.assertFalse(ok)
        self.assertIn("SO-104233", why)
        self.assertIn("Summit", why)

    def test_rejects_fake_citations_and_answers_without_evidence(self):
        policy = CaseRequestPerceiver().perceive("What is the return window?", REP)
        self.assertFalse(self.v.check(policy, "30 days [made-up-policy.md#3].", [])[0])
        self.assertFalse(self.v.check(policy, "30 days.", [])[0])
        self.assertTrue(self.v.check(policy, "30 days [customer-service-policy.md#0].", [])[0])

    def test_denied_or_skill_results_are_not_evidence(self):
        policy = CaseRequestPerceiver().perceive("What is the return window?", REP)
        results = [ToolResult("load_skill", True, {"name": "x"}), ToolResult("search_accounts", False, denied=True)]
        self.assertFalse(self.v.check(policy, "30 days.", results)[0])


class EvalTests(unittest.TestCase):
    def test_all_starter_cases_pass_with_scripted_llm(self):
        def agent_for(case):
            reset_systems()
            agent, _ = traced(scripted_llm(case.id))
            return agent
        report = run_evals(agent_for, load_cases(ROOT / "evals" / "cases.json"))
        self.assertEqual(report.success_rate, 1.0, report.to_markdown())

    def test_verifier_rejection_sends_the_model_back_for_evidence(self):
        reset_systems()
        agent, sink = traced(scripted_llm("invented-id-rejected-then-corrected"))
        out = agent.run("Has SO-104233 for Summit Contractors been delivered?", REP)
        checks = [s.attrs for s in sink.spans if s.name == "think.goal_check"]
        self.assertFalse(checks[0]["achieved"])
        self.assertIn("SHP-55013", checks[0]["reason"])
        self.assertTrue(checks[-1]["achieved"])
        self.assertNotIn("SHP-55013", out.answer)


if __name__ == "__main__":
    unittest.main()
