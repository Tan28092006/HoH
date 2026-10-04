# HoH 0.3.26 — one suite run per ticket, and fewer stalls

Not yet packaged or installed. Follows the
[2026-10 architecture review](../reviews/2026-10-harness-architecture-review.md).

## Why

The owner paused HoH on 2026-09-26 because each ticket took about 30 minutes.
The frozen project suite ran three times per ticket (Developer, QA, merge gate),
each merge waited for a Planner turn that restated "keep" for every open ticket,
and some waits stalled outright.

## What changed

- **The merge gate reuses QA's run.** Fast-forward only lands when main equals
  the candidate's frozen base, so the tree reaching main is the commit QA ran.
  When a completed QA job passed the same frozen command and timeout on that
  candidate, the gate records `gate_mode: reused_qa_run` instead of running the
  suite again. Otherwise it runs as before (`gate_mode: executed`).
- **Drift is detected before the gate.** A merge whose base is no longer main
  goes straight to base-drift rework instead of after a full suite.
- **QA runs the suite once per candidate.** `qa.run_project_tests` returns a
  completed passing run of the same command (`reused: true`); `rerun: true` forces
  a fresh one. The Developer reference no longer asks for a full-suite run.
- **Disjoint merges do not stop the board.** The server resolves a merge's
  reconciliation as `keep` (`resolved_by: system:scope-disjoint`) only when every
  open ticket has a scope whose root misses all merged paths, none depends on the
  merged ticket, and no `docs/`, `spec/` or repository-root file changed.
  Offers are then not cancelled. Anything else still waits for Planner, and
  Planner still receives the `delivery_completed` report.
- **Wake-ups survive a barrier.** Resolving a reconciliation re-arms work
  deliveries agents acknowledged while blocked; before, they idled until their
  12-hour wait expired.
- **A barrier cannot strand a landed merge.** Once Git has fast-forwarded,
  completion records the landing even if a reconciliation opened during the gate.
- **All service writes take the write lock**, including project, feature and
  ticket creation; a test enforces it.
- **The suite runs across workers.** `scripts/test.ps1` passes `-n auto`:
  17.6 minutes serial, 4.3 minutes with ten workers, identical results.
- The QA card and verdict docstring name the accepted values `passed`/`failed`.

## Invariants kept

Independent QA still executes the frozen command on the frozen candidate; main
still moves only to a commit with a passing run of that command; every
reconciliation still records one decision per open ticket, tagged when the
server made it.

## Verification

Full suite with ten workers: 952 passed, 34 failed, 30 skipped. The 34 failures
are exactly the set on `main` 2fa75f3 before these changes.
