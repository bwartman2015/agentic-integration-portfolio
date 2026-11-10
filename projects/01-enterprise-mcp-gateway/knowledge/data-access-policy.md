# Data access policy (synthetic)

## Regions
Every customer account belongs to one sales region: East, Central or West. A customer service rep sees accounts in their own region only. Orders, shipments and invoices follow the region of the account they belong to. Customer service leads see every region.

## When access is denied
If a lookup is denied because the record is outside your region, do not retry it and do not look for the same data another way. Tell the rep that the record belongs to another region and hand the request to a CS lead, with the order or account number. A record that is not linked to an account with a known region can be read by a CS lead only.

## Permissions
Reading CRM, ERP, shipping and AR data each needs its own permission. Opening a case needs the case-write permission and a lead's approval.
