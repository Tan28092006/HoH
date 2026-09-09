from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


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
    """Small transactional store for the MVP control plane."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            # WAL allows many MCP reader processes and one short-lived writer to coexist.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            self._migrate_qa_test_job_lease_foreign_key(connection)

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
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
