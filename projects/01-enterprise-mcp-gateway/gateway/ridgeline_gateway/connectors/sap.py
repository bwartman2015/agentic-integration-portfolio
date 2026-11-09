"""SAP ERP connector: SAP Business Accelerator Hub sandbox, `API_SALES_ORDER_SRV` (OData v2, APIKey header).

The sandbox is SAP's own read-only demo data, so Ridgeline's planted scenarios (SO-104233, ...) are not
in it; those run on the seed backend. Order numbers map both ways as `SO-` + 6 digits, zero-padded:
SAP "1" <-> SO-000001. SAP orders above 999999 cannot be addressed through this tool. Orders keep the
seed key set; SAP-only fields sit under `source`. Note: a wrong SAP_BASE_URL path on api.sap.com also
answers 404, which surfaces as "not found" (see docs/live-sandbox-setup.md troubleshooting).
Deterministic code owns the mapping; amounts are passed through as numbers, never recomputed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from ..config import Secret
from .base import ConnectorError, HttpConnector

_ODATA_DATE = re.compile(r"^/Date\((-?\d+)(?:[+-]\d{4})?\)/$")
PROCESS_STATUS = {"A": "Open", "B": "In process", "C": "Completed", "": "Not relevant"}
DELIVERY_STATUS = {"A": "Not delivered", "B": "Partially delivered", "C": "Fully delivered", "": "Not relevant"}


def to_sap_id(sales_order: str) -> str:
    return str(int(sales_order.removeprefix("SO-")))


def to_ridgeline_id(sap_id: str) -> str | None:
    return f"SO-{int(sap_id):06d}" if sap_id.isdigit() and int(sap_id) <= 999_999 else None


def odata_date(value: Any) -> str | None:
    m = _ODATA_DATE.match(value) if isinstance(value, str) else None
    if not m:
        return None
    return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date().isoformat()


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _unit_price(item: dict[str, Any]) -> float | None:
    """NetPriceAmount is the price per NetPriceQuantity units; only a per-1 price is a unit price."""
    return _num(item.get("NetPriceAmount")) if _num(item.get("NetPriceQuantity")) == 1.0 else None


def to_order(d: dict[str, Any]) -> dict[str, Any]:
    """Same key set as a seed order (what the copilot's prompts and verifier read), `None` where SAP has
    no equivalent, plus one `source` object for the SAP-only fields."""
    items = (d.get("to_Item") or {}).get("results") or []
    return {
        "sales_order": to_ridgeline_id(str(d.get("SalesOrder", ""))),
        "account_id": None,               # SAP sold-to parties are not Ridgeline CRM accounts
        "account_name": None,
        "po_number": d.get("PurchaseOrderByCustomer") or None,
        "order_date": odata_date(d.get("SalesOrderDate")),
        "ship_from": None,
        "service": None,
        "lines": [{"line": int(i["SalesOrderItem"]) if str(i.get("SalesOrderItem", "")).isdigit() else i.get("SalesOrderItem"),
                   "sku": i.get("Material"), "description": i.get("SalesOrderItemText"),
                   "qty": _num(i.get("RequestedQuantity")), "unit_price": _unit_price(i),
                   "amount": _num(i.get("NetAmount"))} for i in items],
        "order_total": _num(d.get("TotalNetAmount")),
        "currency": d.get("TransactionCurrency"),
        "status": PROCESS_STATUS.get(d.get("OverallSDProcessStatus") or "", d.get("OverallSDProcessStatus")),
        "promised_delivery": odata_date(d.get("RequestedDeliveryDate")),
        "shipments": [],
        "invoices": [],
        "delivery_status": [],
        "source": {"system": "sap_sandbox", "sales_order": d.get("SalesOrder"), "sold_to_party": d.get("SoldToParty"),
                   "overall_delivery_status": DELIVERY_STATUS.get(d.get("OverallDeliveryStatus") or "",
                                                                  d.get("OverallDeliveryStatus")),
                   "line_units": [i.get("RequestedQuantityUnit") for i in items]},
    }


@dataclass
class SapSandboxErp:
    http: HttpConnector
    api_key: Secret

    def _headers(self) -> dict[str, str]:
        return {"APIKey": self.api_key.reveal(), "Accept": "application/json"}

    async def fetch_order(self, sales_order: str) -> dict[str, Any] | None:
        sap_id = quote(to_sap_id(sales_order), safe="")
        data = await self.http.json("GET", f"{self.http.base_url}/A_SalesOrder('{sap_id}')",
                                    params={"$expand": "to_Item", "$format": "json"},
                                    headers=self._headers(), expect_404=True)
        if data is None:
            return None
        d = data.get("d") if isinstance(data, dict) else None
        if not isinstance(d, dict) or str(d.get("SalesOrder")) != to_sap_id(sales_order):
            raise ConnectorError("SAP returned an unexpected payload.")
        return to_order(d)

    async def sample_order_ids(self, top: int = 5) -> list[str]:
        """Admin helper for the live check: a few addressable order numbers from the sandbox."""
        data = await self.http.json("GET", f"{self.http.base_url}/A_SalesOrder",
                                    params={"$top": str(top), "$select": "SalesOrder", "$format": "json"},
                                    headers=self._headers())
        rows = ((data or {}).get("d") or {}).get("results") or []
        return [i for i in (to_ridgeline_id(str(r.get("SalesOrder", ""))) for r in rows) if i]
