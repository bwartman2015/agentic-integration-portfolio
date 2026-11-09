---
name: credit-hold-explainer
description: Explain why an order is blocked on credit and what happens next
trigger: credit hold, blocked, credit block, released
---
# Steps
1. get_order_360 for the order; read the credit check status.
2. get_credit_exposure: limit, exposure, overdue amounts.
3. Explain in plain English: exposure vs limit, which invoices are overdue.
4. Next step: the AR credit team releases blocks (cite the glossary). Never promise a release.
