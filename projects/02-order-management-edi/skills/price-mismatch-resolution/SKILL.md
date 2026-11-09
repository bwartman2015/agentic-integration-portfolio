---
name: price-mismatch-resolution
description: Resolve a PO line whose price does not match the partner price agreement
trigger: price mismatch, price variance, wrong price
---
# Steps
1. get_partner_profile and read the price agreement for the SKU.
2. Compute the variance per unit and in total.
3. If within $0.01 per unit: recommend accept.
4. If the PO price is lower than the agreement: recommend hold and explain the expected deduction risk (cite retail-compliance-chargebacks.md).
5. request_order_decision with the recommendation and evidence.
# Notes
Never change the price yourself.
