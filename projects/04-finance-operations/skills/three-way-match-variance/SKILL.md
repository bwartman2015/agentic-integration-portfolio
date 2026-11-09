---
name: three-way-match-variance
description: Resolve a 3-way match exception on a supplier invoice
trigger: 3-way, three way, match exception, price variance, quantity variance
---
# Steps
1. three_way_match(invoice_id) for line variances (source of truth).
2. check_duplicate_invoice.
3. Apply ap-tolerance-policy.md: release within tolerance; hold for quantity not received; request credit memo above price tolerance.
4. propose_ap_resolution with variances and policy citation.
# Notes
Never compute variances yourself if three_way_match returned them.
