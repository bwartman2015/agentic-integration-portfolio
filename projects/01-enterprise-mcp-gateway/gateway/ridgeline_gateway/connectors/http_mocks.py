"""Carrier and AR connectors over HTTP (the mock carrier and AR APIs in `mock_apis/`, Docker Compose)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ..config import Secret
from .base import ConnectorError, HttpConnector


@dataclass
class CarrierHttp:
    http: HttpConnector
    api_key: Secret

    async def fetch_tracking(self, sales_order: str) -> dict[str, Any] | None:
        data = await self.http.json("GET", f"{self.http.base_url}/v1/orders/{quote(sales_order, safe='')}/tracking",
                                    headers={"X-API-Key": self.api_key.reveal(), "Accept": "application/json"},
                                    expect_404=True)
        if data is None:
            return None
        if not isinstance(data, dict) or data.get("sales_order") != sales_order or not isinstance(data.get("shipments"), list):
            raise ConnectorError("Carrier API returned an unexpected payload.")
        return data


@dataclass
class ArHttp:
    http: HttpConnector
    token: Secret

    async def fetch_open_items(self, account_id: str) -> list[dict[str, Any]] | None:
        data = await self.http.json("GET", f"{self.http.base_url}/v1/accounts/{quote(account_id, safe='')}/open-items",
                                    headers={"Authorization": f"Bearer {self.token.reveal()}",
                                             "Accept": "application/json"},
                                    expect_404=True)
        if data is None:
            return None
        if not isinstance(data, dict) or data.get("account_id") != account_id or not isinstance(data.get("invoices"), list):
            raise ConnectorError("AR API returned an unexpected payload.")
        for item in data["invoices"]:
            amount = item.get("amount") if isinstance(item, dict) else None
            if (not isinstance(item, dict) or item.get("account_id") != account_id
                    or isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount)):
                raise ConnectorError("AR API returned an unexpected payload.")
        return data["invoices"]
