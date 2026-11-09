"""ACT: every external action is a registered Tool with a schema, scope and side-effect class.
Handlers start as TODO stubs: back them with local mocks first (Milestone 0), then real or sandbox systems."""
from agentcore import Tool, ToolRegistry


def get_order_360(customer, sales_order=None):
    """Order 360 via MCP gateway -> API-M -> CPI: order, deliveries, billing, credit status. TODO(M5)."""
    raise NotImplementedError("get_order_360")


def get_credit_exposure(customer):
    """Customer credit limit, exposure and block reason (mock credit API). TODO(M5)."""
    raise NotImplementedError("get_credit_exposure")


TOOLS = [
    Tool('get_order_360', 'Order 360 via MCP gateway -> API-M -> CPI: order, deliveries, billing, credit status', handler=get_order_360,
         params={'customer': 'string', 'sales_order': 'string'}, required=['customer'],
         scope='erp.read', side_effect='read', category='mcp'),
    Tool('get_credit_exposure', 'Customer credit limit, exposure and block reason (mock credit API)', handler=get_credit_exposure,
         params={'customer': 'string'}, required=['customer'],
         scope='credit.read', side_effect='read', category='api'),
]


def build_registry(names: list[str] | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in TOOLS:
        if names is None or tool.name in names:
            reg.register(tool)
    return reg
