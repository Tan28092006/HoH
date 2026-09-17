# HoH 0.3.21 — four enforcement gaps closed

Each change turns a rule the skills stated into a rule the server enforces. The
common failure they address is the same: an agent that skipped the rule got no
signal, so the iteration was paid for twice.

## 1. A consequential write must name the packet it was decided from

`dev.submit_candidate`, `qa.submit_verdict` and `integrator.merge_candidate` now
require `context_snapshot_id`. It was optional, and omitting it skipped the
freshness check entirely, leaving only `expected_version` — which does not move
when a Planner decision, the repository, the barrier, a retirement or a
dependency changes.

The three tools declare the parameter as required in their schema, so a client
sees it, and an empty or blank value is refused with an instruction rather than
accepted. Nothing else changed: `work.claim`, `qa.claim_candidate` and the
service layer still accept an optional snapshot, because an internal worker has
no packet to quote.

Effect per round: a submission, verdict or merge decided from a packet that has
since moved is refused before Git runs, instead of producing a candidate QA must
reject or a merge Planner must reconcile.

## 2. A barrier that nobody resolves gets louder

A reconciliation barrier freezes offers, claims, submissions and merges until
Planner audits every open ticket. If no Planner chat is open, every other role
sat silent on a single `repository_reconciliation_required` notification.

Waiting roles now receive a `planning_barrier_pending` event once the barrier
crosses 30 minutes, 2 hours, 8 hours and 24 hours. It carries `opened_at`,
`age_seconds`, `escalation_level` and `pending_count`, and its `next_action`
names the age and tells a non-Planner role to ask the operator to resume a
Planner. Each level has its own event key, so an escalation is delivered even
though the first notification for that barrier was already acknowledged.
Planner keeps `repository_changed` as its event kind and is told the age without
being told to find a Planner.

`work.inbox` and the dashboard overview report `age_seconds` per barrier and
`oldest_age_seconds`. Ticket packets carry `opened_at` only: a clock reading
inside a packet would make every read a different content digest and defeat
snapshot stability.

## 3. A validation contract must fit the packet it will live in

The `ticket` section is protected from compaction, so a contract is carried in
full by every packet for the ticket's whole life. Limits were 2,000 characters
per criterion and 4,000 per expected observation, which is more than a whole
role packet's budget.

New bounds: 600 characters per criterion, 600 per expected observation, 200 per
scenario ref, and the whole contract at most 1,200 estimated tokens. The refusal
names the measured size and the budget and suggests splitting the ticket.
`planner.configure_validation_contract` returns `contract_estimated_tokens`, so a
Planner sees the permanent cost at the moment it commits to it.

Measured against the live database, the largest contract on any ticket is 1,031
tokens with a 372-character criterion, and no open ticket would be refused by
the new bounds.

## 4. Two installations must never name the same branch

A profile identity was `sha256(project:role:instance)[:16]`, with no
installation component. Two HoH installations sharing a Git remote and using the
same project, role and instance names derived the same agent id, and therefore
the same branch and worktree names. The server refused to reuse an existing
branch, so the failure was loud, but it was avoidable.

New profiles are salted with the installation id. A profile that already exists
keeps its legacy identity: `installed_profile_actor` returns the unsalted id when
a credential row or an `agent_presence` row for it exists, so an installed chat
keeps its credential, leases, worktrees and branches. `profile_actor` without a
salt is unchanged and remains the legacy derivation.

## Compatibility

- No schema change.
- Breaking for MCP clients only in that three tools now require an argument the
  cards already told agents to pass. Reload the client so it sees the schema.
- Skills: Developer, QA and Integrator cards name the required snapshot; the
  common card explains the barrier escalation. Planner card unchanged.
- New tests: `tests/test_team_quality_gates.py` (12).
