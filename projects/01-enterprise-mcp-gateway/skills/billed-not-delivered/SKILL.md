---
name: billed-not-delivered
description: Handle a customer billed for goods they have not received
trigger: billed, invoice, charged, not received, not delivered
---
# Steps
1. get_open_invoices for the account and find the invoice for the order.
2. track_shipment and compare quantity delivered (POD cartons) to quantity billed.
3. If billed > delivered, recommend create_case with reason "billed not delivered" and cite the billing dispute policy.
# Notes
Case creation is a write and needs approval.
