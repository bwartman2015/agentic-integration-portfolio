"""Tests for the nine agentic AI concepts built into agent-core (no network except localhost A2A)."""
import copy
import dataclasses
import json
import pathlib
import pickle
import tempfile
import unittest

from agentcore import (A2AClient, Agent, AgentCard, AgentSkill, Checkpointer, EvalCase, FakeLLM,
                       InMemorySink, JSONLSink, KeywordRetriever, Principal, Reasoner, ShortTermMemory, SkillLibrary,
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

    def test_progress_file_and_trace_are_redacted_at_rest_when_a_redactor_is_set(self):
        mask = lambda text: text.replace("jo@example.com", "[EMAIL REDACTED]")
        tools = ToolRegistry()
        tools.register(Tool("get_contact", "Contact for an account", handler=lambda account: {"email": "jo@example.com", "n": 3},
                            params={"account": "string"}, required=["account"], scope="ar.read"))
        script = [{"type": "tool", "tool": "get_contact", "args": {"account": "A1"}, "reasoning": "ask jo@example.com"},
                  {"type": "answer", "answer": "done"}]
        with tempfile.TemporaryDirectory() as d:
            trace = pathlib.Path(d) / "traces.jsonl"
            agent = Agent(name="t", reasoner=Reasoner(FakeLLM(copy.deepcopy(script))), tools=tools,
                          checkpointer=Checkpointer(d, redact=mask), tracer=Tracer(JSONLSink(str(trace), redact=mask)))
            out = agent.run("who is the contact jo@example.com?", FIN)
            progress = (pathlib.Path(d) / f"{out.trace_id}.json").read_text()
            for text in (progress, trace.read_text()):
                self.assertNotIn("jo@example.com", text)
                self.assertIn("[EMAIL REDACTED]", text)
            saved = Checkpointer(d).load(out.trace_id)                 # still valid JSON, structure intact
            self.assertEqual((saved["status"], saved["tool_results"][0]["tool"]), ("answered", "get_contact"))
            self.assertTrue(all(json.loads(line)["trace_id"] == out.trace_id for line in trace.read_text().splitlines()))
            # Without a redactor nothing changes (other projects keep their behaviour).
            plain = Agent(name="t", reasoner=Reasoner(FakeLLM(copy.deepcopy(script))), tools=tools,
                          checkpointer=Checkpointer(d)).run("who is the contact jo@example.com?", FIN)
            self.assertIn("jo@example.com", (pathlib.Path(d) / f"{plain.trace_id}.json").read_text())


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


class PrincipalAttributeTests(unittest.TestCase):
    """Attributes carry verified, non-scope claims (for example the regions a caller may see)."""

    def test_defaults_to_no_attributes_and_stays_hashable(self):
        p = Principal("rep", frozenset({"crm.read"}))
        self.assertEqual(p.attributes, {})
        self.assertEqual(p.attr("regions"), ())
        self.assertEqual(p, Principal("rep", frozenset({"crm.read"})))
        self.assertEqual(hash(p), hash(Principal("rep", frozenset({"crm.read"}), {"regions": ["West"]})))

    def test_values_are_normalised_to_tuples_and_cannot_be_changed(self):
        p = Principal("rep", attributes={"regions": ["Central", "East"], "desk": "inside-sales"})
        self.assertEqual(p.attr("regions"), ("Central", "East"))
        self.assertEqual(p.attr("desk"), ("inside-sales",))
        self.assertNotEqual(p, Principal("rep"))
        with self.assertRaises(TypeError):
            p.attributes["regions"] = ("West",)

    def test_principal_can_be_copied_pickled_and_serialised(self):
        p = Principal("rep", frozenset({"crm.read"}), {"regions": ["Central", "East"]})
        for clone in (copy.deepcopy(p), copy.copy(p), pickle.loads(pickle.dumps(p))):
            self.assertEqual(clone, p)
            self.assertEqual(clone.attr("regions"), ("Central", "East"))
            with self.assertRaises(TypeError):
                clone.attributes["regions"] = ("West",)
        self.assertEqual(dataclasses.asdict(p)["attributes"], {"regions": ("Central", "East")})
        with self.assertRaises(TypeError):
            p.attributes.update(regions=("West",))
        with self.assertRaises(TypeError):
            del p.attributes["regions"]

    def test_policy_hook_and_eval_cases_see_attributes(self):
        seen = []

        def policy(principal, tool, args):
            seen.append(principal.attr("regions"))
            return "West" in principal.attr("regions"), "region rule"

        from agentcore import Guardrails
        script = [{"type": "tool", "tool": "get_invoice", "args": {"invoice_id": "INV-3"}},
                  {"type": "answer", "answer": "INV-3 has 1200 open"}]
        cases = [EvalCase("in", "open amount on INV-3?", ["ar.read"], attributes={"regions": ["West"]},
                          expect_tools=["get_invoice"]),
                 EvalCase("out", "open amount on INV-3?", ["ar.read"], attributes={"regions": ["East"]},
                          forbid_tools=["get_invoice"])]
        rep = run_evals(lambda c: Agent(name="t", reasoner=Reasoner(FakeLLM(list(script))), tools=invoice_tools(),
                                        guardrails=Guardrails(policy=policy)), cases, "finance")
        self.assertEqual(seen, [("West",), ("East",)])
        self.assertEqual(rep.success_rate, 1.0, rep.to_markdown())


class RunContextTests(unittest.TestCase):
    """Tools can learn which agent run called them (for downstream audit logs), and a project can act on
    an answered run (for example, learn aliases from tool results)."""

    def test_tools_see_the_trace_id_of_the_run_and_nothing_outside_it(self):
        from agentcore import current_trace_id
        seen = []
        tools = ToolRegistry()
        tools.register(Tool("whoami", "record the trace id", handler=lambda: seen.append(current_trace_id()) or {"ok": 1},
                            scope="ar.read"))
        llm = FakeLLM([{"type": "tool", "tool": "whoami", "args": {}}, {"type": "answer", "answer": "done"}])
        out = Agent(name="t", reasoner=Reasoner(llm), tools=tools).run("go", FIN)
        self.assertEqual(seen, [out.trace_id])
        self.assertIsNone(current_trace_id())

    def test_on_answer_sees_the_percept_outcome_and_tool_results(self):
        got = []
        llm = FakeLLM([{"type": "tool", "tool": "get_invoice", "args": {"invoice_id": "INV-3"}},
                       {"type": "answer", "answer": "INV-3 has 1200 open"}])
        out = Agent(name="t", reasoner=Reasoner(llm), tools=invoice_tools(),
                    on_answer=lambda p, o, r: got.append((p.content, o.status, [x.tool for x in r]))).run("INV-3?", FIN)
        self.assertEqual(got, [("INV-3?", "answered", ["get_invoice"])])
        self.assertEqual(out.status, "answered")

    def test_a_failing_on_answer_hook_does_not_lose_the_answer(self):
        def boom(*_a):
            raise RuntimeError("hook failed")
        llm = FakeLLM([{"type": "answer", "answer": "fine"}])
        out = Agent(name="t", reasoner=Reasoner(llm), tools=invoice_tools(), on_answer=boom).run("q?", FIN)
        self.assertEqual((out.status, out.answer), ("answered", "fine"))


class MemoryAudienceTests(unittest.TestCase):
    """Long-term memory must not carry one caller's answers into another caller's prompt."""

    @staticmethod
    def agent(memory, script, audience=None):
        return Agent(name="t", reasoner=Reasoner(FakeLLM(script)), tools=invoice_tools(), memory=memory,
                     memory_audience=audience)

    def test_episodes_are_recalled_only_for_the_same_audience(self):
        from agentcore import SQLiteMemory
        memory = SQLiteMemory()
        audience = lambda p: ",".join(p.attr("regions"))          # noqa: E731
        lead = Principal("lead", frozenset({"ar.read"}), {"regions": ["*"]})
        rep = Principal("rep", frozenset({"ar.read"}), {"regions": ["Central"]})
        other_rep = Principal("rep-2", frozenset({"ar.read"}), {"regions": ["Central"]})
        question = "open amount on invoice INV-3?"
        self.agent(memory, [{"type": "answer", "answer": "INV-3 has 1200 open (West account)"}], audience).run(question, lead)

        llm = FakeLLM([{"type": "answer", "answer": "I can't see that invoice"}])
        rep_agent = Agent(name="t", reasoner=Reasoner(llm), tools=invoice_tools(), memory=memory, memory_audience=audience)
        rep_agent.run(question, rep)
        self.assertEqual(ctx_of(llm)["long_term_memory"], [])

        llm2 = FakeLLM([{"type": "answer", "answer": "same as before"}])
        Agent(name="t", reasoner=Reasoner(llm2), tools=invoice_tools(), memory=memory, memory_audience=audience).run(question, other_rep)
        recalled = ctx_of(llm2)["long_term_memory"]
        self.assertEqual(len(recalled), 1)
        self.assertIn("I can't see that invoice", recalled[0])
        self.assertNotIn("1200", json.dumps(recalled))

    def test_items_without_an_audience_are_not_recalled_once_audiences_are_on(self):
        from agentcore import MemoryItem, SQLiteMemory
        memory = SQLiteMemory()
        memory.remember(MemoryItem("Q: open amount on invoice INV-3?\nA: 1200 open"))
        llm = FakeLLM([{"type": "answer", "answer": "ok"}])
        Agent(name="t", reasoner=Reasoner(llm), tools=invoice_tools(), memory=memory,
              memory_audience=lambda p: p.id).run("open amount on invoice INV-3?", FIN)
        self.assertEqual(ctx_of(llm)["long_term_memory"], [])

    def test_a_crowd_of_other_audiences_does_not_hide_the_callers_own_memory(self):
        from agentcore import MemoryItem, SQLiteMemory
        memory = SQLiteMemory()
        for i in range(60):
            memory.remember(MemoryItem("Q: open amount on invoice INV-3?\nA: other", meta={"audience": f"x{i}"}))
        memory.remember(MemoryItem("Q: open amount on invoice INV-3?\nA: mine", meta={"audience": FIN.id}))
        llm = FakeLLM([{"type": "answer", "answer": "ok"}])
        Agent(name="t", reasoner=Reasoner(llm), tools=invoice_tools(), memory=memory,
              memory_audience=lambda p: p.id).run("open amount on invoice INV-3?", FIN)
        self.assertEqual(ctx_of(llm)["long_term_memory"], ["Q: open amount on invoice INV-3?\nA: mine"])

    def test_a_store_that_filters_by_audience_gets_the_audience_in_the_query(self):
        """Stores like pgvector filter in SQL instead of the loop over-fetching and discarding."""
        from agentcore import MemoryItem

        class FilteringStore:
            filters_audience = True

            def __init__(self):
                self.calls, self.items = [], []

            def recall(self, query, k=3, audience=None):
                self.calls.append((query, k, audience))
                return [i for i in self.items if i.meta.get("audience") == audience][:k]

            def remember(self, item):
                self.items.append(item)

        store = FilteringStore()
        store.items.append(MemoryItem("Q: x\nA: theirs", meta={"audience": "other"}))
        store.items.append(MemoryItem("Q: x\nA: mine", meta={"audience": FIN.id}))
        llm = FakeLLM([{"type": "answer", "answer": "ok"}])
        Agent(name="t", reasoner=Reasoner(llm), tools=invoice_tools(), memory=store,
              memory_audience=lambda p: p.id).run("open amount on invoice INV-3?", FIN)
        self.assertEqual(store.calls, [("open amount on invoice INV-3?", 3, FIN.id)])
        self.assertEqual(ctx_of(llm)["long_term_memory"], ["Q: x\nA: mine"])

    def test_sqlite_memory_filters_by_audience_itself(self):
        from agentcore import MemoryItem, SQLiteMemory
        memory = SQLiteMemory()
        self.assertTrue(memory.filters_audience)
        memory.remember(MemoryItem("Q: invoice INV-3\nA: theirs", meta={"audience": "other"}))
        memory.remember(MemoryItem("Q: invoice INV-3\nA: mine", meta={"audience": "me"}))
        memory.remember(MemoryItem("Q: invoice INV-3\nA: untagged"))
        self.assertEqual([m.text for m in memory.recall("invoice INV-3", 5, audience="me")], ["Q: invoice INV-3\nA: mine"])
        self.assertEqual(len(memory.recall("invoice INV-3", 5)), 3)

    def test_without_an_audience_function_memory_is_shared_as_before(self):
        from agentcore import SQLiteMemory
        memory = SQLiteMemory()
        self.agent(memory, [{"type": "answer", "answer": "INV-3 has 1200 open"}]).run("open amount on invoice INV-3?", FIN)
        llm = FakeLLM([{"type": "answer", "answer": "ok"}])
        Agent(name="t", reasoner=Reasoner(llm), tools=invoice_tools(), memory=memory).run("open amount on invoice INV-3?", FIN)
        self.assertEqual(len(ctx_of(llm)["long_term_memory"]), 1)


if __name__ == "__main__":
    unittest.main()
