"""STATE (Memory & State): the current system snapshot, re-read before every THINK step.

The current case: account, open orders and their latest status for this conversation (refreshed
each iteration). The snapshot is filtered by the principal's scopes and, since Milestone 4, by the
policy's region rule (`guardrails.may_see`), so state never shows a caller data that its tools could
not fetch (least privilege also applies to context injection). A record the caller may not see appears
only as "access denied", never with its contents.
Milestone 0 reads the in-process mock systems directly, with the checks above.
TODO(Milestone 6): read state through the gateway with the caller's token, so context reads are also
policy-checked, rate-limited and audited like tool calls.
"""
from __future__ import annotations

from typing import Any

from agentcore import Percept
from mock_systems import AS_OF, get_systems

from .guardrails import may_see

DENIED = {"error": "access denied: outside your region"}


class CurrentStateProvider:
    def snapshot(self, percept: Percept) -> dict[str, Any]:
        self._seen: dict[tuple[str, str | None], bool] = {}     # one policy decision per (tool, region)
        scopes = percept.principal.scopes
        systems = get_systems()
        snap: dict[str, Any] = {"as_of": AS_OF.isoformat()}
        not_visible = [s for s in ("crm.read", "erp.read", "shipping.read", "ar.read") if s not in scopes]
        if not_visible:
            snap["not_visible_without_scope"] = not_visible

        account_ids: list[str] = []
        if "crm.read" in scopes:
            accounts = []
            for name in percept.entities.get("account", []):
                hits = systems.crm.find_accounts(name)
                if hits and not self._may_see(percept, "search_accounts", hits[0].get("region") or None):
                    accounts.append({"name": name, **DENIED})
                    continue
                accounts.append(hits[0] if hits else {"name": name, "error": "not found"})
                account_ids += [h["account_id"] for h in hits[:1]]
            if accounts:
                snap["accounts"] = accounts

        orders = percept.entities.get("sales_order", [])
        if "erp.read" in scopes:
            if orders:
                snap["orders"] = {so: self._order_status(systems, so)
                                                    if self._order_visible(systems, percept, "get_sales_order", so) else dict(DENIED)
                                  for so in orders}
            if account_ids:
                snap["open_orders"] = {a: systems.erp.orders_for(a) for a in account_ids}
        if "shipping.read" in scopes and orders:
            snap["shipments"] = {so: self._shipment_status(systems, so)
                                 if self._order_visible(systems, percept, "track_shipment", so) else dict(DENIED)
                                 for so in orders}
        if "ar.read" in scopes and account_ids:
            snap["open_invoices"] = {}
            for a in account_ids:
                items = systems.ar.open_items(a)
                snap["open_invoices"][a] = {"count": len(items),
                                            "total_open": round(sum(i["amount"] for i in items), 2)}
        return snap

    def _may_see(self, percept: Percept, tool_name: str, region: str | None) -> bool:
        key = (tool_name, region)
        if key not in self._seen:
            self._seen[key] = may_see(percept.principal, tool_name, region)
        return self._seen[key]

    def _order_visible(self, systems: Any, percept: Percept, tool_name: str, so: str) -> bool:
        """Region rule for an order's records. An order that does not exist has nothing to hide."""
        order = systems.dataset.orders.get(so)
        if order is None:
            return True
        region = (systems.dataset.accounts.get(order.get("account_id")) or {}).get("region") or None
        return self._may_see(percept, tool_name, region)

    @staticmethod
    def _order_status(systems: Any, so: str) -> dict[str, Any]:
        try:
            o = systems.erp.order(so)
        except KeyError:
            return {"error": "not found"}
        return {"status": o["status"], "account_id": o["account_id"], "promised_delivery": o["promised_delivery"]}

    @staticmethod
    def _shipment_status(systems: Any, so: str) -> list[dict[str, Any]] | dict[str, str]:
        try:
            t = systems.carrier.tracking(so)
        except KeyError:
            return {"error": "not found"}
        return [{"shipment_id": s["shipment_id"], "status": s["status"], "eta": s["eta"],
                 "business_days_late": s["business_days_late"]} for s in t["shipments"]]


def get_state() -> CurrentStateProvider:
    return CurrentStateProvider()
