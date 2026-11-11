"""Behavioural tests for the seven-layer loop, using a scripted FakeLLM (no network)."""
import unittest

from agentcore import (Agent, FakeLLM, Guardrails, InMemorySink, LLMPlanner, Principal, Reasoner,
                       SQLiteMemory, Tool, ToolRegistry, Tracer)

ANALYST = Principal("analyst-agent", frozenset({"erp.read"}))
OPS = Principal("ops-agent", frozenset({"erp.read", "crm.write"}))


def registry(log=None):
    log = log if log is not None else []
    r = ToolRegistry()
    r.register(Tool("get_sales_order", "Get an SAP sales order by id",
                    handler=lambda order_id: {"order_id": order_id, "status": "Open", "contact": "jo@example.com"},
                    params={"order_id": "string"}, required=["order_id"], scope="erp.read"))
    r.register(Tool("create_task", "Create a CRM follow-up task",
                    handler=lambda subject: log.append(subject) or {"created": True},
                    params={"subject": "string"}, required=["subject"], scope="crm.write", side_effect="write"))
    return r


def make(llm, tools=None, guardrails=None, memory=None, planner=False, verifier=None):
    sink = InMemorySink()
    kw = dict(name="test", reasoner=Reasoner(llm), tools=tools or registry(),
              guardrails=guardrails or Guardrails(), memory=memory or SQLiteMemory(),
              tracer=Tracer(sink))
    if planner:
        kw["planner"] = LLMPlanner(llm)
    if verifier:
        kw["verifier"] = verifier
    return Agent(**kw), sink


class AgentLoopTests(unittest.TestCase):
    def test_answers_directly(self):
        agent, sink = make(FakeLLM([{"type": "answer", "answer": "Hello"}]))
        out = agent.run("say hello", ANALYST)
        self.assertEqual(out.status, "answered")
        self.assertTrue(out.answered_directly)
        self.assertFalse(any(s.layer == "act" for s in sink.spans))

    def test_tool_then_answer_traces_every_layer(self):
        llm = FakeLLM([{"type": "tool", "tool": "get_sales_order", "args": {"order_id": "42"}},
                       {"type": "answer", "answer": "Order 42 is Open; contact jo@example.com"}])
        agent, sink = make(llm)
        out = agent.run("status of order 42?", ANALYST)
        self.assertEqual(out.status, "answered")
        self.assertEqual(out.tool_calls, 1)
        self.assertIn("[EMAIL REDACTED]", out.answer)          # output guardrail
        layers = {s.layer for s in sink.spans}
        self.assertTrue({"perceive", "think", "remember", "act", "guardrails"} <= layers)
        self.assertGreater(out.cost_usd, 0)                     # cost tracked
        self.assertIn("Open", llm.calls[1]["messages"][-1]["content"])  # tool result fed back to THINK

    def test_scope_denial_is_fed_back_and_tool_not_run(self):
        log = []
        llm = FakeLLM([{"type": "tool", "tool": "create_task", "args": {"subject": "follow up"}},
                       {"type": "answer", "answer": "I am not permitted to create tasks."}])
        agent, _ = make(llm, tools=registry(log))
        out = agent.run("create a follow-up task", ANALYST)
        self.assertEqual(log, [])
        self.assertIn("denied by permission", llm.calls[1]["messages"][-1]["content"])
        self.assertEqual(out.status, "answered")

    def test_write_requires_approval(self):
        log = []
        llm = FakeLLM([{"type": "tool", "tool": "create_task", "args": {"subject": "x"}},
                       {"type": "answer", "answer": "done"}])
        agent, _ = make(llm, tools=registry(log), guardrails=Guardrails(approval=lambda p, t, a: False))
        agent.run("create task", OPS)
        self.assertEqual(log, [])
        llm2 = FakeLLM([{"type": "tool", "tool": "create_task", "args": {"subject": "x"}},
                        {"type": "answer", "answer": "done"}])
        agent2, _ = make(llm2, tools=registry(log), guardrails=Guardrails(approval=lambda p, t, a: True))
        agent2.run("create task", OPS)
        self.assertEqual(log, ["x"])

    def test_iteration_cap_escalates(self):
        loop = [{"type": "tool", "tool": "get_sales_order", "args": {"order_id": "1"}}] * 10
        agent, sink = make(FakeLLM(loop), guardrails=Guardrails(max_iterations=3))
        out = agent.run("loop forever", ANALYST)
        self.assertEqual(out.status, "escalated")
        self.assertEqual(out.iterations, 3)
        self.assertTrue(any(s.name == "guardrails.escalate" for s in sink.spans))

    def test_escalation_message_is_pii_masked(self):
        llm = FakeLLM([{"type": "escalate", "reasoning": "customer jo@example.com wants a refund"}])
        agent, _ = make(llm)
        out = agent.run("refund please", ANALYST)
        self.assertEqual(out.status, "escalated")
        self.assertNotIn("jo@example.com", out.answer)
        self.assertIn("[EMAIL REDACTED]", out.answer)

    def test_mask_covers_house_phone_formats_and_leaves_other_numbers(self):
        g = Guardrails()
        for phone in ("555-0142", "555-555-0142", "555.555.0142", "(555) 555-0142", "+1 555 555 0142"):
            self.assertEqual(g.mask(f"call {phone} today"), "call [PHONE REDACTED] today", phone)
        for text in ("PRO 123.4567", "weight 120 1500", "1Z 999 1234", "SO-104233", "INV-90412",
                     "2026-11-05", "5550142", "ACCT-1002", "$8,370.00"):
            self.assertEqual(g.mask(text), text)

    def test_mask_keeps_sentence_punctuation(self):
        self.assertEqual(Guardrails().mask("Write to jo@example.com."), "Write to [EMAIL REDACTED].")

    def test_goal_check_forces_another_iteration(self):
        class NeedsTool:
            def check(self, percept, answer, results):
                return (bool(results), "must look up the order first")
        llm = FakeLLM([{"type": "answer", "answer": "probably open"},
                       {"type": "tool", "tool": "get_sales_order", "args": {"order_id": "7"}},
                       {"type": "answer", "answer": "Order 7 is Open"}])
        agent, _ = make(llm, verifier=NeedsTool())
        out = agent.run("status of 7", ANALYST)
        self.assertEqual(out.answer, "Order 7 is Open")
        self.assertEqual(out.iterations, 3)

    def test_long_term_memory_recalled_on_next_run(self):
        mem = SQLiteMemory()
        a1, _ = make(FakeLLM([{"type": "answer", "answer": "Acme is account 0012"}]), memory=mem)
        a1.run("what is the account id for Acme", ANALYST)
        llm = FakeLLM([{"type": "answer", "answer": "0012"}])
        a2, _ = make(llm, memory=mem)
        a2.run("Acme account id?", ANALYST)
        self.assertIn("0012", llm.calls[0]["messages"][0]["content"])

    def test_plan_executes_steps_in_order(self):
        llm = FakeLLM([{"type": "plan"},
                       {"goal": "g", "steps": [
                           {"description": "a", "tool": "get_sales_order", "args": {"order_id": "1"}},
                           {"description": "b", "tool": "get_sales_order", "args": {"order_id": "2"}}]},
                       {"type": "answer", "answer": "both open"}])
        agent, sink = make(llm, planner=True)
        out = agent.run("compare orders 1 and 2", ANALYST)
        self.assertEqual(out.tool_calls, 2)
        self.assertTrue(any(s.name == "plan.make" for s in sink.spans))

    def test_bad_args_reported_not_crashed(self):
        llm = FakeLLM([{"type": "tool", "tool": "get_sales_order", "args": {}},
                       {"type": "answer", "answer": "need an order id"}])
        agent, _ = make(llm)
        out = agent.run("status?", ANALYST)
        self.assertEqual(out.status, "answered")
        self.assertIn("missing required args", llm.calls[1]["messages"][-1]["content"])

    def test_catalog_hides_unpermitted_tools(self):
        self.assertEqual([t["name"] for t in registry().catalog(ANALYST)], ["get_sales_order"])


if __name__ == "__main__":
    unittest.main()
