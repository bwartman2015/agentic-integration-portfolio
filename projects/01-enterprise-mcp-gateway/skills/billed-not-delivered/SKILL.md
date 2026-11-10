---
name: billed-not-delivered
description: Handle a customer billed for goods they have not received
trigger: billed, invoice, charged, not received, not delivered
---
# Steps
1. search_accounts to get the account_id if only a name was given.
2. get_open_invoices for the account and find the invoice for the order.
3. track_shipment and compare cartons delivered (proof of delivery) to what was billed.
4. If billed > delivered, recommend create_case with reason "billed not delivered" and cite the billing dispute policy.

# Example input
"Pinecrest Hardware says they were invoiced for SO-104301 but only got half of it."
# Example output
"Pinecrest Hardware was invoiced $3,036.00 on INV-90377 for all of SO-104301, but proof of delivery for SHP-55107 shows only 5 of 10 cartons received on 2026-11-03. Per the billing dispute policy, open a case with reason "billed not delivered"; AR will hold dunning on INV-90377 for 15 days [customer-service-policy.md#0]. Opening the case needs a CS lead's approval."
# Notes
If a lookup is denied because the account is outside the rep's region, stop and escalate to a CS lead [data-access-policy.md#0]; do not retry.
Case creation is a write and needs approval. Quote amounts exactly as the tools return them; never compute a credit or refund amount yourself.
