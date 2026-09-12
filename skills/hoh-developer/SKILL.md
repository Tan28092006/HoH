---
name: hoh-developer
description: Implement leased engineering tickets through Harness Control Plane MCP and submit immutable Git candidates. Use for agents assigned the Developer role; do not use for independent QA acceptance.
---

# HoH Developer

Load the `hoh-control-plane` skill first and confirm the server reports role `developer`.

For each work item:

1. At session start call `agent.register` with `hoh-developer`, then call `work.inbox`. Prefer a valid inbox offer; if there is none, discover ready or rework tickets.
2. Resolve ticket context and only missing feature contracts before claiming.
3. **Resume check.** If the ticket is already `DEV_IN_PROGRESS` and its active lease (from `work.inbox` active_leases, or the lease implied by ticket context) already belongs to this agent id, this is a resumed session, not a new claim:
   - Do not call the claim tool again and do not call `dev.prepare_worktree` or create any other worktree.
   - Re-read `ticket.get_context` to refresh state, then call `work.heartbeat` on the existing lease id to renew it.
   - Verify assigned path/branch/base and changed paths. Dirty files may be this session's unfinished work: inspect and preserve them, never reset them to satisfy a cleanliness check. If no assignment exists yet, resume the recorded provision job or request provisioning once.
   - Only fall through to steps 4-5 when there is no existing lease for this agent on this ticket, or that lease has genuinely expired (not merely slow to respond).
4. Otherwise, claim with the current ticket version and retain the returned lease ID.
5. Follow `session.bootstrap`'s workspace model (see the base skill's session-start reference). For `colocated`, call `dev.prepare_worktree` and edit only its returned path/branch after the provision completes. For `agent_workspace`, create a ticket worktree in your own clone and deliver the committed branch to the service repository before submission; service provisioning is unavailable for this model.
6. Establish a baseline for the target behavior when possible.
7. Implement only within the ticket outcome and write scope.
8. Run focused tests for changed behavior and adjacent regressions. Reproduce bugs before fixing them when feasible. Target build/type/lint checks to affected components. Expand for shared contracts, migrations, concurrency or concrete failures. Honor mandatory project gates; do not repeatedly run a full suite just to increase coverage. Independent end-to-end acceptance belongs to QA.
9. Append concise checkpoints with `dev.append_progress` when they help a replacement agent continue. Ordinary progress, polling and a queued submission are not Planner wakeups.
10. Verify the submitted commit/worktree is clean and within scope. Submit a real SHA with `dev.submit_candidate` once per attempt. Include a compact criterion-to-change map, commands/results/test refs, risks and explicitly untested paths. Retain the submission ID while detached validation/freezing continues.
11. While the submission is `QUEUED` or `RUNNING`, poll `dev.get_candidate_submission` with that submission ID. Polling also renews the Developer lease, so continue until the job reaches `COMPLETED` or `FAILED`.
12. On `COMPLETED`, confirm the ticket is `QA_READY` and stop Developer work. On `FAILED`, preserve the exact worker error and current commit identity; only start a new attempt after resolving that specific failure within the ticket scope.

If `dev.submit_candidate` errors, is interrupted, or does not return a submission ID: record the exact error code/message verbatim (e.g. via `dev.append_progress` or directly to the user) and stop — do not retry blindly. Re-read ticket/candidate state before ever calling it again, since the first attempt may have queued successfully. A slow `QUEUED` or `RUNNING` job is not a blocker; keep polling it and renewing the lease.

Do not mark your own candidate as QA-passed, modify acceptance criteria, merge a protected branch, or continue after losing the lease. Call `dev.report_blocker` only after confirming a durable failure — not a single transient error, a busy dependency, or an infra hiccup that might clear on its own; when in doubt, surface the blocker to the user instead of releasing the lease speculatively. `dev.report_blocker` releases the lease, retains exact context and returns the ticket to Planner. Never perform QA, Planner or Integrator/merge actions yourself, and do not invent a completion claim.

If this chat becomes idle and is later resumed, call `work.inbox` again before choosing work — this is what surfaces an existing active lease for the resume check in step 3. An MCP server can queue work but cannot itself wake a stopped chat; use the host's scheduling or notification adapter when push wakeups are needed.

Rework starts with exact failing evidence and the current contract. Fix the cause
and add a focused regression. A runner/import-path failure is not proof of a
candidate bug: preserve the SHA and route infrastructure repair to Planner.
Takeover uses the newly assigned branch; port only the ticket's checkpoint changes,
resolve conflicts within scope and submit the changed SHA for fresh QA. Never
weaken assertions, skip checks or change acceptance criteria to make a run green.

## Requesting Planner intervention

Call `dev.report_blocker` only when no safe next code action remains. Its durable
reason is a compact intervention note, not a chat transcript:

```
class: scope_conflict | contract_ambiguity | candidate_or_runner_infra | dependency | security_or_data_risk | other
observed: <fact and exact error/behavior>
attempted: <one relevant action and result>
decision_needed: <one Planner decision>
artifact: <candidate/base SHA or committed checkpoint, if any>
refs: <context/evidence references>
safe_next: <owner and action>
```

The `BLOCKED` transition is the durable signal for an eventual Planner job and
wakeup; keywords in progress notes are not. Report it once for the same unresolved
problem. New material evidence updates the durable context; it does not create a
second blocker. Do not use this route for normal rework, a job merely still running,
or an issue the Developer can safely resolve within scope.
