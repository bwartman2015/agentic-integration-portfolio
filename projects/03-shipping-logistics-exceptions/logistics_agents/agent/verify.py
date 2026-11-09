"""Goal achieved? Prefer deterministic checks over asking the LLM."""
from agentcore import Percept, ToolResult


class ExceptionDispositionVerifier:
    """
    Goal achieved when the exception has a disposition (rerouted / rescheduled / claim opened /
    customer notified / escalated), and the customer was notified whenever the new ETA slips more
    than 1 business day past the promised date.
    """

    def check(self, percept: Percept, answer: str, results: list[ToolResult]) -> tuple[bool, str]:
        # TODO(Milestone 0): implement deterministically from `results`.
        raise NotImplementedError("ExceptionDispositionVerifier.check")


class FreightAuditVerifier:
    """
    Goal achieved when every invoice line has been priced against get_contract_rate with a computed
    variance, and any line over the $25 tolerance is in a dispute.
    """

    def check(self, percept: Percept, answer: str, results: list[ToolResult]) -> tuple[bool, str]:
        # TODO(Milestone 0): implement deterministically from `results`.
        raise NotImplementedError("FreightAuditVerifier.check")
