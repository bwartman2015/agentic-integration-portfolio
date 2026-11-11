"""EVALS (concept 5): score the agent's outputs against expected outcomes, over time.

cases.json:
[
  {"id": "order-status-basic",
   "input": "What's the status of Acme's latest order?",
   "scopes": ["crm.read", "erp.read"],
   "attributes": {"regions": ["Central"]},                     # optional Principal.attributes
   "expect_status": "answered",
   "expect_tools": ["search_accounts", "get_sales_order"],   # must be called (any order)
   "forbid_tools": ["create_task"],                            # must NOT be called
   "expect_answer_contains": ["4500017", ["not found", "couldn't find"]],   # case-insensitive substrings;
                                                                 # a list = any one of these phrasings
   "max_cost_usd": 0.05, "max_iterations": 4,
   "approval": "approve"}                                     # optional: the human decision a write gets
]
run_evals() returns an EvalReport; save it to evals/results/ and compare_to_baseline() fails the
build when the success rate drops, so quality can only improve over time.
"""
from __future__ import annotations

import json
import pathlib
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .types import Principal


@dataclass
class EvalCase:
    id: str
    input: Any
    scopes: list[str] = field(default_factory=list)
    expect_status: str = "answered"
    expect_tools: list[str] = field(default_factory=list)
    forbid_tools: list[str] = field(default_factory=list)
    expect_answer_contains: list[str | list[str]] = field(default_factory=list)
    max_cost_usd: float | None = None
    max_iterations: int | None = None
    tags: list[str] = field(default_factory=list)
    attributes: dict[str, list[str]] = field(default_factory=dict)   # Principal.attributes, e.g. regions
    # The human decision this case assumes for writes ("approve" | "reject" | "timeout"); the project's
    # eval runner installs a matching approver, the same for every model, so cases mean the same thing.
    approval: str | None = None


@dataclass
class CaseResult:
    id: str
    passed: bool
    failures: list[str]
    status: str
    tools_used: list[str]
    cost_usd: float
    iterations: int
    tool_selection_ok: bool
    trace_id: str = ""
    latency_s: float = 0.0


@dataclass
class EvalReport:
    agent: str
    results: list[CaseResult]
    created: float = field(default_factory=time.time)

    @property
    def success_rate(self) -> float:
        return sum(r.passed for r in self.results) / len(self.results) if self.results else 0.0

    @property
    def tool_selection_accuracy(self) -> float:
        return sum(r.tool_selection_ok for r in self.results) / len(self.results) if self.results else 0.0

    @property
    def avg_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.results) / len(self.results) if self.results else 0.0

    @property
    def avg_latency_s(self) -> float:
        return sum(r.latency_s for r in self.results) / len(self.results) if self.results else 0.0

    def summary(self) -> dict[str, Any]:
        return {"agent": self.agent, "cases": len(self.results), "success_rate": round(self.success_rate, 3),
                "tool_selection_accuracy": round(self.tool_selection_accuracy, 3),
                "avg_cost_usd": round(self.avg_cost_usd, 5),
                "total_cost_usd": round(sum(r.cost_usd for r in self.results), 5),
                "avg_latency_s": round(self.avg_latency_s, 2),
                "avg_iterations": round(sum(r.iterations for r in self.results) / max(len(self.results), 1), 2)}

    def to_markdown(self) -> str:
        s = self.summary()
        lines = [f"## Eval report: {self.agent}", "",
                 f"Success {s['success_rate']:.0%} · tool selection {s['tool_selection_accuracy']:.0%} · "
                 f"avg cost ${s['avg_cost_usd']:.4f} · avg latency {s['avg_latency_s']}s · "
                 f"avg iterations {s['avg_iterations']}", "",
                 "| Case | Result | Tools used | Latency | Failures |", "|---|---|---|---|---|"]
        for r in self.results:
            lines.append(f"| {r.id} | {'PASS' if r.passed else 'FAIL'} | {', '.join(r.tools_used) or '-'} | "
                         f"{r.latency_s:.1f}s | {'; '.join(r.failures) or '-'} |")
        return "\n".join(lines)

    def save(self, directory: str) -> pathlib.Path:
        d = pathlib.Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{self.agent}-{int(self.created)}.json"
        path.write_text(json.dumps({"summary": self.summary(), "results": [asdict(r) for r in self.results]}, indent=2))
        return path


def load_cases(path: str | pathlib.Path) -> list[EvalCase]:
    return [EvalCase(**c) for c in json.loads(pathlib.Path(path).read_text())]


def run_evals(agent_factory: Callable[[EvalCase], Any], cases: list[EvalCase], agent_name: str = "agent") -> EvalReport:
    """agent_factory builds a FRESH agent per case (isolated memory/state), optionally case-specific."""
    results = []
    for case in cases:
        agent = agent_factory(case)
        start = time.perf_counter()
        try:
            out = agent.run(case.input, Principal(f"eval:{case.id}", frozenset(case.scopes), case.attributes))
        except Exception as e:
            results.append(CaseResult(case.id, False, [f"crashed: {type(e).__name__}: {e}"], "error", [], 0.0, 0, False,
                                      latency_s=round(time.perf_counter() - start, 2)))
            continue
        latency = round(time.perf_counter() - start, 2)
        fails = []
        if out.status != case.expect_status:
            fails.append(f"status {out.status} != {case.expect_status}")
        used = set(out.tools_used)
        missing = [t for t in case.expect_tools if t not in used]
        forbidden = [t for t in case.forbid_tools if t in used]
        if missing:
            fails.append(f"missing tools {missing}")
        if forbidden:
            fails.append(f"forbidden tools used {forbidden}")
        low = out.answer.lower()
        for s in case.expect_answer_contains:
            options = [s] if isinstance(s, str) else list(s)
            if not any(o.lower() in low for o in options):
                fails.append(f"answer lacks {s!r}")
        if case.max_cost_usd is not None and out.cost_usd > case.max_cost_usd:
            fails.append(f"cost ${out.cost_usd:.4f} > ${case.max_cost_usd}")
        if case.max_iterations is not None and out.iterations > case.max_iterations:
            fails.append(f"iterations {out.iterations} > {case.max_iterations}")
        results.append(CaseResult(case.id, not fails, fails, out.status, out.tools_used, out.cost_usd,
                                  out.iterations, not missing and not forbidden, out.trace_id, latency))
    return EvalReport(agent_name, results)


def compare_to_baseline(report: EvalReport, baseline_path: str | pathlib.Path, tolerance: float = 0.0) -> tuple[bool, str]:
    p = pathlib.Path(baseline_path)
    if not p.exists():
        return True, "no baseline yet"
    base = json.loads(p.read_text())["summary"]
    now = report.summary()
    if now["success_rate"] + tolerance < base["success_rate"]:
        return False, f"regression: success {now['success_rate']} < baseline {base['success_rate']}"
    base_tools = base.get("tool_selection_accuracy", 0.0)
    if now["tool_selection_accuracy"] + tolerance < base_tools:
        return False, f"regression: tool selection {now['tool_selection_accuracy']} < baseline {base_tools}"
    return True, (f"ok: success {now['success_rate']} vs baseline {base['success_rate']}; "
                  f"tool selection {now['tool_selection_accuracy']} vs baseline {base_tools}")
