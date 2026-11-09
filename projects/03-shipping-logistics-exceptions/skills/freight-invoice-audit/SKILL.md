---
name: freight-invoice-audit
description: Audit LTL freight invoice lines against the carrier contract
trigger: freight invoice, freight bill, audit, overbilled, accessorial
---
# Steps
1. For each line: get_contract_rate for carrier, lane, class and weight.
2. Recompute: base x (1 - discount) + fuel (capped) + authorized accessorials.
3. Variance = billed - expected. Over $25 -> include in dispute_freight_invoice with the reason.
4. Unauthorized accessorials (e.g. inside delivery) are disputed in full.
