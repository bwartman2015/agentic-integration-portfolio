"""PERCEIVE: raw input -> typed Percept. The LLM never sees raw input."""
from typing import Any

from agentcore import Percept, Principal


class Order360QueryPerceiver:
    """
    Extract a typed query: entities {customer, sales_order, po_number, date_range}; resolve customer
    aliases from memory where possible.

    Percept.kind = 'order360_question'
    """

    def perceive(self, raw: Any, principal: Principal) -> Percept:
        # TODO(Milestone 0): validate raw input, normalize it, extract entities.
        raise NotImplementedError("Order360QueryPerceiver.perceive")
