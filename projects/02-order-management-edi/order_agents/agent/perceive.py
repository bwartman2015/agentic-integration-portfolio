"""PERCEIVE: raw input -> typed Percept. The LLM never sees raw input."""
from typing import Any

from agentcore import Percept, Principal


class FlaggedOrderPerceiver:
    """
    Take a canonical order that FAILED the deterministic rules/anomaly checks plus its evidence and
    produce a Percept with entities {partner_id, po_number, exceptions}. Clean orders never reach an
    agent.

    Percept.kind = 'order_exception'
    """

    def perceive(self, raw: Any, principal: Principal) -> Percept:
        # TODO(Milestone 0): validate raw input, normalize it, extract entities.
        raise NotImplementedError("FlaggedOrderPerceiver.perceive")


class PartnerGuidePerceiver:
    """
    Turn a new retail partner's implementation guide and sample X12 files into a Percept with
    entities {partner_id, transaction_sets}.

    Percept.kind = 'partner_onboarding'
    """

    def perceive(self, raw: Any, principal: Principal) -> Percept:
        # TODO(Milestone 0): validate raw input, normalize it, extract entities.
        raise NotImplementedError("PartnerGuidePerceiver.perceive")
