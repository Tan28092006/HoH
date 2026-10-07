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
  idempotent, contention-retried call wrapper; `mcp/errors.py` maps every tool
  failure to one `isError` result `{code, message, details, retry_hint}`; `mcp/workers.py`
  spawns workers only in opt-in legacy mode; `mcp/http_app.py` authenticates the bearer. Over
  HTTP each bearer credential gets its own FastMCP instance, so tools outside the
  caller's role are absent. Over stdio the role comes from `HOH_AGENT_ROLE`.
- **Supervisor** — `supervisor.py` runs queued jobs in managed worker processes
  (`managed_worker.py`, `managed_process.py`) and recovers RUNNING jobs on start.
  Job kinds: QA runs, provisioning, submissions, merges and checks of main
  (`mainline`).
  `HOH_WORKER_MODE` defaults to `supervised`, so MCP only queues; the explicit
  opt-in `legacy` mode spawns unsupervised `*_worker.py` processes with no job
  lock, timeout or recovery. The desktop host and `local_agent` set `supervised`.
- **Scheduler** — `scheduler.py` sweeps expired leases, reconciles every
  previously observed repository against Git (`sweep_repositories`), and
  dispatches offers.

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

`ControlPlaneService` (`service/__init__.py`) is one class assembled from 25
mixins over a shared kernel, `service/_common.py`. Mixins import only `_common`,
but they call each other through `self`. About 156 distinct cross-mixin method
uses exist, and `validation` and `leases` act as a shared kernel for everything.
Reading one mixin rarely tells you every rule that applies; search for the
helper you are about to change across `service/`.

| Mixin module | Owns |
| --- | --- |
| `project_setup.py` | Project creation and repository configuration |
| `repository_reconciliation.py` | Repository sync, reconciliations and the barrier (state from `ExecutionGate` in `_common.py`) |
| `repository_sweep.py` | The scheduler's Git sweep: throttled observation, landings already on main (recorded ungated), stuck merge jobs |
| `run_guardrails.py` | Merge budgets, the merge-queue pause (`MergeQueueGuard`), checks of main after a landing, revert tickets |
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
| `board.py` | Board, event lists and digests for the UI; `cycle_metrics` |
| `usage.py` | `work.report_usage` and the dispatcher session soft cap |
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
  (`_event` in `validation.py`, which writes through `_write_audit_event` in
  `_common.py`). Events are an audit trail, not an event-sourced log: commands
  are not replayed, and most aggregates cannot be rebuilt from their events.
- **Ticket status and version can be folded from the log.** Ticket creation
  (`_record_ticket_created`) and every `_transition_ticket` write one
  `ticket.transitioned` event in the same transaction: from and to status, from
  and to version, the changed fields, and the new commits and (truncated) blocked
  reason. It is the canonical record of the change; the domain event a call site
  also writes (`ticket.claimed`, `candidate.frozen` ...) says why, and nothing
  that reads domain events changed. `replay_ticket_states(connection,
  project_id)` folds the transition events in sequence order and reports every
  version or status gap per ticket; comparing it with the `tickets` rows is the
  reconciliation an audit tool runs. A raw ticket write would show up as a
  divergence, which is one more reason none is allowed.
- **Whether a request's effect committed is read from the audit trail.** Each
  call runs under one correlation id (`mcp/invoke.py` and the REST middleware
  open one; `run_idempotent` opens one when none is active), and every event the
  request writes carries it. `run_idempotent` stores that id on the idempotency
  reservation. A retry that finds the reservation IN_PROGRESS past
  `IDEMPOTENCY_STALE_SECONDS` asks whether the reserving actor wrote an event
  under it: if so, the record is completed with a reconstructed
  `status: committed` outcome (the committed events' ids, versions and
  payloads) and the command does not run; if not, the key is reserved afresh
  and the command runs. Only a legacy row with no correlation id still answers
  `idempotency_unknown`. Complete and release update by `record_id` and only an
  IN_PROGRESS row, so a request that finishes after its key was reclaimed cannot
  overwrite or delete the newer reservation. Events written under the same id by
  internal identities (`lease-recovery`, `retirement-finalizer`) are housekeeping,
  not the request's effect.
- **Multi-transaction commands are covered by idempotency, not by the
  transport.** A `ContentionError` means one transaction never began, not that
  the call did nothing. When a command raises after its own event committed,
  `run_idempotent` binds the key to the committed outcome; a `ContentionError`
  there is answered with that outcome (keyed or not), any other error is raised
  and a keyed retry replays the outcome. The MCP transport retries a contended
  call only while `request_has_committed` is false. The claim keeps its 0.3.31
  post-commit tolerance on top, so it still returns its real lease when packet
  or brief compilation contends.
- **Git is reconciled by state as well as by event.** Recovery settles one merge
  job at a time from its intent; the scheduler's `sweep_repositories` also
  compares every project a Planner has observed with Git, at most once per
  `REPOSITORY_OBSERVE_SECONDS` per project (the stored `observed_at`, which the
  Planner wait checks too, so the two processes never fetch twice in a window).
  It runs the ordinary `sync_repository`, so a commit opens its reconciliation
  with no Planner chat open; it never takes a project's first observation,
  which opens the baseline and belongs to a Planner. An open ticket whose
  candidate Git already has (by ancestry, or by patch-id: a default-branch
  commit whose diff equals `git diff base candidate`) is recorded through
  `record_external_landing` as an ungated landing by `system:sweep`; a live
  lease or job defers it to a later sweep, and any other refusal falls back to
  one `candidate_already_on_main` Planner job (deduplicated by its audit event).
  It gives Planner one job per merge job RUNNING past `MERGE_JOB_STUCK_SECONDS`
  that recovery leaves marked for reconciliation (`merge_job_stuck`, with the
  Git facts). Planner ends a stuck job with
  `planner.close_stuck_merge_job`: recovery runs once more under the repository
  lock (a live operation is never taken over), then the job ends FAILED, its
  intent stays and its lease is released, so `record_external_landing` or a
  retirement can follow. Git, as everywhere, runs outside write transactions.
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
| No new claim, offer, submission or merge while a project-wide reconciliation is pending, or for a ticket a pending ticket-scoped one froze | `ExecutionGate` in `service/_common.py`: `require_open(connection, project_id, ticket_id)` (via `_require_repository_reconciled`) from leases, candidates, integration, worktrees, policy and authoring; `is_closed(..., ticket_id)` for offers in `discovery.py`. The frozen set (`_frozen_ticket_closure` in `repository_reconciliation.py`) is stored on the reconciliation row. `set_ticket_ready` does not call it | `test_team_quality_gates.py`, `test_repository_sync.py`, `test_scoped_barrier.py` |
| Consequential writes name a fresh packet | `require_fresh_context` in `context.py`; `require_snapshot_id` in `service/_common.py`, called by `submit_candidate`, `queue_candidate_submission`, `submit_qa_verdict` and `queue_merge_candidate`, so REST and MCP both require it | `test_context_snapshots.py`, `test_team_quality_gates.py` |
| Candidate is one ordinary commit on the frozen base | Candidate verification in `candidates.py` / `git_worktrees.py` | `test_candidate_submission.py` |
| A drifted queued candidate is rebased server-side into a derived candidate (`derived_from`) that lands only after the frozen suite passes on it, by compare-and-set; QA carries over only when main's changed paths miss the candidate's scope roots; at most `MAX_AUTO_REBASES` per ticket | `_auto_rebase_drifted` in `integration.py`; `rebase_candidate`, `cas_fast_forward` in `git_worktrees.py` | `test_auto_rebase.py` |
| Merge only the current candidate after a fresh QA pass and isolated gate | `integration.py` queued merge and `merge_intents` reconciliation | `test_integration_async_gate.py`, `test_integration_release_policy.py` |
| Git is observed without a waiting Planner; a candidate already on main (ancestry or patch-id) is recorded ungated by `system:sweep`, never as QA approval; a merge job recovery cannot settle reaches Planner once | `sweep_repositories` and `_flag_candidates_already_on_main` in `repository_sweep.py`, called by `scheduler.sweep_once`; `equivalent_landing` in `git_worktrees.py`; throttle `REPOSITORY_OBSERVE_SECONDS` on `project_repositories.observed_at`, shared with `event_wait`; `close_stuck_merge_job` in `integration.py` | `test_repository_sweep.py` |
| No merge starts while the project's merge queue is paused (red main, failure budget, hourly budget) | `MergeQueueGuard` in `service/run_guardrails.py`: `require_open` in `queue_merge_candidate`; `merge_queue_admits` in `execute_queued_merge` and `Supervisor.tick` | — |
| Retried writes do not repeat effects | `service/idempotency.py` (payload-bound key stamped with the request's correlation id; a stale IN_PROGRESS key is settled from that id's audit events, committed → `status: committed` replay, otherwise re-run; complete and release by `record_id`; contention after a committed write returns the committed outcome); `_run_with_contention_retry` in `mcp/invoke.py` retries only while nothing the call wrote has committed | `test_idempotency.py` |
| Validation contracts fit the packet | Limits in `contracts.py`, checked in `project_policy.py` and published in the MCP and REST schemas | `test_team_quality_gates.py`, `test_contract_parity.py` |
| An MCP parameter checked against a fixed set offers that set as its schema `enum` | `Literal` types in `contracts.py`, whose constants the service checks | `test_mcp_enum_params.py` |
| Every MCP failure is one `isError` shape, audited once under the code the client reads, with no internal text | `map_error` in `mcp/errors.py`; `invoke` and `_CompactFastMCP.call_tool` in `mcp/invoke.py` | `test_mcp_error_shape.py` |
| Advertised capabilities name only tools the role has | `CAPABILITY_TOOLS` and `capabilities()` in `discovery.py` | `test_contract_parity.py` |
| The third QA rejection blocks the ticket for Planner; only a recorded resume resets the count | `MAX_QA_REJECTIONS` in `_common.py`; `submit_qa_verdict` in `qa.py`; `resume_ticket_after_failure` in `planner_jobs.py` | `test_rework_budget.py` |
| Every ticket's status and version is explained by its logged transitions | `_record_ticket_created` and `_transition_ticket` write `ticket.transitioned` in the same transaction; `replay_ticket_states` in `_common.py` folds them | `test_ticket_replay.py`, `test_ticket_transitions.py` |
| Agents read 12-character SHAs; the store keeps full ones; a commit input is resolved before comparison | `agent_facing` in `_common.py` (packets in `context.py`, results in `mcp/invoke.py`); `_full_commit` in `validation.py` | `test_short_shas.py` |
| A claim hands the dispatcher a self-contained brief; QA sees Developer claims only by reference; packets name each author | `_worker_brief` in `leases.py`; `_compact_artifacts` and `_SECTION_SOURCES` in `context.py` | `test_worker_brief.py`, `test_context_provenance.py` |
| Skill text names real tools and stays small | `tests/test_skill_assets.py` | — |
| QA's model family differs from the maker's while `checker_distinct_from_maker` is on; unknown is allowed and marked `family_unverified` | `_maker_checker_family_check` in `service/risk_policy.py`, called by `_claim` (QA) in `leases.py` and `submit_qa_verdict` in `qa.py`; the maker's family is stamped on `candidates` at freeze | `test_risk_policy.py` |
| A medium-risk candidate merges only with `adversarial_review` evidence, a high-risk one also only after `policy.approve_high_risk` on that candidate | `_require_risk_tier_cleared` in `service/risk_policy.py`, called by `queue_merge_candidate` in `integration.py` | `test_risk_policy.py` |

## Design decisions

Owner-approved choices, with the reason, so a later change can tell a rule from
an accident. The release note that shipped each one has the history.

### Scope-partitioned barrier (wave 6D)

The barrier protects the plan, not `main`: fast-forward-only merges of a
candidate QA ran on its exact base already keep `main` correct. So the scope of
a reconciliation follows what the result can have invalidated.

- **Project-wide** (`scope = 'project'`): a commit on the default branch
  observed by `planner.sync_repository` (wave 7A; other branches are recorded
  only) that touches docs/spec or a root file, rewrites history, has an unknown
  or truncated diff, or moves neither the default branch nor its upstream; and
  base drift onto a `main` HoH had not observed. Later syncs extend the one
  pending observation instead of opening another, and never narrow it. A
  successful HoH merge no longer opens one (wave 7C, below).
- **Ticket-scoped** (`scope = 'tickets'`): a QA rejection, an integration gate
  regression, and base drift onto a `main` HoH had already observed. None moves
  `main` (the drift's movement was a merge or a sync that opened, and Planner
  resolved or auto-kept, its own reconciliation), so the frozen set is the
  judged ticket plus every open ticket depending on it, transitively. Only those
  tickets are captured, have their offers withdrawn and are refused by the gate;
  every other ticket keeps flowing. The judged ticket is REWORK or BLOCKED as
  before; status alone could not freeze it, because a Developer may claim
  REWORK, and dependents keep their status rather than round-tripping through
  BLOCKED.
- One gate, not two: `ExecutionGate` takes an optional `ticket_id`. A ticket is
  closed while any pending project-wide reconciliation exists or any pending
  ticket-scoped one names it, so a second reconciliation never shrinks the
  first's freeze. A call about no particular ticket sees only the project-wide
  gate. A packet's `planning_barrier` lists only what applies to its ticket, so a
  rejection elsewhere does not stale it. Execution roles are not woken by a
  ticket-scoped barrier; Planner is, with `scope` and `frozen_ticket_ids`.
- Planner resolves both kinds with `planner.resolve_repository_reconciliation`.
  Commit order is kept where reconciliations meet: a project-wide one waits for
  every older one, and two ticket-scoped ones wait only when they captured a
  common ticket.
- **Merges ran in shadow mode until wave 7C.** A merge that auto-keep could
  not resolve closed the whole project. Beside it, `_shadow_merge_partition` stores in
  `shadow_partition` what a scope rule would have frozen: tickets whose scope
  root overlaps a merged path, unscoped tickets, and tickets depending on the
  merged one, transitively; docs/spec, root-file or unknown paths freeze
  everything. When Planner resolves it, `shadow_agreed` records whether every
  ticket Planner did not `keep` fell inside that set, and the outcome is added
  beside the prediction, never into Planner's manifest. `planner.cycle_metrics`
  reports agreement as `barrier_shadow`. Wave 7C now acts on the same rule, so
  merges no longer write new shadow rows; the existing ones remain the record.

### Planner off the critical path (wave 7C, part 1)

Owner-approved: nothing routine waits on Planner. Correctness of `main` is
still held by the fast-forward of a QA-verified tree; only the plan can go
stale, and that is now caught where it matters, at the next QA.

- **HoH's own merge opens no barrier.** `_open_commit_reconciliation` with
  `merged_ticket_id` computes `_shadow_merge_partition` (rule
  `scope-closure-v1`: scope-root overlap, unscoped tickets, transitive
  dependents of the merged ticket; docs/spec, root-file or unknown paths reach
  every open ticket) and writes the reconciliation RESOLVED at once, every
  ticket `keep`, `resolved_by` `system:merge-advisory` (or
  `system:scope-disjoint` when nothing is reached). No offer is withdrawn. The
  reached tickets get `tickets.planning_base_stale` (JSON: merge commit,
  merged ticket, reconciliation, reason, `intent_check: required`) through
  `_flag_planning_base_stale`, which does not bump the ticket version, so live
  leases keep their `expected_version`. Planner receives
  `project.planning_base_advanced` listing them, plus the existing
  `delivery_completed` report; neither blocks anything.
- **`intent_check` at the next QA.** Packets show `planning_base_stale` to
  Developer, QA and Integrator; QA and Integrator packets add
  `ticket.intent_check` with the instruction. `qa.submit_verdict` accepts a
  finding with criterion `intent_check` outside the contract while the flag is
  set, refuses a `passed` verdict without a verified one, and clears the flag
  on a pass. A `failed` verdict whose only non-verified finding is
  `intent_check` blocks the ticket with `blocked_reason = plan_stale`
  (event `ticket.plan_stale`), does not count against the rework budget, and
  opens the usual QA-failure Planner job. Choosing a different model for the
  reviewer is advice in the QA card, not yet enforced (that is wave 7C-3).
- **Outside commits freeze only what they reach.** `_sync_repository_once`
  asks `_outside_commit_frozen_tickets`: for a fast-forward of the default
  branch and/or upstream with a known diff (under 200 paths), the same
  closure without a merged ticket, plus every open dependent of a reached
  ticket, becomes a `scope = 'tickets'` reconciliation; offers are withdrawn
  only for those tickets. When the diff reaches no ticket the record is
  written RESOLVED by `system:no-ticket-reached` with no decisions (nothing is
  kept, so outside commits are still never auto-kept). Project-wide remains
  for the baseline, docs/spec or root files, non-fast-forward (rewritten or
  new upstream) history, unknown or truncated diffs, and an observation that
  moved only non-default branches.
- **Planner is an asynchronous advisor.** It still resolves the reconciliations
  above and can stop, revise or retire any ticket at any time through the
  existing tools; the skill card describes it as an advisor.

### A merge lane beside the test slot (wave 6E)

The supervisor runs one project suite at a time because more overran host
memory (release 0.3.30). That slot is held by what runs a suite, not by job
kind: a QA job, or a merge whose gate executes the suite. A merge whose gate
reuses a passing QA run (`gate_mode: reused_qa_run`) needs only Git, so
`Supervisor._lane` admits it to a separate merge lane and it no longer waits up
to 2,100 s behind a QA suite. Merges stay one at a time; the repository lock
serializes them anyway. The lane is decided before admission from
`merge_gate_reuses_qa_run`, whose inputs are frozen (a candidate's test profile
is written once; a completed passing QA run is never rewritten), so the answer
can only turn from no to yes. The worker is still started with
`--without-test-slot` and `execute_queued_merge(run_suite=False)` leaves the job
QUEUED, untouched, if it would have to run the suite, so a misjudged lane can
never put a second suite on the host.

### Landings are recorded, then main is checked (wave 7C part 2)

- **Auto-recorded landings.** The sweep records work already on main as an
  ungated landing (`integration.landed_ungated`, `gated: false`,
  `qa_verified: false`, `detection.match` = `ancestry` or `patch_id`) attributed
  to `system:sweep`, instead of only opening a Planner job. It never certifies a
  QA verdict or criterion. Patch-id matching (`git patch-id --stable` over
  `git diff base candidate` and the last 200 non-merge commits in
  `base..main`) catches applied diffs and cherry-picks, as HOH-280 landed.
- **Check of main.** Every gated merge and every recorded landing queues one
  `mainline_checks` row, run by the supervisor (`mainline` kind, test slot) with
  the project's current test command on that main commit. A waiting check is
  retargeted to the newest main rather than queueing a second suite. A gated
  fast-forward is recorded as `covered_by_gate` without a run: main is exactly
  the tree the gate just tested with the frozen command. A red check pauses the
  merge queue (`mainline_check_failed`), opens a priority-95 BACKLOG ticket of
  type `revert` naming the commit (git revert or a fix, through the gate; never a
  reset), and raises a high-severity Planner job. An unrunnable check counts as
  a failure but never as red.
- **Budgets.** `project_run_guardrails` holds `max_merges_per_hour` (default 12,
  one test slot's suite rate) and `max_consecutive_failures` (default 3: merge
  gate regressions and infrastructure failures, red or unrunnable checks; reset
  by a merge or a green check). Over the hourly budget, queued merges wait and
  the pause clears itself; any other pause holds until
  `planner.resume_merge_queue`. The pause is a merge-queue flag, not an
  `ExecutionGate` barrier: development and QA keep flowing. Set the limits with
  `planner.configure_run_guardrails`.

### Latency is measured, not argued (wave 6F)

`cycle_metrics` (`board.py`; `planner.cycle_metrics`; REST `cycle-metrics`)
stays a read-only projection of rows the workflow already writes, and reports
every distribution as `count`, `p50`, `p90`. Each delivered ticket's lead time
is split into waits for the next role (ready to Developer claim, freeze to QA
claim, verdict to Integrator claim) and that role's service (claim to freeze,
QA claim to verdict, Integrator claim to merge). Markers come from the
delivered candidate: a new freeze or QA claim resets the later markers, so a
rework round is Developer service time. Project figures read the most recent
500 rows: `barrier_open_seconds` (created to resolved, by scope, excluding
server auto-keeps, plus pending age), `job_queue_seconds` (QA and merge jobs
queued to started, merges also by gate mode) and suite runs per landed ticket
(`qa_test_runs` rows: QA runs and executed gates).

### Dispatcher discipline is measured and bounded (wave 6G)

The fresh-worker-per-ticket rule was prose. It is now measured and nudged, but
deliberately not enforced: the server cannot see whether a chat delegated.

- **Report.** One small tool, `work.report_usage(ticket_id, worker, tokens_in?,
  tokens_out?)`, for execution roles, rather than optional fields on three
  writes whose payloads are bound by idempotency keys and contracts.
  `worker` is `fresh_subagent`, `new_session` or `dispatcher`
  (`contracts.WorkerKind`); token counts are optional because not every client
  shows them. Only an agent with a claim event on the ticket may report, and
  rows go to `ticket_usage_reports`. A rework round reports again.
- **Measure.** `cycle_metrics.worker_usage` gives, over the delivered tickets,
  the share whose every reported round used a fresh worker and tokens per
  ticket (in, out, total) as p50/p90; each ticket row carries its `usage`.
- **Bound.** `agent.register`, which a chat calls once when it starts, stamps
  `agent_presence.session_started_at`. When that session has claimed
  `DISPATCHER_TICKET_LIMIT` (5) distinct tickets, an idle `work.wait_for_event`
  result (not a wait on the agent's own job) carries `advice:
  start_new_session`. The server never refuses a claim over it, and a presence
  from before the column has no session and gets no advice.

### Planner off the critical path (wave 7A)

The owner judged the loop too rigid to be fast. Each rule below replaces a
Planner turn that added no information with a deterministic server decision;
every one is written with a `system:*` actor, so Planner can still reverse it
with the ordinary tools.

- **Only the default branch is an iteration boundary.** `_sync_repository_once`
  opens a reconciliation for a change of the observed mainline, the upstream, or
  a remote head of the default branch (`<remote>/<default_branch>` or the
  tracking upstream). A push to any other remote branch updates
  `observed_remote_refs` and is reported in `branch_updates`, but is not
  captured as an observed commit and freezes nothing: it is not delivered
  behavior, and before this every teammate push closed the whole project.
- **One pending observation per project.** When a sync finds a change while a
  sync-opened reconciliation (project-wide, `observation_ref` a `ctx:entry:`)
  is still PENDING, it appends to that one instead of opening another: observed
  commits are the union by SHA, tickets already captured keep the version first
  observed, and newly open tickets join. The new observation still gets its own
  context entry and `project.repository_observed` event (`extended: true`).
  HOH-280 had a sweep and a Planner sync open two duplicates Planner resolved in
  turn. Reconciliations opened by merges, drift or QA results are not merged
  into it.

- **The server approves QA capability from host preflight.**
  `run_capability_preflight` (`service/capability.py`) sets the agent's approved
  profile to declared ∩ proven ∩ verification profile, bound to its registered
  machine, in the transaction that records the preflight
  (`_approve_from_preflight`, event `qa.capability_auto_approved`,
  `capability_approved_by = system:host-preflight`), and closes the review jobs
  that approval clears. Planner could only ever approve that same set
  (`_proven_by_preflight`, release 0.3.25), so its step added a wait and no
  information. Nothing is approved from a record taken on another host. The
  approval persists across sessions of that agent on that machine until its
  declaration or machine changes (`register_agent`) or the project's preflight
  commands, test command or capability profile change; then
  `_revoke_host_preflight_approvals` clears it (`system:approval-revoked`) and
  the inbox ignores preflights older than the revocation, so QA reruns one.
  `planner.configure_agent_capability_profile` stays for overrides, which a
  revocation leaves alone.

- **A rejection resolves its own barrier.** `_open_commit_reconciliation`
  takes `rework_ticket_id` and `resolved_by`. A QA rejection
  (`submit_qa_verdict`, `system:qa-rejection`) and an integration gate
  regression (`system:gate-regression`) pass the judged ticket, already REWORK
  or BLOCKED, and the ticket-scoped reconciliation is resolved in the same
  transaction: `rework` for that ticket, `keep` for its dependents, which their
  dependency edge still holds. Offers are not withdrawn. It stays PENDING for
  Planner when the rejection spent the rework budget (`MAX_QA_REJECTIONS`), for
  drift, or when an older pending reconciliation captured one of the same
  tickets (commit order). Planner kept having to restate exactly this.

- **Environment failures are not QA rejections.** `qa_runner._execute_pytest`
  classifies a run as infrastructure (`INFRASTRUCTURE_REASONS`, shared with
  `service/qa.py`; the manifest's `classification` is `infrastructure_failure`)
  on MemoryError, Windows commit exhaustion (0xC000012D) or a paging-file
  message, pytest exit 2 without a collection error, 3 or 4, a collection
  import error whose failing frame lies outside the worktree (`pytest_probe`
  `foreign_import_errors`), or a provenance mismatch. Exit 2 with the
  candidate's own collection errors stays `test_failure`. Memory, exit-code and
  foreign-import failures are retried once by the runner with half the xdist
  workers (`retry_of` in the manifest); what still fails parks the ticket for
  infrastructure as before and never reaches `MAX_QA_REJECTIONS`. A failed
  verdict whose evidence cites only infrastructure runs is refused
  (`verdict_cites_only_infrastructure`). A frozen `-n auto` runs as
  `min(CPUs, min(free commit, free physical) / 600 MB)` workers, at least one,
  from `resource_probe`; the frozen command and digest are unchanged and the
  manifest records `xdist_workers` requested and effective.
### Maker/checker families and risk tiers (wave 7C3)

- **Families.** `agent.register(model_family, model?)` stores the family on
  `agent_presence`; `local_agent setup --model` derives it (`--model-family`
  overrides) and a stdio server registers it when the agent omits it. The
  maker's family is copied onto `candidates.maker_model_family` at freeze, so a
  later re-registration cannot change who made it; a candidate without one falls
  back to its submitter's presence.
- **Checker distinct from maker** is a project policy
  (`project_risk_policies.checker_distinct_from_maker`, default on, set with
  `planner.configure_risk_policy`). A QA claim, and the verdict write, are
  refused when the families are equal. An unknown family, or `other` on either
  side, is allowed and recorded as `family_unverified` on the claim event, the
  verdict event and the evidence bundle: refusing it would lock out every agent
  that predates the field. The intent-check reviewer is a QA claim, so it is
  bound by the same rule.
- **Risk tier** is computed, never declared: from the candidate's changed paths
  once one exists, else from the write scope. `high` matches the project's high
  patterns (default: store schema and migrations, auth, credentials, `deploy/`,
  `skills/`, `docs/spec/`); `medium` matches the medium patterns (shared core
  modules) or changes more than `medium_max_files` (10) files; the rest is
  `low`. The tier is in every packet as `ticket.risk` and in `worker_brief`.
- **Gate.** Low needs the suite and scenarios. Medium also needs an evidence
  item of type `adversarial_review` on the candidate. High needs that and a
  `policy.approve_high_risk(ticket_id, reason)` record (Planner or Admin) bound
  to the current candidate, so a rework needs a fresh sign-off. The merge queue
  refuses with `adversarial_review_missing` or `high_risk_signoff_missing`.

## Change routing

| When you change | Also |
| --- | --- |
| An MCP tool's name, arguments or semantics | Update the role skill card and reference that mention it, the prompts generated by `agent_kit.py` and `local_agent.py`, and the REST model in `api.py` if one exists (a structured payload type or limit belongs in `contracts.py`, shared by both); run `test_mcp*.py`, `test_contract_parity.py`, `test_skill_*.py` |
| A ticket status or transition | `domain.py`, the guards in every mixin that checks that status, discovery filters, `docs/releases/` note; write it through `_transition_ticket` with the acting `actor` so it is logged, and run `test_ticket_replay.py` |
| Packet shape, budgets or references | `context.py`, `context.resolve`, the role skills; read `CONTEXT_DISTRIBUTION_REVIEW.md` and releases 0.3.18–0.3.21 |
| The schema | `store_schema.py` DDL and `ADDITIVE_COLUMNS`/`MIGRATED_INDEXES` in `store_migrations.py`; `postgres_store.py` translation; `test_store_migrations.py` against a copy of a legacy database |
| Wait, wake-up or event behavior | `event_wait.py`, `agent_wakeup.py`, `docs/GUI_EVENT_WAIT.md`, release 0.3.24 |
| Worker lifecycle | `supervisor.py`, the job's mixin, `docs/RUNTIME_OPERATIONS.md` |
| Anything a client installs | `skills/`, `agent_assets/`, `agent_kit.py`, `local_agent.py`; installed clients change only on upgrade |
