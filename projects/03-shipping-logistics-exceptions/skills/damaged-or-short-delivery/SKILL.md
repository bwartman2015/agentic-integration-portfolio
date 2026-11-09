---
name: damaged-or-short-delivery
description: Handle damaged or short deliveries and open a freight claim
trigger: damaged, damage, short, missing cartons, refused
---
# Steps
1. get_proof_of_delivery: compare cartons delivered vs shipped and read delivery notations.
2. If short or damaged with a notation: open_freight_claim with the carton count and value as evidence.
3. notify_customer of the replacement plan.
4. Flag the order so AR expects a possible deduction (Project 04).
# Notes
Claims need approval. No notation on the POD means a weaker claim; say so.
