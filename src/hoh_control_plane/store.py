from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Iterator

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


class SchemaMigrationError(DomainError):
    """An upgrade could not leave the persisted schema in a usable state.

    This is raised instead of committing a half-migrated database, so the fault
    surfaces once at startup with the table that caused it rather than as a raw
    storage error on some unrelated write hours later.
    """

    code = "schema_migration_failed"
    status_code = 500


class StoreIntegrityError(DomainError):
    """A write violated a durable constraint the schema declares.

    Named here rather than caught as a driver exception so a service module can
    recognise a foreign key or uniqueness violation without knowing which driver
    raised it, and so no raw storage-engine text reaches a caller.
    """

    code = "state_conflict"
    status_code = 409


@dataclass(frozen=True)
class Dialect:
    """What a backend must supply that SQLite currently provides implicitly.

    Written down so a second backend is a list of decisions rather than a hunt
    through the schema for everything that silently depended on SQLite.
    """

    name: str
    # The parameter marker the driver expects. Statements are written in qmark
    # style everywhere and translated on the way out.
    placeholder: str
    # How a monotonic sequence column is declared.
    identity_column: str
    # Run once per connection to enforce foreign keys, if the backend does not
    # enforce them by default.
    foreign_keys_statement: str | None
    # Run once per connection to bound how long a statement waits for a lock.
    lock_timeout_statement: str | None
    # Whether the backend can alter a constraint in place. SQLite cannot, which
    # is the entire reason for the rename-create-copy-drop machinery below.
    alters_constraints_in_place: bool


SQLITE_DIALECT = Dialect(
    name="sqlite",
    placeholder="?",
    identity_column="INTEGER PRIMARY KEY AUTOINCREMENT",
    foreign_keys_statement="PRAGMA foreign_keys = ON",
    lock_timeout_statement=None,  # set through connect(timeout=...) instead
    alters_constraints_in_place=False,
)


def translate_placeholders(statement: str, placeholder: str) -> str:
    """Rewrite qmark parameter markers for a backend that expects another style.

    Only markers outside string literals are rewritten: a question mark inside
    quoted SQL text is data, not a parameter, and rewriting it would silently
    corrupt the statement.
    """
    if placeholder == "?":
        return statement
    out: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(statement):
        character = statement[index]
        if quote is not None:
            out.append(character)
            if character == quote:
                # A doubled quote is an escaped quote, not the end of the literal.
                if index + 1 < len(statement) and statement[index + 1] == quote:
                    out.append(statement[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if character in ("'", '"'):
            quote = character
            out.append(character)
        elif character == "?":
            out.append(placeholder)
        else:
            out.append(character)
        index += 1
    return "".join(out)


class StoreConnection:
    """A connection the service layer can use without naming a driver.

    Wraps the driver's own connection so statements stay written in one
    placeholder style and so every failure a caller can reasonably act on
    arrives as a control-plane error rather than as driver text.
    """

    def __init__(
        self,
        raw: Any,
        dialect: Dialect,
        integrity_errors: tuple[type[BaseException], ...] = (sqlite3.IntegrityError,),
    ) -> None:
        self._raw = raw
        self.dialect = dialect
        # Which driver exception means "a constraint refused this". Passed in
        # rather than hardcoded, so a second backend translates its own errors
        # without this class learning about every driver.
        self._integrity_errors = integrity_errors

    def execute(self, statement: str, parameters: Any = ()) -> Any:
        try:
            return self._raw.execute(
                translate_placeholders(statement, self.dialect.placeholder), parameters
            )
        except self._integrity_errors as error:
            raise StoreIntegrityError(
                "This change conflicts with a constraint the schema enforces."
            ) from error

    def executescript(self, script: str) -> Any:
        return self._raw.executescript(script)

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()

    @property
    def raw(self) -> Any:
        """The driver connection, for migration code that is backend-specific."""
        return self._raw

    def __getattr__(self, name: str) -> Any:
        # Anything the seam does not need to shape is the driver's own; falling
        # through keeps this a seam rather than a reimplementation of DB-API.
        return getattr(self._raw, name)


def _is_lock_contention(error: sqlite3.OperationalError) -> bool:
    message = str(error).lower()
    return "locked" in message or "busy" in message


# A table rebuilt in place is renamed to this suffix, copied from, and dropped.
# The suffix is also how a foreign key left pointing at one of those dropped
# temporaries is recognized and repaired below.
_MIGRATION_TABLE_SUFFIX = "_legacy"


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
    workspace_model TEXT NOT NULL DEFAULT 'colocated'
        CHECK (workspace_model IN ('colocated', 'agent_workspace')),
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

CREATE TABLE IF NOT EXISTS spec_sections (
    section_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    document TEXT NOT NULL,
    heading_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    -- A span rather than the prose: the document stays the source of truth in
    -- git, and this row only says which part of it to open.
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    token_estimate INTEGER NOT NULL,
    -- Heading level, so a cold reader can ask for the outline rather than all
    -- 106 sections: a table of contents that costs half the document it indexes
    -- has not saved anybody anything.
    depth INTEGER NOT NULL DEFAULT 1,
    -- The revision this section last moved at, which is what a reader compares
    -- its own cursor against to learn whether it has drifted.
    changed_at_revision INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'present',
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, document, heading_path)
);

CREATE INDEX IF NOT EXISTS spec_sections_cursor
    ON spec_sections(project_id, changed_at_revision);

CREATE TABLE IF NOT EXISTS spec_revisions (
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experience_notes (
    note_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    -- The code this was learned in, not the ticket it was learned on. A ticket
    -- closes; the trap in the file stays. Indexing by scope is what lets a note
    -- reach an agent who was not there when it was written.
    file_scope TEXT NOT NULL,
    lesson TEXT NOT NULL,
    -- Provenance so a reader can weigh it, held as a plain reference rather than
    -- a foreign key: removing the ticket must not remove what it taught.
    learned_on_ticket TEXT,
    actor_id TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    -- The same lesson learned again is not a second lesson. Counting instead of
    -- appending is what keeps this from becoming the prose nobody reads, and
    -- turns repetition into the signal that it is worth reading first.
    times_seen INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS experience_notes_project
    ON experience_notes(project_id, times_seen DESC, last_seen_at DESC);

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
    environment_digest TEXT,
    storage_checksum TEXT,
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

-- Provenance travels with the observation: which scenario or command produced
-- it, which environment ran it, and the checksum of the artifact it points at.
-- An observation whose artifact cannot be tied back to the frozen candidate is
-- not acceptance evidence.
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    criterion TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    observation TEXT NOT NULL,
    storage_ref TEXT NOT NULL,
    scenario_ref TEXT,
    environment_digest TEXT,
    storage_checksum TEXT,
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
    updated_at TEXT NOT NULL,
    skill_manifest TEXT NOT NULL DEFAULT '[]',
    protocol_version TEXT,
    skill_compatibility TEXT NOT NULL DEFAULT '{}',
    -- Where to knock. An agent that registers none is not broken: it polls,
    -- which stays a complete path on its own.
    endpoint TEXT
);
CREATE INDEX IF NOT EXISTS agent_presence_role_expiry
    ON agent_presence(role, expires_at);

-- A project's accepted skill/protocol versions. Absence of a row means no
-- enforcement is configured yet: every agent stays compatible by default, so
-- introducing this table never locks out an already-registered fleet.
CREATE TABLE IF NOT EXISTS project_skill_policies (
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
    accepted_protocol_versions TEXT NOT NULL,
    accepted_skill_versions TEXT NOT NULL,
    configured_by TEXT NOT NULL,
    configured_at TEXT NOT NULL
);

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
    closed_at TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (delivery_status IN ('pending', 'delivered', 'failed', 'skipped')),
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT
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

CREATE TABLE IF NOT EXISTS requirements (
    requirement_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    statement TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    registered_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- A requirement is satisfied when the work pointing at it is done, so there is
-- deliberately no status column here to disagree with the board.
CREATE TABLE IF NOT EXISTS requirement_links (
    requirement_id TEXT NOT NULL REFERENCES requirements(requirement_id),
    subject_type TEXT NOT NULL CHECK (subject_type IN ('ticket', 'feature')),
    subject_id TEXT NOT NULL,
    linked_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (requirement_id, subject_type, subject_id)
);

CREATE INDEX IF NOT EXISTS requirement_links_subject
    ON requirement_links (subject_type, subject_id);

CREATE TABLE IF NOT EXISTS context_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('ticket', 'feature')),
    subject_id TEXT NOT NULL,
    role TEXT NOT NULL,
    source_versions TEXT NOT NULL,
    generated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS context_snapshots_subject
    ON context_snapshots (subject_type, subject_id);

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
    correlation_id TEXT,
    causation_id TEXT,
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
        self.dialect = SQLITE_DIALECT
        # Where this store's companion artifacts live. For a file-backed store
        # that is the directory holding the file, which is where they have always
        # been; a store that is not a file has to be told.
        self.artifact_root = self.path.parent

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            # WAL allows many MCP reader processes and one short-lived writer to coexist.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(SCHEMA)
            # The migrations below rebuild a table by rename-create-copy-drop.
            # Two pragmas make that safe, and neither can be changed once a
            # transaction is open, so both are set here rather than inside a
            # migration:
            #   legacy_alter_table keeps the RENAME from rewriting *other*
            #   tables' foreign keys to aim at the temporary table this
            #   migration is about to drop -- that rewrite is exactly how
            #   planner_interventions and planner_job_leases were left
            #   referencing a planner_jobs_legacy table that no longer exists;
            #   foreign_keys=OFF lets a rebuild copy rows across while a parent
            #   table is momentarily renamed away, as SQLite's own table-rebuild
            #   procedure requires.
            # Enforcement is not weakened for anyone else: this connection is
            # used only for the upgrade and closed immediately after, every
            # serving connection still opens with foreign_keys=ON, and
            # _require_clean_foreign_keys re-proves each rebuilt table before
            # this transaction commits.
            connection.execute("PRAGMA legacy_alter_table = ON")
            connection.execute("PRAGMA foreign_keys = OFF")
            self._begin_immediate(connection)
            self._migrate_qa_test_job_lease_foreign_key(connection)
            self._migrate_planner_job_types(connection)
            self._migrate_agent_presence_skill_columns(connection)
            self._migrate_evidence_provenance(connection)
            self._migrate_workspace_model(connection)
            self._migrate_event_correlation(connection)
            self._migrate_agent_wakeup(connection)
            self._migrate_spec_section_depth(connection)
            repaired = self._migrate_stale_migration_table_references(connection)
            self._require_clean_foreign_keys(connection, repaired)

    @staticmethod
    def _migrate_agent_presence_skill_columns(connection: sqlite3.Connection) -> None:
        """Add skill/protocol compatibility columns to a database created before them.

        Plain additive columns with defaults: no CHECK or foreign key changes, so
        this needs none of the rename-create-copy-drop machinery the other
        migrations require, just ALTER TABLE ADD COLUMN guarded by presence.
        """
        existing_columns = {
            row["name"]
            for row in connection.execute('PRAGMA table_info("agent_presence")').fetchall()
        }
        if "skill_manifest" not in existing_columns:
            connection.execute(
                "ALTER TABLE agent_presence ADD COLUMN skill_manifest TEXT NOT NULL DEFAULT '[]'"
            )
        if "protocol_version" not in existing_columns:
            connection.execute("ALTER TABLE agent_presence ADD COLUMN protocol_version TEXT")
        if "skill_compatibility" not in existing_columns:
            connection.execute(
                "ALTER TABLE agent_presence ADD COLUMN skill_compatibility TEXT NOT NULL DEFAULT '{}'"
            )

    @classmethod
    def _migrate_stale_migration_table_references(
        cls, connection: sqlite3.Connection
    ) -> list[str]:
        """Repoint foreign keys left aiming at a dropped rebuild temporary.

        A database upgraded by the earlier rename-create-copy-drop code carries
        real damage: renaming planner_jobs out of the way silently rewrote every
        other table's foreign key to the temporary planner_jobs_legacy, and the
        same migration then dropped it. The child tables -- planner_interventions
        and planner_job_leases -- were left pointing at nothing, so the first
        insert into either failed with a raw storage error that surfaced as a
        generic state conflict, and an evidence-bearing QA FAIL verdict could not
        commit. Setting legacy_alter_table stops new damage; this heals databases
        that already took it, in the same transaction, without hand repair.

        Returns the tables it rebuilt. Idempotent: a database whose references
        are already live is left untouched.
        """
        existing = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        repaired: list[str] = []
        for table in sorted(existing):
            if table.startswith("sqlite_"):
                continue
            stale_targets = {
                target
                for target in (
                    foreign_key["table"]
                    for foreign_key in connection.execute(
                        f'PRAGMA foreign_key_list("{table}")'
                    ).fetchall()
                )
                # Only a name this project's own rebuild would have produced, and
                # only when the table it stood in for is present: a foreign key to
                # some other missing table is a different fault and is left alone
                # for PRAGMA foreign_key_check to report rather than guessed at.
                if target not in existing
                and target.endswith(_MIGRATION_TABLE_SUFFIX)
                and target[: -len(_MIGRATION_TABLE_SUFFIX)] in existing
            }
            if stale_targets:
                cls._repoint_foreign_keys(connection, table, stale_targets)
                repaired.append(table)
        return repaired

    @staticmethod
    def _repoint_foreign_keys(
        connection: sqlite3.Connection, table: str, stale_targets: set[str]
    ) -> None:
        """Rebuild one table with its foreign keys aimed back at the live parents.

        The stored definition is reused verbatim apart from the reference target,
        so any column, check or default this table has acquired since it was first
        created survives the repair untouched.
        """
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        repaired_sql = definition["sql"]
        for stale in sorted(stale_targets):
            live = stale[: -len(_MIGRATION_TABLE_SUFFIX)]
            # SQLite quotes the target when it rewrites a reference during a
            # rename, but a hand-written definition may not be quoted at all.
            repaired_sql = repaired_sql.replace(f'REFERENCES "{stale}"', f'REFERENCES "{live}"')
            repaired_sql = repaired_sql.replace(f"REFERENCES {stale}", f"REFERENCES {live}")
        if any(stale in repaired_sql for stale in stale_targets):
            raise SchemaMigrationError(
                f"Foreign key references to a dropped migration table remain in {table}"
            )

        columns = ", ".join(
            f'"{row["name"]}"'
            for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        )
        indexes = [
            row["sql"]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? "
                "AND sql IS NOT NULL",
                (table,),
            ).fetchall()
        ]
        index_names = [
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ? "
                "AND sql IS NOT NULL",
                (table,),
            ).fetchall()
        ]
        # The rebuilt table reclaims these names, so release them before the
        # rename rather than colliding with the copies riding on the old table.
        for index_name in index_names:
            connection.execute(f'DROP INDEX IF EXISTS "{index_name}"')
        rebuild_name = f"{table}__rebuild"
        connection.execute(f'ALTER TABLE "{table}" RENAME TO "{rebuild_name}"')
        connection.execute(repaired_sql)
        connection.execute(
            f'INSERT INTO "{table}" ({columns}) SELECT {columns} FROM "{rebuild_name}"'
        )
        connection.execute(f'DROP TABLE "{rebuild_name}"')
        for index_sql in indexes:
            connection.execute(index_sql)

    @staticmethod
    def _require_clean_foreign_keys(connection: sqlite3.Connection, tables: list[str]) -> None:
        """Prove each rebuilt table's rows still resolve, before committing.

        Scoped to the tables this upgrade actually touched: a violation that
        predates the upgrade elsewhere in the database is not this migration's to
        adjudicate, and refusing to start over one would take the control plane
        down for a fault the upgrade neither caused nor can fix.
        """
        for table in tables:
            violations = connection.execute(
                f'PRAGMA foreign_key_check("{table}")'
            ).fetchall()
            if violations:
                raise SchemaMigrationError(
                    f"Upgraded table {table} has {len(violations)} unresolved "
                    "foreign key references"
                )

    @staticmethod
    def _migrate_spec_section_depth(connection: sqlite3.Connection) -> None:
        """Add the heading level to an index written before the outline needed it.

        CREATE TABLE IF NOT EXISTS does nothing to a table that already exists,
        so the first database to carry this index -- written minutes before the
        column was added -- needs the column put there explicitly. Existing rows
        default to depth 1, which shows them in every outline until the next sync
        records their real level: too much rather than too little.
        """
        existing = {
            row["name"] for row in connection.execute('PRAGMA table_info("spec_sections")')
        }
        if existing and "depth" not in existing:
            connection.execute(
                "ALTER TABLE spec_sections ADD COLUMN depth INTEGER NOT NULL DEFAULT 1"
            )

    @staticmethod
    def _migrate_agent_wakeup(connection: sqlite3.Connection) -> None:
        """Add delivery columns to a database written before wakeups existed.

        Additive, and every default is the behaviour that database already had:
        no endpoint means polling, and a pending delivery on an existing offer is
        simply one that was never attempted.
        """
        for table, columns in (
            ("agent_presence", (("endpoint", "TEXT"),)),
            (
                "work_offers",
                (
                    ("delivery_status", "TEXT NOT NULL DEFAULT 'pending'"),
                    ("delivery_attempts", "INTEGER NOT NULL DEFAULT 0"),
                    ("delivered_at", "TEXT"),
                ),
            ),
        ):
            existing = {
                row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')
            }
            if not existing:
                continue
            for column, declaration in columns:
                if column not in existing:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                    )

    @staticmethod
    def _migrate_event_correlation(connection: sqlite3.Connection) -> None:
        """Add trace columns to an audit trail written before they existed.

        Added rather than rebuilt so no historical event is rewritten. Older
        events keep NULL, which reads as "not traced" rather than as an event
        that belonged to no request.
        """
        existing = {
            row["name"] for row in connection.execute('PRAGMA table_info("audit_events")')
        }
        if not existing:
            return
        for column in ("correlation_id", "causation_id"):
            if column not in existing:
                connection.execute(f"ALTER TABLE audit_events ADD COLUMN {column} TEXT")
        # Created here rather than in the schema script: on a database that
        # predates these columns the script runs before this migration, so an
        # index declared there would reference a column that does not exist yet.
        connection.execute(
            "CREATE INDEX IF NOT EXISTS audit_events_correlation "
            "ON audit_events (correlation_id)"
        )

    @staticmethod
    def _migrate_workspace_model(connection: sqlite3.Connection) -> None:
        """Record where a project's working trees live, defaulting to co-located.

        Added rather than rebuilt so every configured repository survives. An
        existing project was necessarily co-located, because that was the only
        model the service could serve, so the default states the truth about it
        rather than leaving it unknown.
        """
        existing = {
            row["name"]
            for row in connection.execute('PRAGMA table_info("project_repositories")')
        }
        if not existing or "workspace_model" in existing:
            return
        connection.execute(
            "ALTER TABLE project_repositories ADD COLUMN workspace_model TEXT "
            "NOT NULL DEFAULT 'colocated'"
        )

    @staticmethod
    def _migrate_evidence_provenance(connection: sqlite3.Connection) -> None:
        """Add provenance columns to evidence written before they existed.

        These are added rather than rebuilt so every historical observation, and
        the verdicts and audit events referencing it, survive untouched. Older
        rows keep NULL provenance, which reads as "not recorded" rather than as a
        verified environment.
        """
        for table, columns in (
            ("evidence", ("scenario_ref", "environment_digest", "storage_checksum")),
            ("qa_test_runs", ("environment_digest", "storage_checksum")),
        ):
            existing = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if not existing:
                continue
            for column in columns:
                if column not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")

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
    def connection(self, *, immediate: bool = False) -> Iterator[StoreConnection]:
        raw = sqlite3.connect(self.path, timeout=self.busy_timeout_seconds)
        raw.row_factory = sqlite3.Row
        if self.dialect.foreign_keys_statement:
            raw.execute(self.dialect.foreign_keys_statement)
        raw.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_seconds * 1000)}")
        connection = StoreConnection(raw, self.dialect)
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
