---
name: dlq-transient-recovery
description: Recover a DLQ backlog caused by a temporarily unavailable endpoint
trigger: 503, timeout, unavailable, dlq growing, backlog
---
# Steps
1. get_metrics: confirm the target is healthy again (error rate at baseline).
2. replay_dlq in batches of <= 100, re-checking metrics between batches.
3. If errors return: pause_consumer and escalate.
# Notes
Never replay while the target is still failing.
