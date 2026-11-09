"""In-memory mock CRM, ERP, carrier and AR systems over the seeded dataset.

These stand in for Salesforce, the SAP sandbox, the carrier API and the AR API. The gateway's
connectors arrive in Milestone 2 (the carrier and AR connectors can reuse these mocks), and the copilot
switches from these in-process mocks to the gateway's MCP tools in Milestone 6.
Every read returns a copy, so callers cannot mutate the data.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date
from typing import Any

from .seed import DEFAULT_SEED, Dataset, build_dataset, business_days_between

_CLOSED_ORDER_STATUSES = {"Delivered", "Cancelled"}


class MockCRM:
    def __init__(self, ds: Dataset):
        self._ds = ds
        self.cases: list[dict[str, Any]] = []

    @staticmethod
    def _public(a: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy({k: a[k] for k in ("account_id", "name", "type", "region", "primary_contact")})

    def find_accounts(self, query: str) -> list[dict[str, Any]]:
        q = " ".join(query.lower().split())
        if not q:
            raise ValueError("empty account query")
        hits = []
        for a in self._ds.accounts.values():
            forms = [a["name"].lower(), a["account_id"].lower(), *(x.lower() for x in a["aliases"])]
            if any(q in f for f in forms) or a["name"].lower() in q:
                hits.append(self._public(a))
        return hits

    def account(self, account_id: str) -> dict[str, Any]:
        if account_id not in self._ds.accounts:
            raise KeyError(f"no account {account_id}")
        return self._public(self._ds.accounts[account_id])

    def open_case(self, account_id: str, subject: str, summary: str) -> dict[str, Any]:
        self.account(account_id)
        case = {"case_id": f"CASE-{10001 + len(self.cases)}", "account_id": account_id, "subject": subject,
                "summary": summary, "status": "New"}
        self.cases.append(case)
        return copy.deepcopy(case)


class MockERP:
    def __init__(self, ds: Dataset):
        self._ds = ds

    def order(self, sales_order: str) -> dict[str, Any]:
        o = self._ds.orders.get(sales_order)
        if o is None:
            raise KeyError(f"no sales order {sales_order}")
        out = copy.deepcopy(o)
        out["account_name"] = self._ds.accounts[o["account_id"]]["name"]
        out["delivery_status"] = [{"shipment_id": s, "status": self._ds.shipments[s]["status"]}
                                  for s in o["shipments"]]
        return out

    def orders_for(self, account_id: str, open_only: bool = True) -> list[dict[str, Any]]:
        return [{"sales_order": o["sales_order"], "status": o["status"], "order_date": o["order_date"],
                 "promised_delivery": o["promised_delivery"]}
                for o in sorted(self._ds.orders.values(), key=lambda o: o["order_date"])
                if o["account_id"] == account_id and not (open_only and o["status"] in _CLOSED_ORDER_STATUSES)]


class MockCarrier:
    def __init__(self, ds: Dataset):
        self._ds = ds

    def tracking(self, sales_order: str) -> dict[str, Any]:
        o = self._ds.orders.get(sales_order)
        if o is None:
            raise KeyError(f"no sales order {sales_order}")
        shipments = []
        for sid in o["shipments"]:
            s = copy.deepcopy(self._ds.shipments[sid])
            pod = s["proof_of_delivery"]
            reference = (pod["delivered_at"][:10] if pod else s["eta"]) or s["promised_delivery"]
            s["business_days_late"] = business_days_between(date.fromisoformat(s["promised_delivery"]),
                                                             date.fromisoformat(reference))
            shipments.append(s)
        return {"sales_order": sales_order, "shipments": shipments,
                **({} if shipments else {"note": "not shipped yet"})}


class MockAR:
    def __init__(self, ds: Dataset):
        self._ds = ds

    def open_items(self, account_id: str) -> list[dict[str, Any]]:
        if account_id not in self._ds.accounts:
            raise KeyError(f"no account {account_id}")
        return [copy.deepcopy(i) for i in sorted(self._ds.invoices.values(), key=lambda i: i["issue_date"])
                if i["account_id"] == account_id and i["status"] == "open"]


@dataclass
class Systems:
    dataset: Dataset
    crm: MockCRM
    erp: MockERP
    carrier: MockCarrier
    ar: MockAR


_systems: Systems | None = None


def reset_systems(seed: int = DEFAULT_SEED) -> Systems:
    """Rebuild every mock system from the seed (tests and evals call this for isolation)."""
    global _systems
    ds = build_dataset(seed)
    _systems = Systems(ds, MockCRM(ds), MockERP(ds), MockCarrier(ds), MockAR(ds))
    return _systems


def get_systems() -> Systems:
    return _systems or reset_systems()
