"""ACT: every external action is a registered Tool with a schema, scope and side-effect class.
Handlers start as TODO stubs: back them with local mocks first (Milestone 0), then real or sandbox systems."""
from agentcore import Tool, ToolRegistry


def track_shipment(shipment_id):
    """Current tracking events, ETA and exception codes. TODO(M2)."""
    raise NotImplementedError("track_shipment")


def get_proof_of_delivery(shipment_id):
    """Proof of delivery: signer, timestamp, cartons delivered vs shipped. TODO(M2)."""
    raise NotImplementedError("get_proof_of_delivery")


def get_shipment_order(shipment_id):
    """Sales order, customer and promised delivery date for a shipment. TODO(M2)."""
    raise NotImplementedError("get_shipment_order")


def get_contract_rate(carrier, lane, weight_lb, freight_class=None):
    """Contracted rate for a carrier, lane, freight class and weight. TODO(M4)."""
    raise NotImplementedError("get_contract_rate")


def reroute_or_reschedule(shipment_id, action, reason):
    """Request a carrier reroute or delivery reschedule (write; approval). TODO(M3)."""
    raise NotImplementedError("reroute_or_reschedule")


def notify_customer(sales_order, message):
    """Send a proactive delay/damage notice to the customer contact (write). TODO(M3)."""
    raise NotImplementedError("notify_customer")


def open_freight_claim(shipment_id, claim_type, amount, evidence):
    """Open a carrier damage/shortage claim with evidence (write; approval). TODO(M3)."""
    raise NotImplementedError("open_freight_claim")


def dispute_freight_invoice(invoice_no, lines, reason):
    """Short-pay and dispute freight invoice lines with the variance (write; approval). TODO(M4)."""
    raise NotImplementedError("dispute_freight_invoice")


TOOLS = [
    Tool('track_shipment', 'Current tracking events, ETA and exception codes', handler=track_shipment,
         params={'shipment_id': 'string'}, required=['shipment_id'],
         scope='shipping.read', side_effect='read', category='api'),
    Tool('get_proof_of_delivery', 'Proof of delivery: signer, timestamp, cartons delivered vs shipped', handler=get_proof_of_delivery,
         params={'shipment_id': 'string'}, required=['shipment_id'],
         scope='shipping.read', side_effect='read', category='api'),
    Tool('get_shipment_order', 'Sales order, customer and promised delivery date for a shipment', handler=get_shipment_order,
         params={'shipment_id': 'string'}, required=['shipment_id'],
         scope='shipping.read', side_effect='read', category='api'),
    Tool('get_contract_rate', 'Contracted rate for a carrier, lane, freight class and weight', handler=get_contract_rate,
         params={'carrier': 'string', 'lane': 'string', 'freight_class': 'string', 'weight_lb': 'number'}, required=['carrier', 'lane', 'weight_lb'],
         scope='freight.read', side_effect='read', category='api'),
    Tool('reroute_or_reschedule', 'Request a carrier reroute or delivery reschedule (write; approval)', handler=reroute_or_reschedule,
         params={'shipment_id': 'string', 'action': 'string', 'reason': 'string'}, required=['shipment_id', 'action', 'reason'],
         scope='shipping.act', side_effect='write', category='api'),
    Tool('notify_customer', 'Send a proactive delay/damage notice to the customer contact (write)', handler=notify_customer,
         params={'sales_order': 'string', 'message': 'string'}, required=['sales_order', 'message'],
         scope='shipping.notify', side_effect='write', category='external'),
    Tool('open_freight_claim', 'Open a carrier damage/shortage claim with evidence (write; approval)', handler=open_freight_claim,
         params={'shipment_id': 'string', 'claim_type': 'string', 'amount': 'number', 'evidence': 'string'}, required=['shipment_id', 'claim_type', 'amount', 'evidence'],
         scope='freight.claim', side_effect='write', category='api'),
    Tool('dispute_freight_invoice', 'Short-pay and dispute freight invoice lines with the variance (write; approval)', handler=dispute_freight_invoice,
         params={'invoice_no': 'string', 'lines': 'array', 'reason': 'string'}, required=['invoice_no', 'lines', 'reason'],
         scope='freight.audit', side_effect='write', category='api'),
]


def build_registry(names: list[str] | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in TOOLS:
        if names is None or tool.name in names:
            reg.register(tool)
    return reg
