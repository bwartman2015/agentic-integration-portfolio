# AR deduction and short-pay policy (synthetic)

| Code | Meaning | Treatment |
|------|---------|-----------|
| SHORT | Customer claims short shipment | Validate against proof of delivery (Logistics). If POD shows full delivery, dispute. |
| PRICE | Price discrepancy | Compare invoice price to PO/agreement. Valid -> credit memo; invalid -> dispute. |
| COMPL | Retail compliance fine (late ASN, missed appointment) | Validate with Order Management / Logistics evidence. |
| SMALL | Short pay under $25 | Auto write-off proposal (still needs clerk posting). |

Unapplied cash older than 5 days must be escalated to the AR supervisor.
