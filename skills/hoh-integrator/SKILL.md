---
name: hoh-integrator
description: Integrate QA-verified candidates while preserving artifact identity and mainline health through Harness Control Plane MCP. Use for agents assigned the Integrator role; do not bypass QA or policy gates.
---

# HoH Integrator

Load the `hoh-control-plane` skill first and confirm the server reports role `integrator`.

Only select candidates that the server reports as verified or merge-ready. Check base drift, file conflicts, protected-branch policy and required regression gates before merging.

If rebase, conflict resolution or another operation changes the candidate artifact, record the new identity and send it through the required QA gates again. Never reuse evidence from a different commit as proof for the changed artifact.

Record integration evidence and final mainline identity. Requeue the ticket with a specific conflict or regression reason when integration cannot safely complete.

## Protected asynchronous integration

Register, check the inbox and resume an owned lease/job before claiming another
candidate. Process one candidate at a time. Read the exact candidate/base SHA,
mandatory QA outcomes, evidence provenance and current integration holds or
authorization. Retrieve full evidence only for missing or inconsistent gate data.

Claim via integrator.claim_candidate with the current version, then invoke
integrator.merge_candidate once using the returned lease/version. Retain
merge_job_id and poll integrator.get_merge_candidate until terminal; polls renew
the integration lease. COMPLETED alone is not success: inspect merge_status,
ticket state and actual mainline SHA. Lost acknowledgment requires reconciliation
before retry. Missing async tools or stalled workers go to Planner with evidence,
not a raw Git merge, direct service call, SQL or force-push fallback.

Use the current runtime's policy and fresh ticket context, including Planner holds.
The protected gate requires independent evidence for every mandatory criterion,
the registered regression run, verified artifact identity and an unchanged base.
When a project has a registered regression command, the current runtime runs it
in an isolated candidate checkout before fast-forwarding. Preserve that gate;
an old skill or ticket describing it as future work does not disable it.
Inspect the returned gate evidence and mainline identity, including any semantic
record commit written after the candidate fast-forward.

If Git proves the work is already on main, inspect current planning authorization
and use `integrator.record_external_landing` to reconcile an existing landing
without another candidate or a fictitious merge. This records deployed work;
it adds no QA verdict and must not certify unmet criteria. Preserve prior evidence.
For a ported change, cite the actual landed implementation commit and explain
why the historical candidate itself was not merged.

Recheck drift atomically in the gate. base_drift requires Developer rework and
preserves historical QA on its old SHA. Missing mandatory evidence, failed or
insufficient QA, a non-fast-forward path, or an explicit additional ticket gate
blocks integration and is recorded precisely.

In this deployment Integrator does not edit candidate code. Textual conflicts go
to an assigned Developer/integration ticket with bounded scope and then fresh QA.
Semantic conflicts may merge cleanly: verify contracts and interaction/regression
gates, then route conflicting behavior to QA/Planner for an evidence-based decision.
Never choose product semantics silently or transfer QA verdicts to changed SHAs.

Honor candidate-specific holds/authorization; permission for a different SHA or
base-drift-only attempt is not permission to merge a replacement. Under the
standing policy above, do not wait for a per-candidate Planner/human authorization.
Planner authorization is needed only for an exception, hold/release, or new policy
rule, and must name the exact SHA/action.
Avoid repeating valid QA runs unless integration conditions or policy require it.
Record job ID, artifact/base, gate refs, terminal outcome and actual mainline SHA.
Post-merge regression creates a durable Planner incident and pauses dependent
integration; rollback follows established recovery policy. Continue to another
candidate only when the assigned session policy permits.
