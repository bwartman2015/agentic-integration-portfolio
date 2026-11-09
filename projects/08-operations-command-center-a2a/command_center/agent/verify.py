"""Goal achieved? Prefer deterministic checks over asking the LLM."""
from agentcore import Percept, ToolResult


class ResolutionVerifier:
    """
    Goal achieved when at least the finance and shipping specialists were consulted, findings agree
    or the conflict is stated, and a resolution package with a recommendation (accept deduction +
    freight claim / dispute with evidence / needs human) was recorded.
    """

    def check(self, percept: Percept, answer: str, results: list[ToolResult]) -> tuple[bool, str]:
        # TODO(Milestone 0): implement deterministically from `results`.
        raise NotImplementedError("ResolutionVerifier.check")
