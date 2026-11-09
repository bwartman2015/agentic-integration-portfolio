"""Run evals/cases.json against the agents (EVALS concept). Usage: python3 evals/run_evals.py [--update-baseline]"""
import pathlib
import sys

from agentcore import compare_to_baseline, load_cases, run_evals
from cs_copilot.agent.build import BUILDERS

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_AGENT = 'cs-copilot'


def main() -> int:
    cases = load_cases(HERE / "cases.json")
    agent_for = lambda c: BUILDERS[(c.tags or [DEFAULT_AGENT])[0]]()
    report = run_evals(agent_for, cases, agent_name="01-enterprise-mcp-gateway")
    print(report.to_markdown())
    path = report.save(str(HERE / "results"))
    ok, msg = compare_to_baseline(report, HERE / "baseline.json")
    print(msg)
    if "--update-baseline" in sys.argv:
        (HERE / "baseline.json").write_text(path.read_text())
        print("baseline updated")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
