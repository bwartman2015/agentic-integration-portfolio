"""PERCEIVE: raw input -> typed Percept. The LLM never sees raw input."""
from typing import Any

from agentcore import Percept, Principal


class FinanceRequestPerceiver:
    """
    Normalize a finance ops request or queue item (AP exception, unapplied remittance, deduction)
    into a Percept with entities {queue, document_ids, counterparty, amount}.

    Percept.kind = 'finance_request'
    """

    def perceive(self, raw: Any, principal: Principal) -> Percept:
        # TODO(Milestone 0): validate raw input, normalize it, extract entities.
        raise NotImplementedError("FinanceRequestPerceiver.perceive")
