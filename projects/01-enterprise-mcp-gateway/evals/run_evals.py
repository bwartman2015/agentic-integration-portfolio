"""Run evals/cases.json against the agents (EVALS concept).

    make eval P=01               scripted model, offline, on the host: baseline.json (the CI gate)
    make eval P=01 LLM=local     Qwen2.5-1.5B on llama.cpp in the copilot container: baseline.local.json,
                                 the official real-model baseline (free, offline)
    make eval P=01 LLM=claude    optional, paid: a hosted model (your own API key); no committed baseline
    add --update-baseline to save this run as the provider's baseline (refused if it is lower than the
    current one, unless --accept-regression is given too)

Every provider runs the same cases on the same offline backend: the in-process tools over the seeded
mock systems and an in-memory long-term memory. No eval run uses the gateway, a database or Neon. A
case's human decision for writes (`approval` in cases.json) is installed the same way for every model.
The scripted model replays fixtures/llm/<case id>.json, which tests the harness; real models are
measured: success, tool selection, cost (from token usage and your prices) and latency.
"""
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
os.environ.setdefault("LLM_PROVIDER", "scripted")
for unsafe in ("MEMORY_BACKEND", "MEMORY_DATABASE_URL", "DATABASE_URL", "GATEWAY_URL", "OPA_URL"):
    os.environ.pop(unsafe, None)                            # offline backend only, whatever the shell has
os.environ["MEMORY_SQLITE"] = ":memory:"                   # fresh memory per agent: no cross-case leakage
os.environ.setdefault("PROGRESS_DIR", tempfile.mkdtemp(prefix="cs-copilot-evals-"))
RESULTS = pathlib.Path(os.environ.get("EVAL_RESULTS_DIR", str(HERE / "results")))
RESULTS.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TRACE_FILE", str(RESULTS / "traces.jsonl"))

from agentcore import compare_to_baseline, load_cases, run_evals  # noqa: E402
from cs_copilot.agent.build import BUILDERS  # noqa: E402
from cs_copilot.agent.guardrails import scripted_approver, set_approver  # noqa: E402
from cs_copilot.agent.reason import get_llm  # noqa: E402
from mock_systems import reset_systems  # noqa: E402

DEFAULT_AGENT = 'cs-copilot'
BASELINES = {"scripted": "baseline.json", "anthropic": "baseline.claude.json", "local": "baseline.local.json"}
# Real models: a per-case cost cap of their own (the cases' caps are sized for the scripted model), and
# a tolerance of about one case against their baseline, because a real model is not deterministic.
REAL_MAX_COST = float(os.environ.get("EVAL_MAX_COST_PER_CASE", "0.25"))


def provider() -> str:
    return os.environ["LLM_PROVIDER"]


def baseline_path() -> pathlib.Path:
    return HERE / BASELINES[provider()]


def tolerance(cases: int) -> float:
    return 0.0 if provider() == "scripted" else round(1 / max(cases, 1) + 1e-6, 4)


LLMS: list = []


def agent_for(case):
    reset_systems()                                             # every case starts from the seeded data
    set_approver(scripted_approver(case.approval))
    if provider() != "scripted":
        case.max_cost_usd = REAL_MAX_COST
    llm = get_llm(script=case.id)
    LLMS.append(llm)
    return BUILDERS[(case.tags or [DEFAULT_AGENT])[0]](llm)


def main() -> int:
    if provider() not in BASELINES:
        print(f"LLM_PROVIDER must be one of {', '.join(BASELINES)}")
        return 2
    cases = load_cases(HERE / "cases.json")
    try:
        report = run_evals(agent_for, cases, agent_name=f"01-enterprise-mcp-gateway-{provider()}")
    finally:
        set_approver(None)
    served = sorted({m for llm in LLMS for m in getattr(llm, "served_models", ())})
    print(f"provider={provider()}  baseline={baseline_path().name}" + (f"  served by: {', '.join(served)}" if served else ""))
    print(report.to_markdown())
    s = report.summary()
    print(f"total cost this run: ${s['total_cost_usd']:.4f}")
    path = report.save(str(RESULTS))
    if provider() == "anthropic":
        print("report only: the optional hosted model has no committed baseline")
        return 0
    updating = "--update-baseline" in sys.argv
    if not baseline_path().exists() and not updating:
        print(f"FAIL: no {baseline_path().name}; review the report, then rerun with --update-baseline")
        return 1
    ok, msg = compare_to_baseline(report, baseline_path(), tolerance=tolerance(len(cases)))
    print(msg)
    if updating and (ok or "--accept-regression" in sys.argv):
        baseline_path().write_text(path.read_text())
        print(f"baseline updated: {baseline_path().name}")
    elif updating:
        print("baseline NOT updated: this run is below it (add --accept-regression to lower it on purpose)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
