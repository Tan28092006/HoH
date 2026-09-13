# HoH 0.3.17 — retirement fencing and failure feedback

Planner can now retire an obsolete ticket from any nonterminal phase through a
pending repository reconciliation. HoH revokes dispatch, leases and non-merge
workers while preserving worktrees, candidates and evidence. A merge already in
progress is reconciled against the default branch before the request becomes
`APPLIED`; if its candidate landed first, the request becomes `LANDED` and the
ticket remains truthfully `DONE` for Planner to assess a revert.

Developer, QA and Integrator have a common `work.report_failure` path for one
durable error before idle. Reports require either the caller's current lease or
its terminal failed worker job, deduplicate by evidence fingerprint, block unsafe
execution and open one Planner intervention. Normal stale-context, lease/state,
empty-inbox and first transient errors are excluded. Existing QA verdict and
recognized infrastructure paths remain the single report for their batch.

Planner can resolve a failure job and atomically resume the same ticket to the
appropriate role. Candidate identity is retained for QA and Integrator recovery;
Integrator recovery additionally requires prior QA verification on that exact
candidate. Ticket context exposes retirement state so a revoked session stops
instead of trying to reclaim or submit stale work.

The user requested validation on the live Legal workflow. No automated test suite
is claimed for this release. Source compilation, package construction, upgrade
health and preservation checks are recorded separately.
