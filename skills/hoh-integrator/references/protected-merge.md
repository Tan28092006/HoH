# Protected merge and recovery

The protected gate requires current independent evidence for every mandatory
contract criterion, registered regression run, verified artifact identity and
unchanged base. When a project defines an isolated regression command, preserve
that gate. A green aggregate result alone is insufficient.

Poll merge jobs; a lost acknowledgement requires state reconciliation before any
retry. Missing async tools or stalled workers route to Planner with evidence, not
raw Git, direct service calls, SQL or force-push. A terminal merge failure already
blocks the ticket and creates Planner work (`git_precondition_failed`: a dirty or
wrongly checked-out shared checkout, nothing written, no source rework), except
`precondition_failed:<outcome>` (barrier, lease, ticket or release evidence
changed before Git), which keeps the ticket MERGE_READY and requeues it.
Report another durable tool failure once only when it did not create a Planner job.

If Planner retires the ticket during merge, stop and let HoH reconcile actual
mainline SHA. `LANDED` means assess the landed behavior/revert; it is not a fake
cancellation. Base drift, conflict, non-fast-forward, missing evidence or changed
SHA requires rework and fresh QA. Integrator does not resolve textual conflicts
by editing the candidate; route a bounded Developer ticket. Semantic conflicts
need QA/Planner evidence-based decisions.

If Git proves code already exists on default branch, use
`integrator.record_external_landing` with current planning authorization and exact
implementation SHA. It records a landing but creates no QA verdict or acceptance
claim. Preserve historical evidence and explain any ported change.
