# Scenario selection and evidence

Use a compact matrix: criterion/preservation rule | scenario and expected result |
method | actual result/status | evidence ref. Cover every mandatory clause and
select risks from the changed behavior, rather than applying every case blindly.

- Inputs/data: absent versus empty, min/max and just outside bounds, malformed
  values, Unicode/long text, duplicates, ordering, pagination and existing data.
- Lifecycle: invalid transition, stale version, expired/wrong-owner lease, late
  old-worker write, cancellation, resume and recovery.
- Concurrency/durability when affected: competing claims, disjoint tickets, lost
  acknowledgment/retry, duplicate dispatch, restart, partial failure, old-schema
  migrations and recovery without duplicate effects. Synchronize races using
  isolated processes/databases and retain observed final state.
- Authorization when affected: wrong role/project, tampered refs, secret leakage,
  path traversal and scope enforcement. Use authorized disposable fixtures.
- Integration: real boundary calls plus units; mocks must not hide the behavior
  under test. Inspect deleted tests, weakened assertions and skips against the
  requirement. Include adjacent verified behavior that this change could regress.

Normal use and relevant failure boundaries need coverage. Mark risk categories
not applicable with a reason when omission would otherwise be ambiguous. Run cheap
artifact/setup checks first, then focused scenarios and the registered regression
suite. Retain one valid authoritative run per SHA/environment/command; repeat only
for changed conditions, flaky diagnosis or policy. A bounded diagnostic repeat
(normally at most two) must preserve failures as well as successes; never rerun
until green. Budget exhaustion leaves unchecked criteria insufficient, not passed.

For each observation retain criterion, SHA, command/scenario, fixture/seed,
environment/source identity, expected/actual result, impact, reproduction and
log/trace/screenshot refs. Separate observed fact from suspected cause. Coverage
thresholds are supplementary and never establish all acceptance behavior.

Use verified for evidence-supported behavior, failed for a valid contradiction,
flaky for inconsistent comparable observations (include attempts/conditions), and
insufficient_evidence for missing/invalid observations or infrastructure failure.
An import path pointing to main invalidates attribution to the frozen candidate:
request runner repair and rerun the same SHA. A timeout/DB error/missing browser is
not proof of a product bug. When the API requires a non-pass verdict, use its
supported insufficient-evidence representation and name the infrastructure owner.
