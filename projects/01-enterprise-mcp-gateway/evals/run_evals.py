"""Run evals/cases.json against the agents (EVALS concept). Usage: python3 evals/run_evals.py [--update-baseline]

LLM_PROVIDER defaults to `scripted` here: each case replays fixtures/llm/<case id>.json, which tests the
harness (tools, guardrails, verifier, citations) offline. Its baseline is evals/baseline.json.
With a real model (LLM_PROVIDER=ollama) the baseline is evals/baseline.ollama.json.
"""
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
os.environ.setdefault("LLM_PROVIDER", "scripted")
os.environ.setdefault("MEMORY_SQLITE", ":memory:")          # fresh memory per agent: no cross-case leakage
os.environ.setdefault("PROGRESS_DIR", tempfile.mkdtemp(prefix="cs-copilot-evals-"))
(HERE / "results").mkdir(exist_ok=True)
os.environ.setdefault("TRACE_FILE", str(HERE / "results" / "traces.jsonl"))

from agentcore import compare_to_baseline, load_cases, run_evals  # noqa: E402
from cs_copilot.agent.build import BUILDERS  # noqa: E402
from cs_copilot.agent.guardrails import scripted_approver, set_approver  # noqa: E402
from cs_copilot.agent.reason import get_llm, scripted_approval  # noqa: E402
from mock_systems import reset_systems  # noqa: E402

DEFAULT_AGENT = 'cs-copilot'


def baseline_path() -> pathlib.Path:
    provider = os.environ["LLM_PROVIDER"]
    return HERE / ("baseline.json" if provider == "scripted" else f"baseline.{provider}.json")


def agent_for(case):
    reset_systems()                                             # every case starts from the seeded data
    set_approver(scripted_approver(scripted_approval(case.id) if os.environ["LLM_PROVIDER"] == "scripted" else None))
    return BUILDERS[(case.tags or [DEFAULT_AGENT])[0]](get_llm(script=case.id))


def main() -> int:
    cases = load_cases(HERE / "cases.json")
    report = run_evals(agent_for, cases, agent_name="01-enterprise-mcp-gateway")
    print(f"provider={os.environ['LLM_PROVIDER']}  baseline={baseline_path().name}")
    print(report.to_markdown())
    path = report.save(str(HERE / "results"))
    updating = "--update-baseline" in sys.argv
    if not baseline_path().exists() and not updating:
        print(f"FAIL: no {baseline_path().name}; review the report, then rerun with --update-baseline")
        return 1
    ok, msg = compare_to_baseline(report, baseline_path())
    print(msg)
    if updating:
        baseline_path().write_text(path.read_text())
        print(f"baseline updated: {baseline_path().name}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
