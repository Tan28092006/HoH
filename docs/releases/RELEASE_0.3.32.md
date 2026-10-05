# HoH 0.3.32 — scoped barriers, replayable audit, measured loop

Wave 6, from the [system-design re-review](../reviews/2026-10-05-system-design-review.md)
and the latency analysis. Schema version 2: an older binary refuses the upgraded
database, so this release cannot be rolled back in place.

## Flow

- **Scoped barrier.** QA rejection, integration regression and drift onto an
  already-observed `main` freeze only that ticket and its transitive dependents;
  other work keeps flowing. Outside commits and merges stay project-wide.
- **Merge shadow mode.** A reviewed merge records the partition the scope rule
  would have frozen and whether Planner agreed (`cycle_metrics.barrier_shadow`).
  Nothing acts on it yet.
- **Merge lane.** A merge that reuses QA's run no longer waits for the test slot.

## Correctness

- **Idempotency settled from the audit trail.** A stale reservation is answered
  as committed or re-run using its `correlation_id`; completion is keyed by
  record (no ABA); the transport never retries a committed request.
- **Replayable ticket audit.** Every transition writes `ticket.transitioned`;
  `replay_ticket_states` reproduces each ticket's status and version.
- **Git sweep.** The scheduler observes repositories without a waiting Planner,
  flags candidates already on `main`, and flags stuck merge jobs;
  `planner.close_stuck_merge_job` closes one without an admin.

## Measurement

- `cycle_metrics` splits wait from service per phase, adds barrier-open time,
  job queue age and suite runs per landed ticket, all as p50/p90.
- `work.report_usage` records whether a fresh worker did the ticket and its
  tokens; after five tickets an idle wait advises the dispatcher to start a new
  session (advisory only).

## Verification

Full suite with coverage: 1167 passed, 31 skipped, coverage 83%. UI and
PostgreSQL tests not run.
