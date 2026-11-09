"""Seed backends: the deterministic, offline Ridgeline dataset (`mock_systems/`). Used by tests, the
demo scenarios (SO-104233 and friends) and any system whose live backend is not configured."""
from __future__ import annotations

from typing import Any

from mock_systems import get_systems

from .base import note_backend


class SeedCrm:
    async def find_accounts(self, query: str) -> list[dict[str, Any]]:
        note_backend("seed")
        return get_systems().crm.find_accounts(query)


class SeedErp:
    async def fetch_order(self, sales_order: str) -> dict[str, Any] | None:
        note_backend("seed")
        systems = get_systems()
        if sales_order not in systems.dataset.orders:
            return None
        return systems.erp.order(sales_order)


class SeedCarrier:
    async def fetch_tracking(self, sales_order: str) -> dict[str, Any] | None:
        note_backend("seed")
        systems = get_systems()
        if sales_order not in systems.dataset.orders:
            return None
        return systems.carrier.tracking(sales_order)


class SeedAr:
    async def fetch_open_items(self, account_id: str) -> list[dict[str, Any]] | None:
        note_backend("seed")
        systems = get_systems()
        if account_id not in systems.dataset.accounts:
            return None
        return systems.ar.open_items(account_id)
