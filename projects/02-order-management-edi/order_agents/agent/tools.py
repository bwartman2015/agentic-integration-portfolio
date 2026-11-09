"""ACT: every external action is a registered Tool with a schema, scope and side-effect class.
Handlers start as TODO stubs: back them with local mocks first (Milestone 0), then real or sandbox systems."""
from agentcore import Tool, ToolRegistry


def get_order_history(partner_id, sku=None):
    """Partner's order history stats for a SKU (Postgres). TODO(M3)."""
    raise NotImplementedError("get_order_history")


def get_partner_profile(partner_id):
    """Trading partner profile, ship-to list and pricing agreement. TODO(M3)."""
    raise NotImplementedError("get_partner_profile")


def get_available_to_promise(sku, dc=None):
    """Available-to-promise quantity for a SKU by distribution center. TODO(M3)."""
    raise NotImplementedError("get_available_to_promise")


def parse_x12(x12):
    """Parse X12 text into canonical JSON. TODO(M1)."""
    raise NotImplementedError("parse_x12")


def draft_mapping(partner_id, yaml):
    """Save a drafted partner mapping YAML under mappings/. TODO(M5)."""
    raise NotImplementedError("draft_mapping")


def run_mapping_tests(partner_id):
    """Run the mapping test harness; returns pass_rate and failures. TODO(M5)."""
    raise NotImplementedError("run_mapping_tests")


def request_order_decision(po_number, recommendation, explanation):
    """Send a flagged order + recommendation to the n8n approval workflow. TODO(M4)."""
    raise NotImplementedError("request_order_decision")


TOOLS = [
    Tool('get_order_history', "Partner's order history stats for a SKU (Postgres)", handler=get_order_history,
         params={'partner_id': 'string', 'sku': 'string'}, required=['partner_id'],
         scope='orders.read', side_effect='read', category='database'),
    Tool('get_partner_profile', 'Trading partner profile, ship-to list and pricing agreement', handler=get_partner_profile,
         params={'partner_id': 'string'}, required=['partner_id'],
         scope='orders.read', side_effect='read', category='database'),
    Tool('get_available_to_promise', 'Available-to-promise quantity for a SKU by distribution center', handler=get_available_to_promise,
         params={'sku': 'string', 'dc': 'string'}, required=['sku'],
         scope='orders.read', side_effect='read', category='api'),
    Tool('parse_x12', 'Parse X12 text into canonical JSON', handler=parse_x12,
         params={'x12': 'string'}, required=['x12'],
         scope='orders.read', side_effect='read', category='code'),
    Tool('draft_mapping', 'Save a drafted partner mapping YAML under mappings/', handler=draft_mapping,
         params={'partner_id': 'string', 'yaml': 'string'}, required=['partner_id', 'yaml'],
         scope='orders.mapping', side_effect='write', category='file'),
    Tool('run_mapping_tests', 'Run the mapping test harness; returns pass_rate and failures', handler=run_mapping_tests,
         params={'partner_id': 'string'}, required=['partner_id'],
         scope='orders.mapping', side_effect='read', category='code'),
    Tool('request_order_decision', 'Send a flagged order + recommendation to the n8n approval workflow', handler=request_order_decision,
         params={'po_number': 'string', 'recommendation': 'string', 'explanation': 'string'}, required=['po_number', 'recommendation', 'explanation'],
         scope='orders.triage', side_effect='write', category='external'),
]


def build_registry(names: list[str] | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    for tool in TOOLS:
        if names is None or tool.name in names:
            reg.register(tool)
    return reg
