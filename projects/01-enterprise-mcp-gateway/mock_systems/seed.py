"""Seeded synthetic data for Ridgeline Supply Co. (fictional). Same seed -> same data.

Planted scenarios (fixed; used by the demo, tests and evals):
  SO-104233  Summit Contractors LLC: LTL shipment SHP-55012, delivery attempted but the dock was
             closed; new ETA is 3 business days past the promise. Billed on INV-90388.
  SO-104301  Pinecrest Hardware: only 5 of 10 cartons delivered on SHP-55107, but INV-90377
             bills the whole order.
  SO-104290  Big Box Outdoor: delivered in full; INV-90412 ($20,000) is open (the Project 08
             short-pay scenario).
Everything else is background data generated from the seed.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

AS_OF = date(2026, 11, 9)          # fixed "today" so every run is reproducible
DEFAULT_SEED = 7
CARRIER = "Granite Freight Lines"
DCS = {"RNO": "Reno NV", "CMH": "Columbus OH", "DFW": "Dallas TX"}
SLA_BUSINESS_DAYS = {"ground": 5, "ltl": 5, "expedited": 2}
SKUS: dict[str, tuple[str, float]] = {
    "HV-200": ("Hi-vis safety vest, class 2", 18.50),
    "HH-110": ("Hard hat, type I", 24.00),
    "GL-330": ("Cut-resistant gloves, 12 pairs", 46.00),
    "TP-800": ("Trail tarp 10x12", 39.50),
    "HL-450": ("LED headlamp", 22.75),
    "FA-120": ("First aid kit, 50 person", 64.00),
}
_NAME_A = ["Alder", "Basalt", "Cobble", "Driftwood", "Elkhorn", "Fernhill", "Gravelpit", "Hollowell"]
_NAME_B = ["Builders", "Outfitters", "Contracting", "Supply House", "Hardware Co."]


def add_business_days(start: date, days: int) -> date:
    d = start
    while days > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


def business_days_between(start: date, end: date) -> int:
    """Business days after `start` up to and including `end` (0 when end <= start)."""
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


@dataclass
class Dataset:
    seed: int
    accounts: dict[str, dict[str, Any]]
    orders: dict[str, dict[str, Any]]
    shipments: dict[str, dict[str, Any]]
    invoices: dict[str, dict[str, Any]]


def _lines(items: list[tuple[str, int]]) -> tuple[list[dict[str, Any]], float]:
    lines = []
    for n, (sku, qty) in enumerate(items, start=1):
        desc, price = SKUS[sku]
        lines.append({"line": n * 10, "sku": sku, "description": desc, "qty": qty,
                      "unit_price": price, "amount": round(qty * price, 2)})
    return lines, round(sum(l["amount"] for l in lines), 2)


def _account(ds: Dataset, account_id: str, name: str, aliases: list[str], kind: str, region: str,
             contact_role: str, contact_email: str) -> None:
    ds.accounts[account_id] = {"account_id": account_id, "name": name, "aliases": aliases, "type": kind,
                               "region": region,
                               "primary_contact": {"role": contact_role, "email": contact_email}}


def _order(ds: Dataset, so: str, account_id: str, po: str, order_date: date, dc: str, service: str,
           items: list[tuple[str, int]], status: str) -> dict[str, Any]:
    lines, total = _lines(items)
    order = {"sales_order": so, "account_id": account_id, "po_number": po, "order_date": order_date.isoformat(),
             "ship_from": DCS[dc], "service": service, "lines": lines, "order_total": total, "currency": "USD",
             "status": status, "promised_delivery": None, "shipments": [], "invoices": []}
    ds.orders[so] = order
    return order


def _shipment(ds: Dataset, order: dict[str, Any], shp: str, ship_date: date, destination: str, cartons: int,
              events: list[dict[str, str]], status: str, eta: date | None,
              pod: dict[str, Any] | None) -> None:
    promised = add_business_days(ship_date, SLA_BUSINESS_DAYS[order["service"]])
    order["promised_delivery"] = promised.isoformat()
    order["shipments"].append(shp)
    ds.shipments[shp] = {"shipment_id": shp, "sales_order": order["sales_order"], "carrier": CARRIER,
                         "service": order["service"], "origin": order["ship_from"], "destination": destination,
                         "ship_date": ship_date.isoformat(), "promised_delivery": promised.isoformat(),
                         "eta": eta.isoformat() if eta else None, "status": status, "cartons_shipped": cartons,
                         "events": events, "proof_of_delivery": pod}


def _invoice(ds: Dataset, order: dict[str, Any], inv: str, issue: date, status: str) -> None:
    order["invoices"].append(inv)
    ds.invoices[inv] = {"invoice": inv, "account_id": order["account_id"], "sales_order": order["sales_order"],
                        "issue_date": issue.isoformat(), "due_date": (issue + timedelta(days=30)).isoformat(),
                        "amount": order["order_total"], "currency": "USD",
                        "billed_qty": sum(l["qty"] for l in order["lines"]), "status": status}


def _ev(day: str, code: str, description: str, location: str) -> dict[str, str]:
    return {"date": day, "code": code, "description": description, "location": location}


def _plant(ds: Dataset) -> None:
    _account(ds, "ACCT-1001", "Big Box Outdoor", ["BBO"], "Retail (EDI)", "East",
             "Accounts payable", "ap.bbo@example.com")
    _account(ds, "ACCT-1002", "Summit Contractors LLC", ["Summit Contractors"], "Contractor", "Central",
             "Purchasing", "purchasing.summit@example.com")
    _account(ds, "ACCT-1003", "Pinecrest Hardware", ["Pinecrest"], "Regional retailer", "West",
             "Store orders", "orders.pinecrest@example.com")

    # Late: delivery attempted, dock closed, new ETA 3 business days past the promise.
    o = _order(ds, "SO-104233", "ACCT-1002", "PO-7104", date(2026, 10, 28), "CMH", "ltl",
               [("HH-110", 120), ("HV-200", 160), ("GL-330", 55)], "Shipped")
    _shipment(ds, o, "SHP-55012", date(2026, 10, 29), "Peoria IL", 12, [
        _ev("2026-10-29", "PICKED_UP", "Picked up at Ridgeline DC", "Columbus OH"),
        _ev("2026-11-02", "IN_TRANSIT", "Arrived at terminal", "Indianapolis IN"),
        _ev("2026-11-05", "OUT_FOR_DELIVERY", "Out for delivery", "Peoria IL"),
        _ev("2026-11-05", "DELIVERY_ATTEMPTED", "Delivery attempted: consignee dock closed", "Peoria IL"),
    ], "EXCEPTION", date(2026, 11, 10), None)
    _invoice(ds, o, "INV-90388", date(2026, 10, 29), "open")

    # Short: 5 of 10 cartons delivered, whole order billed.
    o = _order(ds, "SO-104301", "ACCT-1003", "PO-7215", date(2026, 10, 27), "RNO", "ground",
               [("TP-800", 40), ("HL-450", 64)], "Delivered")
    _shipment(ds, o, "SHP-55107", date(2026, 10, 28), "Bend OR", 10, [
        _ev("2026-10-28", "PICKED_UP", "Picked up at Ridgeline DC", "Reno NV"),
        _ev("2026-11-03", "DELIVERED", "Delivered; driver noted shortage", "Bend OR"),
    ], "DELIVERED", None, {"delivered_at": "2026-11-03T14:12", "signed_by": "Receiving clerk",
                           "cartons_delivered": 5, "cartons_shipped": 10,
                           "notes": "5 of 10 cartons received; shortage noted on delivery receipt"})
    _invoice(ds, o, "INV-90377", date(2026, 10, 28), "open")

    # Delivered in full; open invoice later short-paid (Project 08).
    o = _order(ds, "SO-104290", "ACCT-1001", "PO-7190", date(2026, 10, 20), "CMH", "ground",
               [("HH-110", 500), ("FA-120", 125)], "Delivered")
    _shipment(ds, o, "SHP-54980", date(2026, 10, 21), "BBO DC 14, Indianapolis IN", 40, [
        _ev("2026-10-21", "PICKED_UP", "Picked up at Ridgeline DC", "Columbus OH"),
        _ev("2026-10-27", "DELIVERED", "Delivered", "Indianapolis IN"),
    ], "DELIVERED", None, {"delivered_at": "2026-10-27T09:40", "signed_by": "BBO receiving",
                           "cartons_delivered": 40, "cartons_shipped": 40, "notes": ""})
    _invoice(ds, o, "INV-90412", date(2026, 10, 21), "open")


def _generate(ds: Dataset, rng: random.Random) -> None:
    names = rng.sample([f"{a} {b}" for a in _NAME_A for b in _NAME_B], 5)
    for i, name in enumerate(names, start=4):
        slug = "".join(c for c in name.lower() if c.isalnum())
        _account(ds, f"ACCT-{1000 + i}", name, [], rng.choice(["Contractor", "Regional retailer", "Web store"]),
                 rng.choice(["East", "Central", "West"]), "Purchasing", f"purchasing.{slug}@example.com")
    so_numbers = rng.sample(range(104400, 104900), 18)
    po_numbers = rng.sample(range(7300, 8000), 18)
    shp_seq, inv_seq = 56001, 90501
    for so_n, po_n in zip(so_numbers, po_numbers):
        account_id = rng.choice(sorted(ds.accounts))
        order_date = AS_OF - timedelta(days=rng.randint(1, 25))
        age = business_days_between(order_date, AS_OF)
        items = [(sku, rng.randint(5, 200)) for sku in rng.sample(sorted(SKUS), rng.randint(1, 3))]
        status = "Open" if age <= 1 else "Picking" if age == 2 else "Shipped"
        o = _order(ds, f"SO-{so_n}", account_id, f"PO-{po_n}", order_date, rng.choice(sorted(DCS)),
                   rng.choice(["ground", "ltl", "expedited"]), items, status)
        if status != "Shipped":
            continue
        ship = add_business_days(order_date, 1)
        promised = add_business_days(ship, SLA_BUSINESS_DAYS[o["service"]])
        cartons = max(1, sum(q for _, q in items) // 25)
        shp = f"SHP-{shp_seq}"
        shp_seq += 1
        events = [_ev(ship.isoformat(), "PICKED_UP", "Picked up at Ridgeline DC", o["ship_from"])]
        if promised <= AS_OF:
            o["status"] = "Delivered"
            events.append(_ev(promised.isoformat(), "DELIVERED", "Delivered", "Customer site"))
            pod = {"delivered_at": f"{promised.isoformat()}T11:00", "signed_by": "Customer receiving",
                   "cartons_delivered": cartons, "cartons_shipped": cartons, "notes": ""}
            _shipment(ds, o, shp, ship, "Customer site", cartons, events, "DELIVERED", None, pod)
        else:
            events.append(_ev(add_business_days(ship, 1).isoformat(), "IN_TRANSIT", "In transit", "Carrier terminal"))
            _shipment(ds, o, shp, ship, "Customer site", cartons, events, "IN_TRANSIT", promised, None)
        _invoice(ds, o, f"INV-{inv_seq}", ship, "paid" if o["status"] == "Delivered" and rng.random() < 0.5 else "open")
        inv_seq += 1


def build_dataset(seed: int = DEFAULT_SEED) -> Dataset:
    ds = Dataset(seed, {}, {}, {}, {})
    _plant(ds)
    _generate(ds, random.Random(seed))
    return ds
