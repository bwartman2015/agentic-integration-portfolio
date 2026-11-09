"""Goal achieved? Prefer deterministic checks over asking the LLM."""
from agentcore import Percept, ToolResult


class APResolutionVerifier:
    """
    Goal achieved when each exception invoice has a proposal (release/hold/credit-memo) whose
    explanation cites the three_way_match variances, and duplicates were checked.
    """

    def check(self, percept: Percept, answer: str, results: list[ToolResult]) -> tuple[bool, str]:
        # TODO(Milestone 0): implement deterministically from `results`.
        raise NotImplementedError("APResolutionVerifier.check")


class CashApplicationVerifier:
    """
    Goal achieved when applied + deductions + unapplied equals the remittance amount to the cent,
    and every deduction has a reason code.
    """

    def check(self, percept: Percept, answer: str, results: list[ToolResult]) -> tuple[bool, str]:
        # TODO(Milestone 0): implement deterministically from `results`.
        raise NotImplementedError("CashApplicationVerifier.check")


class ManagerVerifier:
    """
    Goal achieved when every sub-task was delegated to the right specialist and the final summary
    lists each proposal with its specialist's evidence.
    """

    def check(self, percept: Percept, answer: str, results: list[ToolResult]) -> tuple[bool, str]:
        # TODO(Milestone 0): implement deterministically from `results`.
        raise NotImplementedError("ManagerVerifier.check")
