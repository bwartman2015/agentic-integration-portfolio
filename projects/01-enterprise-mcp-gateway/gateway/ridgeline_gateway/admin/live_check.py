"""Call every gateway tool through MCP (the same path an agent uses, with a real Keycloak token) and report
each tool's result with its configured backend. The gateway's own log line for each call records the
backend that actually answered and the verified client: `docker compose logs gateway`.

    make live-check P=01

Runs in the one-off `ops` container (it holds the agent client secrets; the gateway container does not)
against http://gateway:8000/mcp, authenticating as cs-rep. Exit code 0 only when every tool returned
data. With ERP_BACKEND=sap_sandbox it asks for LIVE_CHECK_SAP_ORDER (default SO-000001, one of SAP's own
sandbox demo orders), since SO-104233 exists only in the seed backend.

Authorization (Milestone 4): cs-rep is a Central-region rep, and Summit Contractors (ACCT-1002, SO-104233)
is a Central account, so the rep reads all four. SAP sandbox orders belong to no Ridgeline account, so
their region is unknown and the policy shows them to CS leads only: with ERP_BACKEND=sap_sandbox the two
order-based tools are called as cs-lead. The `client` column says who made each call.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from ..connectors import ConnectorError
from .token import token_for

URL = os.environ.get("LIVE_CHECK_URL", "http://gateway:8000/mcp")
CLIENT = os.environ.get("LIVE_CHECK_CLIENT", "cs-rep")
LEAD_CLIENT = "cs-lead"
SYSTEM_OF = {"search_accounts": "crm", "get_sales_order": "erp", "track_shipment": "carrier",
             "get_open_invoices": "ar"}


def configured_backends(env: dict[str, str]) -> dict[str, str]:
    """Backend names only (no secrets needed): what .env selects for each system."""
    raw = {"crm": env.get("CRM_BACKEND", "seed"), "erp": env.get("ERP_BACKEND", "seed"),
           "carrier": env.get("CARRIER_BACKEND", "seed"), "ar": env.get("AR_BACKEND", "seed")}
    return {k: {"carrier": "carrier_api", "ar": "ar_api"}.get(k, v) if v == "http" else v for k, v in raw.items()}


def summarize(tool: str, data: dict[str, Any]) -> tuple[bool, str]:
    if tool == "search_accounts":
        m = data.get("matches") or []
        return bool(m), f"{len(m)} match(es): " + ", ".join(f"{x.get('name')} ({x.get('account_id')})" for x in m[:3])
    if tool == "get_sales_order":
        return bool(data.get("sales_order")), (f"{data.get('sales_order')} status={data.get('status')} "
                                               f"total={data.get('order_total')} {data.get('currency')}")
    if tool == "track_shipment":
        s = data.get("shipments") or []
        return "shipments" in data, f"{len(s)} shipment(s)" + (f", first status={s[0].get('status')}" if s else "")
    return "invoices" in data, f"{data.get('count')} open invoice(s), total_open={data.get('total_open')}"


def plan_calls(backends: dict[str, str], client: str, sap_order: str) -> list[tuple[str, dict[str, str], str]]:
    """(tool, arguments, client) per call. Records without a Ridgeline account are lead-only by policy."""
    sap = backends["erp"] == "sap_sandbox"
    order_client = LEAD_CLIENT if sap else client
    return [("search_accounts", {"query": "Summit"}, client),
            ("get_sales_order", {"sales_order": sap_order if sap else "SO-104233"}, order_client),
            ("track_shipment", {"sales_order": "SO-104233"}, order_client),
            ("get_open_invoices", {"account_id": "ACCT-1002"}, client)]


async def _call(token: str, tool: str, args: dict[str, str]) -> tuple[bool, str]:
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as http:
        async with Client(streamable_http_client(URL, http_client=http)) as client:
            result = await client.call_tool(tool, args)
    if result.is_error:
        return False, (result.content[0].text if result.content else "error")
    return summarize(tool, result.structured_content or {})


async def run() -> int:
    backends = configured_backends(dict(os.environ))
    calls = plan_calls(backends, CLIENT, os.environ.get("LIVE_CHECK_SAP_ORDER", "SO-000001"))
    tokens = {c: await asyncio.to_thread(token_for, c) for c in sorted({c for _, _, c in calls})}
    failures = 0
    print(f"authenticated as {', '.join(tokens)} (Keycloak client credentials)")
    print(f"{'tool':<18} {'client':<8} {'backend':<12} {'result':<7} summary   (backend = configured in .env)")
    for tool, args, who in calls:
        ok, summary = await _call(tokens[who], tool, args)
        failures += not ok
        print(f"{tool:<18} {who:<8} {backends[SYSTEM_OF[tool]]:<12} {'ok' if ok else 'FAIL':<7} {summary}")
    print("all tools returned data" if not failures else f"{failures} tool(s) did not return data")
    return 0 if not failures else 1


def main() -> int:
    try:
        return asyncio.run(run())
    except ConnectorError as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
