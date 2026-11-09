---
name: quantity-spike-triage
description: Triage a PO line far above the partner's normal order quantity
trigger: quantity spike, 10x, unusual quantity, anomaly
---
# Steps
1. get_order_history for partner + SKU; note the mean and max quantity.
2. get_available_to_promise across DCs.
3. If stock covers it and the partner has ordered similar volume before seasonally: recommend accept.
4. If stock is short: recommend accept-partial with the available quantity per DC.
5. Possible keying error (e.g. 1200 vs 120): recommend hold and confirm with the buyer.
