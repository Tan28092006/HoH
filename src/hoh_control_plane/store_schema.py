"""The SQLite schema script and the value vocabulary stored in it."""
from __future__ import annotations


SCHEMA = """
PRAGMA foreign_keys = ON;

-- Which schema version wrote this database (store_migrations.SCHEMA_VERSION).
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    default_branch TEXT NOT NULL,
    verification_profile TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_repositories (
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
    repository_path TEXT NOT NULL,
    workspace_model TEXT NOT NULL DEFAULT 'colocated'
        CHECK (workspace_model IN ('colocated', 'agent_workspace')),
    observed_mainline_commit TEXT,
    observed_upstream_commit TEXT,
    observed_remote_refs TEXT NOT NULL DEFAULT '{}',
    observed_at TEXT,
    configured_by TEXT NOT NULL,
    configured_at TEXT NOT NULL
);

-- Every observed remote movement is a planning boundary.  The observation is
-- immutable and stays pending until Planner has reconciled the board against
-- the new artifact.  Execution admission consults this table, so a model
-- cannot acknowledge a Git event and continue from an old plan by accident.
-- scope 'project' closes the whole project; scope 'tickets' (a result that did
-- not move main) closes only frozen_ticket_ids. shadow_partition records the
-- scope rule's prediction for a merge Planner reviews, and shadow_agreed
-- whether Planner's non-keep decisions fell inside it (wave 6D, shadow only).
CREATE TABLE IF NOT EXISTS repository_reconciliations (
    reconciliation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    observation_ref TEXT NOT NULL UNIQUE,
    observed_commits TEXT NOT NULL,
    ticket_versions TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'RESOLVED')),
    created_at TEXT NOT NULL,
    resolved_by TEXT,
    resolved_at TEXT,
    decision_manifest TEXT,
    scope TEXT NOT NULL DEFAULT 'project',
    frozen_ticket_ids TEXT,
    shadow_partition TEXT,
    shadow_agreed INTEGER
);
CREATE INDEX IF NOT EXISTS repository_reconciliations_project_status
    ON repository_reconciliations(project_id, status, created_at);

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
    status TEXT NOT NULL CHECK (status IN ('BACKLOG', 'READY', 'DEV_IN_PROGRESS', 'QA_READY', 'QA_IN_PROGRESS', 'REWORK', 'VERIFIED', 'MERGE_READY', 'DONE', 'BLOCKED', 'CANCELLED', 'SUPERSEDED')),
    priority INTEGER NOT NULL DEFAULT 50,
    required_role TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL,
    validation_contract TEXT NOT NULL DEFAULT '[]',
    preservation_constraints TEXT NOT NULL,
    file_scope TEXT NOT NULL,
    blocked_reason TEXT,
    base_commit TEXT,
    candidate_commit TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Planner may discover that a ticket is obsolete while an execution worker is
-- still alive. The request is durable so authority can be revoked immediately,
-- while a possibly-started Git merge is reconciled before the terminal ticket
-- state is published.
CREATE TABLE IF NOT EXISTS ticket_retirement_requests (
    request_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    reconciliation_id TEXT NOT NULL REFERENCES repository_reconciliations(reconciliation_id),
    target_status TEXT NOT NULL CHECK (target_status IN ('CANCELLED', 'SUPERSEDED')),
    replacement_ticket_id TEXT REFERENCES tickets(ticket_id),
    reason TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'APPLIED', 'LANDED', 'FAILED')),
    requested_at TEXT NOT NULL,
    completed_at TEXT,
    outcome TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ticket_retirement_one_pending
    ON ticket_retirement_requests(ticket_id) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS ticket_retirement_project_status
    ON ticket_retirement_requests(project_id, status, requested_at);

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
    -- How far this section reaches once its nested sections are counted in. A
    -- section's own span ends where its first child begins, so a trimmed outline
    -- that reported that span would send a reader to the preamble and call it
    -- the section. Computed when the document is synced rather than on every
    -- read: syncing is rare and reading is not.
    subtree_end_line INTEGER,
    subtree_tokens INTEGER,
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
    last_seen_at TEXT NOT NULL,
    source_commit TEXT,
    retired_at TEXT,
    retired_by TEXT,
    retired_reason TEXT
);

CREATE INDEX IF NOT EXISTS experience_notes_project
    ON experience_notes(project_id, times_seen DESC, last_seen_at DESC);

-- What each note attaches to, one row per rooted scope, so the question "which
-- notes reach this ticket" is answered by an index instead of by loading every
-- note in the project and deciding in Python. Reading a ticket's context is the
-- hottest path there is; it must not get slower as the project accumulates.
CREATE TABLE IF NOT EXISTS experience_note_scopes (
    note_id TEXT NOT NULL REFERENCES experience_notes(note_id),
    scope_root TEXT NOT NULL,
    PRIMARY KEY (note_id, scope_root)
);

CREATE INDEX IF NOT EXISTS experience_note_scopes_root
    ON experience_note_scopes(scope_root);

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

CREATE TABLE IF NOT EXISTS runtime_state (
    state_key TEXT PRIMARY KEY,
    state_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS worker_executions (
    execution_id TEXT PRIMARY KEY,
    runner_id TEXT NOT NULL,
    job_kind TEXT NOT NULL,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'FINISHED', 'INTERRUPTED')),
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    completed_at TEXT,
    outcome TEXT
);
CREATE INDEX IF NOT EXISTS worker_executions_runner_status ON worker_executions(runner_id, status);
CREATE TABLE IF NOT EXISTS runtime_actions (
    action_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS worktree_ownership (
    ticket_id TEXT PRIMARY KEY REFERENCES tickets(ticket_id),
    lease_id TEXT NOT NULL,
    branch_ref TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merge_intents (
    merge_job_id TEXT PRIMARY KEY REFERENCES merge_jobs(merge_job_id),
    repository_path TEXT NOT NULL,
    default_branch TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    candidate_commit TEXT NOT NULL,
    created_at TEXT NOT NULL
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
    -- Whether that digest was read from bytes this machine holds, or is the
    -- agent's claim about a file kept elsewhere. Indistinguishable otherwise,
    -- and the difference is exactly what "verified" means here.
    checksum_source TEXT,
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
    execution_capabilities TEXT NOT NULL DEFAULT '[]',
    approved_execution_capabilities TEXT NOT NULL DEFAULT '[]',
    capability_approval_refs TEXT NOT NULL DEFAULT '[]',
    capability_approved_machine_id TEXT,
    capability_approved_by TEXT,
    capability_approved_at TEXT,
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
    endpoint TEXT,
    -- When this agent last registered, i.e. when its current chat began.
    session_started_at TEXT
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
        job_type IN ('qa_failure', 'developer_blocker', 'integration_conflict', 'delivery_completed')
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
        event_class IN ('developer_blocker', 'qa_failure', 'integration_conflict', 'delivery_completed')
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
    generated_at TEXT NOT NULL,
    content_digest TEXT
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

-- How an execution role says it worked a ticket (work.report_usage): a fresh
-- worker or the dispatcher chat itself, and the tokens it spent when known.
-- Self-reported and advisory; it measures dispatcher discipline, it grants
-- nothing.
CREATE TABLE IF NOT EXISTS ticket_usage_reports (
    report_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    agent_id TEXT NOT NULL,
    role TEXT NOT NULL,
    worker TEXT NOT NULL CHECK (worker IN ('fresh_subagent', 'dispatcher', 'new_session')),
    tokens_in INTEGER,
    tokens_out INTEGER,
    reported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ticket_usage_reports_ticket
    ON ticket_usage_reports(ticket_id, reported_at);

CREATE TABLE IF NOT EXISTS agent_event_waiters (
    agent_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    role TEXT NOT NULL,
    wait_id TEXT NOT NULL,
    notification_port INTEGER NOT NULL,
    state TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_event_deliveries (
    event_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    event_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    UNIQUE(agent_id, project_id, event_key)
);
CREATE INDEX IF NOT EXISTS agent_event_pending
    ON agent_event_deliveries(agent_id, project_id, state);
CREATE TABLE IF NOT EXISTS agent_event_reservations (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    event_key TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY(project_id, event_key)
);

-- One counter per identifier prefix. Incremented inside the writer's own
-- transaction, so an identifier is unique for the same reason the row is.
CREATE TABLE IF NOT EXISTS id_sequences (
    prefix TEXT PRIMARY KEY,
    next_value INTEGER NOT NULL
);

-- QA equipment as observed by the HoH host, never as reported by QA. A Planner
-- approves capabilities only against these rows, for the same agent and the
-- same machine. The machine is kept as a fingerprint, never as its name, and a
-- probe keeps an output digest, never the output.
CREATE TABLE IF NOT EXISTS capability_preflights (
    preflight_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    agent_id TEXT NOT NULL,
    machine_fingerprint TEXT NOT NULL,
    host_matches_machine INTEGER NOT NULL,
    requested_capabilities TEXT NOT NULL,
    outcomes TEXT NOT NULL,
    record_digest TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS capability_preflights_agent
    ON capability_preflights (project_id, agent_id, created_at);

-- Preflight commands a Planner configures for capabilities no built-in probe
-- can observe. A command is configuration, like the project test command, so it
-- lives here rather than in the portable verification profile.
CREATE TABLE IF NOT EXISTS project_capability_preflights (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    capability TEXT NOT NULL,
    command TEXT NOT NULL,
    configured_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, capability)
);
"""


# The ticket lifecycle the tickets.status CHECK admits; it must equal
# domain.TicketStatus (tests/test_store_migrations.py holds them together).
TICKET_STATUSES = (
    "BACKLOG", "READY", "DEV_IN_PROGRESS", "QA_READY", "QA_IN_PROGRESS", "REWORK",
    "VERIFIED", "MERGE_READY", "DONE", "BLOCKED", "CANCELLED", "SUPERSEDED",
)
TICKET_STATUS_CHECK = "CHECK (status IN ({}))".format(
    ", ".join(f"'{status}'" for status in TICKET_STATUSES)
)


# How an evidence row's checksum was obtained. Both are legitimate, but only one
# proves an artifact, and section 25 asks that no verdict hang off an artifact
# nobody identified -- so which it was has to survive in the row.
CHECKSUM_ARTIFACT = "artifact"
CHECKSUM_CLAIMED = "claimed"
