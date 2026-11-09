"""Goal achieved? Prefer deterministic checks over asking the LLM."""
from agentcore import Percept, ToolResult


class DiagnosisVerifier:
    """
    Goal achieved when the incident is classified (transient / degradation / mapping-code / data /
    config-security / poison) with trace or DLQ evidence and a business-impact statement.
    """

    def check(self, percept: Percept, answer: str, results: list[ToolResult]) -> tuple[bool, str]:
        # TODO(Milestone 0): implement deterministically from `results`.
        raise NotImplementedError("DiagnosisVerifier.check")


class MetricsBaselineVerifier:
    """
    Goal achieved when get_metrics shows error rate and DLQ depth back within baseline, OR the on-
    call human was paged for a class that must not be auto-fixed.
    """

    def check(self, percept: Percept, answer: str, results: list[ToolResult]) -> tuple[bool, str]:
        # TODO(Milestone 0): implement deterministically from `results`.
        raise NotImplementedError("MetricsBaselineVerifier.check")


class CommanderVerifier:
    """
    Goal achieved when diagnosis and remediation were both delegated, the outcome is verified
    against metrics, and an incident report was written.
    """

    def check(self, percept: Percept, answer: str, results: list[ToolResult]) -> tuple[bool, str]:
        # TODO(Milestone 0): implement deterministically from `results`.
        raise NotImplementedError("CommanderVerifier.check")
