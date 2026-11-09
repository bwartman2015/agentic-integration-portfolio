"""ACT: every external action is a registered Tool with a schema, scope and side-effect class.

Milestone 0: handlers are backed by the in-process mock systems (mock_systems/, seeded synthetic data).
Milestone 6 replaces them with the gateway's MCP tools via agentcore.register_tool_source, keeping the
same names, scopes and side effects. Errors (unknown ids) are raised and returned to THINK as data.
"""
from __future__ import annotations

from typing import Any

from agentcore import Tool, ToolRegistry
from mock_systems import get_systems


def _norm(value: str) -> str:
    return value.strip().upper()


def search_accounts(query: str) -> dict[str, Any]:
    """Find a customer account in CRM (MCP tool on the gateway)."""
    return {"query": query, "matches": get_systems().crm.find_accounts(query)}


def get_sales_order(sales_order: str) -> dict[str, Any]:
    """Sales order header, lines and delivery status from ERP (MCP)."""
    return get_systems().erp.order(_norm(sales_order))


def track_shipment(sales_order: str) -> dict[str, Any]:
    """Carrier tracking events and proof of delivery for an order (MCP)."""
    return get_systems().carrier.tracking(_norm(sales_order))


def get_open_invoices(account_id: str) -> dict[str, Any]:
    """Open AR invoices for an account (MCP). Totals are computed here, not by the LLM."""
    account_id = _norm(account_id)
    invoices = get_systems().ar.open_items(account_id)
    return {"account_id": account_id, "invoices": invoices, "count": len(invoices),
            "total_open": round(sum(i["amount"] for i in invoices), 2)}


def create_case(account_id: str, subject: str, summary: str) -> dict[str, Any]:
    """Create a CRM case with a summary and next step (write; approval)."""
    return get_systems().crm.open_case(_norm(account_id), subject, summary)


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
