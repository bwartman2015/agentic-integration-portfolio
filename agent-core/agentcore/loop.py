"""The agent loop (the HARNESS) that wires the seven layers and the agentic concepts together.

    Input -> PERCEIVE -> input guardrails
          -> REMEMBER: recall memory, RAG retrieve, select skills          (context injection)
          -> loop:  STATE snapshot -> compaction -> THINK (can I answer directly?)
                      answer -> goal check -> output guardrails -> write memory -> Output
                      plan / tool -> GUARDRAILS.authorize -> ACT -> results back to THINK
                    checkpoint progress file every iteration                (persist)
                    budgets exceeded -> escalate to a human
    OBSERVE wraps every step.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .act import Executor, ToolRegistry
from .guardrails import Guardrails, GuardrailViolation
from .harness import Checkpointer, Compactor, TruncatingCompactor, build_context
from .memory import LongTermMemory, MemoryItem, SQLiteMemory, ShortTermMemory
from .observe import Tracer
from .perceive import Perceiver, TextPerceiver
from .plan import Planner
from .rag import Retriever
from .reason import Reasoner
from .skills import SkillLibrary
from .state import StateProvider
from .types import Outcome, Percept, Principal, Step, ToolResult


class GoalVerifier(Protocol):
    """'Goal achieved?' Projects implement deterministic checks where possible
    (tests pass, metrics back to baseline, order dispositioned)."""
    def check(self, percept: Percept, answer: str, results: list[ToolResult]) -> tuple[bool, str]: ...


class AcceptNonEmpty:
    def check(self, percept, answer, results):
        return (bool(answer and answer.strip()), "empty answer")


@dataclass
class Agent:
    name: str
    reasoner: Reasoner
    tools: ToolRegistry
    perceiver: Perceiver = field(default_factory=TextPerceiver)
    memory: LongTermMemory = field(default_factory=SQLiteMemory)
    planner: Planner | None = None
    guardrails: Guardrails = field(default_factory=Guardrails)
    verifier: GoalVerifier = field(default_factory=AcceptNonEmpty)
    tracer: Tracer = field(default_factory=Tracer)
    executor: Executor = field(default_factory=Executor)
    state: StateProvider | None = None              # concept 1: current system snapshot
    retriever: Retriever | None = None              # concept 3: RAG
    skills: SkillLibrary | None = None              # concept 7: reusable capabilities
    compactor: Compactor = field(default_factory=TruncatingCompactor)   # concept 4: control
    checkpointer: Checkpointer | None = None        # concept 4: persist
    rag_k: int = 4
    on_escalate: Callable[[Outcome], None] | None = None
    # Who may recall what this run remembers. Set it whenever answers depend on what the caller is
    # allowed to see: an episode is tagged with the caller's audience key and recalled only for callers
    # with the same key, so memory cannot carry one caller's data into another caller's prompt.
    # None = one shared memory (every caller sees the same data).
    memory_audience: Callable[[Principal], str] | None = None

    def __post_init__(self) -> None:
        if self.skills and self.skills.skills and self.tools.get("load_skill") is None:
            self.tools.register(self.skills.load_skill_tool())

    def run(self, raw: Any, principal: Principal) -> Outcome:
        cost_start = self.tracer.cost_usd
        percept = self.perceiver.perceive(raw, principal)
        trace = percept.trace_id
        with self.tracer.span(trace, "agent.run", agent=self.name, principal=principal.id):
            with self.tracer.span(trace, "perceive.parse", kind=percept.kind, source=percept.source):
                pass
            try:
                with self.tracer.span(trace, "guardrails.input"):
                    self.guardrails.check_input(percept.content)
            except GuardrailViolation as v:
                return Outcome("rejected", f"Input rejected ({v.rule}).", 0, 0, 0.0, trace)

            # ---- context injection: memory, RAG, skills ----
            with self.tracer.span(trace, "remember.recall") as s:
                recalled = [m.text for m in self._recall(percept)]
                s.attrs["hits"] = len(recalled)
            chunks = []
            if self.retriever:
                with self.tracer.span(trace, "remember.retrieve") as s:
                    chunks = self.retriever.retrieve(percept.content, k=self.rag_k)
                    s.attrs["cites"] = [c.cite for c in chunks]
            selected, skills_catalog = [], []
            if self.skills:
                with self.tracer.span(trace, "harness.skills") as s:
                    skills_catalog = self.skills.catalog()
                    selected = self.skills.select(percept.content)
                    s.attrs["active"] = [sk.name for sk in selected]

            stm = ShortTermMemory()
            results: list[ToolResult] = []
            iterations = tool_calls = 0
            catalog = self.tools.catalog(principal)

            def finish(outcome: Outcome) -> Outcome:
                outcome.tools_used = [r.tool for r in results if r.ok]
                self._checkpoint(trace, percept, iterations, outcome.status, stm, results)
                return outcome

            while True:
                cost = self.tracer.cost_usd - cost_start
                try:
                    self.guardrails.check_budget(iterations, tool_calls, cost)
                except GuardrailViolation as v:
                    return finish(self._escalate(trace, f"Budget guardrail hit ({v.rule}: {v.detail}).",
                                                 iterations, tool_calls, cost, stm))
                iterations += 1

                state: dict[str, Any] = {}
                if self.state:
                    with self.tracer.span(trace, "remember.state") as s:
                        state = self.state.snapshot(percept)
                        s.attrs["keys"] = sorted(state)
                if self.compactor.compact(stm):
                    with self.tracer.span(trace, "harness.compact", messages=len(stm.messages)):
                        pass

                context = build_context(percept, state=state, recalled=recalled, chunks=chunks,
                                        skills_catalog=skills_catalog, selected_skills=selected,
                                        tool_catalog=catalog)
                with self.tracer.span(trace, "think.decide", iteration=iterations) as s:
                    decision = self.reasoner.decide(context, stm.messages)
                    self.tracer.record_llm(s, self.reasoner.last_response)
                    s.attrs.update(decision=decision.kind, reasoning=decision.reasoning[:300])

                if decision.kind == "escalate":
                    return finish(self._escalate(trace, f"Agent escalated: {decision.reasoning}", iterations,
                                                 tool_calls, self.tracer.cost_usd - cost_start, stm))

                if decision.kind == "answer":
                    answer = decision.answer or ""
                    with self.tracer.span(trace, "think.goal_check") as s:
                        ok, why = self.verifier.check(percept, answer, results)
                        s.attrs.update(achieved=ok, reason="" if ok else why)
                    if ok:
                        with self.tracer.span(trace, "guardrails.output"):
                            safe = self.guardrails.mask(answer)
                        with self.tracer.span(trace, "remember.write"):
                            self.memory.remember(MemoryItem(
                                text=f"Q: {percept.content}\nA: {safe}", kind="episode",
                                meta={"tools": [r.tool for r in results if r.ok], "trace_id": trace,
                                      **({"audience": self.memory_audience(principal)}
                                         if self.memory_audience else {})}))
                        return finish(Outcome("answered", safe, iterations, tool_calls,
                                              self.tracer.cost_usd - cost_start, trace,
                                              answered_directly=(iterations == 1 and tool_calls == 0),
                                              history=stm.messages))
                    stm.add("user", f"Goal not achieved yet: {why}. Continue.")
                    self._checkpoint(trace, percept, iterations, "working", stm, results)
                    continue

                if decision.kind == "plan" and self.planner:
                    with self.tracer.span(trace, "plan.make") as s:
                        plan = self.planner.make_plan(percept, stm.messages, catalog)
                        self.tracer.record_llm(s, getattr(self.planner, "last_response", None))
                        s.attrs["steps"] = [st.description for st in plan.steps]
                    stm.add("assistant", json.dumps({"plan": [st.description for st in plan.steps]}))
                    steps = plan.steps
                elif decision.kind == "plan":
                    stm.add("user", "No planner is configured; choose a single tool instead.")
                    continue
                else:
                    stm.add("assistant", json.dumps({"tool": decision.tool, "args": decision.args}))
                    steps = [Step(description=decision.reasoning, tool=decision.tool, args=decision.args)]

                for step in steps:
                    result = self._act(trace, principal, step)
                    tool_calls += 1
                    results.append(result)
                    payload = result.output if result.ok else {"error": result.error, "denied": result.denied}
                    stm.add("user", f"Result of {result.tool}: {json.dumps(payload, default=str)[:4000]}")
                    if not result.ok:
                        break
                self._checkpoint(trace, percept, iterations, "working", stm, results)

    def _act(self, trace: str, principal: Principal, step: Step) -> ToolResult:
        tool = self.tools.get(step.tool or "")
        if tool is None:
            step.status = "failed"
            return ToolResult(tool=str(step.tool), ok=False, error="unknown tool")
        try:
            with self.tracer.span(trace, "guardrails.authorize", tool=tool.name):
                self.guardrails.authorize(principal, tool, step.args)
        except GuardrailViolation as v:
            step.status = "denied"
            return ToolResult(tool=tool.name, ok=False, error=f"denied by {v.rule}: {v.detail}", denied=True)
        with self.tracer.span(trace, f"act.{tool.name}", category=tool.category, side_effect=tool.side_effect) as s:
            result = self.executor.execute(tool, step.args)
            s.attrs.update(ok=result.ok, error=result.error)
            if not result.ok:
                s.status = "tool_error"
        step.status = "done" if result.ok else "failed"
        return result

    def _checkpoint(self, trace, percept, iteration, status, stm, results) -> None:
        if self.checkpointer:
            with self.tracer.span(trace, "harness.checkpoint", status=status):
                self.checkpointer.save(trace_id=trace, agent=self.name, percept=percept, iteration=iteration,
                                       status=status, history=stm.messages, results=results)

    def _recall(self, percept: Percept, k: int = 3) -> list[MemoryItem]:
        """Long-term recall, limited to the caller's audience when `memory_audience` is set. The store is
        asked for progressively more candidates so other audiences' items cannot crowd out the caller's."""
        if self.memory_audience is None:
            return self.memory.recall(percept.content, k)
        audience = self.memory_audience(percept.principal)
        want = k * 8
        while True:
            found = self.memory.recall(percept.content, want)
            mine = [m for m in found if m.meta.get("audience") == audience]
            if len(mine) >= k or len(found) < want or want >= 4096:
                return mine[:k]
            want *= 4

    def _escalate(self, trace, message, iterations, tool_calls, cost, stm) -> Outcome:
        message = self.guardrails.mask(message)        # escalations are output too: same PII masking
        with self.tracer.span(trace, "guardrails.escalate", reason=message):
            out = Outcome("escalated", message, iterations, tool_calls, cost, trace, history=stm.messages)
            if self.on_escalate:
                self.on_escalate(out)
        return out
