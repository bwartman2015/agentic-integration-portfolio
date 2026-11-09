---
name: x12-850-mapping
description: Draft and test an X12 850 mapping for a new retail partner
trigger: onboard, new partner, mapping, implementation guide
---
# Steps
1. Read retrieved sections of the partner's implementation guide.
2. parse_x12 on each sample file.
3. Draft YAML mapping BEG, N1 loops, PO1 lines, CTT to the canonical model; draft_mapping.
4. run_mapping_tests; fix failing fields; repeat until pass_rate >= 0.90.
# Notes
Activation of a mapping always needs human review.
