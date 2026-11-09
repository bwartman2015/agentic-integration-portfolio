"""ACT: every external action is a registered Tool with a schema, scope and side-effect class.
Handlers start as TODO stubs: back them with local mocks first (Milestone 0), then real or sandbox systems."""
from agentcore import Tool, ToolRegistry


def get_purchase_order(po_number):
    """PO lines, prices and quantities. TODO(M2)."""
    raise NotImplementedError("get_purchase_order")


def get_goods_receipts(po_number):
    """Goods receipts posted against a PO. TODO(M2)."""
    raise NotImplementedError("get_goods_receipts")


def get_supplier_invoice(invoice_id):
    """Supplier invoice header and lines. TODO(M2)."""
    raise NotImplementedError("get_supplier_invoice")


def three_way_match(invoice_id):
    """Deterministic 3-way match: returns variances per line vs tolerance. TODO(M2)."""
    raise NotImplementedError("three_way_match")


def check_duplicate_invoice(invoice_id):
    """Find likely duplicate supplier invoices (same vendor, amount, near date). TODO(M2)."""
    raise NotImplementedError("check_duplicate_invoice")


def propose_ap_resolution(invoice_id, action, explanation):
    """Propose release / hold / request-credit-memo for a clerk to post (write). TODO(M2)."""
    raise NotImplementedError("propose_ap_resolution")


def get_remittance(remittance_id):
    """Remittance advice: payer, amount, invoices referenced, deduction codes. TODO(M3)."""
    raise NotImplementedError("get_remittance")


def get_open_items(customer_id):
    """Open AR invoices for a customer. TODO(M3)."""
    raise NotImplementedError("get_open_items")


def match_remittance(remittance_id):
    """Deterministic matcher: proposes invoice-level application for a remittance. TODO(M3)."""
    raise NotImplementedError("match_remittance")


def propose_cash_application(remittance_id, applications, deductions=None):
    """Propose cash application + deduction coding for a clerk to post (write). TODO(M3)."""
    raise NotImplementedError("propose_cash_application")


TOOLS = [
    Tool('get_purchase_order', 'PO lines, prices and quantities', handler=get_purchase_order,
         params={'po_number': 'string'}, required=['po_number'],
         scope='ap.read', side_effect='read', category='api'),
    Tool('get_goods_receipts', 'Goods receipts posted against a PO', handler=get_goods_receipts,
         params={'po_number': 'string'}, required=['po_number'],
         scope='ap.read', side_effect='read', category='api'),
    Tool('get_supplier_invoice', 'Supplier invoice header and lines', handler=get_supplier_invoice,
         params={'invoice_id': 'string'}, required=['invoice_id'],
         scope='ap.read', side_effect='read', category='api'),
    Tool('three_way_match', 'Deterministic 3-way match: returns variances per line vs tolerance', handler=three_way_match,
         params={'invoice_id': 'string'}, required=['invoice_id'],
         scope='ap.read', side_effect='read', category='code'),
    Tool('check_duplicate_invoice', 'Find likely duplicate supplier invoices (same vendor, amount, near date)', handler=check_duplicate_invoice,
         params={'invoice_id': 'string'}, required=['invoice_id'],
         scope='ap.read', side_effect='read', category='api'),
    Tool('propose_ap_resolution', 'Propose release / hold / request-credit-memo for a clerk to post (write)', handler=propose_ap_resolution,
         params={'invoice_id': 'string', 'action': 'string', 'explanation': 'string'}, required=['invoice_id', 'action', 'explanation'],
         scope='ap.propose', side_effect='write', category='api'),
    Tool('get_remittance', 'Remittance advice: payer, amount, invoices referenced, deduction codes', handler=get_remittance,
         params={'remittance_id': 'string'}, required=['remittance_id'],
         scope='ar.read', side_effect='read', category='api'),
    Tool('get_open_items', 'Open AR invoices for a customer', handler=get_open_items,
         params={'customer_id': 'string'}, required=['customer_id'],
         scope='ar.read', side_effect='read', category='api'),
    Tool('match_remittance', 'Deterministic matcher: proposes invoice-level application for a remittance', handler=match_remittance,
         params={'remittance_id': 'string'}, required=['remittance_id'],
         scope='ar.read', side_effect='read', category='code'),
    Tool('propose_cash_application', 'Propose cash application + deduction coding for a clerk to post (write)', handler=propose_cash_application,
         params={'remittance_id': 'string', 'applications': 'array', 'deductions': 'array'}, required=['remittance_id', 'applications'],
         scope='ar.propose', side_effect='write', category='api'),
]


def build_registry(names: list[str] | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in TOOLS:
        if names is None or tool.name in names:
            reg.register(tool)
    return reg
