---
name: late-delivery-playbook
description: Handle a shipment that will miss its promised delivery date
trigger: late, delay, missed delivery, eta, exception
---
# Steps
1. track_shipment and get_shipment_order: compute days late vs promised date and vs carrier SLA.
2. If the customer requires appointments, reroute_or_reschedule to the next available appointment.
3. If more than 1 business day late: notify_customer with the new ETA (customer-delivery-requirements.md).
4. If late beyond the contract SLA: note eligibility for the 50% freight refund for the freight audit agent.
# Example output
"SHP-55012 is 2 days late (dock closed). Rescheduled to Thu 8 am appointment, customer notified, refund-eligible."
