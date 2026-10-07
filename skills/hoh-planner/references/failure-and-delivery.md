# Failure recovery and delivery reports

Treat `developer_blocker`, `qa_failure` and `integration_conflict` as input to
the ordinary control cycle. Claim the Planner job, resolve current ticket context
and narrow evidence refs, then check relevant Git/docs/spec. Route source defect
to Developer, runner/artifact/transport defect to infrastructure while preserving
the candidate, ambiguous behavior to a contract decision, and drift to
integration/rebase.

Retire obsolete work through reconciliation. Otherwise revise bounded contract or
dependencies, or call `planner.resume_ticket_after_failure` to close the Planner
job and atomically route unchanged work to Developer, QA or Integrator. QA and
Integrator resume require the current frozen candidate; Integrator also requires
current QA verification. Never close a fixable Planner job while leaving its
ticket stranded `BLOCKED`.

`rework_budget_exhausted` means QA rejected the ticket three times; the service
blocked it instead of looping Developer and QA again. Decide whether to split
it, revise its contract or cancel it. Resuming it with
`planner.resume_ticket_after_failure` records the budget reset; say why one more
attempt will differ.

For `qa.infrastructure/*`, preserve the candidate. After real remediation,
append a Planner decision naming readiness evidence and call
`planner.recover_candidate_for_qa` with the original failed run/finding and
current version. Do not make product agents repair the HoH runtime.
A job marked `qa_machine:<fingerprint>:infrastructure` groups one machine's
failures and quarantines it (`qa_machine_quarantined`) until you resolve the
job; resolve it only after that machine is repaired.

For `delivery_completed`, resolve the ticket, candidate, merge job and actual
mainline SHA. Update feature progress, unblock only dependencies truly satisfied,
release/create the next bounded work only if warranted, then resolve the Planner
job. Acknowledging the event alone is not a planning decision. Replayed merge
recovery must not create a second delivery report.
