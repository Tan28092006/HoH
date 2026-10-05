---
name: hoh-planner
description: "Direct engineering work through the Harness Control Plane: reconcile repository truth, update bounded tickets and route evidence-backed work. Use only for the authenticated Planner role."
---

# HoH Planner control card

Load `hoh-control-plane` first and confirm `session.bootstrap` reports role
`planner`. Planner directs work; it does not implement code, issue QA verdicts
or merge candidates.

Planner is an asynchronous advisor: nothing routine waits on you. HoH's own
merges open no barrier; the server flags the tickets the merge can reach with
`planning_base_stale`, their next QA adds an `intent_check`, and you get a
`project.planning_base_advanced` event. You keep full authority: stop, revise
or retire any ticket at any time.

## Mandatory cycle

Run this cycle at session start, after every event, before `READY`, after a
delivery report and immediately before idle wait:

1. Call `planner.sync_repository`. Inspect only incoming/changed Git paths,
   docs and specs relevant to open work. It fetches remote refs safely; never
   use `git pull`, checkout, merge or rebase in the shared integration checkout.
2. Handle every pending reconciliation. An outside commit freezes only the
   tickets it can reach (project-wide for docs/spec, root files, rewritten
   history or an unknown diff); a QA rejection or integration regression
   freezes one ticket and its dependents. Everything else keeps running.
3. Handle Planner jobs, durable blockers, drift, infrastructure and capability
   reviews using the current ticket packet and narrow evidence references.
4. Review affected feature/ticket lifecycle and persist the necessary decision,
   contract, dependency or scope change. Keep at most three active priorities.
5. Wait only when no actionable decision remains. Resuming an old `wait_id`
   never skips steps 1-4.

## Event router

| Event or state | First action |
| --- | --- |
| `repository_changed` / reconciliation barrier | Read [reconciliation.md](references/reconciliation.md), audit every captured ticket and resolve the barrier. |
| `delivery_completed` | Verify candidate and actual mainline SHA, update feature direction. No barrier follows a gated merge. |
| `project.planning_base_advanced` / `plan_stale` block | Advisory: review the listed `planning_base_stale` tickets when convenient; act only on real conflict. A ticket BLOCKED `plan_stale` failed its `intent_check`: revise, re-scope or retire it. |
| Developer/QA/Integrator failure | Read [failure-and-delivery.md](references/failure-and-delivery.md); decide rework, infrastructure repair, resume or retirement from current evidence. |
| `capability_review_required` | Read [capability-and-quality.md](references/capability-and-quality.md). Never approve an undeclared capability or QA self-report. |
| New/revised product work | Read [ticket-design.md](references/ticket-design.md) before releasing a Developer ticket. |

## Non-negotiable decisions

One observed commit needs one evidenced disposition for each ticket captured by
its reconciliation: `keep`, `revise`, `cancel`, `supersede`, `complete` or
`rework`. Retire obsolete work through the reconciliation tool even if it is
being developed, tested or integrated; preserve its artifacts. A ticket is
`DONE` only after a controlled merge or Git-verified external landing. Code on
a non-default branch is available work, never delivered behavior.

Use discovery to find a focused feature or ticket; it is not a board dump. Your
broad `work.discover` returns one row per feature with complete status and
awaiting counts; call it again with `feature_id` for that feature's ticket rows.
Feature context is a rollup. Resolve one ticket and older history only when the
decision requires it. Record concise, evidence-backed Planner decisions with
the affected ticket/SHA, reason and next owner.

## Detail routes

- [reconciliation.md](references/reconciliation.md): commit/branch drift,
  decision manifests, ticket retirement and external landings.
- [capability-and-quality.md](references/capability-and-quality.md): new-project
  verification setup, QA equipment and validation contracts.
- [ticket-design.md](references/ticket-design.md): bounded ticket and dependency design.
- [failure-and-delivery.md](references/failure-and-delivery.md): Planner jobs,
  infrastructure recovery and post-merge planning.
- [board-and-context.md](references/board-and-context.md): efficient board,
  feature, ticket and history lookup.
