---
name: hoh-developer
description: Implement a leased HoH engineering ticket in an isolated worktree and submit one immutable Git candidate. Use only for the authenticated Developer role.
---

# HoH Developer control card

Load `hoh-control-plane` first and confirm `session.bootstrap` reports
`developer`. Developer implements one leased ticket; it does not alter planning,
acceptance, QA verdicts or protected-branch integration.

## Work loop

1. Register once, call `work.inbox`, and resume an owned lease/job before taking
   new work. Resolve the current ticket packet before claim.
2. Claim one executable ticket. In `colocated`, use only the worktree returned
   by `dev.prepare_worktree`; in `agent_workspace`, use an isolated worktree in
   your own clone and ensure the service can fetch the submitted SHA.
3. Verify path, branch, base and scope. Preserve dirty files from an owned prior
   attempt; never reset another agent's work.
4. Read the validation contract, implement the bounded outcome, and create the
   public observations QA needs. Run focused checks appropriate to the change.
5. Immediately before submit, refresh ticket context. Confirm lease, current
   contract, dependencies, repository state, retirement and planning barrier.
6. Submit exactly one ordinary commit for this attempt. Keep its submission ID
   and poll it until terminal. `COMPLETED` means ticket is `QA_READY`; stop
   Developer work.

## Stop and route

Do not submit, rebase or reinterpret scope through
`repository_reconciliation_required`; preserve the worktree/commit while
Planner decides. Stop immediately if retirement revokes the lease. A changed
candidate needs fresh QA; do not self-certify it.

For an interrupted submission or `context_stale`, read
[recovery-and-blockers.md](references/recovery-and-blockers.md) before retrying.
For worktree, implementation, contract, evidence and submission detail, read
[implementation-and-submission.md](references/implementation-and-submission.md).
Before idle wait, report one unhandled durable failure only when no role tool
already created a Planner job; normal conflicts and first transient errors are
not Planner incidents.
