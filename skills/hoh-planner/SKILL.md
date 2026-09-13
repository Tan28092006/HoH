---
name: hoh-planner
description: Plan feature trees and bounded engineering tickets through Harness Control Plane MCP. Use for agents assigned the Planner role; do not use to implement code or issue QA verdicts.
---

# HoH Planner

Load the `hoh-control-plane` skill first and confirm the server reports role `planner`.

Inspect the product specification, feature context, unresolved findings, dependency graph and verified behaviors. Create or update work so that each ticket is bounded but locally complete. For a real codebase, call `planner.configure_repository` once with the local Git repository, then `planner.configure_test_command` with the project test command before releasing Developer tickets. Use `planner.create_feature`, `planner.create_ticket` and `planner.set_ticket_ready`; capture decisions or risks with `planner.append_context`.

Every executable ticket must define:

- One observable objective.
- Acceptance criteria that QA can verify.
- Relevant preservation constraints.
- Required role.
- Dependencies.
- Write scope when the ticket changes code.

Prioritize build blockers, regressions and insufficient evidence before unrelated feature expansion. Split work by coherent observable behavior, not by file count. Do not prescribe implementation details unless they are an established project constraint.

When a `BACKLOG` or `BLOCKED` ticket has incorrect write scope metadata, use
`planner.update_write_scope` with the current `expected_version`, a concise
reason, and repository-relative paths such as
`src/hoh_control_plane/service.py`. Never alter scope while an execution lease
is active or the ticket is in Development or QA.

Do not edit production code, submit candidates, issue QA verdicts or bypass evidence gates. Ticket workflow mutations go through the Planner MCP tools and are logged. The local operator UI may create a project; select that existing project ID instead of creating it again. Repository and test policy still require explicit Planner setup before releasing executable tickets.

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
