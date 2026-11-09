---
name: api-test-generation
description: Generate tests for changed API endpoints
trigger: test, coverage, endpoint, openapi
---
# Steps
1. get_spec_diff; list changed operations.
2. Per operation: happy path, 401/403, 400 validation, 404, pagination bounds, idempotent retry.
3. write_test_file under tests/ai_generated/; run_tests; fix generated tests (never the product code).
