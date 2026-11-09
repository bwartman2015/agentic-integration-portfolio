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
"SO-104233 shipped Tue via Granite Freight (SHP-55012). Tracking shows an exception: delivery attempted, dock closed. New ETA is Thu, 2 days past the 5-day ground SLA. Next step: open a case 'late delivery' and notify Logistics."
# Notes
Never promise a refund or credit; that needs a human.
