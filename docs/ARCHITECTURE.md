# Architecture map

Where code lives, which way dependencies point, and where each invariant is
actually enforced. Read this before changing a module or placing new code.
Symbols are named rather than line numbers so the map does not rot with edits.
`tests/test_architecture_boundaries.py` enforces the dependency direction below.

## What runs

One host process (`deploy/desktop/hoh_desktop.py --host` locally) serves:

- **REST + dashboard** — `api.py` (FastAPI) and `static/`. The UI is read-only.
- **MCP** — the `mcp/` package; `mcp_server.py` is the `hoh-mcp` entry point.
  `mcp/server.py` builds one FastMCP per actor from the role groups in
  `mcp/tools/` (`common`, `planner`, `developer`, `qa`, `integrator`, each with
  `register(server, service, actor, invoke)`); `mcp/invoke.py` is the audited,
  idempotent, contention-retried call wrapper and error text; `mcp/workers.py`
  spawns workers only in opt-in legacy mode; `mcp/http_app.py` authenticates the bearer. Over
  HTTP each bearer credential gets its own FastMCP instance, so tools outside the
  caller's role are absent. Over stdio the role comes from `HOH_AGENT_ROLE`.
- **Supervisor** — `supervisor.py` runs queued jobs in managed worker processes
  (`managed_worker.py`, `managed_process.py`) and recovers RUNNING jobs on start.
  `HOH_WORKER_MODE` defaults to `supervised`, so MCP only queues; the explicit
  opt-in `legacy` mode spawns unsupervised `*_worker.py` processes with no job
  lock, timeout or recovery. The desktop host and `local_agent` set `supervised`.
- **Scheduler** — `scheduler.py` sweeps expired leases and dispatches offers.

State lives in one database (SQLite by default; PostgreSQL optional) and Git:
ticket worktrees, candidates and evidence under the artifact root beside the
database.

## Layers and dependency direction

Dependencies point down this list. A module may import from layers below it,
never above.

| Layer | Modules | Notes |
| --- | --- | --- |
| Transports and client setup | `mcp_server`, `mcp/`, `api`, `auth`, `__main__`; `agent_kit`, `local_agent`, `local_credentials`, `desktop_control`, `profile_migration`, `agent_assets/` | Translate requests to service calls; build client kits and prompts |
| Orchestration | `event_wait`, `supervisor`, `scheduler`, `agent_wakeup`, `planner_wakeup`, `*_worker`, `seed`, `operations` | Long waits, wake-ups, worker processes |
| Service | `service/` (`ControlPlaneService`) | All workflow rules, role checks and state transitions |
| Adapters | `git_worktrees`, `qa_runner`, `managed_process`, `resource_lock`, `resource_probe`, `capability_probe`, `pytest_probe`, `observability` | Git, subprocesses, file locks, host probes, invocation logging |
| Storage | `store` (connection seam), `store_schema` (DDL), `store_migrations` (additive columns, indexes, SQLite rebuilds), `postgres_store`, `store_factory` | Schema, additive migrations, connections and transactions |
| Domain | `domain`, `scopes`, `contracts` | Enums, `Actor`, error types, pure helpers; `contracts` holds the pydantic types and limits for structured agent writes (verdict, findings, evidence, developer claims, validation contracts) that MCP signatures, REST models and service checks all use |

Known exceptions, listed in the boundary test: `store` sends the post-commit
wake-up hint through `event_wait`; `service.repository_reconciliation` reads `barrier_age_seconds`
from `event_wait`; `service.discovery` uses the `agent_wakeup` vocabulary.

## The service

`ControlPlaneService` (`service/__init__.py`) is one class assembled from 24
mixins over a shared kernel, `service/_common.py`. Mixins import only `_common`,
but they call each other through `self`. About 156 distinct cross-mixin method
uses exist, and `validation` and `leases` act as a shared kernel for everything.
Reading one mixin rarely tells you every rule that applies; search for the
helper you are about to change across `service/`.

| Mixin module | Owns |
| --- | --- |
| `project_setup.py` | Project creation and repository configuration |
| `repository_reconciliation.py` | Repository sync, reconciliations and the barrier (state from `ExecutionGate` in `_common.py`) |
| `project_policy.py` | Test command, verification and capability profiles, validation contracts, skill policy |
| `ticket_authoring.py` | Features and tickets: create, ready, write scope, fields, split |
| `ticket_retirement.py` | Stop, cancel, supersede and retire tickets through a pending reconciliation |
| `candidate_recovery.py` | Requeue a candidate or return it to QA |
| `requirements.py` | Requirement registration, links and coverage |
| `planner_context.py` | Planner context entries |
| `discovery.py` | `work.discover`, Planner rollup, capabilities, agent presence, inbox and offers |
| `context.py` | Role-projected ticket and feature packets, compaction, snapshots, `context.resolve`, freshness checks |
| `leases.py` | Claim, heartbeat, expiry sweep, write-scope matching |
| `candidates.py` | Candidate submission (sync and queued) and one-commit verification |
| `qa.py` | QA test runs, evidence, verdicts |
| `integration.py` | Queued merge, isolated gate, merge reconciliation, external landings |
| `planner_jobs.py` | Planner jobs, interventions, failure reports and their fingerprints |
| `worktrees.py` | Developer worktree provisioning |
| `capability.py` | QA capability declarations, preflight and approval |
| `experience.py` | Scoped advisory lessons |
| `spec.py` | Spec digest |
| `session.py` | `session.bootstrap` |
| `developer.py` | Progress and blockers |
| `board.py` | Board, event lists and digests for the UI |
| `idempotency.py` | Idempotency key reservation and replay |
| `validation.py` | Version checks, audit `_event`, bounded-text validators |

## Transactions

- A write is `with self.store.connection(immediate=True)`: SQLite
  `BEGIN IMMEDIATE`, or one PostgreSQL advisory lock per schema. **All writers are
  serialized.** Guards are Python checks, but every ticket write then goes
  through `_transition_ticket` in `service/_common.py`, which repeats the
  checked version and status as `WHERE version = ? AND status IN (...)` and
  raises `ConflictError` when no row matches (`test_ticket_transitions.py`
  forbids a raw `UPDATE tickets` elsewhere). Other tables' updates still rely on
  the serialization: do not add a write through `connection()` without
  `immediate`, and do not move PostgreSQL to row locks without guarding them.
- `tickets.status` has a `CHECK` over the `TicketStatus` values. New databases
  get it from the DDL; an existing SQLite database gets it by a verbatim table
  rebuild (`_migrate_ticket_status_check`), skipped when a stored status or
  foreign key is already invalid. An existing PostgreSQL database is not
  altered, since no PostgreSQL upgrade path is under test.
- Do not run Git, subprocesses or large file I/O inside a write transaction. The
  queued paths follow the pattern *check and mark RUNNING → external work with no
  transaction → re-check and complete*. The synchronous `submit_candidate` path follows it
  too: Git verification runs before a short re-validating write, and verdict
  evidence and project-test reports are hashed or written before the lock.
- Every state change records an `audit_events` row in the same transaction
  (`_event` in `validation.py`). Events are an audit trail, not an event-sourced
  log: current state cannot be rebuilt from them.
- Schema changes are additive (`ADDITIVE_COLUMNS`, `MIGRATED_INDEXES` in
  `store_migrations.py`) plus SQLite table rebuilds detected by schema text.
  `schema_meta` records `SCHEMA_VERSION` (`store_migrations.py`) on both
  backends. `initialize` refuses, before touching the file, a database whose
  version is newer than the code's (`SchemaVersionError`); an older or
  unrecorded one (version 0) runs the migrations and is stamped. Bump
  `SCHEMA_VERSION` with every schema change; `test_schema_version.py`.

## Where invariants are enforced

| Invariant | Enforced in | Tests |
| --- | --- | --- |
| Role sees only its tools | Per-credential FastMCP in `mcp/server.py` and `mcp/http_app.py`; `require_role` in the service | `test_mcp.py`, `test_mcp_transport_auth.py` |
| One live lease per ticket; lease is the fencing token | Partial unique index in `store_schema.py`; `_claim`, `_require_active_lease` in `leases.py` | `test_postgres_concurrency.py`, `test_git_worktrees.py` |
| No new claim, offer, submission or merge while a reconciliation is pending | `ExecutionGate` in `service/_common.py`: `require_open` (via `_require_repository_reconciled`) from leases, candidates, integration, worktrees, policy and authoring; `is_closed` for offers in `discovery.py`. `set_ticket_ready` does not call it | `test_team_quality_gates.py`, `test_repository_sync.py` |
| Consequential writes name a fresh packet | `require_fresh_context` in `context.py`; `require_snapshot_id` in `service/_common.py`, called by `submit_candidate`, `queue_candidate_submission`, `submit_qa_verdict` and `queue_merge_candidate`, so REST and MCP both require it | `test_context_snapshots.py`, `test_team_quality_gates.py` |
| Candidate is one ordinary commit on the frozen base | Candidate verification in `candidates.py` / `git_worktrees.py` | `test_candidate_submission.py` |
| Merge only the current candidate after a fresh QA pass and isolated gate | `integration.py` queued merge and `merge_intents` reconciliation | `test_integration_async_gate.py`, `test_integration_release_policy.py` |
| Retried writes do not repeat effects | `service/idempotency.py` (payload-bound key; completion and release retry contention and never mask a committed effect; a stale IN_PROGRESS key is reclaimed as `idempotency_unknown`) | `test_idempotency.py` |
| Validation contracts fit the packet | Limits in `contracts.py`, checked in `project_policy.py` and published in the MCP and REST schemas | `test_team_quality_gates.py`, `test_contract_parity.py` |
| Advertised capabilities name only tools the role has | `CAPABILITY_TOOLS` and `capabilities()` in `discovery.py` | `test_contract_parity.py` |
| The third QA rejection blocks the ticket for Planner; only a recorded resume resets the count | `MAX_QA_REJECTIONS` in `_common.py`; `submit_qa_verdict` in `qa.py`; `resume_ticket_after_failure` in `planner_jobs.py` | `test_rework_budget.py` |
| Agents read 12-character SHAs; the store keeps full ones; a commit input is resolved before comparison | `agent_facing` in `_common.py` (packets in `context.py`, results in `mcp/invoke.py`); `_full_commit` in `validation.py` | `test_short_shas.py` |
| A claim hands the dispatcher a self-contained brief; QA sees Developer claims only by reference; packets name each author | `_worker_brief` in `leases.py`; `_compact_artifacts` and `_SECTION_SOURCES` in `context.py` | `test_worker_brief.py`, `test_context_provenance.py` |
| Skill text names real tools and stays small | `tests/test_skill_assets.py` | — |

## Change routing

| When you change | Also |
| --- | --- |
| An MCP tool's name, arguments or semantics | Update the role skill card and reference that mention it, the prompts generated by `agent_kit.py` and `local_agent.py`, and the REST model in `api.py` if one exists (a structured payload type or limit belongs in `contracts.py`, shared by both); run `test_mcp*.py`, `test_contract_parity.py`, `test_skill_*.py` |
| A ticket status or transition | `domain.py`, the guards in every mixin that checks that status, discovery filters, `docs/releases/` note |
| Packet shape, budgets or references | `context.py`, `context.resolve`, the role skills; read `CONTEXT_DISTRIBUTION_REVIEW.md` and releases 0.3.18–0.3.21 |
| The schema | `store_schema.py` DDL and `ADDITIVE_COLUMNS`/`MIGRATED_INDEXES` in `store_migrations.py`; `postgres_store.py` translation; `test_store_migrations.py` against a copy of a legacy database |
| Wait, wake-up or event behavior | `event_wait.py`, `agent_wakeup.py`, `docs/GUI_EVENT_WAIT.md`, release 0.3.24 |
| Worker lifecycle | `supervisor.py`, the job's mixin, `docs/RUNTIME_OPERATIONS.md` |
| Anything a client installs | `skills/`, `agent_assets/`, `agent_kit.py`, `local_agent.py`; installed clients change only on upgrade |
