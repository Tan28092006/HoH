# HoH 0.3.27 — packets carry the records roles were missing, and main is green

## What changed

- `worktree_recovery` and `integration_regression_failed` entries were written
  but never projected into any role's packet. Developer and Planner now see
  worktree recovery; every execution role and Planner see integration
  regressions.
- Planner's QA-recovery decision was written as `planning_decision`, a type no
  role reads, so it vanished from every packet. It is now `planner_decision`.
- The Integrator card names the barrier check by its field
  (`repository_reconciliation_required` is false) instead of a dotted path that
  read like a tool name.
- The 34 tests that failed on `main` since 0.3.17 were updated to the documented
  behavior of 0.3.17–0.3.26 or fixed with the code above. No test was removed
  and none is marked xfail.

## Verification

Full suite with ten workers: 987 passed, 30 skipped, 0 failed.
