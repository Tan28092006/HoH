# HoH 0.3.34: autonomy with fewer waits, and stricter evidence

Wave 7 lets work move without a Planner approving every step. Wave 8 fixes the
findings of the 2026-10-06 re-review
([review](../reviews/2026-10-06-harness-re-review.md)). Owner decisions: the
wave 7 design (firstmate autonomy) was approved on 2026-10-05.

## Wave 7: autonomy

- **Narrower barriers.**
  - Only commits on the default branch open a reconciliation, and new
    observations extend the one pending sync reconciliation.
  - HoH's own merges no longer open a barrier: tickets they reach are flagged
    `planning_base_stale`.
  - An outside commit freezes only the tickets its paths reach. It freezes the
    whole project only for docs/spec, root files, rewritten history or an
    unknown diff.
  - A QA rejection resolves its own ticket-scoped barrier.
- **Approvals made by the server.**
  - QA capability is approved from host preflight.
  - Outside landings are recorded as ungated automatically.
  - Planner acts as an asynchronous advisor.
- **Less wasted rework.**
  - The runner classifies environment failures as infrastructure and retries
    once with fewer workers.
  - The `-n` worker count is sized from available memory.
  - A drifted queued candidate is rebased by the server and verified again.
- **Guardrails.**
  - Maker and checker must come from different model families.
  - Tickets carry risk tiers.
  - Gated merges are capped per hour, and consecutive failures are capped.
  - Every landing queues a check of `main`. A red `main` pauses the merge queue
    and opens a revert ticket.

## Wave 8: findings of the re-review

- **Evidence.**
  - Only a run the runner classified as `passed` is reused or gates a merge.
  - A checker whose agent ID matches the maker's is refused with `self_review`.
  - `provenance_modules` is frozen per candidate from its base.
  - A run with no passed tests fails with `no_tests_passed`.
  - A `passed` verdict needs every finding verified.
  - Local evidence refs must exist.
- **Credentials.**
  - Client configs read the token from an environment variable
    (`HOH_MCP_TOKEN_<AGENT_ID>`) instead of a literal Bearer header.
  - `agent-credentials.json` stores `sha256:` digests.
  - Plaintext entries still authenticate until each profile is set up again.
- **Liveness.**
  - Acknowledged jobs no longer starve the Planner wait, and delivery reports
    close on acknowledgement.
  - Dependents of a retired ticket are rewired or escalated.
  - Offers skip tickets whose dependencies are not done.
  - Precondition failures have their own outcomes instead of "infrastructure".
  - QA infrastructure failures are grouped per machine, which is quarantined.
  - The sweep only detects changes.
- **Environment safety.**
  - Serving processes refuse a database older than their schema. The upgrade
    runs `hoh-ops migrate`, which takes a backup first.
  - Development scripts default to `tmp/dev/control-plane.db`, refuse the real
    data path, and seed demo data only when asked.
  - `bootstrap-local.ps1` refuses checkouts other than `main`.
  - `start-supervisor.ps1` is added.
- **Agent interface.**
  - Reconciliation decisions are typed.
  - Every reference the server emits resolves through `context.resolve`.
  - Enumerated parameters carry the service's own value sets.
  - Every MCP error maps to `{code, message, details, retry_hint}`.
  - A claim returns a compact `{lease, worker_brief}` with `project_id`.
- **Git side effects.**
  - The fast-forward of the shared checkout moves the ref by compare-and-set
    and cannot be cancelled midway.
  - A candidate landed by hand during the gate is recorded, not reworked.
  - Candidates are pinned under `refs/hoh/candidates/<id>`.
  - Paths are parsed with `-z`, so non-ASCII names survive.
  - Write scope stays reserved until merge.
- **Tests and CI.**
  - A per-function edge-case audit (`tmp/test-gap-audit.md`) and tests for
    every barrier guard, candidate shape and `work.report_failure`.
  - Shared test helpers live in `tests/helpers`.
  - `scripts/test.ps1 --fast` runs a fast lane.
  - `.github/workflows/ci.yml` runs on Windows and Ubuntu.

## Upgrade notes

- Run the profile setup for each agent once after the upgrade, so its token
  moves into the user environment and the credential store holds digests.
- `ticket_type` now accepts only a fixed set of values.
- Storage errors report `state_conflict` instead of `database_error`.

## Not done

- The zero-token crew runner (7B1), the worktree pool (7B4), run budgets and
  durable notifications (7D1, 7D2), and the measurement lane (8F).
- Lead time after this release has not been measured yet.

## Verification

The full suite passed on the integration branch: 1804 passed, 31 skipped,
0 failed, coverage 84.66%. PostgreSQL tests, browser tests and the CI workflow
itself were not run.
