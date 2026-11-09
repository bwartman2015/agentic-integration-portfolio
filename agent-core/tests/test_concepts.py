"""Tests for the nine agentic AI concepts built into agent-core (no network except localhost A2A)."""
import json
import pathlib
import tempfile
import unittest

from agentcore import (A2AClient, Agent, AgentCard, AgentSkill, Checkpointer, EvalCase, FakeLLM,
                       InMemorySink, KeywordRetriever, Principal, Reasoner, ShortTermMemory, SkillLibrary,
                       StaticState, Tool, ToolRegistry, Tracer, TruncatingCompactor, agent_as_tool,
                       compare_to_baseline, parse_skill, register_tool_source, remote_agent_tool, run_evals,
                       serve_agent)

FIN = Principal("finance-agent", frozenset({"ar.read"}))


def ctx_of(llm, call=0):
    return json.loads(llm.calls[call]["messages"][0]["content"])


def invoice_tools():
    r = ToolRegistry()
    r.register(Tool("get_invoice", "Get an AR invoice", handler=lambda invoice_id: {"invoice": invoice_id, "open_amount": 1200.0},
                    params={"invoice_id": "string"}, required=["invoice_id"], scope="ar.read"))
    return r


class MemoryAndStateTests(unittest.TestCase):
    def test_state_snapshot_injected_each_iteration(self):
        calls = {"n": 0}

        class CountingState:
            def snapshot(self, percept):
                calls["n"] += 1
                return {"dlq_depth": 40 - 20 * (calls["n"] - 1)}

        llm = FakeLLM([{"type": "tool", "tool": "get_invoice", "args": {"invoice_id": "INV-1"}},
                       {"type": "answer", "answer": "done"}])
        agent = Agent(name="t", reasoner=Reasoner(llm), tools=invoice_tools(), state=CountingState())
        agent.run("check invoice INV-1", FIN)
        self.assertEqual(ctx_of(llm, 0)["current_state"], {"dlq_depth": 40})
        self.assertEqual(ctx_of(llm, 1)["current_state"], {"dlq_depth": 20})


class RagTests(unittest.TestCase):
    def test_retrieved_chunks_injected_with_citations(self):
        kb = KeywordRetriever()
        kb.add("credit-policy.md", "Customers over their credit limit go on credit hold.\n\n"
                                   "Short payments under $50 are written off automatically.")
        kb.add("shipping-sla.md", "Standard ground shipments must leave the DC within 2 business days.")
        llm = FakeLLM([{"type": "answer", "answer": "Write it off [credit-policy.md#0]"}])
        Agent(name="t", reasoner=Reasoner(llm), tools=invoice_tools(), retriever=kb).run(
            "customer short payment of $30, write off or dispute?", FIN)
        cites = [c["cite"] for c in ctx_of(llm)["retrieved_context"]]
        self.assertEqual(cites[0], "credit-policy.md#0")


class SkillsTests(unittest.TestCase):
    SKILL = """---
name: short-pay-resolution
description: Resolve a customer short payment against an invoice
trigger: short pay, short payment, underpaid
---
# Steps
- Read the invoice with get_invoice
- Compare remittance to open amount
"""

    def test_trigger_injects_skill_and_load_skill_tool_registered(self):
        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d) / "short-pay-resolution").mkdir()
            (pathlib.Path(d) / "short-pay-resolution" / "SKILL.md").write_text(self.SKILL)
            lib = SkillLibrary.from_dir(d)
            llm = FakeLLM([{"type": "tool", "tool": "load_skill", "args": {"name": "short-pay-resolution"}},
                           {"type": "answer", "answer": "following the skill"}])
            agent = Agent(name="t", reasoner=Reasoner(llm), tools=invoice_tools(), skills=lib)
            out = agent.run("Acme sent a short payment on INV-9", FIN)
            ctx = ctx_of(llm)
            self.assertEqual(ctx["active_skills"][0]["name"], "short-pay-resolution")
            self.assertIn("load_skill", [t["name"] for t in ctx["tool_catalog"]])
            self.assertIn("load_skill", out.tools_used)

    def test_bad_skill_rejected(self):
        with self.assertRaises(ValueError):
            parse_skill("no frontmatter here")

    def test_agent_written_skill_is_a_draft_behind_approval(self):
        with tempfile.TemporaryDirectory() as d:
            lib = SkillLibrary([], drafts_dir=str(pathlib.Path(d) / "_drafts"))
            tool = lib.write_draft_tool()
            self.assertEqual(tool.side_effect, "write")
            res = tool.handler(name="freight-claim", description="File a freight claim", trigger="damaged", body="- steps")
            self.assertIn("_drafts", res["draft"])


class HarnessTests(unittest.TestCase):
    def test_compaction_bounds_context(self):
        stm = ShortTermMemory()
        for i in range(20):
            stm.add("user", f"Result of tool{i}: " + "x" * 1000)
        self.assertTrue(TruncatingCompactor(max_chars=5000, keep_last=4).compact(stm))
        self.assertEqual(len(stm.messages), 6)
        self.assertIn("compacted", stm.messages[1]["content"])

    def test_progress_file_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            llm = FakeLLM([{"type": "tool", "tool": "get_invoice", "args": {"invoice_id": "INV-2"}},
                           {"type": "answer", "answer": "INV-2 open 1200"}])
            out = Agent(name="t", reasoner=Reasoner(llm), tools=invoice_tools(), checkpointer=Checkpointer(d)).run("INV-2?", FIN)
            saved = Checkpointer(d).load(out.trace_id)
            self.assertEqual(saved["status"], "answered")
            self.assertEqual(saved["tool_results"][0]["tool"], "get_invoice")


class EvalsTests(unittest.TestCase):
    def test_eval_scores_and_baseline(self):
        script = {"ok": [{"type": "tool", "tool": "get_invoice", "args": {"invoice_id": "INV-3"}},
                         {"type": "answer", "answer": "INV-3 has 1200 open"}],
                  "bad": [{"type": "answer", "answer": "no idea"}]}
        cases = [EvalCase("ok", "open amount on INV-3?", ["ar.read"], expect_tools=["get_invoice"], expect_answer_contains=["1200"]),
                 EvalCase("bad", "open amount on INV-4?", ["ar.read"], expect_tools=["get_invoice"])]
        rep = run_evals(lambda c: Agent(name="t", reasoner=Reasoner(FakeLLM(script[c.id])), tools=invoice_tools()), cases, "finance")
        self.assertEqual(rep.success_rate, 0.5)
        self.assertIn("missing tools", rep.results[1].failures[0])
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d) / "baseline.json"
            base.write_text(json.dumps({"summary": {"success_rate": 1.0}}))
            ok, msg = compare_to_baseline(rep, base)
            self.assertFalse(ok)
            self.assertIn("regression", msg)
            # tool-selection accuracy is guarded too, not only the success rate
            base.write_text(json.dumps({"summary": {"success_rate": 0.5, "tool_selection_accuracy": 1.0}}))
            ok, msg = compare_to_baseline(rep, base)
            self.assertFalse(ok)
            self.assertIn("tool selection", msg)


class McpToolSourceTests(unittest.TestCase):
    def test_server_tools_registered_and_governed_by_client(self):
        class FakeMcpServer:
            def list_tools(self):
                return [{"name": "get_shipment", "description": "Track a shipment",
                         "inputSchema": {"type": "object", "properties": {"tracking_no": {"type": "string"}}, "required": ["tracking_no"]}},
                        {"name": "cancel_shipment", "description": "Cancel", "inputSchema": {"type": "object", "properties": {"tracking_no": {"type": "string"}}}}]

            def call_tool(self, name, arguments):
                return {"tool": name, "status": "In transit", **arguments}

        reg = ToolRegistry()
        names = register_tool_source(reg, FakeMcpServer(), scope_for=lambda n: "shipping.read" if n.startswith("get") else "shipping.write",
                                     side_effect_for=lambda n: "read" if n.startswith("get") else "write", prefix="tms.")
        self.assertEqual(names, ["tms.get_shipment", "tms.cancel_shipment"])
        self.assertEqual(reg.get("tms.cancel_shipment").side_effect, "write")
        self.assertEqual(reg.get("tms.get_shipment").handler(tracking_no="1Z9")["status"], "In transit")


class OrchestrationTests(unittest.TestCase):
    def test_manager_delegates_to_micro_agent(self):
        finance = Agent(name="finance", reasoner=Reasoner(FakeLLM([
            {"type": "tool", "tool": "get_invoice", "args": {"invoice_id": "INV-5"}},
            {"type": "answer", "answer": "INV-5 has 1200 open"}])), tools=invoice_tools())
        mgr_tools = ToolRegistry()
        mgr_tools.register(agent_as_tool(finance, name="ask_finance", description="AR specialist", principal=FIN, scope="delegate.finance"))
        sink = InMemorySink()
        mgr = Agent(name="manager", reasoner=Reasoner(FakeLLM([
            {"type": "tool", "tool": "ask_finance", "args": {"task": "open amount on INV-5"}},
            {"type": "answer", "answer": "Finance reports INV-5 has 1200 open"}])), tools=mgr_tools, tracer=Tracer(sink))
        out = mgr.run("why is Acme on credit hold?", Principal("ops-lead", frozenset({"delegate.finance"})))
        self.assertEqual(out.tools_used, ["ask_finance"])
        self.assertTrue(any(s.name == "act.ask_finance" for s in sink.spans))


class A2ATests(unittest.TestCase):
    def test_card_discovery_and_task_roundtrip_over_http(self):
        shipping = Agent(name="shipping", reasoner=Reasoner(FakeLLM([{"type": "answer", "answer": "PO 7781 shipped partial, 2 of 3 cartons"}])),
                         tools=ToolRegistry())
        card = AgentCard("Shipping Agent", "Tracks shipments and delivery exceptions", url="",
                         skills=[AgentSkill("track", "Track shipment", "Status and exceptions for a PO or tracking number")])
        server = serve_agent(shipping, card, Principal("shipping-agent", frozenset()), bearer_token="s3cret")
        try:
            client = A2AClient(card.url, bearer_token="s3cret")
            self.assertEqual(client.card().skills[0].id, "track")
            tool = remote_agent_tool(client, scope="delegate.shipping")
            res = tool.handler(task="status of PO 7781")
            self.assertEqual(res["state"], "completed")
            self.assertIn("partial", res["answer"])
            with self.assertRaises(Exception):
                A2AClient(card.url).send("no token")
        finally:
            server.shutdown()


if __name__ == "__main__":
    unittest.main()
