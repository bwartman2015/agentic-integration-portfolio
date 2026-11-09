"""Offline demo of agent-core with a scripted FakeLLM: seven layers + the agentic concepts.

Scenario (fictional Ridgeline Supply Co.): a customer short-paid an invoice; the agent uses
STATE, RAG (policy), a SKILL, PLAN + tools, GUARDRAILS (PII masking), a progress file, and traces.
Run:  make demo   (or from agent-core/: PYTHONPATH=. python3 examples/demo.py)
"""
import tempfile

from agentcore import (Agent, Checkpointer, FakeLLM, Guardrails, InMemorySink, KeywordRetriever, LLMPlanner,
                       Principal, Reasoner, Skill, SkillLibrary, SQLiteMemory, StaticState, Tool, ToolRegistry, Tracer)

tools = ToolRegistry()
tools.register(Tool("get_remittance", "Remittance advice for a payment",
                    handler=lambda remittance_id: {"remittance": remittance_id, "invoice": "INV-90412", "paid": 15140.0,
                                                   "deduction_code": "SHORT", "payer_contact": "ap@bigboxoutdoor.example"},
                    params={"remittance_id": "string"}, required=["remittance_id"], scope="ar.read"))
tools.register(Tool("get_invoice", "AR invoice",
                    handler=lambda invoice_id: {"invoice": invoice_id, "amount": 20000.0, "sales_order": "SO-104290"},
                    params={"invoice_id": "string"}, required=["invoice_id"], scope="ar.read"))

kb = KeywordRetriever()
kb.add("ar-deduction-policy.md", "SHORT deductions must be validated against proof of delivery before acceptance.")
skills = SkillLibrary([Skill("short-pay-resolution", "Explain and apply a customer short payment",
                             "1. get_remittance 2. get_invoice 3. validate with POD 4. propose application",
                             trigger=["short-paid", "short-pay", "short pay"])])

llm = FakeLLM([
    {"type": "plan", "reasoning": "Need remittance and invoice before explaining the deduction."},
    {"goal": "Explain the short payment", "steps": [
        {"description": "read remittance", "tool": "get_remittance", "args": {"remittance_id": "RM-22031"}},
        {"description": "read invoice", "tool": "get_invoice", "args": {"invoice_id": "INV-90412"}}]},
    {"type": "answer", "answer": "Big Box Outdoor paid $15,140 of $20,000 on INV-90412: a $4,860 SHORT deduction. "
                                 "Per policy it must be validated against proof of delivery [ar-deduction-policy.md#0]. "
                                 "Contact: ap@bigboxoutdoor.example"},
])

sink = InMemorySink()
agent = Agent(name="ar-demo", reasoner=Reasoner(llm), planner=LLMPlanner(llm), tools=tools, memory=SQLiteMemory(),
              state=StaticState({"period": "open", "unapplied_cash_usd": 41200}), retriever=kb, skills=skills,
              checkpointer=Checkpointer(tempfile.mkdtemp()), guardrails=Guardrails(max_iterations=4), tracer=Tracer(sink))

out = agent.run("Why did Big Box Outdoor short-pay remittance RM-22031?", Principal("ar-analyst", frozenset({"ar.read"})))
print(f"status={out.status} iterations={out.iterations} tools={out.tools_used} cost=${out.cost_usd:.4f}")
print("answer:", out.answer)
print("\ntrace", out.trace_id)
for s in sink.spans:
    attrs = {k: v for k, v in s.attrs.items() if k not in ("reasoning",)}
    print(f"  {s.layer:<10} {s.name:<24} {attrs}")
