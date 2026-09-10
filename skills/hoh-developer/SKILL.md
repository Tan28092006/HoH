---
name: hoh-developer
description: Implement leased engineering tickets through Harness Control Plane MCP and submit immutable Git candidates. Use for agents assigned the Developer role; do not use for independent QA acceptance.
---

# HoH Developer

Load the `hoh-control-plane` skill first and confirm the server reports role `developer`.

For each work item:

1. At session start call `agent.register` with `hoh-developer`, then call `work.inbox`. Prefer a valid inbox offer; if there is none, discover ready or rework tickets.
2. Resolve feature and ticket context before claiming.
3. **Resume check.** If the ticket is already `DEV_IN_PROGRESS` and its active lease (from `work.inbox` active_leases, or the lease implied by ticket context) already belongs to this agent id, this is a resumed session, not a new claim:
   - Do not call the claim tool again and do not call `dev.prepare_worktree` or create any other worktree.
   - Re-read `ticket.get_context` to refresh state, then call `work.heartbeat` on the existing lease id to renew it.
   - Reuse the worktree path/branch/base commit already on record for that lease as-is; verify it (clean tree, expected HEAD/branch/base, changed paths within write scope) before doing any further work.
   - Only fall through to steps 4-5 when there is no existing lease for this agent on this ticket, or that lease has genuinely expired (not merely slow to respond).
4. Otherwise, claim with the current ticket version and retain the returned lease ID.
5. If the project has a configured repository and step 3 did not apply, call `dev.prepare_worktree` and work only in its returned path and branch.
6. Establish a baseline for the target behavior when possible.
7. Implement only within the ticket outcome and write scope.
8. Test each meaningful change and preserve previously verified behavior.
9. Append concise checkpoints with `dev.append_progress` when they help a replacement agent continue.
10. Submit a real Git commit SHA with `dev.submit_candidate`, exactly once per attempt. Treat the returned submission ID as the durable acknowledgment; Git validation and candidate freezing continue in a detached worker.
11. While the submission is `QUEUED` or `RUNNING`, poll `dev.get_candidate_submission` with that submission ID. Polling also renews the Developer lease, so continue until the job reaches `COMPLETED` or `FAILED`.
12. On `COMPLETED`, confirm the ticket is `QA_READY` and stop Developer work. On `FAILED`, preserve the exact worker error and current commit identity; only start a new attempt after resolving that specific failure within the ticket scope.

If `dev.submit_candidate` errors, is interrupted, or does not return a submission ID: record the exact error code/message verbatim (e.g. via `dev.append_progress` or directly to the user) and stop — do not retry blindly. Re-read ticket/candidate state before ever calling it again, since the first attempt may have queued successfully. A slow `QUEUED` or `RUNNING` job is not a blocker; keep polling it and renewing the lease.

Do not mark your own candidate as QA-passed, modify acceptance criteria, merge a protected branch, or continue after losing the lease. Call `dev.report_blocker` only after confirming a durable failure — not a single transient error, a busy dependency, or an infra hiccup that might clear on its own; when in doubt, surface the blocker to the user instead of releasing the lease speculatively. `dev.report_blocker` releases the lease, retains exact context and returns the ticket to Planner. Never perform QA, Planner or Integrator/merge actions yourself, and do not invent a completion claim.

If this chat becomes idle and is later resumed, call `work.inbox` again before choosing work — this is what surfaces an existing active lease for the resume check in step 3. An MCP server can queue work but cannot itself wake a stopped chat; use the host's scheduling or notification adapter when push wakeups are needed.
