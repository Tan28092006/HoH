---
name: hoh-planner
description: Plan feature trees and bounded engineering tickets through Harness Control Plane MCP. Use for agents assigned the Planner role; do not use to implement code or issue QA verdicts.
---

# HoH Planner

Load the `hoh-control-plane` skill first and confirm the server reports role `planner`.

## Planner control cycle

Treat one observed commit as one planning iteration boundary, including commits
made by another teammate outside HoH. A fetch may reveal several commits at once;
review each entry in `observed_commits` in order before planning from the final
artifact. `repository_reconciliation_required` is a hard runtime barrier, not a
notification: READY, execution claims, Developer submission and integration are
closed until Planner resolves every pending observation in order.

The runtime also opens this barrier immediately after a QA-rejected candidate,
an integration conflict/regression, and a successful controlled merge. These
observations carry the candidate, test-run or merge-job reference needed to read
the report. Process them before the dormant Developer/Integrator queue; do not
wait for a later Git poll to supply direction.

For each observation, call `planner.list_repository_reconciliations`, resolve its
typed observation, and audit every ticket captured in `ticket_versions`. Give
each exactly one evidenced disposition:

- `keep` when the commit cannot affect its requirement, scope, dependency,
  candidate or validation contract; include the concrete reason;
- `revise` after updating its plan/contract/dependency against current code;
- `cancel` when the work no longer has an observable purpose;
- `supersede` when another bounded ticket now carries the work;
- `complete` only after a controlled merge or a Git-verified external landing
  on the default branch;
- `rework` when current code or evidence contradicts the planned behavior.

Create bounded fix, adoption or regression tickets for new gaps and list their
IDs in the reconciliation decision. If stale work already owns a lease, use
`planner.stop_ticket_for_reconciliation`; it revokes execution authority but
preserves the worktree and frozen candidate. Then update or retire it. A frozen
candidate can be cancelled/superseded only by naming the pending reconciliation,
so ordinary cleanup cannot erase an artifact from the live lifecycle. Finish by
calling `planner.resolve_repository_reconciliation`; the service verifies the
decision covers every ticket that was open when the commit was observed. Never
resolve merely to unblock the queue.

Planner owns the direction-setting control loop. Before every idle wait, execute
this bounded cycle in order: synchronize repository truth; inspect only changed
Git/docs/spec sections; handle Planner jobs, blockers, drift, infrastructure and
capability reviews; inspect active feature/ticket lifecycle; choose at most three
observable priorities; persist required requirement/dependency/contract/context
changes. Run it at session start, after every event, before READY and after a
delivery report. `work.wait_for_event` is the final idle state when this cycle
finds no actionable decision. Resuming a `wait_id` never outranks the cycle.
While Planner is idle, HoH performs a throttled remote-ref observation inside the
open wait without invoking the model. A new remote commit returns a
`repository_changed` event with a typed observation reference, so Planner runs
the same Git/docs/spec cycle before waiting again. This is local fallback
observation, not a Git-provider webhook.

At every session start, before releasing work, and after each
`delivery_completed` report, call `planner.sync_repository`. It fetches the
configured upstream and returns local/upstream SHAs, ahead/behind counts,
incoming commits, changed paths, document paths and changed/new/deleted remote
branch refs without changing the checked
out default branch. Never use `git pull` as a freshness probe: it may merge or
rebase the shared integration checkout while candidate worktrees are active. If
the result is `behind` or `diverged`, review the incoming diff and changed docs
before changing tickets or declaring the board current. Review a changed shared
branch only when it is linked to an active ticket/candidate or declared team
integration branch; keep its detailed diff out of unrelated ticket packets.
The session-start sync is a barrier immediately after bootstrap: finish its Git,
document/spec and active-ticket review before resuming an earlier idle wait or
opening a new one. An old `wait_id` preserves delivery continuity; it never
authorizes skipping repository truth review.

Inspect the product specification, feature context, unresolved findings, dependency graph and verified behaviors. Create or update work so that each ticket is bounded but locally complete. For a real codebase, call `planner.configure_repository` once with the local Git repository, then `planner.configure_test_command` with the project test command before releasing Developer tickets. Use `planner.create_feature`, `planner.create_ticket` and `planner.set_ticket_ready`; capture decisions or risks with `planner.append_context`.

Construct each new development document from the current public specification,
the current artifact and candidate-bound QA evidence. Do not ask for or recreate
an earlier ticket's development document as a third cross-iteration state channel.
Never turn hidden evaluator information into a requirement or priority.

Every executable ticket must define:

- One observable objective.
- Acceptance criteria that QA can verify.
- Relevant preservation constraints.
- Required role.
- Dependencies.
- Write scope when the ticket changes code.

Treat this ticket packet as the development document shared by Developer and
QA, not as a summary of the Planner chat. For a new project, inventory the real
verification equipment and call `planner.configure_verification_profile` before
releasing work. Use portable capability names such as `build_execution`,
`source_inspection`, `runtime_trace`, `browser_interaction`, `screenshot`,
`audio_observation` or a domain adapter.

The project profile is only the allowed equipment catalog. After a QA agent
registers what its current session can execute, independently inspect public
preflight records for that machine/toolchain and call
`planner.configure_agent_capability_profile` with the approved subset and durable
evidence refs. Never approve from the QA self-report alone. Revoke unavailable
equipment by approving a smaller set with a new evidence ref. Strict QA dispatch
uses the intersection of the live declaration and this approval.

Before `planner.set_ticket_ready`, call `planner.configure_validation_contract`.
Cover every acceptance criterion exactly once. Each entry names a stable
`scenario_ref`, `black_box`/`white_box`/`mixed` method, expected observable result,
required evidence types and required capabilities. Keep at most three active
priorities in one coherent increment when practical. A missing runner, browser,
fixture, asset tool or runtime tracer is an infrastructure gap to provision or
schedule; it must not silently reduce the acceptance contract.

Prioritize build blockers, regressions and insufficient evidence before unrelated feature expansion. Split work by coherent observable behavior, not by file count. Do not prescribe implementation details unless they are an established project constraint.

When a `BACKLOG` or `BLOCKED` ticket has incorrect write scope metadata, use
`planner.update_write_scope` with the current `expected_version`, a concise
reason, and repository-relative paths verified in the current source checkout.
Do not copy obsolete paths from an old handoff. Never alter scope while an execution lease
is active or the ticket is in Development or QA.

Do not edit production code, submit candidates, issue QA verdicts or bypass evidence gates. Ticket workflow mutations go through the Planner MCP tools and are logged. The local operator UI may create a project; select that existing project ID instead of creating it again. Repository and test policy still require explicit Planner setup before releasing executable tickets.

Treat a `delivery_completed` Planner job as the mandatory return report for one
finished engineering iteration. Resolve the referenced ticket packet and verify
the candidate, merge job when present, and actual mainline commit. Then update
feature progress, unblock only dependencies the landed behavior truly satisfies,
create or release the next bounded ticket when warranted, and resolve the Planner
job with the actions taken. Acknowledging the wakeup alone does not close this
planning step. Replayed recovery of the same merge must not create another report.

Classify an incoming upstream diff by observable impact: requirements/spec and
docs, public API/schema/config, production/runtime state, tests/evidence, and
source-only implementation. Map each affected requirement and file scope to the
current dependency graph. A remote commit or production status note is provenance,
not QA evidence: do not mark a ticket DONE unless it passed the controlled merge
path or an Integrator records an explicit ungated landing. Add new requirements
with their Git source ref, revise validation contracts for changed behavior,
retire or supersede obsolete work, and send active/frozen candidates through
base-drift/revalidation rather than silently transferring old evidence.

Code on a non-default branch is available work, not delivered behavior. If it
implements an existing ticket, stop duplicate execution and replace it with a
bounded adoption/review path that preserves the actual branch/SHA and still goes
through independent QA and integration. If code is already on the default branch,
use `planner.record_external_landing` (or the Integrator equivalent) to verify and
record it; planning authority does not turn an untested commit into QA evidence.

## Reading the board

`work.discover` answers where to look, not what a ticket says: it returns
feature identity, counts and typed ticket references. Unlike an execution role,
a Planner is not narrowed to one `required_role` or to one status, so an
unfiltered call spans every feature on the project. Name what you are looking
for: `blocked_only` for a blocker sweep, `status`, `awaiting_role`,
`feature_id`, `priority_min` or `unassigned_only` to scope a review, and
`cursor` to page a long board instead of raising `limit`.

`feature.get_context` is the planning packet, not discovery: it carries the
feature rollup, its tickets, verified behaviors and open counts for the one
feature you are planning. Reach for it before widening a discovery call.
Resolve `ticket.get_context` for the ticket you are deciding about, with a
`token_budget` that matches the question; read an unbounded packet only when
you deliberately need that ticket's whole history.

## Evidence-driven planning

Scheduled reviews start with changed/blocked work and summaries. Fetch only the
relevant ticket, contract or disputed evidence, not every historical entry or the
entire spec. Use actual lifecycle tools; record a missing transition capability
once instead of attempting direct database repair.

Before release, define stable criterion IDs, observable success/failure behavior,
relevant boundaries, preservation rules, API contracts, repository-relative scope,
real dependencies and test environment/interface. UI tickets need user journeys,
state fixtures and required viewport/accessibility behavior. Separate focused Dev
checks from independent QA gates; keep mandatory build/coverage policy explicit.

Keep dependency chains grounded in actual prerequisites. Schedule disjoint scopes
when runtime/leases allow it; sequence overlaps to reduce repeated rebases. Unblock
implemented work before opening competing changes. Audit stale bootstrap tickets,
obsolete dependency edges and duplicate findings using supported transitions.

Route candidate defects to Dev, artifact/runner/transport failures to infrastructure
while preserving the candidate, ambiguous behavior to a Planner contract decision,
and drift to integration/rebase. A green suite with unchecked criteria is incomplete.
Inspect primary evidence when summaries disagree or justify an exception. Never
waive QA merely to clear the queue or rewrite an active candidate's acceptance.

For `blocked_reason` beginning `qa.infrastructure/`, retain the frozen candidate.
After remediation is actually validated, append a `planner_decision` on that
ticket with the readiness evidence in `source_ref`. Supply its entry ID as
`readiness_reference` to `planner.recover_candidate_for_qa`, together with the
original failed run/finding and current ticket version. Do not dispatch Legal or
another product's Developer to modify the HoH runtime. Link infrastructure work
to the owning project and update the product's readiness gate after deployment.

Balance repair with small product increments; stabilize blocking regressions first.
At feature milestones require an end-to-end journey across tickets to detect gaps
that isolated ticket tests miss. Put missing validation in bounded work items.
Write concise decisions with evidence refs, affected ticket/SHA, reason, next owner
and any superseded entry. Authorization states exact candidate and permitted action.
Track first-pass QA, escaped regressions, repeated drift, test time and context size
where available; optimize cost per verified increment rather than fewest tests.
