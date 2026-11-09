---
name: short-pay-resolution
description: Explain and apply a customer short payment
trigger: short pay, short payment, underpaid, deduction
---
# Steps
1. get_remittance and match_remittance.
2. For each deduction code, apply ar-deduction-policy.md; SHORT needs proof-of-delivery evidence (ask the Shipping agent through the manager or Project 08).
3. Balance: applied + deductions + unapplied = remittance amount.
4. propose_cash_application.
# Example output
"$4,860 short on INV-90412 coded SHORT. POD shows 2 of 3 cartons delivered, so the deduction is valid. Propose a credit memo request to Logistics' freight claim."
