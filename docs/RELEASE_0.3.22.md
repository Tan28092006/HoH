# HoH 0.3.22 — the Planner board is counted, not listed

Planner discovery runs at every planning boundary, so its cost is paid on every
cycle for the life of a project. This release changes what that call returns.

## Delivered work leaves the discovery surface

`CLOSED_TICKET_STATUSES` is `{CANCELLED, SUPERSEDED}`; `DONE` was never in it.
Execution roles never saw finished tickets because their status filters exclude
them, but Planner and Admin have no status filter, so every ticket ever
delivered was carried in a payload whose stated purpose is handing out
actionable work. On the live `hoh-control-plane` project that was 50 of 62 rows.

Discovery now excludes `DONE` alongside `CANCELLED` and `SUPERSEDED`, for every
role. A caller that wants a finished ticket asks for it by status, and then it
is not hidden: `status=["DONE"]` returns those rows. Ticket context is
unchanged, so a delivered ticket remains fully readable by reference.

This was found by building the rollup below and comparing its counts against
the database. The counts disagreed.

## The broad Planner call returns one row per feature

`work.discover` gains `detail`. For Planner and Admin the broad call now returns
`view: "feature_rollup"`: one row per feature carrying `ticket_counts` by
status, `awaiting` counts by role, `open_ticket_count`,
`dependency_blocked_count`, `blocked_ticket_count`, `top_priority`,
`oldest_created_at`, `last_movement_at` and the feature's `context_ref`. No
ticket rows, and no ticket identifiers. `planning_jobs` is unchanged and still
accompanies the rollup: the Planner's cycle reads its jobs from this call before
anything else, so a board view that dropped them would make the default call the
one that hides the work Planner must do first.

The Planner drills in by calling again with `feature_id`, which returns that
feature's ticket rows. Any narrowing argument does the same: `status`,
`awaiting_role`, `priority_min`, `blocked_only`, `unassigned_only` or a
`cursor`. `detail=true` returns rows across the board; `detail=false` forces the
rollup. Execution roles are unaffected and always receive rows, because their
question is which ticket to claim and a count cannot answer it.

Two properties the row view does not have:

- **Counts are complete.** The row view is capped at `limit` and paginates, so a
  total derived from a page would under-report the backlog the Planner is
  deciding from. The rollup aggregates in SQL over the whole board.
- **Dependency-blocked work is counted, not filtered.** The row view hides a
  ticket whose dependency is unfinished, because it cannot be claimed. Hiding it
  from a board total would let a feature look finished when its remainder is
  merely waiting, so the rollup reports it in `dependency_blocked_count` and
  leaves it out of `awaiting`, since no role can act on it yet.

## Measured on the live database (Planner, estimated tokens)

| Project | 0.3.21 rows | Rows without delivered work | 0.3.22 rollup |
| --- | ---: | ---: | ---: |
| hoh-control-plane | 5,237 | 1,455 | 1,372 |
| legal-uit | 843 | 664 | 597 |
| demo | 523 | 523 | 445 |

For `hoh-control-plane` that is a 74 percent reduction against 0.3.21, and the
result is complete where the old payload was truncated at 50 rows with a cursor.
Rollup counts match the database exactly: 12, 6 and 4 open tickets.

## Compatibility

- No schema change.
- Breaking for a caller that assumed `features[].tickets` always exists in a
  Planner response. Pass `detail=true` for the previous shape. Both shapes now
  carry `view`.
- Four existing discovery tests were pointed at `detail=True`, which is where
  the row contract they assert now lives.
- New tests: `tests/test_planner_board_rollup.py` (11). Two lifecycle tests were
  pointed at `detail=True` and extended to assert the rollup's counts and
  planning queue.
- The Planner card describes the rollup and the drill-down.
