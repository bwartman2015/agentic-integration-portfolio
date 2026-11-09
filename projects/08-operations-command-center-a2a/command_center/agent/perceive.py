"""PERCEIVE: raw input -> typed Percept. The LLM never sees raw input."""
from typing import Any

from agentcore import Percept, Principal


class DisputePerceiver:
    """
    Turn a dispute or cross-functional question (deduction, missing shipment, order discrepancy)
    into a Percept with entities {customer, invoice, sales_order, po_number, amount,
    deduction_code}.

    Percept.kind = 'dispute'
    """

    def perceive(self, raw: Any, principal: Principal) -> Percept:
        # TODO(Milestone 0): validate raw input, normalize it, extract entities.
        raise NotImplementedError("DisputePerceiver.perceive")
