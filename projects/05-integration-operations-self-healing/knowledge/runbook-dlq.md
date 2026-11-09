# Runbook: dead-letter queue growth (synthetic)

1. Check the consumer's error traces. 503/timeouts from the target = transient: wait for health, then replay in batches of <= 100.
2. Schema/validation errors = mapping/code: quarantine affected messages, open a fix PR, replay after merge.
3. 401/403 = expired credential: do NOT retry (lockout risk). Page on-call; the secret rotation is human-only.
4. Unknown SKU/customer = reference data: route to the data steward queue.
