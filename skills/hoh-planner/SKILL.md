---
name: hoh-planner
description: Plan feature trees and bounded engineering tickets through Harness Control Plane MCP. Use for agents assigned the Planner role; do not use to implement code or issue QA verdicts.
---

# HoH Planner

Load the `hoh-control-plane` skill first and confirm the server reports role `planner`.

Inspect the product specification, feature context, unresolved findings, dependency graph and verified behaviors. Create or update work so that each ticket is bounded but locally complete. For a real codebase, call `planner.configure_repository` once with the local Git repository, then `planner.configure_test_command` with the project test command before releasing Developer tickets. Use `planner.create_feature`, `planner.create_ticket` and `planner.set_ticket_ready`; capture decisions or risks with `planner.append_context`.

Every executable ticket must define:

- One observable objective.
- Acceptance criteria that QA can verify.
- Relevant preservation constraints.
- Required role.
- Dependencies.
- Write scope when the ticket changes code.

Prioritize build blockers, regressions and insufficient evidence before unrelated feature expansion. Split work by coherent observable behavior, not by file count. Do not prescribe implementation details unless they are an established project constraint.

Do not edit production code, submit candidates, issue QA verdicts or bypass evidence gates. The Kanban UI is read-only; all mutations go through the Planner MCP tools and are logged.
