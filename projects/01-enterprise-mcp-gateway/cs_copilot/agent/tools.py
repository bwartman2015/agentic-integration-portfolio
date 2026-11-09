"""ACT: every external action is a registered Tool with a schema, scope and side-effect class.
Handlers start as TODO stubs: back them with local mocks first (Milestone 0), then real or sandbox systems."""
from agentcore import Tool, ToolRegistry


def search_accounts(query):
    """Find a customer account in CRM (MCP tool on the gateway). TODO(M2)."""
    raise NotImplementedError("search_accounts")


def get_sales_order(sales_order):
    """Sales order header, lines and delivery status from ERP (MCP). TODO(M2)."""
    raise NotImplementedError("get_sales_order")


def track_shipment(sales_order):
    """Carrier tracking events and proof of delivery for an order (MCP). TODO(M2)."""
    raise NotImplementedError("track_shipment")


def get_open_invoices(account_id):
    """Open AR invoices for an account (MCP). TODO(M2)."""
    raise NotImplementedError("get_open_invoices")


def create_case(account_id, subject, summary):
    """Create a CRM case with a summary and next step (write; approval). TODO(M2)."""
    raise NotImplementedError("create_case")


TOOLS = [
    Tool('search_accounts', 'Find a customer account in CRM (MCP tool on the gateway)', handler=search_accounts,
         params={'query': 'string'}, required=['query'],
         scope='crm.read', side_effect='read', category='mcp'),
    Tool('get_sales_order', 'Sales order header, lines and delivery status from ERP (MCP)', handler=get_sales_order,
         params={'sales_order': 'string'}, required=['sales_order'],
         scope='erp.read', side_effect='read', category='mcp'),
    Tool('track_shipment', 'Carrier tracking events and proof of delivery for an order (MCP)', handler=track_shipment,
         params={'sales_order': 'string'}, required=['sales_order'],
         scope='shipping.read', side_effect='read', category='mcp'),
    Tool('get_open_invoices', 'Open AR invoices for an account (MCP)', handler=get_open_invoices,
         params={'account_id': 'string'}, required=['account_id'],
         scope='ar.read', side_effect='read', category='mcp'),
    Tool('create_case', 'Create a CRM case with a summary and next step (write; approval)', handler=create_case,
         params={'account_id': 'string', 'subject': 'string', 'summary': 'string'}, required=['account_id', 'subject', 'summary'],
         scope='crm.write', side_effect='write', category='mcp'),
]


def build_registry(names: list[str] | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in TOOLS:
        if names is None or tool.name in names:
            reg.register(tool)
    return reg
