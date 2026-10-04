# HoH 0.3.28 — correctness wave 1

Closes C3 and C5–C10 from the
[2026-10 architecture review](../reviews/2026-10-harness-architecture-review.md).

- **C3 — no Git inside the write lock.** `submit_candidate` verifies the commit,
  changed paths and scope with no transaction held, then re-checks version,
  lease, barrier, snapshot and the worktree row in a short write. QA verdict
  evidence hashing and test report writing also moved out of the transaction.
- **C5 — the snapshot rule lives in the service.** `submit_candidate`,
  `queue_candidate_submission`, `submit_qa_verdict` and `queue_merge_candidate`
  refuse a missing `context_snapshot_id` whatever the transport. REST
  `/candidates` and `/qa-verdict` now require the field.
- **C6 — idempotency failures after a committed effect.** Completion and release
  retry on contention and never turn a committed effect into an error. An
  `IN_PROGRESS` key older than 600 s is cleared and answered with
  `idempotency_unknown` (re-read state) instead of blocking for 24 hours.
- **C7 — evidence matched by field.** Audit payloads used for authorization are
  parsed and compared exactly; `x_1000` no longer matches `x_10000`.
- **C8 — repository sync lost update.** The local commit is read after the fetch,
  the compare-and-set includes `observed_mainline_commit`, and a miss retries
  once, so a merge landing mid-fetch no longer opens a spurious reconciliation.
- **C9 — legacy synchronous merge removed.** Only the queued path (intent,
  repository lock, recovery) can move main. Tests were migrated; the semantic
  decision-record commit that only the legacy path wrote is not ported.
- **C10 — abandoned Planner jobs return.** The lease sweep and the Planner wait
  expire Planner job leases, so a job held by a dead Planner chat goes back to
  READY and wakes a Planner.
- `scripts/test.ps1` keeps pytest's temporary repositories on `D:\.hoh-pytest`.

## Verification

Full suite, ten workers: 1004 passed, 30 skipped, 0 failed. Coverage gate and
PostgreSQL paths were not run.
