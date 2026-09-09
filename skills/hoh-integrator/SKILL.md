---
name: hoh-integrator
description: Integrate QA-verified candidates while preserving artifact identity and mainline health through Harness Control Plane MCP. Use for agents assigned the Integrator role; do not bypass QA or policy gates.
---

# HoH Integrator

Load the `hoh-control-plane` skill first and confirm the server reports role `integrator`.

Only select candidates that the server reports as verified or merge-ready. Check base drift, file conflicts, protected-branch policy and required regression gates before merging.

If rebase, conflict resolution or another operation changes the candidate artifact, record the new identity and send it through the required QA gates again. Never reuse evidence from a different commit as proof for the changed artifact.

Record integration evidence and final mainline identity. Requeue the ticket with a specific conflict or regression reason when integration cannot safely complete.
