# Integration flow catalog (synthetic)

| Flow | Producer -> Consumer | Business impact if down |
|------|----------------------|-------------------------|
| order-events | EDI gateway -> ERP adapter | Retail POs not acknowledged: $250/PO chargebacks after 24h |
| shipment-events | TMS -> ERP + EDI 856 | ASNs late: 2% compliance fines |
| invoice-events | ERP -> EDI 810 + AR | Invoices not sent: cash delayed |
