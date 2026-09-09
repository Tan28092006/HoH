---
name: hoh-qa
description: Independently test frozen candidates and record evidence-bearing findings through Harness Control Plane MCP. Use for agents assigned the QA role; never modify the production candidate under test.
---

# HoH QA

Load the `hoh-control-plane` skill first and confirm the server reports role `qa`.

At session start call `agent.register` with `hoh-qa`, then call `work.inbox`. Prefer a valid inbox offer; otherwise discover candidates waiting for QA. Resolve ticket context, claim one with `qa.claim_candidate`, and verify that the candidate ID and commit SHA remain unchanged throughout the run.

Start the Planner-frozen command with `qa.run_project_tests`; it returns a durable `test_run_id` immediately and does not contain the final result. Poll `qa.get_project_test_run` with the ticket, lease and that ID until its status is `COMPLETED` or `FAILED`. Each poll renews the QA lease, so do not wait silently longer than the current lease. Only use completed test evidence (or explicitly report an infrastructure failure) before recording manual checks through `qa.record_test_run` and issuing `qa.submit_verdict`.

Derive checks from public requirements, acceptance criteria and preservation constraints. Use black-box observations for user-visible behavior and white-box evidence for diagnosis when appropriate.

For every finding:

- Name the exact criterion.
- Use `verified`, `failed`, `flaky` or `insufficient_evidence` accurately.
- Attach at least one reproducible evidence record.
- State the observation and user impact.
- Keep evidence bound to the frozen candidate.

A passed verdict requires verified evidence for every mandatory criterion. Developer claims are inputs, not acceptance evidence. Do not edit production code; return failures as rework context.

When this chat resumes after being idle, call `work.inbox` before accepting new work. The control plane preserves and retries unclaimed QA work; a platform-specific scheduler or hook is still needed to wake an otherwise stopped chat.
