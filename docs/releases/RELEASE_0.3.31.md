# HoH 0.3.31 — role agents as subagents, guarded state, shared contracts

Waves 4 and 5 after the [2026-10 architecture review](../reviews/2026-10-harness-architecture-review.md)
and the [role-agent review](../reviews/2026-10-role-agents-as-subagents.md).

## Role agents work like subagents

- **One ticket, one fresh worker.** Claims return a `worker_brief` (ticket, role,
  lease, snapshot, skills, worktree, exact next calls). The waiting chat is a thin
  dispatcher that hands the brief to a fresh subagent, or starts a new session
  per ticket, so context no longer accumulates across tickets.
- **QA is not anchored by the maker.** QA gets the Developer's claims only by
  reference and records its own observations first.
- **Rework budget.** The third failed QA verdict blocks the ticket
  (`rework_budget_exhausted`) and opens a Planner job; a recorded resume resets it.
- **Model per role.** `local_agent setup --model`: Codex profile model, a Claude
  Code worker agent with its own model; strongest model for Planner, a
  different model for QA than for Developer.
- **Provenance.** Packets say which author wrote each section; planner text
  directs the work, everything else is advisory.
- **Short SHAs.** Agents read 12-character commit SHAs and digests and may send
  7+ character prefixes; the store keeps full values. Echoed short evidence
  digests are accepted and stored in full.

## Correctness

- `HOH_WORKER_MODE` defaults to `supervised`; `legacy` is an explicit opt-in.
- Every ticket update goes through `_transition_ticket` with version and status
  predicates in SQL; `tickets.status` has a CHECK (SQLite databases are rebuilt
  in place when safe; PostgreSQL is not altered).
- `schema_meta` records the schema version; an older binary refuses a newer
  database before touching it.
- `contracts.py` is the one source for verdict, finding, evidence and contract
  shapes and limits, used by MCP, REST and the service. `capabilities()`
  advertises only tools the role has.
- Planner reads the frozen test command from `session.bootstrap` or
  `planner.get_test_profile`.
- A committed claim no longer reports an error when compiling its packet or
  brief hits contention; it returns the lease and asks for one
  `ticket.get_context`. Before, the transport retried a claim the caller held.
- A candidate already on the default branch is no longer treated as base drift:
  queueing refuses with `candidate_already_on_main`, and a job that finds it
  landed closes without rework so the landing can be recorded.
- Four timing-sensitive tests made deterministic.

Both fixes come from the [system-design re-review](../reviews/2026-10-05-system-design-review.md).

## Behaviour changes for clients

- MCP refuses unknown keys in findings, evidence and contract entries; evidence
  needs `storage_ref`. Verdicts are `passed`/`failed` at the schema.
- Context entries carry `source` instead of `actor_role`.

## Verification

Full suite with coverage: see the release commit. UI and PostgreSQL tests not run.
