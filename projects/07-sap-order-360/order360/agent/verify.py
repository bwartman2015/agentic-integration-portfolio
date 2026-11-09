"""Goal achieved? Prefer deterministic checks over asking the LLM."""
from agentcore import Percept, ToolResult


class CorrelatedAnswerVerifier:
    """
    Goal achieved when the answer states order/delivery/billing status in plain English, cites the
    source system, and includes the correlation_id returned by the iFlow.
    """

    def check(self, percept: Percept, answer: str, results: list[ToolResult]) -> tuple[bool, str]:
        # TODO(Milestone 0): implement deterministically from `results`.
        raise NotImplementedError("CorrelatedAnswerVerifier.check")
