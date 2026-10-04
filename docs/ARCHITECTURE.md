# Architecture map

Where code lives, which way dependencies point, and where each invariant is
actually enforced. Read this before changing a module or placing new code.
Symbols are named rather than line numbers so the map does not rot with edits.
`tests/test_architecture_boundaries.py` enforces the dependency direction below.

## What runs

One host process (`deploy/desktop/hoh_desktop.py --host` locally) serves:

- **REST + dashboard** — `api.py` (FastAPI) and `static/`. The UI is read-only.
- **MCP** — `mcp_server.py`. Over HTTP each bearer credential gets its own
  FastMCP instance, so tools outside the caller's role are absent. Over stdio the
  role comes from `HOH_AGENT_ROLE`.
- **Supervisor** — `supervisor.py` runs queued jobs in managed worker processes
  (`managed_worker.py`, `managed_process.py`) and recovers RUNNING jobs on start.
  `HOH_WORKER_MODE=supervised` selects it; the default `legacy` mode spawns
  unsupervised `*_worker.py` processes. The desktop host sets `supervised`.
- **Scheduler** — `scheduler.py` sweeps expired leases and dispatches offers.

State lives in one database (SQLite by default; PostgreSQL optional) and Git:
ticket worktrees, candidates and evidence under the artifact root beside the
database.

## Layers and dependency direction

Dependencies point down this list. A module may import from layers below it,
never above.

| Layer | Modules | Notes |
| --- | --- | --- |
| Transports and client setup | `mcp_server`, `api`, `auth`, `__main__`; `agent_kit`, `local_agent`, `local_credentials`, `desktop_control`, `profile_migration`, `agent_assets/` | Translate requests to service calls; build client kits and prompts |
| Orchestration | `event_wait`, `supervisor`, `scheduler`, `agent_wakeup`, `planner_wakeup`, `*_worker`, `seed`, `operations` | Long waits, wake-ups, worker processes |
| Service | `service/` (`ControlPlaneService`) | All workflow rules, role checks and state transitions |
| Adapters | `git_worktrees`, `qa_runner`, `managed_process`, `resource_lock`, `resource_probe`, `capability_probe`, `pytest_probe`, `observability` | Git, subprocesses, file locks, host probes, invocation logging |
| Storage | `store`, `postgres_store`, `store_factory` | Schema, additive migrations, connections and transactions |
| Domain | `domain`, `scopes` | Enums, `Actor`, error types, pure helpers |

Known exceptions, listed in the boundary test: `store` sends the post-commit
wake-up hint through `event_wait`; `service.planning` reads `barrier_age_seconds`
from `event_wait`; `service.discovery` uses the `agent_wakeup` vocabulary.

## The service

`ControlPlaneService` (`service/__init__.py`) is one class assembled from 17
mixins over a shared kernel, `service/_common.py`. Mixins import only `_common`,
but they call each other through `self`. About 156 distinct cross-mixin method
uses exist, and `validation` and `leases` act as a shared kernel for everything.
Reading one mixin rarely tells you every rule that applies; search for the
helper you are about to change across `service/`.

| Mixin module | Owns |
| --- | --- |
| `planning.py` | Project setup, repository sync and the reconciliation barrier, project policy (test command, verification and capability profiles), ticket authoring, retirement, candidate recovery, requirements, Planner context. The largest module; see the review for a split. |
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
  serialized.** Most guards are Python checks followed by an unguarded
  `UPDATE ... WHERE ticket_id = ?`; they are correct only because of that
  serialization. Do not add a write through `connection()` without `immediate`,
  and do not move PostgreSQL to row locks without adding status or version
  predicates to the updates.
- Do not run Git, subprocesses or large file I/O inside a write transaction. The
  queued paths follow the pattern *check and mark RUNNING → external work with no
  transaction → re-check and complete*. The synchronous `submit_candidate` path follows it
  too: Git verification runs before a short re-validating write, and verdict
  evidence and project-test reports are hashed or written before the lock.
- Every state change records an `audit_events` row in the same transaction
  (`_event` in `validation.py`). Events are an audit trail, not an event-sourced
  log: current state cannot be rebuilt from them.
- Schema changes are additive (`ADDITIVE_COLUMNS`, `MIGRATED_INDEXES` in
  `store.py`) plus SQLite table rebuilds detected by schema text. There is no
  schema version number.

## Where invariants are enforced

| Invariant | Enforced in | Tests |
| --- | --- | --- |
| Role sees only its tools | Per-credential FastMCP in `mcp_server.py`; `require_role` in the service | `test_mcp.py`, `test_mcp_transport_auth.py` |
| One live lease per ticket; lease is the fencing token | Partial unique index in `store.py`; `_claim`, `_require_active_lease` in `leases.py` | `test_postgres_concurrency.py`, `test_git_worktrees.py` |
| No new claim, offer, submission or merge while a reconciliation is pending | `_require_repository_reconciled` in `planning.py`, called from leases, candidates, integration, worktrees; inline check for offers in `discovery.py`. `set_ticket_ready` does not call it | `test_team_quality_gates.py`, `test_repository_sync.py` |
| Consequential writes name a fresh packet | `require_fresh_context` in `context.py`; `require_snapshot_id` in `service/_common.py`, called by `submit_candidate`, `queue_candidate_submission`, `submit_qa_verdict` and `queue_merge_candidate`, so REST and MCP both require it | `test_context_snapshots.py`, `test_team_quality_gates.py` |
| Candidate is one ordinary commit on the frozen base | Candidate verification in `candidates.py` / `git_worktrees.py` | `test_candidate_submission.py` |
| Merge only the current candidate after a fresh QA pass and isolated gate | `integration.py` queued merge and `merge_intents` reconciliation | `test_integration_async_gate.py`, `test_integration_release_policy.py` |
| Retried writes do not repeat effects | `service/idempotency.py` (payload-bound key; completion and release retry contention and never mask a committed effect; a stale IN_PROGRESS key is reclaimed as `idempotency_unknown`) | `test_idempotency.py` |
| Validation contracts fit the packet | Size limits in `planning.py` | `test_team_quality_gates.py` |
| Skill text names real tools and stays small | `tests/test_skill_assets.py` | — |

## Change routing

| When you change | Also |
| --- | --- |
| An MCP tool's name, arguments or semantics | Update the role skill card and reference that mention it, the prompts generated by `agent_kit.py` and `local_agent.py`, and the REST model in `api.py` if one exists; run `test_mcp*.py`, `test_contract_parity.py`, `test_skill_*.py` |
| A ticket status or transition | `domain.py`, the guards in every mixin that checks that status, discovery filters, `docs/releases/` note |
| Packet shape, budgets or references | `context.py`, `context.resolve`, the role skills; read `CONTEXT_DISTRIBUTION_REVIEW.md` and releases 0.3.18–0.3.21 |
| The schema | `store.py` DDL and `ADDITIVE_COLUMNS`/`MIGRATED_INDEXES`; `postgres_store.py` translation; `test_store_migrations.py` against a copy of a legacy database |
| Wait, wake-up or event behavior | `event_wait.py`, `agent_wakeup.py`, `docs/GUI_EVENT_WAIT.md`, release 0.3.24 |
| Worker lifecycle | `supervisor.py`, the job's mixin, `docs/RUNTIME_OPERATIONS.md` |
| Anything a client installs | `skills/`, `agent_assets/`, `agent_kit.py`, `local_agent.py`; installed clients change only on upgrade |
