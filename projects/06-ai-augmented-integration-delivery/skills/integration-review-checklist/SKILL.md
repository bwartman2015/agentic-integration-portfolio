---
name: integration-review-checklist
description: Review an integration code change against Ridgeline API standards
trigger: review, pull request, pr, diff
---
# Checklist
- [ ] Idempotency on writes (Idempotency-Key)
- [ ] Timeouts and bounded retries on outbound calls
- [ ] No secrets or PII in logs
- [ ] Error mapping to the standard error model
- [ ] Pagination on list endpoints
- [ ] Backward compatible, or versioned
# Output
One comment per finding: file, line, checklist item, suggested fix.
