"""Call every gateway tool through MCP (the same path an agent uses) and report each tool's result with
its configured backend. The gateway's own log line for each call records the backend that actually
answered (`backend`, `upstream_status`): `docker compose logs gateway`.

    make live-check P=01

Runs inside the gateway container against http://127.0.0.1:8000/mcp. Exit code 0 only when every tool
returned data. For the SAP sandbox (SAP's demo orders, not Ridgeline's) it first asks the sandbox for
an existing order number, since SO-104233 exists only in the seed backend.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from mcp import Client

from ..config import ConfigError, load_settings
from ..connectors import ConnectorError, build_connectors
from ..connectors.sap import SapSandboxErp

URL = os.environ.get("LIVE_CHECK_URL", "http://127.0.0.1:8000/mcp")
SYSTEM_OF = {"search_accounts": "crm", "get_sales_order": "erp", "track_shipment": "carrier",
             "get_open_invoices": "ar"}


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


async def run() -> int:
    settings = load_settings()
    backends = {k: {"carrier": "carrier_api", "ar": "ar_api"}.get(k, v) if v == "http" else v
                for k, v in settings.backends().items()}
    order = "SO-104233"
    if settings.erp_backend == "sap_sandbox":
        erp = build_connectors(settings).erp
        assert isinstance(erp, SapSandboxErp)
        ids = await erp.sample_order_ids()
        if not ids:
            print("SAP sandbox returned no addressable sales orders", file=sys.stderr)
            return 1
        order = ids[0]
    calls = [("search_accounts", {"query": "Summit"}), ("get_sales_order", {"sales_order": order}),
             ("track_shipment", {"sales_order": "SO-104233"}), ("get_open_invoices", {"account_id": "ACCT-1002"})]
    failures = 0
    print(f"{'tool':<18} {'backend':<12} {'result':<7} summary   (backend = configured in .env)")
    async with Client(URL) as client:
        for tool, args in calls:
            result = await client.call_tool(tool, args)
            if result.is_error:
                ok, summary = False, (result.content[0].text if result.content else "error")
            else:
                ok, summary = summarize(tool, result.structured_content or {})
            failures += not ok
            print(f"{tool:<18} {backends[SYSTEM_OF[tool]]:<12} {'ok' if ok else 'FAIL':<7} {summary}")
    print("all tools returned data" if not failures else f"{failures} tool(s) did not return data")
    return 0 if not failures else 1


def main() -> int:
    try:
        return asyncio.run(run())
    except (ConfigError, ConnectorError) as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
