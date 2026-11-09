---
name: schema-drift-fix
description: Handle payload schema drift breaking a transformer
trigger: schema, validation error, unexpected field, mapping error, keyerror
---
# Steps
1. sample_dlq: identify the new/changed field.
2. quarantine affected message ids.
3. open_fix_pr with the sample payload and stack trace as evidence.
4. After a human merges: replay quarantined messages.
