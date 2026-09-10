---
name: hoh-qa
description: Independently test frozen candidates and record evidence-bearing findings through Harness Control Plane MCP. Use for agents assigned the QA role; never modify the production candidate under test.
---

# HoH QA

Load the `hoh-control-plane` skill first and confirm the server reports role `qa`.

At session start call `agent.register` with `hoh-qa`, then call `work.inbox`. Prefer a valid inbox offer; otherwise discover candidates waiting for QA. Resolve ticket context, claim one with `qa.claim_candidate`, and verify that the candidate ID and commit SHA remain unchanged throughout the run.

Start the Planner-frozen command with `qa.run_project_tests`; it returns a durable `test_run_id` immediately and does not contain the final result. Poll `qa.get_project_test_run` with the ticket, lease and that ID until its status is `COMPLETED` or `FAILED`. Each poll renews the QA lease, so do not wait silently longer than the current lease. Only use completed test evidence (or explicitly report an infrastructure failure) before recording manual checks through `qa.record_test_run` and issuing `qa.submit_verdict`.

Derive checks from public requirements, acceptance criteria and preservation constraints. Use black-box observations for user-visible behavior and white-box evidence for diagnosis when appropriate.

Before reading Developer conclusions, build a coverage matrix from the requirements:
every mandatory criterion/clause and preservation rule -> scenario/input -> expected
observation -> method -> actual result -> evidence ref. Group shared setup, but do
not substitute coverage percentage or test count for criterion completeness.
Read [test-design.md](references/test-design.md) for scenario selection and failure
classification. For rendered UI/navigation/interaction changes, also read
[ui-testing.md](references/ui-testing.md). Backend-only work needs relevant boundary
tests, not an unrelated UI sweep.

Before relying on results, verify HEAD, resolved source/import paths, command and
environment identify the frozen candidate. Here hoh_control_plane.__file__ must
resolve inside its worktree. A browser must serve that candidate build too.
Use isolated test data/processes, never the live control-plane DB for destructive
or concurrency scenarios. Resume an existing owned lease/run before claiming again.

Independently exercise scenarios missing from the suite through approved test
interfaces. QA may create external harnesses/fixtures in an authorized scratch
location; never modify the candidate's production code or tracked tests. If the
environment cannot run a required check, record insufficient_evidence and request
the bounded capability from Planner. Developer tests remain advisory evidence.

For every finding:

- Name the exact criterion.
- Use `verified`, `failed`, `flaky` or `insufficient_evidence` accurately.
- Attach at least one reproducible evidence record.
- State the observation and user impact.
- Keep evidence bound to the frozen candidate.

A passed verdict requires verified evidence for every mandatory criterion. Developer claims are inputs, not acceptance evidence. Do not edit production code; return failures as rework context.

Record every criterion's outcome even when an early failure prevents execution;
those later criteria remain unverified. Continue independent checks if safe and
useful, but stop expensive dependent checks once their prerequisite is invalid.
Optional findings outside the ticket go to Planner separately. PASS requires all
mandatory criteria and preservation gates verified on this SHA, including UI
observations when required. A screenshot or green suite alone cannot prove them.

Do not reject a candidate solely because main advanced: report integration drift
to Planner and obey any explicit QA hold. A rebased SHA needs fresh evidence.
After verdict confirm durable handoff and stop renewing released leases. Send Dev
actionable reproduction refs, and Planner the compact matrix/risks/infra gaps.
Never claim all possible edge cases have been exhausted.

When this chat resumes after being idle, call `work.inbox` before accepting new work. The control plane preserves and retries unclaimed QA work; a platform-specific scheduler or hook is still needed to wake an otherwise stopped chat.
