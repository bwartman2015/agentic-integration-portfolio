"""Goal achieved? Prefer deterministic checks over asking the LLM."""
from agentcore import Percept, ToolResult


class TestsPassVerifier:
    """
    Goal achieved when the last run_tests passed and coverage of changed endpoints >= 80%.
    """

    def check(self, percept: Percept, answer: str, results: list[ToolResult]) -> tuple[bool, str]:
        # TODO(Milestone 0): implement deterministically from `results`.
        raise NotImplementedError("TestsPassVerifier.check")


class ReviewQualityVerifier:
    """
    Goal achieved when every posted comment references a file and line and maps to an item in the
    review checklist skill.
    """

    def check(self, percept: Percept, answer: str, results: list[ToolResult]) -> tuple[bool, str]:
        # TODO(Milestone 0): implement deterministically from `results`.
        raise NotImplementedError("ReviewQualityVerifier.check")


class PROrchestratorVerifier:
    """
    Goal achieved when tests and review were both delegated and a PR summary lists results,
    governance findings and risk.
    """

    def check(self, percept: Percept, answer: str, results: list[ToolResult]) -> tuple[bool, str]:
        # TODO(Milestone 0): implement deterministically from `results`.
        raise NotImplementedError("PROrchestratorVerifier.check")
