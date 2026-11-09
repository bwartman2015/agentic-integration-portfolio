"""Goal achieved? Prefer deterministic checks over asking the LLM."""
from agentcore import Percept, ToolResult


class EvidenceBackedAnswerVerifier:
    """
    Goal achieved when every entity in the request is addressed and each factual claim is backed by
    a successful tool result or a cited policy document.
    """

    def check(self, percept: Percept, answer: str, results: list[ToolResult]) -> tuple[bool, str]:
        # TODO(Milestone 0): implement deterministically from `results`.
        raise NotImplementedError("EvidenceBackedAnswerVerifier.check")
