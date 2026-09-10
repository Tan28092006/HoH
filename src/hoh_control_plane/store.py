from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from hoh_control_plane.domain import DomainError


class ContentionError(DomainError):
    """The database could not acquire its write lock under concurrent load.

    This is the one documented, retryable error a caller sees instead of a raw
    SQLite "database is locked"/"database is busy" message: no statement in the
    attempted transaction ran, so retrying the same request from scratch is safe
    and is expected to succeed once the competing writer finishes.
    """

    code = "database_contention"
    status_code = 503


# SQLite's own busy handler (set by connect(timeout=...) and PRAGMA busy_timeout
# below) already blocks a single BEGIN IMMEDIATE for this long before raising --
# it is a brief, self-clearing wait for a short-lived local transaction, not a
# way to mask a slow external operation that should never run inside a
# transaction in the first place (see connection() below). The bounded
# retry/backoff policy this budget participates in lives one layer up, in
# mcp_server.invoke(), which retries the whole call after a ContentionError.
_DEFAULT_BUSY_TIMEOUT_SECONDS = 10.0


def _is_lock_contention(error: sqlite3.OperationalError) -> bool:
    message = str(error).lower()
    return "locked" in message or "busy" in message


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    default_branch TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_repositories (
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
    repository_path TEXT NOT NULL,
    configured_by TEXT NOT NULL,
    configured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_test_profiles (
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
    test_command TEXT NOT NULL,
    timeout_seconds INTEGER NOT NULL,
    configured_by TEXT NOT NULL,
    configured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS features (
    feature_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    parent_feature_id TEXT REFERENCES features(feature_id),
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'in_progress',
    priority INTEGER NOT NULL DEFAULT 50,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY,
    feature_id TEXT NOT NULL REFERENCES features(feature_id),
    parent_ticket_id TEXT REFERENCES tickets(ticket_id),
    ticket_type TEXT NOT NULL DEFAULT 'feature_work',
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50,
    required_role TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL,
    preservation_constraints TEXT NOT NULL,
    file_scope TEXT NOT NULL,
    blocked_reason TEXT,
    base_commit TEXT,
    candidate_commit TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ticket_dependencies (
    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    depends_on_ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    PRIMARY KEY (ticket_id, depends_on_ticket_id)
);

CREATE TABLE IF NOT EXISTS context_entries (
    entry_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    feature_id TEXT REFERENCES features(feature_id),
    ticket_id TEXT REFERENCES tickets(ticket_id),
    entry_type TEXT NOT NULL,
    content TEXT NOT NULL,
    source_ref TEXT,
    artifact_commit TEXT,
    actor_id TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    superseded_by TEXT REFERENCES context_entries(entry_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leases (
    lease_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    agent_id TEXT NOT NULL,
    role TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS leases_one_per_ticket ON leases(ticket_id);

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    base_commit TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    branch_ref TEXT NOT NULL,
    developer_claims TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    frozen_at TEXT NOT NULL
);

-- Changed paths are derived from immutable Git commits, rather than supplied
-- by a Developer, so they can enforce the ticket's declared write scope.
CREATE TABLE IF NOT EXISTS candidate_changed_paths (
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    path TEXT NOT NULL,
    PRIMARY KEY (candidate_id, path)
);

CREATE TABLE IF NOT EXISTS candidate_test_profiles (
    candidate_id TEXT PRIMARY KEY REFERENCES candidates(candidate_id),
    test_command TEXT NOT NULL,
    timeout_seconds INTEGER NOT NULL,
    configured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worktrees (
    ticket_id TEXT PRIMARY KEY REFERENCES tickets(ticket_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    agent_id TEXT NOT NULL,
    repository_path TEXT NOT NULL,
    worktree_path TEXT NOT NULL UNIQUE,
    branch_ref TEXT NOT NULL UNIQUE,
    base_commit TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_verified_at TEXT
);

-- `worktrees` holds only the single active assignment, so a takeover would
-- otherwise erase who previously held the ticket. Assignment history is kept
-- append-only beside it and is never rewritten.
CREATE TABLE IF NOT EXISTS worktree_assignments (
    assignment_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    agent_id TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    branch_ref TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    seed_commit TEXT,
    handoff_mode TEXT,
    previous_agent_id TEXT,
    assigned_at TEXT NOT NULL,
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS worktree_assignments_ticket_assigned
    ON worktree_assignments(ticket_id, assigned_at);

-- Worktree creation can invoke external Git operations. Keep that work out
-- of a synchronous MCP request while preserving its Developer lease and
-- a durable polling record.
CREATE TABLE IF NOT EXISTS worktree_provision_jobs (
    provision_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    lease_id TEXT NOT NULL,
    expected_version INTEGER NOT NULL,
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')),
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS worktree_provision_jobs_ticket_created
    ON worktree_provision_jobs(ticket_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS worktree_provision_jobs_one_active_ticket
    ON worktree_provision_jobs(ticket_id) WHERE status IN ('QUEUED', 'RUNNING');

CREATE TABLE IF NOT EXISTS candidate_submission_jobs (
    submission_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    lease_id TEXT NOT NULL,
    expected_version INTEGER NOT NULL,
    commit_sha TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    branch_ref TEXT NOT NULL,
    developer_claims TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')),
    error_message TEXT,
    candidate_id TEXT REFERENCES candidates(candidate_id),
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS candidate_submission_jobs_ticket_created
    ON candidate_submission_jobs(ticket_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS candidate_submission_jobs_one_active_ticket
    ON candidate_submission_jobs(ticket_id) WHERE status IN ('QUEUED', 'RUNNING');

-- Integration merge can invoke external Git operations (fast-forward, base
-- drift detection). Keep that work out of a synchronous MCP request and out
-- of any BEGIN IMMEDIATE write transaction, while preserving the Integrator
-- lease and a durable polling record.
CREATE TABLE IF NOT EXISTS merge_jobs (
    merge_job_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    lease_id TEXT NOT NULL,
    expected_version INTEGER NOT NULL,
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')),
    merge_status TEXT CHECK (merge_status IN ('merged', 'base_drift')),
    mainline_commit TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS merge_jobs_ticket_created
    ON merge_jobs(ticket_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS merge_jobs_one_active_ticket
    ON merge_jobs(ticket_id) WHERE status IN ('QUEUED', 'RUNNING');

CREATE TABLE IF NOT EXISTS qa_test_runs (
    test_run_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    command_digest TEXT NOT NULL,
    exit_code INTEGER,
    timed_out INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL,
    storage_ref TEXT NOT NULL,
    report_path TEXT NOT NULL,
    executed_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS qa_test_runs_candidate_created
    ON qa_test_runs(candidate_id, created_at);

CREATE TABLE IF NOT EXISTS qa_test_jobs (
    test_run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    -- A job retains the lease identifier that authorized it as audit provenance.
    -- It must not prevent the live, expiring lease from being reclaimed.
    lease_id TEXT NOT NULL,
    test_command TEXT NOT NULL,
    command_digest TEXT NOT NULL,
    timeout_seconds INTEGER NOT NULL,
    worktree_path TEXT NOT NULL,
    candidate_commit TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'STALE')),
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS qa_test_jobs_candidate_created
    ON qa_test_jobs(candidate_id, created_at);
CREATE INDEX IF NOT EXISTS qa_test_jobs_active_candidate
    ON qa_test_jobs(candidate_id, status);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    criterion TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    observation TEXT NOT NULL,
    storage_ref TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qa_findings (
    finding_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    criterion TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    observation TEXT NOT NULL,
    evidence_refs TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Presence is advisory: the authenticated Actor remains the authority for role
-- and project scope.  These rows only let the dispatcher find a live agent.
CREATE TABLE IF NOT EXISTS agent_presence (
    agent_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    project_ids TEXT NOT NULL,
    skills TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity BETWEEN 1 AND 20),
    machine_id TEXT,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_presence_role_expiry
    ON agent_presence(role, expires_at);

-- A queue item is created atomically with every hand-off.  It persists when
-- no appropriate agent is online, so it never depends on a chat being open.
CREATE TABLE IF NOT EXISTS work_queue (
    queue_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    target_role TEXT NOT NULL,
    context_version INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'offered', 'claimed', 'cancelled')),
    claimed_by TEXT,
    claimed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS work_queue_one_active_ticket
    ON work_queue(ticket_id) WHERE status IN ('pending', 'offered');
CREATE INDEX IF NOT EXISTS work_queue_dispatch
    ON work_queue(status, target_role, priority DESC, created_at ASC);

-- The outbox is the durable adapter boundary.  A later Slack, webhook or
-- desktop notification adapter can consume it without changing ticket state.
CREATE TABLE IF NOT EXISTS dispatch_outbox (
    outbox_id TEXT PRIMARY KEY,
    queue_id TEXT NOT NULL UNIQUE REFERENCES work_queue(queue_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    target_role TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'dispatched', 'cancelled')),
    created_at TEXT NOT NULL,
    dispatched_at TEXT
);
CREATE INDEX IF NOT EXISTS dispatch_outbox_status
    ON dispatch_outbox(status, created_at);

-- Offers may be fanned out to a few live agents.  The ticket lease remains
-- the single source of truth: the first successful claim closes every offer.
CREATE TABLE IF NOT EXISTS work_offers (
    offer_id TEXT PRIMARY KEY,
    queue_id TEXT NOT NULL REFERENCES work_queue(queue_id),
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('offered', 'claimed', 'expired', 'cancelled')),
    offered_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS work_offers_agent_status
    ON work_offers(agent_id, status, expires_at);
CREATE INDEX IF NOT EXISTS work_offers_queue_status
    ON work_offers(queue_id, status);

-- A consequential command can lose its acknowledgment on the way back to the
-- caller. This record lets the identical retry replay the original result
-- instead of creating a second lease, candidate, verdict or event. Only the
-- request digest is retained, never the raw payload, and project_id is stored
-- as '' rather than NULL so the uniqueness scope holds for unscoped commands.
CREATE TABLE IF NOT EXISTS idempotency_records (
    record_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT '',
    operation TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('IN_PROGRESS', 'COMPLETED')),
    response TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    expires_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idempotency_records_scope
    ON idempotency_records(actor_id, project_id, operation, idempotency_key);
CREATE INDEX IF NOT EXISTS idempotency_records_expiry
    ON idempotency_records(expires_at);

-- A QA failure or a Developer blocker is evidence that someone must plan against.
-- Recording it as its own durable job lets one Planner pick the problem up later
-- without rescanning ticket history, and keeps that queue separate from the
-- executable Developer/QA/Integrator queue in work_queue.
CREATE TABLE IF NOT EXISTS planner_jobs (
    planner_job_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    job_type TEXT NOT NULL CHECK (
        job_type IN ('qa_failure', 'developer_blocker', 'integration_conflict')
    ),
    status TEXT NOT NULL CHECK (status IN ('READY', 'IN_PROGRESS', 'RESOLVED')),
    summary TEXT NOT NULL,
    candidate_id TEXT REFERENCES candidates(candidate_id),
    evidence_refs TEXT NOT NULL,
    context_entry_id TEXT REFERENCES context_entries(entry_id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolution TEXT,
    resolved_by TEXT,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS planner_jobs_project_status
    ON planner_jobs(project_id, status, created_at);
-- One open planning problem per ticket: later evidence updates that job rather
-- than queueing a second Planner into the same decision.
CREATE UNIQUE INDEX IF NOT EXISTS planner_jobs_one_open_per_ticket
    ON planner_jobs(ticket_id) WHERE status IN ('READY', 'IN_PROGRESS');

-- Planner jobs lease separately from tickets: a ticket lease is the exclusive
-- right to change an artifact, while this is the exclusive right to decide.
CREATE TABLE IF NOT EXISTS planner_job_leases (
    lease_id TEXT PRIMARY KEY,
    planner_job_id TEXT NOT NULL REFERENCES planner_jobs(planner_job_id),
    agent_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS planner_job_leases_one_per_job
    ON planner_job_leases(planner_job_id);

-- A Planner job is durable but silent: without this, an urgent blocker waits for
-- the next backlog scan. One intervention request per open Planner job carries a
-- bounded, redacted pointer to the evidence so an adapter can wake a Planner.
-- It records delivery, never the planning decision, and never ticket ownership.
CREATE TABLE IF NOT EXISTS planner_interventions (
    intervention_id TEXT PRIMARY KEY,
    planner_job_id TEXT NOT NULL REFERENCES planner_jobs(planner_job_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    event_class TEXT NOT NULL CHECK (
        event_class IN ('developer_blocker', 'qa_failure', 'integration_conflict')
    ),
    severity TEXT NOT NULL CHECK (severity IN ('high', 'normal')),
    context_ref TEXT NOT NULL,
    artifact_commit TEXT,
    candidate_id TEXT,
    evidence_refs TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'DELIVERED', 'ACKNOWLEDGED')),
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    nonce TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT,
    acknowledged_at TEXT,
    acknowledged_by TEXT
);
-- One live request per planning problem: repeated evidence updates it instead of
-- waking a Planner again for the same unresolved decision.
CREATE UNIQUE INDEX IF NOT EXISTS planner_interventions_one_open_per_job
    ON planner_interventions(planner_job_id) WHERE status IN ('PENDING', 'DELIVERED');
CREATE INDEX IF NOT EXISTS planner_interventions_project_status
    ON planner_interventions(project_id, status, created_at);

CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL,
    project_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    payload TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_tool_calls (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL UNIQUE,
    project_id TEXT,
    actor_id TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    input_summary TEXT NOT NULL,
    outcome TEXT NOT NULL,
    error_code TEXT,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_tool_calls_project_sequence
    ON agent_tool_calls(project_id, sequence);
"""


class SQLiteStore:
    """Small transactional store for the local-pilot control plane.

    This is single-machine, local-pilot storage: one SQLite file on this disk,
    guarded by short, in-process write transactions with WAL-mode concurrent
    reads. It assumes every reader and writer runs on this machine against this
    file -- it is not a multi-machine-safe store, must never be placed on a
    network share, and is expected to be replaced by a PostgreSQL-backed store
    (see HOH-204) before agents span machines.
    """

    def __init__(
        self, path: str | Path, *, busy_timeout_seconds: float = _DEFAULT_BUSY_TIMEOUT_SECONDS
    ) -> None:
        self.path = Path(path)
        # Overridable so tests can exercise real lock-wait-then-fail behavior in
        # well under a second instead of waiting out the production default.
        self.busy_timeout_seconds = busy_timeout_seconds

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            # WAL allows many MCP reader processes and one short-lived writer to coexist.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(SCHEMA)
            self._begin_immediate(connection)
            self._migrate_qa_test_job_lease_foreign_key(connection)
            self._migrate_planner_job_types(connection)

    @staticmethod
    def _migrate_planner_job_types(connection: sqlite3.Connection) -> None:
        """Widen the planner job kinds without discarding existing planning work.

        A database created before Integrator outcomes became a planning source
        still carries the narrower CHECK, which SQLite cannot alter in place.
        """
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'planner_jobs'"
        ).fetchone()
        if definition is None or "integration_conflict" in definition["sql"]:
            return

        columns = (
            "planner_job_id, project_id, ticket_id, job_type, status, summary, candidate_id, "
            "evidence_refs, context_entry_id, created_by, created_at, updated_at, resolution, "
            "resolved_by, resolved_at"
        )
        connection.execute("DROP INDEX IF EXISTS planner_jobs_project_status")
        connection.execute("DROP INDEX IF EXISTS planner_jobs_one_open_per_ticket")
        connection.execute("ALTER TABLE planner_jobs RENAME TO planner_jobs_legacy")
        connection.execute(
            """
            CREATE TABLE planner_jobs (
                planner_job_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
                job_type TEXT NOT NULL CHECK (
                    job_type IN ('qa_failure', 'developer_blocker', 'integration_conflict')
                ),
                status TEXT NOT NULL CHECK (status IN ('READY', 'IN_PROGRESS', 'RESOLVED')),
                summary TEXT NOT NULL,
                candidate_id TEXT REFERENCES candidates(candidate_id),
                evidence_refs TEXT NOT NULL,
                context_entry_id TEXT REFERENCES context_entries(entry_id),
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolution TEXT,
                resolved_by TEXT,
                resolved_at TEXT
            )
            """
        )
        connection.execute(
            f"INSERT INTO planner_jobs ({columns}) SELECT {columns} FROM planner_jobs_legacy"
        )
        connection.execute("DROP TABLE planner_jobs_legacy")
        connection.execute(
            "CREATE INDEX planner_jobs_project_status ON planner_jobs(project_id, status, created_at)"
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX planner_jobs_one_open_per_ticket
                ON planner_jobs(ticket_id) WHERE status IN ('READY', 'IN_PROGRESS')
            """
        )

    @staticmethod
    def _migrate_qa_test_job_lease_foreign_key(connection: sqlite3.Connection) -> None:
        """Remove the legacy live-lease FK without discarding durable QA history.

        An asynchronous test run is authorized by a lease but can outlive it.  The
        old foreign key made lease recovery fail after a test job had been created,
        which blocked every later inbox and claim operation in that transaction.
        """
        foreign_keys = connection.execute("PRAGMA foreign_key_list(qa_test_jobs)").fetchall()
        if not any(
            foreign_key["table"] == "leases" and foreign_key["from"] == "lease_id"
            for foreign_key in foreign_keys
        ):
            return

        columns = (
            "test_run_id, project_id, candidate_id, ticket_id, lease_id, test_command, "
            "command_digest, timeout_seconds, worktree_path, candidate_commit, requested_by, "
            "status, error_message, created_at, started_at, completed_at"
        )
        connection.execute("DROP INDEX IF EXISTS qa_test_jobs_candidate_created")
        connection.execute("DROP INDEX IF EXISTS qa_test_jobs_active_candidate")
        connection.execute("ALTER TABLE qa_test_jobs RENAME TO qa_test_jobs_legacy")
        connection.execute(
            """
            CREATE TABLE qa_test_jobs (
                test_run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
                ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
                lease_id TEXT NOT NULL,
                test_command TEXT NOT NULL,
                command_digest TEXT NOT NULL,
                timeout_seconds INTEGER NOT NULL,
                worktree_path TEXT NOT NULL,
                candidate_commit TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'STALE')),
                error_message TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            )
            """
        )
        connection.execute(
            f"INSERT INTO qa_test_jobs ({columns}) SELECT {columns} FROM qa_test_jobs_legacy"
        )
        connection.execute("DROP TABLE qa_test_jobs_legacy")
        connection.execute(
            "CREATE INDEX qa_test_jobs_candidate_created ON qa_test_jobs(candidate_id, created_at)"
        )
        connection.execute(
            "CREATE INDEX qa_test_jobs_active_candidate ON qa_test_jobs(candidate_id, status)"
        )

    @contextmanager
    def connection(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=self.busy_timeout_seconds)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            f"PRAGMA busy_timeout = {int(self.busy_timeout_seconds * 1000)}"
        )
        try:
            if immediate:
                self._begin_immediate(connection)
            yield connection
            connection.commit()
        except sqlite3.OperationalError as error:
            connection.rollback()
            if _is_lock_contention(error):
                raise ContentionError(
                    "The database is busy with another operation; retry this request."
                ) from error
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _begin_immediate(connection: sqlite3.Connection) -> None:
        """Acquire the write lock before any statement in the transaction runs.

        SQLite's busy handler already waits up to busy_timeout_seconds inside this
        single call. A caller whose wait is exhausted gets ContentionError, a clean
        domain error that is always safe to retry from scratch -- since nothing has
        executed yet, a retry can never re-apply a partial mutation. The bounded
        retry/backoff policy across attempts lives in mcp_server.invoke().
        """
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            if not _is_lock_contention(error):
                raise
            raise ContentionError(
                "The database is busy with another operation; retry this request."
            ) from error
