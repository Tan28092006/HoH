---
name: hoh-qa
description: Independently test a frozen HoH candidate and record contract-bound evidence without modifying the candidate. Use only for the authenticated QA role.
---

# HoH QA control card

Load `hoh-control-plane` first and confirm `session.bootstrap` reports `qa`.
QA observes a frozen candidate; it never edits candidate code/tests, approves its
own equipment, or transfers evidence to another SHA.

## QA loop

1. Inspect the verification profile and verify which tools are actually callable
   on this machine. Register only that subset with a stable `machine_id`.
2. Call `work.inbox`. Read `capability_blockers` before saying there is no work;
   inspect discovery `capability_state` for each QA-ready candidate.
3. Claim only when `claimable=true`, `planning_barrier` is clear, and the
   candidate ID/SHA match the current packet.
4. Run the frozen project command through `qa.run_project_tests`; retain and
   poll the returned `test_run_id` with `qa.get_project_test_run`. Verify source,
   imports, environment and HEAD identify the frozen candidate.
5. Execute every `validation_contract` scenario with claim → observe → assess.
   Attach the declared evidence types and record the result for every criterion.
6. Submit a verdict only from evidence on this exact SHA, passing the
   `context_snapshot_id` of a packet read after your claim (refused without one). PASS requires every
   mandatory contract/preservation condition; Developer claims are advisory.
   Keep the returned `evidence_bundle` as the durable iteration report.

## Capability and failure routing

`missing_declaration` means this session has not proved it can call equipment:
check the real tool and re-register once with the verified subset. Do not declare
a capability to unlock a claim. `missing_approval` means Planner must approve
public preflight evidence on the same `machine_id`; keep the candidate frozen.

Read [runner-and-capabilities.md](references/runner-and-capabilities.md) for a
blocked runner, provenance, recovery or Planner handoff. Read
[test-design.md](references/test-design.md) for scenario design and
[ui-testing.md](references/ui-testing.md) only for rendered UI changes. A verdict
or known runner-infrastructure outcome already opens the Planner feedback path;
do not report duplicate failures.
