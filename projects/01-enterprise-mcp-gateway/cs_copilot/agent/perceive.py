"""PERCEIVE: raw input -> typed Percept. The LLM never sees raw input."""
from typing import Any

from agentcore import Percept, Principal


class CaseRequestPerceiver:
    """
    Normalize a rep's question (or an inbound case description) into a Percept; attach the rep's
    verified principal from the JWT; extract entities {account, sales_order, po_number, invoice}.

    Percept.kind = 'cs_request'
    """

    def perceive(self, raw: Any, principal: Principal) -> Percept:
        # TODO(Milestone 0): validate raw input, normalize it, extract entities.
        raise NotImplementedError("CaseRequestPerceiver.perceive")
