---
name: deduction-investigation
description: Investigate a retail deduction across finance, shipping and orders
trigger: deduction, short-paid, short paid, short ship, chargeback
---
# Steps
1. Delegate to the finance AR agent: deduction details and policy fit.
2. Delegate to the shipping agent: POD and carton count for the order.
3. Delegate to the orders agent: what the ASN said.
4. If findings disagree, ask integration ops whether the ASN flow failed.
5. record_resolution with each specialist's finding and trace id as evidence.
# Example output
"Deduction valid: POD shows 2 of 3 cartons; ASN said 3. Carrier at fault (POD notation). Recommend: accept the $4,860 deduction, file a Granite Freight claim."
