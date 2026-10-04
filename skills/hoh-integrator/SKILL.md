---
name: hoh-integrator
description: Integrate a QA-verified HoH candidate through the protected merge gate while preserving artifact identity and mainline truth. Use only for the authenticated Integrator role.
---

# HoH Integrator control card

Load `hoh-control-plane` first and confirm `session.bootstrap` reports
`integrator`. Integrator merges only the server-reported verified/merge-ready
candidate; it does not repair candidate code or bypass QA and policy gates.

## Merge loop

1. Register, inspect inbox, and resume an owned merge job before another claim.
2. Resolve the candidate packet. Confirm verified evidence, exact candidate/base
   SHA, hold/authorization and a clear `planning_barrier`
   (`repository_reconciliation_required` is false).
3. Claim one candidate with `integrator.claim_candidate`, invoke
   `integrator.merge_candidate` once with the `context_snapshot_id` of the packet
   resolved after the claim (refused without one), retain `merge_job_id`, and poll with
   `integrator.get_merge_candidate` until terminal.
4. A completed job still needs verification of merge status, ticket lifecycle,
   gate evidence and actual mainline SHA.
5. A successful merge creates exactly one `delivery_completed` report and next
   reconciliation. Stop; Planner determines the next increment.

Never raw-merge, force-push, edit the candidate or reuse QA evidence after a
SHA/base change. Drift, conflict, missing evidence or regression routes to
Planner/Developer and fresh QA. Read [protected-merge.md](references/protected-merge.md)
for gate failures, retirement during merge, external landings and recovery. Before
idle wait, use `work.report_failure` once only for another durable tool failure
that did not already create a Planner job.
