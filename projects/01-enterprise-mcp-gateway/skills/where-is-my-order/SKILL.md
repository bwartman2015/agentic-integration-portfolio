---
name: where-is-my-order
description: Answer a customer's 'where is my order' question end to end
trigger: where is, not arrived, hasn't arrived, status of order, tracking
---
# Steps
1. Resolve the account with search_accounts if only a name was given.
2. get_sales_order to get the lines and delivery status.
3. track_shipment for each delivery; check proof of delivery and cartons delivered.
4. Compare promised vs actual delivery using the shipping SLA in the policy documents.
5. Answer with order status, shipment status, ETA, and the next step. Cite the policy if an SLA was missed.

# Example input
"Summit Contractors says SO-104233 hasn't arrived."
# Example output
"SO-104233 shipped 2026-10-29 via Granite Freight Lines (SHP-55012). Tracking shows an exception: delivery attempted 2026-11-05, dock closed. New ETA is 2026-11-10, 3 business days past the 5-business-day LTL SLA [customer-service-policy.md#0]. Next step: open a case 'late delivery' and notify Logistics [escalation-matrix.md#0]."
# Notes
Never promise a refund or credit; that needs a human.
If a lookup is denied because the order or account is outside the rep's region, stop: do not retry or try another tool for the same record. Escalate to a CS lead with the order number and cite the data access policy [data-access-policy.md#0].
