# Release notes

One file per release from 0.3.14. Each file records what changed and why, and
the invariant it introduced. A release note is a historical record: it says what
was true when that release shipped. For current behavior, read the code, tests
and the [architecture map](../ARCHITECTURE.md).

Before relaxing or reshaping one of these invariants, read the release that
introduced it. The "Guards" column says what change should trigger that read.

| Release | Delivered | Guards |
| --- | --- | --- |
| [0.3.14](RELEASE_0.3.14.md) | Every observed commit opens a durable repository reconciliation | Repository sync, reconciliation barrier |
| [0.3.15](RELEASE_0.3.15.md) | Exactly one ordinary commit per Developer candidate | Candidate verification |
| [0.3.16](RELEASE_0.3.16.md) | Rework candidates may descend from the prior assessed candidate | Candidate verification during rework |
| [0.3.17](RELEASE_0.3.17.md) | Reconciliation-bound retirement and `work.report_failure` | Retirement, failure reports |
| [0.3.18](RELEASE_0.3.18.md) | Lifecycle-based QA/Integrator discovery, capability blockers, role-projected packets | QA dispatch, capability gate, packet shape |
| [0.3.19](RELEASE_0.3.19.md) | Skills become short role control cards with conditional references | Expanding any entry skill |
| [0.3.20](RELEASE_0.3.20.md) | Prefix-counter identifiers, `context.resolve`, compacted `changed_paths` | Identifiers, snapshots, packet shape (also read [CONTEXT_DISTRIBUTION_REVIEW](../CONTEXT_DISTRIBUTION_REVIEW.md)) |
| [0.3.21](RELEASE_0.3.21.md) | `context_snapshot_id` required on consequential writes; barrier age escalation; bounded contracts; salted profile ids | Any of those four rules |
| [0.3.22](RELEASE_0.3.22.md) | Planner `work.discover` is a feature rollup; `DONE` leaves discovery | Discovery surface |
| [0.3.23](RELEASE_0.3.23.md) | Setup command fenced; one retype allowed for escaped underscores | Generated setup prompts (keep `python -I -m`) |
| [0.3.24](RELEASE_0.3.24.md) | A waiting role holds one long wait and stays silent (HOH-275) | `work.wait`, GUI event wait |
| [0.3.25](RELEASE_0.3.25.md) | Lease-free host-observed capability preflight; approval enforced by server (HOH-278) | QA capability approval |
| [0.3.26](RELEASE_0.3.26.md) | One suite run per ticket (gate reuses QA run), server-resolved disjoint merges, wake-up re-arm, parallel tests | Merge gate, reconciliation auto-keep, QA run reuse |
| [0.3.27](RELEASE_0.3.27.md) | Recovery and regression entries reach role packets; main green | Packet entry-type projection |
| [0.3.28](RELEASE_0.3.28.md) | Correctness wave 1: no Git in the write lock, service-enforced snapshots, idempotency, exact evidence match, sync CAS, legacy merge removed, Planner job expiry | Candidate submission, idempotency, repository sync |
| [0.3.29](RELEASE_0.3.29.md) | Wave 2: MCP error details, resolvable refs, unchanged packets, compact delivery, dir scopes, experience expiry, cycle metrics, less ceremony | Client setup docs, wait protocol wording |

Releases before 0.3.14 are described in
[RUNTIME_OPERATIONS](../RUNTIME_OPERATIONS.md),
[OPERATIONAL_VALIDATION](../OPERATIONAL_VALIDATION.md) and the Git history.
Validation records for installed releases live with the installation, in
`D:/HoH-service/release/`.
