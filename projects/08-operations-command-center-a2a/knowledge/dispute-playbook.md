# Cross-functional dispute playbook (synthetic)

1. Finance AR: what was deducted, which invoice, which code, and is it within policy?
2. Shipping: what does proof of delivery say (cartons delivered vs shipped, notations)?
3. Orders: what did the 855/856 say we would ship? Did the ASN match the cartons?
4. Integration Ops: did any order/shipment/invoice flow fail for this order (wrong or missing ASN)?
5. SAP Order 360: delivery and billing status in the ERP.
Decision: deduction valid + carrier at fault -> accept deduction, file freight claim. Deduction invalid -> dispute with POD evidence. Conflicting evidence -> human review.
