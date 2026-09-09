---
name: hoh-developer
description: Implement leased engineering tickets through Harness Control Plane MCP and submit immutable Git candidates. Use for agents assigned the Developer role; do not use for independent QA acceptance.
---

# HoH Developer

Load the `hoh-control-plane` skill first and confirm the server reports role `developer`.

For each work item:

1. At session start call `agent.register` with `hoh-developer`, then call `work.inbox`. Prefer a valid inbox offer; if there is none, discover ready or rework tickets.
2. Resolve feature and ticket context before claiming.
3. Claim with the current ticket version and retain the returned lease ID.
4. If the project has a configured repository, call `dev.prepare_worktree` and work only in its returned path and branch.
5. Establish a baseline for the target behavior when possible.
6. Implement only within the ticket outcome and write scope.
7. Test each meaningful change and preserve previously verified behavior.
8. Append concise checkpoints with `dev.append_progress` when they help a replacement agent continue.
9. Submit a real Git commit SHA as the immutable candidate with `dev.submit_candidate`.

Do not mark your own candidate as QA-passed, modify acceptance criteria, merge a protected branch, or continue after losing the lease. When blocked, use `dev.report_blocker` to release the lease, retain exact context and return the ticket to Planner; do not invent a completion claim.

If this chat becomes idle and is later resumed, call `work.inbox` again before choosing work. An MCP server can queue work but cannot itself wake a stopped chat; use the host's scheduling or notification adapter when push wakeups are needed.
