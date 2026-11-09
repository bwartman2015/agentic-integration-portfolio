"""PERCEIVE: raw input -> typed Percept. The LLM never sees raw input."""
from typing import Any

from agentcore import Percept, Principal


class ShipmentExceptionPerceiver:
    """
    Turn a carrier exception event (late, damaged, refused, short delivery) into a Percept with
    entities {shipment_id, sales_order, carrier, exception_code, customer}.

    Percept.kind = 'shipment_exception'
    """

    def perceive(self, raw: Any, principal: Principal) -> Percept:
        # TODO(Milestone 0): validate raw input, normalize it, extract entities.
        raise NotImplementedError("ShipmentExceptionPerceiver.perceive")


class FreightInvoicePerceiver:
    """
    Parse a carrier freight invoice (synthetic EDI 210-like JSON/CSV) into a Percept with entities
    {carrier, invoice_no, shipments, charges}.

    Percept.kind = 'freight_invoice'
    """

    def perceive(self, raw: Any, principal: Principal) -> Percept:
        # TODO(Milestone 0): validate raw input, normalize it, extract entities.
        raise NotImplementedError("FreightInvoicePerceiver.perceive")
