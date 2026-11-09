"""Goal achieved? Prefer deterministic checks over asking the LLM."""
from agentcore import Percept, ToolResult


class TriageVerifier:
    """
    Goal achieved when the order has a recommendation (accept / accept-partial / reject / hold), an
    explanation citing the exception evidence, and request_order_decision succeeded.
    """

    def check(self, percept: Percept, answer: str, results: list[ToolResult]) -> tuple[bool, str]:
        # TODO(Milestone 0): implement deterministically from `results`.
        raise NotImplementedError("TriageVerifier.check")


class MappingVerifier:
    """
    Goal achieved when the latest run_mapping_tests result has pass_rate >= 0.90.
    """

    def check(self, percept: Percept, answer: str, results: list[ToolResult]) -> tuple[bool, str]:
        # TODO(Milestone 0): implement deterministically from `results`.
        raise NotImplementedError("MappingVerifier.check")
