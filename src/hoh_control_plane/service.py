from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from hoh_control_plane.domain import (
    CLOSED_TICKET_STATUSES,
    RETIRABLE_TICKET_STATUSES,
    Actor,
    AuthorizationError,
    ConflictError,
    DomainError,
    IdempotencyConflictError,
    InterventionSeverity,
    InterventionStatus,
    NotFoundError,
    PlannerJobStatus,
    PlannerJobType,
    ReleasePolicyError,
    Role,
    TicketStatus,
    ValidationError,
    require_project,
    require_role,
)
from hoh_control_plane.git_worktrees import CLEAN_CHECKPOINT, DIRTY, GitWorktreeManager
from hoh_control_plane.qa_runner import ProjectTestRunner, TestExecution
from hoh_control_plane.store import SQLiteStore


_SAFE_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

JSON_FIELDS = {
    "acceptance_criteria",
    "preservation_constraints",
    "file_scope",
    "developer_claims",
    "evidence_refs",
    "test_command",
    "payload",
    "input_summary",
    "project_ids",
    "skills",
}
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")

MIN_TOKEN_BUDGET = 200
MAX_TOKEN_BUDGET = 200_000
DEFAULT_TOKEN_BUDGET = 8_000
_CHECKPOINT_CHARS = 200

# Retention policy for idempotency records: a replay is honored for this long,
# after which purge_expired_idempotency_records may delete the row and the same
# key behaves like a fresh request. Long enough to outlive any worker restart
# or operator retry, short enough that keys are not kept indefinitely.
IDEMPOTENCY_RETENTION_SECONDS = 86_400
_MAX_IDEMPOTENCY_KEY_CHARS = 200

# Narration accumulates without bound; every other entry type is a durable
# decision that compaction may shorten but must never drop.
_VERBOSE_ENTRY_TYPES = frozenset({"progress_update", "qa_test_run"})

# Artifact identity and open findings stay in every packet, so these sections
# are compacted to essentials rather than omitted.
_PROTECTED_SECTIONS = frozenset({"ticket", "candidate", "known_failures"})

# What each role cannot act safely without: a Developer that lost its branch and
# base commit, or QA that lost the worktree it must execute in, would do damage
# a smaller packet never justifies.
_ROLE_REQUIRED_SECTIONS: dict[Role, frozenset[str]] = {
    Role.DEVELOPER: frozenset({"worktree"}),
    Role.QA: frozenset({"worktree", "test_runs"}),
    Role.PLANNER: frozenset({"feature", "dependencies"}),
    Role.INTEGRATOR: frozenset({"test_runs"}),
}

# Least important last: compaction walks each list in reverse.
_TICKET_SECTION_PRIORITY: dict[Role, tuple[str, ...]] = {
    Role.DEVELOPER: (
        "worktree",
        "known_failures",
        "context_entries",
        "candidate",
        "dependencies",
        "feature",
        "test_runs",
    ),
    Role.QA: (
        "candidate",
        "test_runs",
        "known_failures",
        "worktree",
        "context_entries",
        "dependencies",
        "feature",
    ),
    Role.PLANNER: (
        "feature",
        "dependencies",
        "known_failures",
        "context_entries",
        "candidate",
        "test_runs",
        "worktree",
    ),
    Role.INTEGRATOR: (
        "candidate",
        "known_failures",
        "test_runs",
        "feature",
        "dependencies",
        "worktree",
        "context_entries",
    ),
}
_FALLBACK_SECTION_PRIORITY = (
    "candidate",
    "known_failures",
    "context_entries",
    "worktree",
    "dependencies",
    "feature",
    "test_runs",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat().replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _serialize(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


def _estimate_tokens(payload: Any) -> int:
    """Deterministic size proxy; exact tokenizer parity is not what the budget guards."""
    return len(_serialize(payload)) // 4


def _snapshot_id(payload: dict[str, Any]) -> str:
    """Identify a compiled packet by its content so equal state compiles equal."""
    material = {
        key: value
        for key, value in payload.items()
        if key not in {"context_snapshot_id", "generated_at"}
    }
    digest = hashlib.sha256(_serialize(material).encode("utf-8")).hexdigest()
    return f"ctxsnap_{digest[:32]}"


def _checkpoint(text: str) -> str:
    if len(text) <= _CHECKPOINT_CHARS:
        return text
    return text[:_CHECKPOINT_CHARS] + "..."


def _validated_budget(token_budget: int | None) -> int:
    if token_budget is None:
        return DEFAULT_TOKEN_BUDGET
    if not MIN_TOKEN_BUDGET <= token_budget <= MAX_TOKEN_BUDGET:
        raise ValidationError(
            f"Context token budget must be between {MIN_TOKEN_BUDGET} and {MAX_TOKEN_BUDGET}"
        )
    return token_budget


def _safe_path_segment(value: str, field: str) -> str:
    if not _SAFE_PATH_SEGMENT_RE.fullmatch(value):
        raise ValidationError(f"{field} cannot be used in a recovery artifact path")
    return value


def _bounded_join(values: tuple[str, ...] | list[str], limit: int = 20) -> str:
    joined = ", ".join(values[:limit])
    if len(values) > limit:
        joined += f", and {len(values) - limit} more"
    return joined


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for field in JSON_FIELDS & result.keys():
        result[field] = json.loads(result[field])
    return result


class ControlPlaneService:
    """Application service enforcing workflow, context and role invariants."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self.git_worktrees = GitWorktreeManager(store.path.parent / "worktrees")
        self.project_tests = ProjectTestRunner(store.path.parent / "evidence")
        # Recovery patches stay outside the worktree root so no agent can reach
        # another agent's abandoned work through its own assigned worktree.
        self.recovery_root = store.path.parent / "recovery"

    def create_project(
        self,
        actor: Actor,
        project_id: str,
        name: str,
        description: str,
        default_branch: str,
    ) -> dict[str, Any]:
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._bounded_text(project_id, "project_id", 80)
        self._bounded_text(name, "name", 160)
        self._bounded_text(description, "description", 4000, allow_blank=True)
        self._bounded_text(default_branch, "default_branch", 200)
        timestamp = _iso()
        try:
            with self.store.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO projects
                        (project_id, name, description, default_branch, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (project_id, name, description, default_branch, timestamp, timestamp),
                )
                self._event(
                    connection,
                    actor,
                    project_id,
                    "project.created",
                    "project",
                    project_id,
                    1,
                    {"name": name},
                )
                project = self._one(
                    connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("Project already exists") from exc
        return project

    def configure_repository(
        self, actor: Actor, project_id: str, repository_path: str
    ) -> dict[str, Any]:
        """Attach a checked local Git repository to a project for strict candidate gating."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._bounded_text(repository_path, "repository_path", 1000)
        normalized_path = self.git_worktrees.validate_repository(repository_path)
        timestamp = _iso()
        with self.store.connection(immediate=True) as connection:
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            existing_repository = _row(
                connection.execute(
                    "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            if existing_repository and existing_repository["repository_path"] != normalized_path:
                worktree_count = connection.execute(
                    "SELECT COUNT(*) FROM worktrees WHERE project_id = ?", (project_id,)
                ).fetchone()[0]
                if worktree_count:
                    raise ConflictError(
                        "Project repository cannot change after ticket worktrees are prepared"
                    )
            next_version = project["version"] + 1
            connection.execute(
                """
                INSERT INTO project_repositories
                    (project_id, repository_path, configured_by, configured_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    repository_path = excluded.repository_path,
                    configured_by = excluded.configured_by,
                    configured_at = excluded.configured_at
                """,
                (project_id, normalized_path, actor.agent_id, timestamp),
            )
            connection.execute(
                "UPDATE projects SET version = ?, updated_at = ? WHERE project_id = ?",
                (next_version, timestamp, project_id),
            )
            self._event(
                connection,
                actor,
                project_id,
                "project.repository_configured",
                "project",
                project_id,
                next_version,
                {"configured": True},
            )
            configured_project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
        return {"project": configured_project, "repository_path": normalized_path}

    def configure_test_command(
        self,
        actor: Actor,
        project_id: str,
        test_command: list[str],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """Register the single shell-free test command frozen into future candidates."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._test_command(test_command)
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
            raise ValidationError("timeout_seconds must be an integer")
        if not 5 <= timeout_seconds <= 1800:
            raise ValidationError("timeout_seconds must be between 5 and 1800")
        timestamp = _iso()
        with self.store.connection(immediate=True) as connection:
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            repository = connection.execute(
                "SELECT project_id FROM project_repositories WHERE project_id = ?", (project_id,)
            ).fetchone()
            if repository is None:
                raise ValidationError(
                    "Planner must configure a project Git repository before its test command"
                )
            next_version = project["version"] + 1
            connection.execute(
                """
                INSERT INTO project_test_profiles
                    (project_id, test_command, timeout_seconds, configured_by, configured_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    test_command = excluded.test_command,
                    timeout_seconds = excluded.timeout_seconds,
                    configured_by = excluded.configured_by,
                    configured_at = excluded.configured_at
                """,
                (project_id, json.dumps(test_command), timeout_seconds, actor.agent_id, timestamp),
            )
            connection.execute(
                "UPDATE projects SET version = ?, updated_at = ? WHERE project_id = ?",
                (next_version, timestamp, project_id),
            )
            self._event(
                connection,
                actor,
                project_id,
                "project.test_command_configured",
                "project",
                project_id,
                next_version,
                {"configured": True, "timeout_seconds": timeout_seconds},
            )
            profile = self._one(
                connection,
                "SELECT * FROM project_test_profiles WHERE project_id = ?",
                (project_id,),
            )
        return profile

    def create_feature(
        self,
        actor: Actor,
        project_id: str,
        feature_id: str,
        title: str,
        objective: str,
        parent_feature_id: str | None = None,
        priority: int = 50,
    ) -> dict[str, Any]:
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._bounded_text(feature_id, "feature_id", 100)
        self._bounded_text(title, "title", 240)
        self._bounded_text(objective, "objective", 8000)
        if parent_feature_id is not None:
            self._bounded_text(parent_feature_id, "parent_feature_id", 100)
        self._priority(priority)
        timestamp = _iso()
        try:
            with self.store.connection() as connection:
                if parent_feature_id:
                    parent = self._one(
                        connection,
                        "SELECT project_id FROM features WHERE feature_id = ?",
                        (parent_feature_id,),
                    )
                    if parent["project_id"] != project_id:
                        raise ValidationError("Parent feature belongs to another project")
                connection.execute(
                    """
                    INSERT INTO features
                        (feature_id, project_id, parent_feature_id, title, objective,
                         priority, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        feature_id,
                        project_id,
                        parent_feature_id,
                        title,
                        objective,
                        priority,
                        timestamp,
                        timestamp,
                    ),
                )
                self._event(
                    connection,
                    actor,
                    project_id,
                    "feature.created",
                    "feature",
                    feature_id,
                    1,
                    {"title": title},
                )
                feature = self._one(
                    connection, "SELECT * FROM features WHERE feature_id = ?", (feature_id,)
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("Feature already exists or project is missing") from exc
        return feature

    def create_ticket(
        self,
        actor: Actor,
        feature_id: str,
        ticket_id: str,
        title: str,
        objective: str,
        required_role: Role,
        acceptance_criteria: list[str],
        preservation_constraints: list[str],
        file_scope: list[str],
        ready: bool = False,
        ticket_type: str = "feature_work",
        priority: int = 50,
        parent_ticket_id: str | None = None,
        dependency_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(ticket_id, "ticket_id", 100)
        self._bounded_text(title, "title", 240)
        self._bounded_text(objective, "objective", 8000)
        self._bounded_text(ticket_type, "ticket_type", 50)
        self._priority(priority)
        self._bounded_list(acceptance_criteria, "acceptance_criteria", 30, 2000, required=True)
        self._bounded_list(preservation_constraints, "preservation_constraints", 30, 2000)
        self._bounded_list(file_scope, "file_scope", 100, 1000)
        if required_role not in {Role.DEVELOPER, Role.QA, Role.INTEGRATOR}:
            raise ValidationError("Ticket must target an executable role")
        dependency_ids = dependency_ids or []
        self._bounded_list(dependency_ids, "dependency_ids", 100, 100)
        timestamp = _iso()
        try:
            with self.store.connection() as connection:
                project_id = self._project_for_feature(connection, feature_id)
                require_project(actor, project_id)
                status = TicketStatus.READY if ready else TicketStatus.BACKLOG
                connection.execute(
                    """
                    INSERT INTO tickets
                        (ticket_id, feature_id, parent_ticket_id, ticket_type, title,
                         objective, status, priority, required_role, acceptance_criteria,
                         preservation_constraints, file_scope, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ticket_id,
                        feature_id,
                        parent_ticket_id,
                        ticket_type,
                        title,
                        objective,
                        status.value,
                        priority,
                        required_role.value,
                        json.dumps(acceptance_criteria),
                        json.dumps(preservation_constraints),
                        json.dumps(file_scope),
                        timestamp,
                        timestamp,
                    ),
                )
                for dependency_id in dependency_ids:
                    if dependency_id == ticket_id:
                        raise ValidationError("Ticket cannot depend on itself")
                    connection.execute(
                        """
                        INSERT INTO ticket_dependencies (ticket_id, depends_on_ticket_id)
                        VALUES (?, ?)
                        """,
                        (ticket_id, dependency_id),
                    )
                self._event(
                    connection,
                    actor,
                    project_id,
                    "ticket.ready" if ready else "ticket.created",
                    "ticket",
                    ticket_id,
                    1,
                    {"title": title, "required_role": required_role.value},
                )
                ticket = self._ticket(connection, ticket_id)
                if ready:
                    self._queue_ticket(connection, actor, project_id, ticket)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("Ticket already exists or dependency is invalid") from exc
        return ticket

    def set_ticket_ready(
        self, actor: Actor, ticket_id: str, expected_version: int
    ) -> dict[str, Any]:
        """Move a planned ticket into the executable queue."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] not in {TicketStatus.BACKLOG.value, TicketStatus.BLOCKED.value}:
                raise ConflictError("Only backlog or blocked tickets can be made ready")
            next_version = ticket["version"] + 1
            timestamp = _iso()
            connection.execute(
                """
                UPDATE tickets SET status = ?, blocked_reason = NULL, version = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (TicketStatus.READY.value, next_version, timestamp, ticket_id),
            )
            self._event(
                connection, actor, project_id, "ticket.ready", "ticket", ticket_id,
                next_version, {"previous_status": ticket["status"]},
            )
            ready_ticket = self._ticket(connection, ticket_id)
            self._queue_ticket(connection, actor, project_id, ready_ticket)
            return {"ticket": ready_ticket}

    def update_ticket_write_scope(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        file_scope: list[str],
        reason: str,
    ) -> dict[str, Any]:
        """Update planned or blocked ticket scope with optimistic concurrency and audit evidence."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_list(file_scope, "file_scope", 100, 1000, required=True)
        self._bounded_text(reason, "reason", 2000)
        normalized_scope = [self._repository_relative_scope(scope) for scope in file_scope]
        if len(set(normalized_scope)) != len(normalized_scope):
            raise ValidationError("file_scope cannot contain duplicate paths")

        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] not in {TicketStatus.BACKLOG.value, TicketStatus.BLOCKED.value}:
                raise ConflictError("Write scope cannot change during active development or QA")
            active_lease = connection.execute(
                "SELECT 1 FROM leases WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if active_lease is not None:
                raise ConflictError("Write scope cannot change while a ticket has an active lease")

            previous_scope = ticket["file_scope"]
            if normalized_scope == previous_scope:
                raise ValidationError("file_scope is unchanged")
            timestamp = _iso()
            next_version = ticket["version"] + 1
            connection.execute(
                """
                UPDATE tickets SET file_scope = ?, version = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (json.dumps(normalized_scope), next_version, timestamp, ticket_id),
            )
            entry_id = _id("ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'write_scope_updated', ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    project_id,
                    ticket["feature_id"],
                    ticket_id,
                    f"Planner updated write scope: {reason}",
                    actor.agent_id,
                    actor.role.value,
                    timestamp,
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                "ticket.write_scope_updated",
                "ticket",
                ticket_id,
                next_version,
                {
                    "previous_file_scope": previous_scope,
                    "file_scope": normalized_scope,
                    "reason": reason,
                },
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "previous_file_scope": previous_scope,
                "context_entry": self._one(
                    connection, "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
                ),
            }

    def cancel_ticket(
        self, actor: Actor, ticket_id: str, expected_version: int, reason: str
    ) -> dict[str, Any]:
        """Retire obsolete unclaimed work so dispatch stops offering it."""
        return self._retire_ticket(
            actor,
            ticket_id,
            expected_version,
            reason,
            next_status=TicketStatus.CANCELLED,
            entry_type="ticket_cancelled",
            event_type="ticket.cancelled",
        )

    def supersede_ticket(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        replacement_ticket_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Retire work that a named same-project replacement ticket now carries."""
        self._bounded_text(replacement_ticket_id, "replacement_ticket_id", 100)
        return self._retire_ticket(
            actor,
            ticket_id,
            expected_version,
            reason,
            next_status=TicketStatus.SUPERSEDED,
            entry_type="ticket_superseded",
            event_type="ticket.superseded",
            replacement_ticket_id=replacement_ticket_id,
        )

    def _retire_ticket(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        reason: str,
        *,
        next_status: TicketStatus,
        entry_type: str,
        event_type: str,
        replacement_ticket_id: str | None = None,
    ) -> dict[str, Any]:
        """Close one ticket's dispatch without rewriting active or evidenced work.

        Nothing is deleted: the ticket, its context, candidates and audit history stay
        readable. Only the queue rows that would keep offering it are closed, in the
        same transaction as the status change, so no agent can be handed retired work.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(reason, "reason", 2000)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if TicketStatus(ticket["status"]) not in RETIRABLE_TICKET_STATUSES:
                raise ConflictError(
                    "Only backlog, ready or blocked tickets can be cancelled or superseded"
                )
            self._require_no_active_work(connection, ticket_id)
            replacement = self._retirement_replacement(
                connection, ticket_id, project_id, replacement_ticket_id
            )
            timestamp = _iso()
            next_version = ticket["version"] + 1
            closed_offers = self._close_ticket_dispatch(connection, ticket_id, timestamp)
            connection.execute(
                "UPDATE tickets SET status = ?, version = ?, updated_at = ? WHERE ticket_id = ?",
                (next_status.value, next_version, timestamp, ticket_id),
            )
            entry_id = _id("ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     source_ref, actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    project_id,
                    ticket["feature_id"],
                    ticket_id,
                    entry_type,
                    reason,
                    f"ticket:{replacement_ticket_id}" if replacement_ticket_id else None,
                    actor.agent_id,
                    actor.role.value,
                    timestamp,
                ),
            )
            # Dependents of retired work stay blocked until a Planner rewires them;
            # naming them here keeps that consequence visible instead of silent.
            dependents = [
                str(row["ticket_id"])
                for row in connection.execute(
                    """
                    SELECT ticket_id FROM ticket_dependencies
                    WHERE depends_on_ticket_id = ? ORDER BY ticket_id
                    """,
                    (ticket_id,),
                ).fetchall()
            ]
            self._event(
                connection,
                actor,
                project_id,
                event_type,
                "ticket",
                ticket_id,
                next_version,
                {
                    "previous_status": ticket["status"],
                    "reason": reason,
                    "replacement_ticket_id": replacement_ticket_id,
                    "closed_offers": closed_offers,
                    "dependent_ticket_ids": dependents,
                },
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "replacement": replacement,
                "closed_offers": closed_offers,
                "dependent_ticket_ids": dependents,
                "context_entry": self._one(
                    connection, "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
                ),
            }

    def requeue_candidate(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        reason: str = "base_drift",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Requeue a stale QA-ready candidate whose base commit has drifted from main."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(ticket_id, "ticket_id", 100)
        self._bounded_text(reason, "reason", 2000)

        if idempotency_key:
            payload = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "reason": reason,
            }
            return self.run_idempotent(
                actor,
                operation="candidate:requeue",
                idempotency_key=idempotency_key,
                payload=payload,
                run=lambda: self._requeue_candidate(actor, ticket_id, expected_version, reason),
            )
        return self._requeue_candidate(actor, ticket_id, expected_version, reason)

    def _requeue_candidate(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        reason: str,
    ) -> dict[str, Any]:
        with self.store.connection() as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] != TicketStatus.QA_READY.value:
                raise ConflictError(
                    f"Only QA_READY tickets can have their candidate requeued (ticket is {ticket['status']})"
                )
            repository = _row(
                connection.execute(
                    "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            if repository is None:
                raise ValidationError("Project repository is required before requeuing a candidate")
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            candidate = _row(
                connection.execute(
                    "SELECT * FROM candidates WHERE ticket_id = ? ORDER BY submitted_at DESC LIMIT 1",
                    (ticket_id,),
                ).fetchone()
            )
            if candidate is None:
                raise ConflictError("Ticket has no frozen candidate to requeue")

        repository_path = repository["repository_path"]
        default_branch = project["default_branch"]
        mainline = f"refs/heads/{default_branch}"
        base = candidate["base_commit"]
        base_is_current_main = self.git_worktrees.commit_descends_from(
            repository_path=repository_path,
            ancestor_commit=mainline,
            descendant_commit=base,
        ) and self.git_worktrees.commit_descends_from(
            repository_path=repository_path,
            ancestor_commit=base,
            descendant_commit=mainline,
        )
        if base_is_current_main:
            raise ConflictError(
                "Candidate base commit has not drifted from current main; candidate is not stale"
            )

        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] != TicketStatus.QA_READY.value:
                raise ConflictError(
                    f"Only QA_READY tickets can have their candidate requeued (ticket is {ticket['status']})"
                )

            active_lease = connection.execute(
                "SELECT 1 FROM leases WHERE ticket_id = ? AND expires_at > ?",
                (ticket_id, _iso()),
            ).fetchone()
            if active_lease is not None:
                raise ConflictError(
                    "A ticket with an active lease cannot have its candidate requeued"
                )
            connection.execute("DELETE FROM leases WHERE ticket_id = ?", (ticket_id,))

            for table in (
                "worktree_provision_jobs",
                "candidate_submission_jobs",
                "qa_test_jobs",
                "merge_jobs",
            ):
                running = connection.execute(
                    f"SELECT 1 FROM {table} WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING')",
                    (ticket_id,),
                ).fetchone()
                if running is not None:
                    raise ConflictError(
                        f"A ticket with a running job ({table}) cannot have its candidate requeued"
                    )

            latest_candidate = _row(
                connection.execute(
                    "SELECT * FROM candidates WHERE ticket_id = ? ORDER BY submitted_at DESC LIMIT 1",
                    (ticket_id,),
                ).fetchone()
            )
            if latest_candidate is None or latest_candidate["candidate_id"] != candidate["candidate_id"]:
                raise ConflictError("Candidate changed during requeue")

            timestamp = _iso()
            next_version = ticket["version"] + 1

            closed_offers = self._close_ticket_dispatch(connection, ticket_id, timestamp)
            connection.execute(
                """
                UPDATE tickets
                SET status = ?, blocked_reason = ?, version = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (TicketStatus.REWORK.value, reason, next_version, timestamp, ticket_id),
            )

            entry_id = _id("ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     source_ref, actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'candidate_requeued', ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    project_id,
                    ticket["feature_id"],
                    ticket_id,
                    reason,
                    f"candidate:{candidate['candidate_id']}",
                    actor.agent_id,
                    actor.role.value,
                    timestamp,
                ),
            )

            self._event(
                connection,
                actor,
                project_id,
                "candidate.requeued",
                "ticket",
                ticket_id,
                next_version,
                {
                    "previous_status": ticket["status"],
                    "reason": reason,
                    "candidate_id": candidate["candidate_id"],
                    "candidate_commit": candidate["commit_sha"],
                    "base_commit": candidate["base_commit"],
                    "closed_offers": closed_offers,
                },
            )

            rework_ticket = self._ticket(connection, ticket_id)
            self._queue_ticket(connection, actor, project_id, rework_ticket)

            return {
                "ticket": rework_ticket,
                "candidate": candidate,
                "closed_offers": closed_offers,
                "context_entry": self._one(
                    connection, "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
                ),
            }

    def _require_no_active_work(self, connection: sqlite3.Connection, ticket_id: str) -> None:
        """Refuse retirement while any agent, worker or frozen artifact still owns this."""
        active_lease = connection.execute(
            "SELECT 1 FROM leases WHERE ticket_id = ? AND expires_at > ?",
            (ticket_id, _iso()),
        ).fetchone()
        if active_lease is not None:
            raise ConflictError("A ticket with an active lease cannot be cancelled or superseded")
        for table in (
            "worktree_provision_jobs",
            "candidate_submission_jobs",
            "qa_test_jobs",
            "merge_jobs",
        ):
            running = connection.execute(
                f"SELECT 1 FROM {table} WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING')",
                (ticket_id,),
            ).fetchone()
            if running is not None:
                raise ConflictError("A ticket with a running job cannot be cancelled or superseded")
        candidate = connection.execute(
            "SELECT 1 FROM candidates WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        if candidate is not None:
            raise ConflictError(
                "A ticket with a frozen candidate cannot be cancelled or superseded"
            )

    def _retirement_replacement(
        self,
        connection: sqlite3.Connection,
        ticket_id: str,
        project_id: str,
        replacement_ticket_id: str | None,
    ) -> dict[str, Any] | None:
        """Resolve the replacement reference that makes a supersede auditable."""
        if replacement_ticket_id is None:
            return None
        if replacement_ticket_id == ticket_id:
            raise ValidationError("A ticket cannot supersede itself")
        replacement = _row(
            connection.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (replacement_ticket_id,)
            ).fetchone()
        )
        if replacement is None:
            raise NotFoundError("Replacement ticket not found")
        if self._project_for_ticket(connection, replacement_ticket_id) != project_id:
            raise ValidationError("Replacement ticket must belong to the same project")
        return replacement

    def _close_ticket_dispatch(
        self, connection: sqlite3.Connection, ticket_id: str, timestamp: str
    ) -> int:
        """Close queue, offer and outbox rows together so no live hand-off survives."""
        queue_ids = [
            str(row["queue_id"])
            for row in connection.execute(
                "SELECT queue_id FROM work_queue WHERE ticket_id = ? AND status IN ('pending', 'offered')",
                (ticket_id,),
            ).fetchall()
        ]
        if not queue_ids:
            return 0
        placeholders = ", ".join("?" for _ in queue_ids)
        closed_offers = connection.execute(
            f"""
            UPDATE work_offers SET status = 'cancelled', closed_at = ?
            WHERE queue_id IN ({placeholders}) AND status = 'offered'
            """,
            (timestamp, *queue_ids),
        ).rowcount
        connection.execute(
            f"""
            UPDATE dispatch_outbox SET status = 'cancelled'
            WHERE queue_id IN ({placeholders}) AND status = 'pending'
            """,
            queue_ids,
        )
        connection.execute(
            f"""
            UPDATE work_queue SET status = 'cancelled', updated_at = ?
            WHERE queue_id IN ({placeholders})
            """,
            (timestamp, *queue_ids),
        )
        return max(closed_offers, 0)

    def append_planner_context(
        self,
        actor: Actor,
        *,
        entry_type: str,
        content: str,
        ticket_id: str | None = None,
        feature_id: str | None = None,
        source_ref: str | None = None,
    ) -> dict[str, Any]:
        """Record an auditable planning decision against exactly one context scope."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        if bool(ticket_id) == bool(feature_id):
            raise ValidationError("Provide exactly one of ticket_id or feature_id")
        self._bounded_text(entry_type, "entry_type", 64)
        self._bounded_text(content, "content", 8000)
        if source_ref is not None:
            self._bounded_text(source_ref, "source_ref", 1000)
        with self.store.connection(immediate=True) as connection:
            if ticket_id:
                ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
                resolved_feature_id = ticket["feature_id"]
                aggregate_type, aggregate_id, version = "ticket", ticket_id, ticket["version"]
            else:
                assert feature_id is not None
                project_id = self._project_for_feature(connection, feature_id)
                require_project(actor, project_id)
                feature = self._one(
                    connection, "SELECT * FROM features WHERE feature_id = ?", (feature_id,)
                )
                resolved_feature_id = feature_id
                aggregate_type, aggregate_id, version = "feature", feature_id, feature["version"]
            entry_id = _id("ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     source_ref, actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id, project_id, resolved_feature_id, ticket_id, entry_type, content,
                    source_ref, actor.agent_id, actor.role.value, _iso(),
                ),
            )
            self._event(
                connection, actor, project_id, "context.planning_appended", aggregate_type,
                aggregate_id, version, {"entry_id": entry_id, "entry_type": entry_type},
            )
            return self._one(
                connection, "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
            )

    def discover_work(
        self,
        actor: Actor,
        project_id: str,
        dependency_ready: bool = True,
        blocked_only: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        require_project(actor, project_id)
        limit = min(max(limit, 1), 100)
        with self.store.connection() as connection:
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            parameters: list[Any] = [project_id]
            where = ["f.project_id = ?"]
            # Retired work stays readable through ticket context, never through
            # a discovery surface that exists to hand out actionable work.
            closed = sorted(status.value for status in CLOSED_TICKET_STATUSES)
            where.append(f"t.status NOT IN ({', '.join('?' for _ in closed)})")
            parameters.extend(closed)
            if actor.role not in {Role.PLANNER, Role.ADMIN}:
                where.append("t.required_role = ?")
                parameters.append(actor.role.value)
            if blocked_only:
                where.append("t.status = ?")
                parameters.append(TicketStatus.BLOCKED.value)
            elif actor.role is Role.DEVELOPER:
                where.append("t.status IN (?, ?)")
                parameters.extend([TicketStatus.READY.value, TicketStatus.REWORK.value])
            elif actor.role is Role.QA:
                where.append("t.status = ?")
                parameters.append(TicketStatus.QA_READY.value)
            elif actor.role is Role.INTEGRATOR:
                where.append("t.status IN (?, ?)")
                parameters.extend(
                    [TicketStatus.VERIFIED.value, TicketStatus.MERGE_READY.value]
                )
            if dependency_ready:
                where.append(
                    """NOT EXISTS (
                        SELECT 1 FROM ticket_dependencies d
                        JOIN tickets dependency ON dependency.ticket_id = d.depends_on_ticket_id
                        WHERE d.ticket_id = t.ticket_id AND dependency.status != 'DONE'
                    )"""
                )
            parameters.append(limit)
            rows = connection.execute(
                f"""
                SELECT t.*, f.project_id, f.title AS feature_title,
                       f.version AS feature_version, f.parent_feature_id
                FROM tickets t
                JOIN features f ON f.feature_id = t.feature_id
                WHERE {' AND '.join(where)}
                ORDER BY t.priority DESC, t.created_at ASC
                LIMIT ?
                """,
                parameters,
            ).fetchall()

        grouped: dict[str, dict[str, Any]] = {}
        for raw in rows:
            ticket = _row(raw)
            assert ticket is not None
            feature_id = ticket["feature_id"]
            feature = grouped.setdefault(
                feature_id,
                {
                    "feature_id": feature_id,
                    "title": ticket.pop("feature_title"),
                    "parent_feature_id": ticket.pop("parent_feature_id"),
                    "context_ref": f"ctx:feature:{feature_id}:v{ticket.pop('feature_version')}",
                    "tickets": [],
                },
            )
            ticket.pop("project_id", None)
            ticket["context_ref"] = (
                f"ctx:ticket:{ticket['ticket_id']}:v{ticket['version']}"
            )
            calls = [
                {
                    "tool": "ticket.get_context",
                    "arguments": {"ticket_ref": f"ticket:{ticket['ticket_id']}"},
                }
            ]
            if actor.role is Role.DEVELOPER:
                calls.append(
                    {
                        "tool": "work.claim",
                        "arguments": {
                            "ticket_id": ticket["ticket_id"],
                            "expected_version": ticket["version"],
                        },
                    }
                )
            elif actor.role is Role.QA:
                calls.append(
                    {
                        "tool": "qa.claim_candidate",
                        "arguments": {
                            "ticket_id": ticket["ticket_id"],
                            "expected_version": ticket["version"],
                        },
                    }
                )
            ticket["recommended_calls"] = calls
            feature["tickets"].append(ticket)

        discovered = {
            "agent": {
                "agent_id": actor.agent_id,
                "role": actor.role.value,
                "capabilities": self.capabilities(actor),
            },
            "project": project,
            "features": list(grouped.values()),
            "next_cursor": None,
        }
        if actor.role in {Role.PLANNER, Role.ADMIN}:
            # Planning decisions are their own queue: a Planner sees them beside,
            # never mixed into, the tickets an execution role can claim.
            discovered["planning_jobs"] = self.discover_planner_jobs(actor, project_id)
        return discovered

    def capabilities(self, actor: Actor) -> list[str]:
        common = [
            "project:read",
            "feature:read",
            "ticket:read",
            "context:resolve",
            "agent:presence",
            "work:inbox",
        ]
        role_capabilities = {
            Role.PLANNER: [
                "feature:write",
                "ticket:plan",
                "ticket:retire",
                "candidate:requeue",
                "repository:configure",
                "test_command:configure",
            ],
            Role.DEVELOPER: [
                "ticket:claim:developer",
                "worktree:prepare",
                "progress:append",
                "candidate:submit",
            ],
            Role.QA: ["ticket:claim:qa", "tests:execute", "evidence:write", "verdict:submit"],
            Role.INTEGRATOR: ["candidate:integrate", "merge:record"],
            Role.ADMIN: ["*"],
        }
        return common + role_capabilities[actor.role]

    def register_agent(
        self,
        actor: Actor,
        *,
        skills: list[str],
        capacity: int = 1,
        machine_id: str | None = None,
        presence_seconds: int = 120,
    ) -> dict[str, Any]:
        """Publish a short-lived, authenticated availability record for dispatch."""
        self._bounded_list(skills, "skills", 30, 100)
        self._capacity(capacity)
        self._presence_seconds(presence_seconds)
        if machine_id is not None:
            self._bounded_text(machine_id, "machine_id", 120)
        timestamp = _now()
        with self.store.connection(immediate=True) as connection:
            existing = _row(
                connection.execute(
                    "SELECT * FROM agent_presence WHERE agent_id = ?", (actor.agent_id,)
                ).fetchone()
            )
            if existing is not None and existing["role"] != actor.role.value:
                raise AuthorizationError("Agent identity cannot change roles")
            connection.execute(
                """
                INSERT INTO agent_presence
                    (agent_id, role, project_ids, skills, capacity, machine_id,
                     last_seen_at, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    project_ids = excluded.project_ids,
                    skills = excluded.skills,
                    capacity = excluded.capacity,
                    machine_id = excluded.machine_id,
                    last_seen_at = excluded.last_seen_at,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    actor.agent_id,
                    actor.role.value,
                    json.dumps(sorted(actor.project_ids)),
                    json.dumps(skills),
                    capacity,
                    machine_id,
                    _iso(timestamp),
                    _iso(timestamp + timedelta(seconds=presence_seconds)),
                    _iso(timestamp),
                    _iso(timestamp),
                ),
            )
            return self._presence(connection, actor.agent_id)

    def heartbeat_agent(
        self, actor: Actor, *, presence_seconds: int = 120
    ) -> dict[str, Any]:
        """Renew an agent's dispatch presence; separate from a ticket lease heartbeat."""
        self._presence_seconds(presence_seconds)
        with self.store.connection(immediate=True) as connection:
            return self._refresh_presence(connection, actor, presence_seconds)

    def get_agent_inbox(
        self,
        actor: Actor,
        project_id: str,
        *,
        limit: int = 20,
        offer_seconds: int = 90,
    ) -> dict[str, Any]:
        """Return durable offers for one role and make a newly-live agent dispatchable."""
        require_project(actor, project_id)
        if actor.role not in {Role.DEVELOPER, Role.QA, Role.INTEGRATOR}:
            raise AuthorizationError("Only execution roles have a work inbox")
        limit = min(max(limit, 1), 50)
        self._offer_seconds(offer_seconds)
        # The dedicated scheduler proactively reclaims expired leases.  An inbox
        # poll is also a safe recovery point: without this fallback, a stopped
        # scheduler leaves an otherwise-live worker waiting forever behind a
        # stale claim.  Recovery uses the internal admin identity rather than
        # granting execution workers permission to sweep leases themselves.
        self.sweep_expired_leases(
            Actor("lease-recovery", Role.ADMIN, frozenset({"*"}))
        )
        now = _now()
        with self.store.connection(immediate=True) as connection:
            presence = self._refresh_presence(connection, actor, 120, now=now)
            self._expire_offers(connection, now)
            pending = connection.execute(
                """
                SELECT * FROM work_queue
                WHERE project_id = ? AND target_role = ? AND status IN ('pending', 'offered')
                ORDER BY priority DESC, created_at ASC
                """,
                (project_id, actor.role.value),
            ).fetchall()
            created_offers: list[dict[str, Any]] = []
            for pending_row in pending:
                created_offers.extend(
                    self._offer_queue(
                        connection,
                        _row(pending_row),
                        now,
                        offer_seconds,
                        fanout=1,
                        only_agent_id=actor.agent_id,
                    )
                )
            offer_rows = connection.execute(
                """
                SELECT o.*, q.project_id, q.ticket_id, q.target_role, q.context_version,
                       q.priority, q.status AS queue_status
                FROM work_offers o
                JOIN work_queue q ON q.queue_id = o.queue_id
                WHERE o.agent_id = ? AND q.project_id = ? AND o.status = 'offered'
                ORDER BY q.priority DESC, o.offered_at ASC
                LIMIT ?
                """,
                (actor.agent_id, project_id, limit),
            ).fetchall()
            offers = [self._inbox_offer(connection, _row(row)) for row in offer_rows]
            lease_rows = connection.execute(
                """
                SELECT l.*
                FROM leases l
                JOIN tickets t ON t.ticket_id = l.ticket_id
                JOIN features f ON f.feature_id = t.feature_id
                WHERE l.agent_id = ? AND l.role = ? AND f.project_id = ?
                  AND l.expires_at > ?
                ORDER BY l.acquired_at ASC
                """,
                (actor.agent_id, actor.role.value, project_id, _iso(now)),
            ).fetchall()
            active_leases = []
            for lease_row in lease_rows:
                lease = _row(lease_row)
                assert lease is not None
                lease["ticket"] = self._ticket(connection, lease["ticket_id"])
                active_leases.append(lease)
        return {
            "agent": presence,
            "active_leases": active_leases,
            "offers": offers,
            "offered_now": len(created_offers),
            "next_poll_after_seconds": 20,
        }

    def dispatch_pending_work(
        self, actor: Actor, *, offer_seconds: int = 90, fanout: int = 3
    ) -> dict[str, Any]:
        """Fan pending hand-offs to live matching agents; safe to call repeatedly."""
        require_role(actor, Role.ADMIN)
        self._offer_seconds(offer_seconds)
        if not isinstance(fanout, int) or isinstance(fanout, bool) or not 1 <= fanout <= 20:
            raise ValidationError("fanout must be an integer from 1 to 20")
        now = _now()
        with self.store.connection(immediate=True) as connection:
            self._reconcile_work_queue(connection, actor)
            self._expire_offers(connection, now)
            queues = connection.execute(
                """
                SELECT * FROM work_queue WHERE status IN ('pending', 'offered')
                ORDER BY priority DESC, created_at ASC
                """
            ).fetchall()
            offers: list[dict[str, Any]] = []
            for queue_row in queues:
                offers.extend(
                    self._offer_queue(
                        connection, _row(queue_row), now, offer_seconds, fanout=fanout
                    )
                )
            pending_count = connection.execute(
                "SELECT COUNT(*) FROM work_queue WHERE status = 'pending'"
            ).fetchone()[0]
        return {
            "offered_count": len(offers),
            "pending_count": pending_count,
            "offers": offers,
        }

    def dispatch_overview(self, actor: Actor, project_id: str) -> dict[str, Any]:
        """Read-only project queue, offer and online-agent state for the dashboard."""
        require_project(actor, project_id)
        now = _iso()
        with self.store.connection() as connection:
            presence_rows = connection.execute(
                "SELECT * FROM agent_presence ORDER BY role, agent_id"
            ).fetchall()
            agents = []
            for row in presence_rows:
                presence = _row(row)
                assert presence is not None
                if "*" in presence["project_ids"] or project_id in presence["project_ids"]:
                    presence["online"] = presence["expires_at"] > now
                    agents.append(presence)
            queue_rows = connection.execute(
                """
                SELECT q.*, COUNT(o.offer_id) AS open_offer_count
                FROM work_queue q
                LEFT JOIN work_offers o ON o.queue_id = q.queue_id AND o.status = 'offered'
                WHERE q.project_id = ? AND q.status IN ('pending', 'offered', 'claimed')
                GROUP BY q.queue_id
                ORDER BY CASE q.status WHEN 'pending' THEN 0 WHEN 'offered' THEN 1 ELSE 2 END,
                         q.priority DESC, q.created_at ASC
                """,
                (project_id,),
            ).fetchall()
            queues = []
            for row in queue_rows:
                queue = _row(row)
                assert queue is not None
                queue["ticket"] = self._ticket(connection, queue["ticket_id"])
                queues.append(queue)
        # Bounded pointers only, so the existing read-only dashboard can show that
        # a Planner is being waited on without exposing the underlying evidence.
        interventions = [
            self.intervention_payload(intervention) | {"status": intervention["status"]}
            for intervention in self.list_planner_interventions(actor, project_id)
        ]
        return {"agents": agents, "queues": queues, "planner_interventions": interventions}

    def get_feature_context(
        self, actor: Actor, feature_ref: str, token_budget: int | None = None
    ) -> dict[str, Any]:
        budget = _validated_budget(token_budget)
        feature_id = self._strip_ref(feature_ref, "feature")
        with self.store.connection() as connection:
            feature = self._one(
                connection, "SELECT * FROM features WHERE feature_id = ?", (feature_id,)
            )
            require_project(actor, feature["project_id"])
            tickets = [
                self._ticket(connection, row["ticket_id"])
                for row in connection.execute(
                    "SELECT ticket_id FROM tickets WHERE feature_id = ? ORDER BY priority DESC",
                    (feature_id,),
                ).fetchall()
            ]
            verified = [
                constraint
                for ticket in tickets
                if ticket["status"] in {TicketStatus.VERIFIED.value, TicketStatus.DONE.value}
                for constraint in ticket["acceptance_criteria"]
            ]
        payload = {
            "context_ref": f"ctx:feature:{feature_id}:v{feature['version']}",
            "context_version": feature["version"],
            "role": actor.role.value,
            "feature": feature,
            "tickets": tickets,
            "verified_behaviors": verified,
            "open_ticket_count": sum(
                ticket["status"] != TicketStatus.DONE.value
                and TicketStatus(ticket["status"]) not in CLOSED_TICKET_STATUSES
                for ticket in tickets
            ),
        }
        return self._compile_context(
            payload,
            token_budget=budget,
            priority=("tickets", "verified_behaviors"),
            reference_prefix=f"ctx:feature:{feature_id}",
            compactors=(self._compact_feature_rollup,),
            protected=frozenset({"feature"}),
        )

    def get_ticket_context(
        self, actor: Actor, ticket_ref: str, token_budget: int | None = None
    ) -> dict[str, Any]:
        budget = _validated_budget(token_budget)
        ticket_id = self._strip_ref(ticket_ref, "ticket")
        with self.store.connection() as connection:
            project_id = self._project_for_ticket(connection, ticket_id)
            require_project(actor, project_id)
            ticket = self._ticket(connection, ticket_id)
            feature = self._one(
                connection,
                "SELECT * FROM features WHERE feature_id = ?",
                (ticket["feature_id"],),
            )
            contexts = [
                _row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM context_entries
                    WHERE ticket_id = ? AND superseded_by IS NULL
                    ORDER BY created_at ASC
                    """,
                    (ticket_id,),
                ).fetchall()
            ]
            candidate = _row(
                connection.execute(
                    """
                    SELECT * FROM candidates WHERE ticket_id = ?
                    ORDER BY submitted_at DESC LIMIT 1
                    """,
                    (ticket_id,),
                ).fetchone()
            )
            if candidate is not None:
                candidate = self._candidate_with_changed_paths(connection, candidate)
            worktree = _row(
                connection.execute(
                    "SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)
                ).fetchone()
            )
            failures: list[dict[str, Any]] = []
            test_runs: list[dict[str, Any]] = []
            if candidate:
                test_runs = [
                    _row(row)
                    for row in connection.execute(
                        """
                        SELECT * FROM qa_test_runs
                        WHERE candidate_id = ?
                        ORDER BY created_at ASC
                        """,
                        (candidate["candidate_id"],),
                    ).fetchall()
                ]
                finding_rows = connection.execute(
                    """
                    SELECT * FROM qa_findings
                    WHERE candidate_id = ? AND status != 'verified'
                    ORDER BY created_at ASC
                    """,
                    (candidate["candidate_id"],),
                ).fetchall()
                for finding_row in finding_rows:
                    finding = _row(finding_row)
                    assert finding is not None
                    evidence_refs = finding["evidence_refs"]
                    placeholders = ",".join("?" for _ in evidence_refs)
                    evidence = []
                    if evidence_refs:
                        evidence = [
                            _row(row)
                            for row in connection.execute(
                                f"SELECT * FROM evidence WHERE evidence_id IN ({placeholders})",
                                evidence_refs,
                            ).fetchall()
                        ]
                    finding["evidence"] = evidence
                    failures.append(finding)
            dependencies = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT dependency.ticket_id, dependency.title, dependency.status
                    FROM ticket_dependencies d
                    JOIN tickets dependency ON dependency.ticket_id = d.depends_on_ticket_id
                    WHERE d.ticket_id = ?
                    """,
                    (ticket_id,),
                ).fetchall()
            ]
        payload = {
            "context_ref": f"ctx:ticket:{ticket_id}:v{ticket['version']}",
            "context_version": ticket["version"],
            "role": actor.role.value,
            "project_id": project_id,
            "feature": feature,
            "ticket": ticket,
            "dependencies": dependencies,
            "context_entries": contexts,
            "candidate": candidate,
            "worktree": worktree,
            "test_runs": test_runs,
            "known_failures": failures,
        }
        return self._compile_context(
            payload,
            token_budget=budget,
            priority=_TICKET_SECTION_PRIORITY.get(actor.role, _FALLBACK_SECTION_PRIORITY),
            reference_prefix=f"ctx:ticket:{ticket_id}",
            compactors=(self._compact_history, self._compact_artifacts),
            protected=_PROTECTED_SECTIONS
            | _ROLE_REQUIRED_SECTIONS.get(actor.role, frozenset()),
        )

    def _compile_context(
        self,
        payload: dict[str, Any],
        *,
        token_budget: int,
        priority: tuple[str, ...],
        reference_prefix: str,
        compactors: tuple[Any, ...],
        protected: frozenset[str],
    ) -> dict[str, Any]:
        """Fit a packet to its budget, shortening narration before dropping sections.

        Protected sections are never dropped, so a packet can still exceed a budget
        smaller than that floor: a Developer without acceptance criteria, or QA
        without the frozen candidate SHA, is more dangerous than an oversized packet.
        """
        omitted: list[dict[str, Any]] = []
        payload["token_budget"] = token_budget
        payload["omitted_sections"] = omitted
        payload["generated_at"] = _iso()
        # Same-width placeholders so the envelope is measured, not added after the
        # packet has already been fitted to its budget.
        payload["estimated_tokens"] = token_budget
        payload["context_snapshot_id"] = f"ctxsnap_{'0' * 32}"

        for compact in compactors:
            if _estimate_tokens(payload) <= token_budget:
                break
            compact(payload, omitted, token_budget)

        for section in reversed(priority):
            if _estimate_tokens(payload) <= token_budget:
                break
            if section == "context_entries":
                self._drop_verbose_entries(payload, omitted, token_budget)
                continue
            if section in protected:
                continue
            value = payload.get(section)
            if not value:
                continue
            omitted.append(
                {
                    "section": section,
                    "reason": "token_budget",
                    "omitted_count": len(value) if isinstance(value, list) else 1,
                    "references": [f"{reference_prefix}:{section}"],
                }
            )
            payload[section] = [] if isinstance(value, list) else None

        omitted.sort(key=lambda entry: (entry["section"], entry["reason"]))
        payload["estimated_tokens"] = _estimate_tokens(payload)
        payload["context_snapshot_id"] = _snapshot_id(payload)
        return payload

    def _compact_history(
        self, payload: dict[str, Any], omitted: list[dict[str, Any]], token_budget: int
    ) -> None:
        """Shorten narration to a checkpoint, keeping every entry's provenance."""
        references = [
            f"ctx:entry:{entry['entry_id']}"
            for entry in payload.get("context_entries") or []
            if entry["entry_type"] in _VERBOSE_ENTRY_TYPES
            and len(entry["content"]) > _CHECKPOINT_CHARS
        ]
        if not references:
            return
        for entry in payload["context_entries"]:
            if entry["entry_type"] in _VERBOSE_ENTRY_TYPES:
                entry["content"] = _checkpoint(entry["content"])
        omitted.append(
            {
                "section": "context_entries.content",
                "reason": "compacted_to_checkpoint",
                "omitted_count": len(references),
                "references": references,
            }
        )

    def _compact_artifacts(
        self, payload: dict[str, Any], omitted: list[dict[str, Any]], token_budget: int
    ) -> None:
        """Keep artifact identity and finding state; shorten only their narration."""
        candidate = payload.get("candidate")
        if candidate and candidate.get("developer_claims"):
            omitted.append(
                {
                    "section": "candidate.developer_claims",
                    "reason": "compacted_to_reference",
                    "omitted_count": len(candidate["developer_claims"]),
                    "references": [
                        f"ctx:candidate:{candidate['candidate_id']}:developer_claims"
                    ],
                }
            )
            candidate["developer_claims"] = []

        references = [
            f"ctx:finding:{finding['finding_id']}"
            for finding in payload.get("known_failures") or []
            if len(finding["observation"]) > _CHECKPOINT_CHARS
        ]
        if not references:
            return
        for finding in payload["known_failures"]:
            finding["observation"] = _checkpoint(finding["observation"])
        omitted.append(
            {
                "section": "known_failures.observation",
                "reason": "compacted_to_checkpoint",
                "omitted_count": len(references),
                "references": references,
            }
        )

    def _compact_feature_rollup(
        self, payload: dict[str, Any], omitted: list[dict[str, Any]], token_budget: int
    ) -> None:
        """Reduce feature tickets to a rollup row plus a typed reference each."""
        tickets = payload.get("tickets") or []
        if not tickets:
            return
        payload["tickets"] = [
            {
                "ticket_id": ticket["ticket_id"],
                "title": ticket["title"],
                "status": ticket["status"],
                "priority": ticket["priority"],
                "required_role": ticket["required_role"],
                "dependency_ids": ticket["dependency_ids"],
            }
            for ticket in tickets
        ]
        omitted.append(
            {
                "section": "tickets.detail",
                "reason": "compacted_to_rollup",
                "omitted_count": len(tickets),
                "references": [f"ctx:ticket:{ticket['ticket_id']}" for ticket in tickets],
            }
        )

    def _drop_verbose_entries(
        self, payload: dict[str, Any], omitted: list[dict[str, Any]], token_budget: int
    ) -> None:
        """Drop the oldest narration first; durable decisions always stay."""
        entries = payload.get("context_entries")
        if not entries:
            return
        # Attached before dropping so each reference it keeps is itself measured;
        # otherwise the packet is refitted and then grown past its budget again.
        record: dict[str, Any] = {
            "section": "context_entries",
            "reason": "token_budget",
            "omitted_count": 0,
            "references": [],
        }
        omitted.append(record)
        while _estimate_tokens(payload) > token_budget:
            index = next(
                (
                    position
                    for position, entry in enumerate(entries)
                    if entry["entry_type"] in _VERBOSE_ENTRY_TYPES
                ),
                None,
            )
            if index is None:
                break
            record["references"].append(f"ctx:entry:{entries.pop(index)['entry_id']}")
            record["omitted_count"] += 1
        if not record["references"]:
            omitted.remove(record)

    def claim_ticket(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        lease_seconds: int,
    ) -> dict[str, Any]:
        require_role(actor, Role.DEVELOPER)
        return self._claim(
            actor,
            ticket_id,
            expected_version,
            lease_seconds,
            allowed_statuses={TicketStatus.READY, TicketStatus.REWORK},
            next_status=TicketStatus.DEV_IN_PROGRESS,
            event_type="ticket.claimed",
        )

    def prepare_worktree(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        expected_version: int,
    ) -> dict[str, Any]:
        """Create one ticket-and-agent-bound Git worktree for active Developer work.

        Inspecting a replaced Developer's worktree shells out to Git, so it runs
        between two short transactions rather than inside the write lock.
        """
        require_role(actor, Role.DEVELOPER)
        snapshot = self._worktree_preparation_snapshot(
            actor, ticket_id, lease_id, expected_version
        )
        if snapshot["reused"] is not None:
            return snapshot["reused"]
        takeover = self._plan_worktree_takeover(snapshot)
        return self._assign_worktree(
            actor, ticket_id, lease_id, expected_version, snapshot, takeover
        )

    def _worktree_preparation_snapshot(
        self, actor: Actor, ticket_id: str, lease_id: str, expected_version: int
    ) -> dict[str, Any]:
        """Authorize the request and read what the Git inspection needs."""
        with self.store.connection() as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
                raise ConflictError("Worktrees can only be prepared for active development work")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            repository = _row(
                connection.execute(
                    "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            if repository is None:
                raise ValidationError(
                    "Planner must configure a project Git repository before preparing a worktree"
                )
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            existing = _row(
                connection.execute(
                    "SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)
                ).fetchone()
            )
            reused = None
            if existing is not None and existing["agent_id"] == actor.agent_id:
                reused = {"ticket": ticket, "worktree": existing, "reused": True}
            return {
                "ticket": ticket,
                "project_id": project_id,
                "project": project,
                "repository": repository,
                "existing": existing,
                "reused": reused,
            }

    def _plan_worktree_takeover(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        """Decide what a replacement Developer may safely inherit from a predecessor."""
        existing = snapshot["existing"]
        if existing is None:
            return None
        ticket = snapshot["ticket"]
        repository_path = snapshot["repository"]["repository_path"]
        default_branch = snapshot["project"]["default_branch"]
        if ticket["candidate_commit"] is not None:
            candidate_is_current = self.git_worktrees.commit_descends_from(
                repository_path=repository_path,
                ancestor_commit=f"refs/heads/{default_branch}",
                descendant_commit=ticket["candidate_commit"],
            )
            return {
                "handoff_mode": "candidate_continuation" if candidate_is_current else "rebase_required",
                "start_commit": ticket["candidate_commit"] if candidate_is_current else None,
                "checkpoint": None,
                "artifact": None,
            }

        checkpoint = self.git_worktrees.inspect_checkpoint(
            repository_path=repository_path,
            worktree_path=existing["worktree_path"],
            expected_branch=existing["branch_ref"],
            expected_base_commit=existing["base_commit"],
            default_branch=default_branch,
        )
        if checkpoint.status == CLEAN_CHECKPOINT:
            return {
                "handoff_mode": "checkpoint_continuation",
                "start_commit": checkpoint.head_commit,
                "checkpoint": checkpoint,
                "artifact": None,
            }
        artifact = None
        if checkpoint.status == DIRTY:
            artifact = self.git_worktrees.capture_recovery_patch(
                worktree_path=existing["worktree_path"],
                destination=self._recovery_artifact_path(
                    snapshot["project_id"], ticket["ticket_id"], existing["agent_id"]
                ),
            )
        return {
            "handoff_mode": "recovery_required",
            "start_commit": None,
            "checkpoint": checkpoint,
            "artifact": artifact,
        }

    def _recovery_artifact_path(self, project_id: str, ticket_id: str, agent_id: str) -> Path:
        segments = [
            _safe_path_segment(project_id, "project_id"),
            _safe_path_segment(ticket_id, "ticket_id"),
            _safe_path_segment(agent_id, "agent_id"),
        ]
        return self.recovery_root.joinpath(*segments) / f"{_id('recovery')}.patch"

    def _assign_worktree(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        expected_version: int,
        snapshot: dict[str, Any],
        takeover: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Create the new worktree and record the assignment under one write lock."""
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
                raise ConflictError("Worktrees can only be prepared for active development work")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            existing = _row(
                connection.execute(
                    "SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)
                ).fetchone()
            )
            if existing is not None and existing["agent_id"] == actor.agent_id:
                return {"ticket": ticket, "worktree": existing, "reused": True}
            inspected = None if snapshot["existing"] is None else snapshot["existing"]["branch_ref"]
            current = None if existing is None else existing["branch_ref"]
            if current != inspected:
                raise ConflictError(
                    "Ticket worktree assignment changed while it was being prepared"
                )

            prepared = self.git_worktrees.prepare(
                repository_path=snapshot["repository"]["repository_path"],
                default_branch=snapshot["project"]["default_branch"],
                project_id=project_id,
                ticket_id=ticket_id,
                agent_id=actor.agent_id,
                start_commit=None if takeover is None else takeover["start_commit"],
            )
            timestamp = _iso()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO worktrees
                        (ticket_id, project_id, agent_id, repository_path, worktree_path,
                         branch_ref, base_commit, created_at, last_verified_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ticket_id,
                        project_id,
                        actor.agent_id,
                        prepared.repository_path,
                        prepared.worktree_path,
                        prepared.branch_ref,
                        prepared.base_commit,
                        timestamp,
                        timestamp,
                    ),
                )
                event_type = "worktree.prepared"
                event_payload: dict[str, Any] = {"branch_ref": prepared.branch_ref}
            else:
                connection.execute(
                    """
                    UPDATE worktrees
                    SET agent_id = ?, repository_path = ?, worktree_path = ?, branch_ref = ?,
                        base_commit = ?, created_at = ?, last_verified_at = ?
                    WHERE ticket_id = ?
                    """,
                    (
                        actor.agent_id,
                        prepared.repository_path,
                        prepared.worktree_path,
                        prepared.branch_ref,
                        prepared.base_commit,
                        timestamp,
                        timestamp,
                        ticket_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE worktree_assignments SET released_at = ?
                    WHERE ticket_id = ? AND released_at IS NULL
                    """,
                    (timestamp, ticket_id),
                )
                assert takeover is not None
                event_type = "worktree.handed_off"
                event_payload = {
                    "previous_agent_id": existing["agent_id"],
                    "previous_branch_ref": existing["branch_ref"],
                    "branch_ref": prepared.branch_ref,
                    "rejected_candidate_commit": ticket["candidate_commit"],
                    "seed_commit": takeover["start_commit"] or prepared.base_commit,
                    "handoff_mode": takeover["handoff_mode"],
                }
                self._record_handoff_context(
                    connection,
                    actor,
                    project_id,
                    ticket,
                    existing,
                    prepared,
                    takeover,
                    snapshot["project"]["default_branch"],
                    timestamp,
                )
            connection.execute(
                """
                INSERT INTO worktree_assignments
                    (assignment_id, ticket_id, project_id, agent_id, worktree_path, branch_ref,
                     base_commit, seed_commit, handoff_mode, previous_agent_id, assigned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _id("wtassign"),
                    ticket_id,
                    project_id,
                    actor.agent_id,
                    prepared.worktree_path,
                    prepared.branch_ref,
                    prepared.base_commit,
                    None if takeover is None else takeover["start_commit"],
                    None if takeover is None else takeover["handoff_mode"],
                    None if existing is None else existing["agent_id"],
                    timestamp,
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                event_type,
                "ticket",
                ticket_id,
                ticket["version"],
                event_payload,
            )
            worktree = self._one(
                connection, "SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)
            )
            return {"ticket": ticket, "worktree": worktree, "reused": False}

    def _record_handoff_context(
        self,
        connection: sqlite3.Connection,
        actor: Actor,
        project_id: str,
        ticket: dict[str, Any],
        previous: dict[str, Any],
        prepared: Any,
        takeover: dict[str, Any],
        default_branch: str,
        timestamp: str,
    ) -> None:
        """Leave the replacement a durable, redacted account of what was inherited."""
        mode = takeover["handoff_mode"]
        if mode == "candidate_continuation":
            return
        lineage = (
            f"Replaced Developer {previous['agent_id']} held branch {previous['branch_ref']} "
            f"at base {previous['base_commit']}."
        )
        if mode == "rebase_required":
            entry_type = "worktree_handoff"
            source_ref = f"candidate:{ticket['candidate_commit']}"
            artifact_commit = ticket["candidate_commit"]
            content = (
                "Rejected candidate diverges from the current default branch. "
                "The new Developer worktree starts at current main; inspect the "
                "frozen candidate and explicitly rebase, cherry-pick, or reimplement "
                "the required changes. No automatic merge was performed. " + lineage
            )
        elif mode == "checkpoint_continuation":
            entry_type = "worktree_handoff"
            source_ref = f"worktree:{previous['branch_ref']}"
            artifact_commit = takeover["start_commit"]
            content = (
                f"{lineage} That worktree was clean at verified checkpoint "
                f"{takeover['start_commit']}, so replacement branch {prepared.branch_ref} "
                "is seeded from that commit. The prior worktree stays read-only and its "
                "branch can no longer freeze a candidate."
            )
        else:
            entry_type = "worktree_recovery"
            checkpoint = takeover["checkpoint"]
            artifact = takeover["artifact"]
            source_ref = None if artifact is None else artifact.path
            artifact_commit = checkpoint.head_commit
            parts = [
                f"Failed closed on code transfer. {lineage} {checkpoint.detail} "
                f"Replacement branch {prepared.branch_ref} starts from current "
                f"{default_branch}; nothing from the prior worktree was trusted, "
                "staged, committed or merged."
            ]
            if artifact is not None:
                parts.append(
                    f"A private recovery patch was retained: sha256={artifact.sha256} "
                    f"bytes={artifact.byte_size}. Review and apply it explicitly, or discard it."
                )
            if checkpoint.untracked_paths:
                parts.append(
                    "Untracked paths left behind (names only): "
                    + _bounded_join(checkpoint.untracked_paths)
                )
            content = " ".join(parts)
        connection.execute(
            """
            INSERT INTO context_entries
                (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                 source_ref, artifact_commit, actor_id, actor_role, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _id("ctx"),
                project_id,
                ticket["feature_id"],
                ticket["ticket_id"],
                entry_type,
                content,
                source_ref,
                artifact_commit,
                actor.agent_id,
                actor.role.value,
                timestamp,
            ),
        )

    def queue_worktree_provision(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        expected_version: int,
    ) -> dict[str, Any]:
        """Queue potentially slow Git worktree setup for an isolated worker."""
        require_role(actor, Role.DEVELOPER)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
                raise ConflictError("Worktrees can only be prepared for active development work")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            existing = _row(
                connection.execute(
                    "SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)
                ).fetchone()
            )
            if existing is not None and existing["agent_id"] == actor.agent_id:
                return {"ticket": ticket, "worktree": existing, "provision": None, "reused": True}
            active = _row(
                connection.execute(
                    """
                    SELECT * FROM worktree_provision_jobs
                    WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (ticket_id,),
                ).fetchone()
            )
            if active is not None:
                if active["requested_by"] != actor.agent_id or active["lease_id"] != lease_id:
                    raise ConflictError("Ticket already has an active worktree provision")
                return {"ticket": ticket, "provision": active, "reused": False}

            provision_id = _id("worktree_job")
            timestamp = _iso()
            connection.execute(
                """
                INSERT INTO worktree_provision_jobs
                    (provision_id, project_id, ticket_id, lease_id, expected_version,
                     requested_by, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', ?)
                """,
                (
                    provision_id,
                    project_id,
                    ticket_id,
                    lease_id,
                    expected_version,
                    actor.agent_id,
                    timestamp,
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                "worktree.provision_queued",
                "ticket",
                ticket_id,
                ticket["version"],
                {"provision_id": provision_id},
            )
            return {
                "ticket": ticket,
                "provision": self._worktree_provision_job(connection, provision_id),
                "reused": False,
            }

    def get_worktree_provision(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        provision_id: str,
        lease_seconds: int = 900,
    ) -> dict[str, Any]:
        """Poll a worktree provision job while renewing its exclusive Developer lease."""
        require_role(actor, Role.DEVELOPER)
        if not 30 <= lease_seconds <= 3600:
            raise ValidationError("Lease duration must be between 30 and 3600 seconds")
        with self.store.connection(immediate=True) as connection:
            ticket, _project_id = self._authorized_ticket(connection, actor, ticket_id)
            if ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
                raise ConflictError("Ticket is not in active development")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            provision = self._worktree_provision_job(connection, provision_id)
            if provision["ticket_id"] != ticket_id or provision["lease_id"] != lease_id:
                raise AuthorizationError("Worktree provision does not belong to this Developer lease")
            if provision["requested_by"] != actor.agent_id:
                raise AuthorizationError("Worktree provision belongs to another Developer")
            timestamp = _now()
            connection.execute(
                "UPDATE leases SET heartbeat_at = ?, expires_at = ? WHERE lease_id = ?",
                (_iso(timestamp), _iso(timestamp + timedelta(seconds=lease_seconds)), lease_id),
            )
            worktree = _row(
                connection.execute("SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)).fetchone()
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "lease": self._lease(connection, lease_id),
                "provision": provision,
                "worktree": worktree,
            }

    def execute_queued_worktree_provision(self, provision_id: str) -> dict[str, Any]:
        """Worker-only Git setup for a previously authorized Developer job."""
        preflight_error: str | None = None
        with self.store.connection(immediate=True) as connection:
            provision = self._worktree_provision_job(connection, provision_id)
            if provision["status"] != "QUEUED":
                return provision
            actor = Actor(
                provision["requested_by"], Role.DEVELOPER, frozenset({provision["project_id"]})
            )
            ticket = self._ticket(connection, provision["ticket_id"])
            if ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
                preflight_error = "Ticket is no longer in active development before worktree setup."
            else:
                try:
                    self._require_active_lease(
                        connection, actor, provision["ticket_id"], provision["lease_id"]
                    )
                    self._expect_version(ticket, provision["expected_version"])
                except DomainError as error:
                    preflight_error = str(error)
            if preflight_error is None:
                connection.execute(
                    """
                    UPDATE worktree_provision_jobs
                    SET status = 'RUNNING', started_at = ? WHERE provision_id = ?
                    """,
                    (_iso(), provision_id),
                )

        if preflight_error is not None:
            return self.fail_queued_worktree_provision(provision_id, preflight_error)

        try:
            self.prepare_worktree(
                actor,
                provision["ticket_id"],
                provision["lease_id"],
                provision["expected_version"],
            )
        except DomainError as error:
            return self.fail_queued_worktree_provision(provision_id, str(error))

        with self.store.connection(immediate=True) as connection:
            completed = self._worktree_provision_job(connection, provision_id)
            if completed["status"] != "RUNNING":
                return completed
            timestamp = _iso()
            connection.execute(
                """
                UPDATE worktree_provision_jobs
                SET status = 'COMPLETED', error_message = NULL, completed_at = ?
                WHERE provision_id = ?
                """,
                (timestamp, provision_id),
            )
            ticket = self._ticket(connection, completed["ticket_id"])
            actor = Actor(
                completed["requested_by"], Role.DEVELOPER, frozenset({completed["project_id"]})
            )
            self._event(
                connection,
                actor,
                completed["project_id"],
                "worktree.provision_completed",
                "ticket",
                completed["ticket_id"],
                ticket["version"],
                {"provision_id": provision_id},
            )
            return self._worktree_provision_job(connection, provision_id)

    def fail_queued_worktree_provision(
        self, provision_id: str, error_message: str
    ) -> dict[str, Any]:
        """Persist a worker error so the owning Developer can decide how to proceed."""
        self._bounded_text(error_message, "worktree_provision.error_message", 4000)
        with self.store.connection(immediate=True) as connection:
            provision = self._worktree_provision_job(connection, provision_id)
            if provision["status"] in {"COMPLETED", "FAILED"}:
                return provision
            timestamp = _iso()
            connection.execute(
                """
                UPDATE worktree_provision_jobs
                SET status = 'FAILED', error_message = ?, completed_at = ?
                WHERE provision_id = ?
                """,
                (error_message, timestamp, provision_id),
            )
            ticket = self._ticket(connection, provision["ticket_id"])
            actor = Actor(
                provision["requested_by"], Role.DEVELOPER, frozenset({provision["project_id"]})
            )
            self._event(
                connection,
                actor,
                provision["project_id"],
                "worktree.provision_failed",
                "ticket",
                provision["ticket_id"],
                ticket["version"],
                {"provision_id": provision_id},
            )
            return self._worktree_provision_job(connection, provision_id)

    def claim_qa(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        lease_seconds: int,
    ) -> dict[str, Any]:
        require_role(actor, Role.QA)
        return self._claim(
            actor,
            ticket_id,
            expected_version,
            lease_seconds,
            allowed_statuses={TicketStatus.QA_READY},
            next_status=TicketStatus.QA_IN_PROGRESS,
            event_type="qa.started",
        )

    def claim_integration(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        lease_seconds: int,
    ) -> dict[str, Any]:
        require_role(actor, Role.INTEGRATOR)
        return self._claim(
            actor,
            ticket_id,
            expected_version,
            lease_seconds,
            allowed_statuses={TicketStatus.VERIFIED, TicketStatus.MERGE_READY},
            next_status=TicketStatus.MERGE_READY,
            event_type="integration.started",
        )

    def merge_candidate(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        expected_version: int,
    ) -> dict[str, Any]:
        """Merge only a QA-verified candidate whose mainline base has not drifted."""
        require_role(actor, Role.INTEGRATOR)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] != TicketStatus.MERGE_READY.value:
                raise ConflictError("Ticket is not ready for integration")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            repository = _row(
                connection.execute(
                    "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            candidate = self._one(
                connection,
                "SELECT * FROM candidates WHERE ticket_id = ? AND commit_sha = ?",
                (ticket_id, ticket["candidate_commit"]),
            )
            if repository is None:
                raise ValidationError("Project repository is required before integrating a candidate")
            merged, mainline_commit = self.git_worktrees.fast_forward_default_branch(
                repository_path=repository["repository_path"],
                default_branch=project["default_branch"],
                expected_base_commit=candidate["base_commit"],
                candidate_commit=candidate["commit_sha"],
            )
            timestamp = _iso()
            next_version = ticket["version"] + 1
            connection.execute("DELETE FROM leases WHERE lease_id = ?", (lease_id,))
            if not merged:
                reason = (
                    "Mainline advanced after this candidate was based; rebase the candidate "
                    "and submit a new QA candidate."
                )
                connection.execute(
                    """
                    INSERT INTO context_entries
                        (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                         source_ref, artifact_commit, actor_id, actor_role, created_at)
                    VALUES (?, ?, ?, ?, 'integration_base_drift', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _id("ctx"),
                        project_id,
                        ticket["feature_id"],
                        ticket_id,
                        reason,
                        f"candidate:{candidate['candidate_id']}",
                        mainline_commit,
                        actor.agent_id,
                        actor.role.value,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE tickets
                    SET status = ?, blocked_reason = ?, version = ?, updated_at = ?
                    WHERE ticket_id = ?
                    """,
                    (TicketStatus.REWORK.value, reason, next_version, timestamp, ticket_id),
                )
                self._event(
                    connection,
                    actor,
                    project_id,
                    "integration.base_drift",
                    "ticket",
                    ticket_id,
                    next_version,
                    {"candidate_id": candidate["candidate_id"], "mainline_commit": mainline_commit},
                )
                rework_ticket = self._ticket(connection, ticket_id)
                self._queue_ticket(connection, actor, project_id, rework_ticket)
                return {"ticket": rework_ticket, "merge_status": "base_drift"}
            connection.execute(
                """
                UPDATE tickets
                SET status = ?, blocked_reason = NULL, version = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (TicketStatus.DONE.value, next_version, timestamp, ticket_id),
            )
            self._event(
                connection,
                actor,
                project_id,
                "integration.completed",
                "ticket",
                ticket_id,
                next_version,
                {"candidate_id": candidate["candidate_id"], "commit_sha": mainline_commit},
            )
            return {"ticket": self._ticket(connection, ticket_id), "merge_status": "merged"}

    def _release_policy_snapshot(
        self, actor: Actor, ticket_id: str
    ) -> dict[str, Any]:
        """Read everything the gate judges, without holding a write lock."""
        with self.store.connection() as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            repository = _row(
                connection.execute(
                    "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            candidate = _row(
                connection.execute(
                    "SELECT * FROM candidates WHERE ticket_id = ? AND commit_sha = ?",
                    (ticket_id, ticket["candidate_commit"]),
                ).fetchone()
            )
            if candidate is None:
                raise ReleasePolicyError(
                    "No frozen candidate matches the ticket's current artifact",
                    {"reason": "candidate_identity_mismatch"},
                )
            candidate_id = candidate["candidate_id"]
            findings = [
                _row(row)
                for row in connection.execute(
                    "SELECT * FROM qa_findings WHERE candidate_id = ?", (candidate_id,)
                ).fetchall()
            ]
            evidence = [
                _row(row)
                for row in connection.execute(
                    "SELECT * FROM evidence WHERE candidate_id = ?", (candidate_id,)
                ).fetchall()
            ]
            test_runs = [
                _row(row)
                for row in connection.execute(
                    "SELECT * FROM qa_test_runs WHERE candidate_id = ? ORDER BY created_at ASC",
                    (candidate_id,),
                ).fetchall()
            ]
            profile = _row(
                connection.execute(
                    "SELECT * FROM candidate_test_profiles WHERE candidate_id = ?",
                    (candidate_id,),
                ).fetchone()
            )
            gates = [
                _row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM context_entries
                    WHERE ticket_id = ? AND superseded_by IS NULL
                      AND entry_type IN ('integration_hold', 'integration_authorization')
                    ORDER BY created_at ASC
                    """,
                    (ticket_id,),
                ).fetchall()
            ]
            verified_event = connection.execute(
                """
                SELECT 1 FROM audit_events
                WHERE aggregate_id = ? AND event_type = 'ticket.verified'
                  AND payload LIKE ?
                """,
                (ticket_id, f'%"{candidate_id}"%'),
            ).fetchone()
        return {
            "ticket": ticket,
            "project": project,
            "project_id": project_id,
            "repository": repository,
            "candidate": candidate,
            "findings": findings,
            "evidence": evidence,
            "test_runs": test_runs,
            "profile": profile,
            "gates": gates,
            "qa_verified": verified_event is not None,
        }

    def _require_release_evidence(self, snapshot: dict[str, Any]) -> None:
        """Judge release eligibility from durable evidence only, never from claims."""
        ticket = snapshot["ticket"]
        candidate = snapshot["candidate"]
        if snapshot["repository"] is None:
            raise ValidationError("Project repository is required before integrating a candidate")
        if not snapshot["qa_verified"]:
            raise ReleasePolicyError(
                "This candidate has no recorded QA verification for its ticket",
                {"reason": "ticket_not_verified"},
            )

        unverified = sorted(
            {
                str(finding["status"])
                for finding in snapshot["findings"]
                if finding["status"] != "verified"
            }
        )
        if unverified:
            raise ReleasePolicyError(
                "The candidate carries QA findings that are not verified",
                {"reason": "unverified_qa_finding", "statuses": unverified},
            )
        evidenced = {
            (str(item["criterion"]))
            for item in snapshot["evidence"]
        }
        verified_criteria = {
            str(finding["criterion"])
            for finding in snapshot["findings"]
            if finding["status"] == "verified" and str(finding["criterion"]) in evidenced
        }
        missing = sorted(set(ticket["acceptance_criteria"]) - verified_criteria)
        if missing:
            raise ReleasePolicyError(
                "Every mandatory acceptance criterion needs candidate-bound QA evidence",
                {"reason": "missing_qa_evidence", "criteria": missing},
            )

        # A qa_test_runs row is only written for a run whose resolved source was
        # the frozen candidate, so its presence is the source-identity proof.
        profile = snapshot["profile"]
        if profile is None:
            raise ReleasePolicyError(
                "The candidate has no registered regression command to satisfy",
                {"reason": "regression_run_missing"},
            )
        digest = hashlib.sha256("\0".join(profile["test_command"]).encode()).hexdigest()
        matching = [run for run in snapshot["test_runs"] if run["command_digest"] == digest]
        if not matching:
            raise ReleasePolicyError(
                "No registered regression run was recorded against this candidate",
                {"reason": "regression_run_missing"},
            )
        if not any(run["exit_code"] == 0 and not run["timed_out"] for run in matching):
            raise ReleasePolicyError(
                "The registered regression run did not complete successfully",
                {"reason": "regression_run_failed"},
            )

        holds = [
            entry
            for entry in snapshot["gates"]
            if entry["entry_type"] == "integration_hold"
            and entry["artifact_commit"] in (None, candidate["commit_sha"])
        ]
        if holds:
            latest_hold = holds[-1]["created_at"]
            # Only an authorization naming this exact SHA can clear a hold; one
            # issued for another artifact says nothing about this candidate.
            cleared = any(
                entry["entry_type"] == "integration_authorization"
                and entry["artifact_commit"] == candidate["commit_sha"]
                and entry["created_at"] > latest_hold
                for entry in snapshot["gates"]
            )
            if not cleared:
                raise ReleasePolicyError(
                    "A current integration hold blocks this candidate",
                    {"reason": "integration_hold", "entry_id": holds[-1]["entry_id"]},
                )

    def _require_mergeable_identity(self, snapshot: dict[str, Any]) -> None:
        """Confirm the exact candidate still fast-forwards current main.

        Git runs here, deliberately outside any transaction, so no write lock is
        held while the repository is inspected.
        """
        repository_path = snapshot["repository"]["repository_path"]
        default_branch = snapshot["project"]["default_branch"]
        candidate = snapshot["candidate"]
        mainline = f"refs/heads/{default_branch}"
        base = candidate["base_commit"]
        base_is_current_main = self.git_worktrees.commit_descends_from(
            repository_path=repository_path,
            ancestor_commit=mainline,
            descendant_commit=base,
        ) and self.git_worktrees.commit_descends_from(
            repository_path=repository_path,
            ancestor_commit=base,
            descendant_commit=mainline,
        )
        if not base_is_current_main:
            raise ReleasePolicyError(
                "Mainline moved after this candidate was based",
                {"reason": "base_drift"},
            )
        if not self.git_worktrees.commit_descends_from(
            repository_path=repository_path,
            ancestor_commit=mainline,
            descendant_commit=candidate["commit_sha"],
        ):
            raise ReleasePolicyError(
                "This candidate would not fast-forward the default branch",
                {"reason": "not_fast_forward"},
            )

    def queue_merge_candidate(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        expected_version: int,
    ) -> dict[str, Any]:
        """Durably acknowledge integration before the detached Git merge worker runs."""
        require_role(actor, Role.INTEGRATOR)
        # The standing release policy is judged before anything is queued, and the
        # Git half runs without a write lock held.
        snapshot = self._release_policy_snapshot(actor, ticket_id)
        if snapshot["ticket"]["status"] == TicketStatus.MERGE_READY.value:
            self._require_release_evidence(snapshot)
            self._require_mergeable_identity(snapshot)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] != TicketStatus.MERGE_READY.value:
                raise ConflictError("Ticket is not ready for integration")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            repository = _row(
                connection.execute(
                    "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            if repository is None:
                raise ValidationError("Project repository is required before integrating a candidate")
            candidate = self._one(
                connection,
                "SELECT * FROM candidates WHERE ticket_id = ? AND commit_sha = ?",
                (ticket_id, ticket["candidate_commit"]),
            )
            active = _row(
                connection.execute(
                    """SELECT * FROM merge_jobs
                       WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING')
                       ORDER BY created_at DESC LIMIT 1""",
                    (ticket_id,),
                ).fetchone()
            )
            if active is not None:
                if (
                    active["requested_by"] == actor.agent_id
                    and active["lease_id"] == lease_id
                    and active["candidate_id"] == candidate["candidate_id"]
                ):
                    return {
                        "ticket": ticket,
                        "merge_job": self._merge_job(connection, active["merge_job_id"]),
                    }
                raise ConflictError("Ticket already has an active merge job")
            merge_job_id = _id("merge_job")
            timestamp = _iso()
            connection.execute(
                """INSERT INTO merge_jobs
                   (merge_job_id, project_id, ticket_id, lease_id, expected_version, candidate_id,
                    requested_by, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?)""",
                (
                    merge_job_id, project_id, ticket_id, lease_id, expected_version,
                    candidate["candidate_id"], actor.agent_id, timestamp,
                ),
            )
            self._event(
                connection, actor, project_id, "integration.merge_queued", "ticket",
                ticket_id, ticket["version"], {"merge_job_id": merge_job_id},
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "merge_job": self._merge_job(connection, merge_job_id),
            }

    def get_merge_candidate(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        merge_job_id: str,
        lease_seconds: int = 900,
    ) -> dict[str, Any]:
        """Poll one merge job and renew the caller's Integrator lease while active."""
        require_role(actor, Role.INTEGRATOR)
        if not 30 <= lease_seconds <= 3600:
            raise ValidationError("Lease duration must be between 30 and 3600 seconds")
        with self.store.connection(immediate=True) as connection:
            ticket, _project_id = self._authorized_ticket(connection, actor, ticket_id)
            job = self._merge_job(connection, merge_job_id)
            if job["ticket_id"] != ticket_id or job["lease_id"] != lease_id:
                raise AuthorizationError("Merge job does not belong to this Integrator lease")
            if job["requested_by"] != actor.agent_id:
                raise AuthorizationError("Merge job belongs to another Integrator")
            lease: dict[str, Any] | None = None
            if job["status"] in {"QUEUED", "RUNNING"}:
                if ticket["status"] != TicketStatus.MERGE_READY.value:
                    raise ConflictError("Ticket is not ready for integration")
                self._require_active_lease(connection, actor, ticket_id, lease_id)
                timestamp = _now()
                connection.execute(
                    "UPDATE leases SET heartbeat_at = ?, expires_at = ? WHERE lease_id = ?",
                    (_iso(timestamp), _iso(timestamp + timedelta(seconds=lease_seconds)), lease_id),
                )
                lease = self._lease(connection, lease_id)
            return {
                "ticket": self._ticket(connection, ticket_id),
                "lease": lease,
                "merge_job": self._merge_job(connection, merge_job_id),
            }

    def execute_queued_merge(self, merge_job_id: str) -> dict[str, Any]:
        """Worker-only Git fast-forward for a previously authorized merge job."""
        preflight_error: str | None = None
        with self.store.connection(immediate=True) as connection:
            job = self._merge_job(connection, merge_job_id)
            if job["status"] != "QUEUED":
                return job
            actor = Actor(job["requested_by"], Role.INTEGRATOR, frozenset({job["project_id"]}))
            ticket = self._ticket(connection, job["ticket_id"])
            try:
                if ticket["status"] != TicketStatus.MERGE_READY.value:
                    raise ConflictError(
                        "Ticket is no longer ready for integration before the merge worker started"
                    )
                self._expect_version(ticket, job["expected_version"])
                self._require_active_lease(connection, actor, job["ticket_id"], job["lease_id"])
            except DomainError as error:
                preflight_error = str(error)
            if preflight_error is None:
                connection.execute(
                    "UPDATE merge_jobs SET status = 'RUNNING', started_at = ? WHERE merge_job_id = ?",
                    (_iso(), merge_job_id),
                )
        if preflight_error is not None:
            return self.fail_queued_merge(merge_job_id, preflight_error)
        try:
            repository, project, candidate = self._merge_git_inputs(merge_job_id)
            if repository is None:
                raise ValidationError("Project repository is required before integrating a candidate")
            merged, mainline_commit = self.git_worktrees.fast_forward_default_branch(
                repository_path=repository["repository_path"],
                default_branch=project["default_branch"],
                expected_base_commit=candidate["base_commit"],
                candidate_commit=candidate["commit_sha"],
            )
            return self._complete_queued_merge(merge_job_id, merged, mainline_commit)
        except DomainError as error:
            return self.fail_queued_merge(merge_job_id, str(error))

    def _merge_git_inputs(
        self, merge_job_id: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
        with self.store.connection() as connection:
            job = self._merge_job(connection, merge_job_id)
            repository = _row(
                connection.execute(
                    "SELECT * FROM project_repositories WHERE project_id = ?", (job["project_id"],)
                ).fetchone()
            )
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (job["project_id"],)
            )
            candidate = self._one(
                connection, "SELECT * FROM candidates WHERE candidate_id = ?", (job["candidate_id"],)
            )
            return repository, project, candidate

    def _complete_queued_merge(
        self, merge_job_id: str, merged: bool, mainline_commit: str
    ) -> dict[str, Any]:
        with self.store.connection(immediate=True) as connection:
            job = self._merge_job(connection, merge_job_id)
            if job["status"] != "RUNNING":
                return job
            actor = Actor(job["requested_by"], Role.INTEGRATOR, frozenset({job["project_id"]}))
            ticket = self._ticket(connection, job["ticket_id"])
            if ticket["status"] != TicketStatus.MERGE_READY.value:
                raise ConflictError("Ticket changed while the merge worker ran")
            self._expect_version(ticket, job["expected_version"])
            self._require_active_lease(connection, actor, job["ticket_id"], job["lease_id"])
            timestamp = _iso()
            next_version = ticket["version"] + 1
            connection.execute("DELETE FROM leases WHERE lease_id = ?", (job["lease_id"],))
            if not merged:
                reason = (
                    "Mainline advanced after this candidate was based; rebase the candidate "
                    "and submit a new QA candidate."
                )
                connection.execute(
                    """
                    INSERT INTO context_entries
                        (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                         source_ref, artifact_commit, actor_id, actor_role, created_at)
                    VALUES (?, ?, ?, ?, 'integration_base_drift', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _id("ctx"), job["project_id"], ticket["feature_id"], job["ticket_id"],
                        reason, f"candidate:{job['candidate_id']}", mainline_commit,
                        actor.agent_id, actor.role.value, timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE tickets
                    SET status = ?, blocked_reason = ?, version = ?, updated_at = ?
                    WHERE ticket_id = ?
                    """,
                    (TicketStatus.REWORK.value, reason, next_version, timestamp, job["ticket_id"]),
                )
                self._event(
                    connection, actor, job["project_id"], "integration.base_drift", "ticket",
                    job["ticket_id"], next_version,
                    {"candidate_id": job["candidate_id"], "mainline_commit": mainline_commit},
                )
                rework_ticket = self._ticket(connection, job["ticket_id"])
                self._queue_ticket(connection, actor, job["project_id"], rework_ticket)
                # A drifted candidate is a planning problem as much as a rework
                # item: the Planner decides whether to rebase, re-scope or drop it.
                self._open_planner_job(
                    connection,
                    actor,
                    job["project_id"],
                    rework_ticket,
                    job_type=PlannerJobType.INTEGRATION_CONFLICT,
                    summary=reason,
                    evidence_refs=[
                        f"candidate:{job['candidate_id']}",
                        f"merge_job:{merge_job_id}",
                    ],
                    candidate_id=job["candidate_id"],
                    artifact_commit=mainline_commit,
                )
                connection.execute(
                    """UPDATE merge_jobs
                       SET status = 'COMPLETED', merge_status = 'base_drift', mainline_commit = ?,
                           error_message = NULL, completed_at = ?
                       WHERE merge_job_id = ?""",
                    (mainline_commit, timestamp, merge_job_id),
                )
                return self._merge_job(connection, merge_job_id)
            connection.execute(
                """
                UPDATE tickets
                SET status = ?, blocked_reason = NULL, version = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (TicketStatus.DONE.value, next_version, timestamp, job["ticket_id"]),
            )
            self._event(
                connection, actor, job["project_id"], "integration.completed", "ticket",
                job["ticket_id"], next_version,
                {"candidate_id": job["candidate_id"], "commit_sha": mainline_commit},
            )
            connection.execute(
                """UPDATE merge_jobs
                   SET status = 'COMPLETED', merge_status = 'merged', mainline_commit = ?,
                       error_message = NULL, completed_at = ?
                   WHERE merge_job_id = ?""",
                (mainline_commit, timestamp, merge_job_id),
            )
            return self._merge_job(connection, merge_job_id)

    def fail_queued_merge(self, merge_job_id: str, error_message: str) -> dict[str, Any]:
        """Terminally record a merge worker failure without blocking Integrator polling."""
        self._bounded_text(error_message, "merge_job.error_message", 4000)
        with self.store.connection(immediate=True) as connection:
            job = self._merge_job(connection, merge_job_id)
            if job["status"] in {"COMPLETED", "FAILED"}:
                return job
            connection.execute(
                """UPDATE merge_jobs SET status = 'FAILED', error_message = ?, completed_at = ?
                   WHERE merge_job_id = ?""",
                (error_message, _iso(), merge_job_id),
            )
            actor = Actor(job["requested_by"], Role.INTEGRATOR, frozenset({job["project_id"]}))
            ticket = self._ticket(connection, job["ticket_id"])
            self._event(
                connection, actor, job["project_id"], "integration.merge_failed", "ticket",
                job["ticket_id"], ticket["version"], {"merge_job_id": merge_job_id},
            )
            return self._merge_job(connection, merge_job_id)

    def _merge_job(self, connection: sqlite3.Connection, merge_job_id: str) -> dict[str, Any]:
        return self._one(
            connection, "SELECT * FROM merge_jobs WHERE merge_job_id = ?", (merge_job_id,)
        )

    def heartbeat(self, actor: Actor, lease_id: str, lease_seconds: int = 900) -> dict[str, Any]:
        if not 30 <= lease_seconds <= 3600:
            raise ValidationError("Lease duration must be between 30 and 3600 seconds")
        with self.store.connection(immediate=True) as connection:
            lease = self._lease(connection, lease_id)
            if lease["agent_id"] != actor.agent_id or lease["role"] != actor.role.value:
                raise AuthorizationError("Lease belongs to another actor")
            timestamp = _now()
            if datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00")) <= timestamp:
                raise ConflictError("Lease has expired")
            expires_at = _iso(timestamp + timedelta(seconds=lease_seconds))
            connection.execute(
                "UPDATE leases SET heartbeat_at = ?, expires_at = ? WHERE lease_id = ?",
                (_iso(timestamp), expires_at, lease_id),
            )
            return self._lease(connection, lease_id)

    def sweep_expired_leases(self, actor: Actor) -> dict[str, Any]:
        """Recover expired Developer and QA leases without waiting for a later claim."""
        require_role(actor, Role.ADMIN)
        timestamp = _now()
        requeued: list[dict[str, Any]] = []
        with self.store.connection(immediate=True) as connection:
            expired_leases = connection.execute(
                "SELECT * FROM leases WHERE expires_at <= ? ORDER BY expires_at ASC",
                (_iso(timestamp),),
            ).fetchall()
            for lease_row in expired_leases:
                lease = _row(lease_row)
                assert lease is not None
                ticket = self._ticket(connection, lease["ticket_id"])
                project_id = self._project_for_ticket(connection, ticket["ticket_id"])
                require_project(actor, project_id)
                fallback = (
                    TicketStatus.MERGE_READY
                    if lease["role"] == Role.INTEGRATOR.value
                    else TicketStatus.QA_READY
                    if lease["role"] == Role.QA.value
                    else TicketStatus.READY
                )
                next_version = ticket["version"] + 1
                connection.execute("DELETE FROM leases WHERE lease_id = ?", (lease["lease_id"],))
                connection.execute(
                    """
                    UPDATE tickets
                    SET status = ?, version = ?, updated_at = ?
                    WHERE ticket_id = ?
                    """,
                    (fallback.value, next_version, _iso(timestamp), ticket["ticket_id"]),
                )
                self._event(
                    connection,
                    actor,
                    project_id,
                    "lease.expired_requeued",
                    "ticket",
                    ticket["ticket_id"],
                    next_version,
                    {"requeued_to": fallback.value, "previous_role": lease["role"]},
                )
                requeued_ticket = self._ticket(connection, ticket["ticket_id"])
                self._queue_ticket(connection, actor, project_id, requeued_ticket)
                requeued.append(
                    {
                        "ticket_id": ticket["ticket_id"],
                        "project_id": project_id,
                        "status": fallback.value,
                        "version": next_version,
                    }
                )
        return {"count": len(requeued), "requeued": requeued}

    def append_progress(
        self, actor: Actor, ticket_id: str, lease_id: str, message: str
    ) -> dict[str, Any]:
        require_role(actor, Role.DEVELOPER)
        self._bounded_text(message, "message", 8000)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            entry_id = _id("ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'progress_update', ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    project_id,
                    ticket["feature_id"],
                    ticket_id,
                    message,
                    actor.agent_id,
                    actor.role.value,
                    _iso(),
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                "ticket.progress_appended",
                "ticket",
                ticket_id,
                ticket["version"],
                {"entry_id": entry_id},
            )
            return self._one(
                connection, "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
            )

    def report_blocker(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        expected_version: int,
        reason: str,
    ) -> dict[str, Any]:
        """Release developer work back to planning with a durable blocker context."""
        require_role(actor, Role.DEVELOPER)
        self._bounded_text(reason, "reason", 4000)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
                raise ConflictError("Only active development work can be blocked")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            timestamp = _iso()
            entry_id = _id("ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'developer_blocker', ?, ?, ?, ?)
                """,
                (entry_id, project_id, ticket["feature_id"], ticket_id, reason,
                 actor.agent_id, actor.role.value, timestamp),
            )
            next_version = ticket["version"] + 1
            connection.execute(
                """
                UPDATE tickets SET status = ?, blocked_reason = ?, version = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (TicketStatus.BLOCKED.value, reason, next_version, timestamp, ticket_id),
            )
            connection.execute("DELETE FROM leases WHERE lease_id = ?", (lease_id,))
            self._event(
                connection, actor, project_id, "ticket.blocked", "ticket", ticket_id,
                next_version, {"entry_id": entry_id},
            )
            blocked_ticket = self._ticket(connection, ticket_id)
            planner_job = self._open_planner_job(
                connection,
                actor,
                project_id,
                blocked_ticket,
                job_type=PlannerJobType.DEVELOPER_BLOCKER,
                summary=reason,
                evidence_refs=[f"context:{entry_id}"],
                context_entry_id=entry_id,
                # A Developer with no safe next code action is stopped work.
                severity=InterventionSeverity.HIGH,
            )
            return {
                "ticket": blocked_ticket,
                "context_entry_id": entry_id,
                "planner_job": planner_job,
            }

    def record_test_run(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        summary: str,
        evidence_refs: list[str],
    ) -> dict[str, Any]:
        """Persist a QA test-run checkpoint before the final evidence-bearing verdict."""
        require_role(actor, Role.QA)
        self._bounded_text(summary, "summary", 8000)
        self._bounded_list(evidence_refs, "evidence_refs", 30, 1000)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            if ticket["status"] != TicketStatus.QA_IN_PROGRESS.value:
                raise ConflictError("Ticket is not in QA")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            entry_id = _id("ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     source_ref, actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'qa_test_run', ?, ?, ?, ?, ?)
                """,
                (entry_id, project_id, ticket["feature_id"], ticket_id, summary,
                 json.dumps(evidence_refs), actor.agent_id, actor.role.value, _iso()),
            )
            self._event(
                connection, actor, project_id, "qa.test_run_recorded", "ticket", ticket_id,
                ticket["version"], {"entry_id": entry_id, "evidence_count": len(evidence_refs)},
            )
            return self._one(
                connection, "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
            )

    def queue_project_tests(
        self, actor: Actor, ticket_id: str, lease_id: str
    ) -> dict[str, Any]:
        """Persist a frozen QA test job and return before its command is executed."""
        require_role(actor, Role.QA)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            if ticket["status"] != TicketStatus.QA_IN_PROGRESS.value:
                raise ConflictError("Ticket is not in QA")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            candidate = self._one(
                connection,
                "SELECT * FROM candidates WHERE ticket_id = ? ORDER BY submitted_at DESC LIMIT 1",
                (ticket_id,),
            )
            active_job = connection.execute(
                """
                SELECT test_run_id FROM qa_test_jobs
                WHERE candidate_id = ? AND status IN ('QUEUED', 'RUNNING')
                """,
                (candidate["candidate_id"],),
            ).fetchone()
            if active_job is not None:
                raise ConflictError("Candidate already has an active project test run")
            test_profile = _row(
                connection.execute(
                    "SELECT * FROM candidate_test_profiles WHERE candidate_id = ?",
                    (candidate["candidate_id"],),
                ).fetchone()
            )
            if test_profile is None:
                raise ValidationError("Candidate has no Planner-frozen project test command")
            worktree = _row(
                connection.execute("SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)).fetchone()
            )
            if worktree is None:
                raise ValidationError("Candidate has no assigned Git worktree")
            command = test_profile["test_command"]
            self._test_command(command)
            timeout_seconds = test_profile["timeout_seconds"]
            if not isinstance(timeout_seconds, int) or not 5 <= timeout_seconds <= 1800:
                raise ValidationError("Candidate test command timeout is invalid")
            candidate_commit = candidate["commit_sha"]
            # Queueing must return promptly. The detached worker re-verifies this
            # immutable path and commit before it executes the test command.
            worktree_path = worktree["worktree_path"]
            test_run_id = _id("testrun")
            timestamp = _iso()
            connection.execute(
                """
                INSERT INTO qa_test_jobs
                    (test_run_id, project_id, candidate_id, ticket_id, lease_id, test_command,
                     command_digest, timeout_seconds, worktree_path, candidate_commit,
                     requested_by, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?)
                """,
                (
                    test_run_id,
                    project_id,
                    candidate["candidate_id"],
                    ticket_id,
                    lease_id,
                    json.dumps(command),
                    hashlib.sha256("\0".join(command).encode()).hexdigest(),
                    timeout_seconds,
                    worktree_path,
                    candidate_commit,
                    actor.agent_id,
                    timestamp,
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                "qa.automated_test_queued",
                "ticket",
                ticket_id,
                ticket["version"],
                {"test_run_id": test_run_id, "candidate_id": candidate["candidate_id"]},
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "test_run": self._qa_test_job(connection, test_run_id),
            }

    def get_project_test_run(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        test_run_id: str,
        lease_seconds: int = 900,
    ) -> dict[str, Any]:
        """Poll a persisted QA test job and renew the caller's exclusive QA lease."""
        require_role(actor, Role.QA)
        if not 30 <= lease_seconds <= 3600:
            raise ValidationError("Lease duration must be between 30 and 3600 seconds")
        with self.store.connection(immediate=True) as connection:
            ticket, _project_id = self._authorized_ticket(connection, actor, ticket_id)
            if ticket["status"] != TicketStatus.QA_IN_PROGRESS.value:
                raise ConflictError("Ticket is not in QA")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            job = self._qa_test_job(connection, test_run_id)
            if job["ticket_id"] != ticket_id or job["lease_id"] != lease_id:
                raise AuthorizationError("Project test run does not belong to this QA lease")
            timestamp = _now()
            expires_at = _iso(timestamp + timedelta(seconds=lease_seconds))
            connection.execute(
                "UPDATE leases SET heartbeat_at = ?, expires_at = ? WHERE lease_id = ?",
                (_iso(timestamp), expires_at, lease_id),
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "lease": self._lease(connection, lease_id),
                "test_run": self._qa_test_job(connection, test_run_id),
            }

    def execute_queued_project_test(self, test_run_id: str) -> dict[str, Any]:
        """Worker-only execution for a previously authorized, immutable QA test job."""
        preflight_error: str | None = None
        with self.store.connection(immediate=True) as connection:
            job = self._qa_test_job(connection, test_run_id)
            if job["status"] != "QUEUED":
                return job
            actor = Actor(job["requested_by"], Role.QA, frozenset({job["project_id"]}))
            ticket = self._ticket(connection, job["ticket_id"])
            if ticket["status"] != TicketStatus.QA_IN_PROGRESS.value:
                preflight_error = "Ticket is no longer in QA before the test worker started."
            else:
                try:
                    self._require_active_lease(connection, actor, job["ticket_id"], job["lease_id"])
                    candidate = self._one(
                        connection,
                        "SELECT * FROM candidates WHERE candidate_id = ? AND ticket_id = ?",
                        (job["candidate_id"], job["ticket_id"]),
                    )
                    if candidate["commit_sha"] != job["candidate_commit"]:
                        preflight_error = "Candidate changed before the test worker started."
                except DomainError as error:
                    preflight_error = str(error)
                if preflight_error is None:
                    connection.execute(
                        "UPDATE qa_test_jobs SET status = 'RUNNING', started_at = ? WHERE test_run_id = ?",
                        (_iso(), test_run_id),
                    )

        if preflight_error is not None:
            return self.fail_queued_project_test(test_run_id, preflight_error)

        try:
            worktree_path = self.git_worktrees.verify_frozen_candidate(
                job["worktree_path"], job["candidate_commit"]
            )
            execution = self.project_tests.execute(
                job["test_command"], worktree_path, job["timeout_seconds"]
            )
            if execution.artifact_mismatch:
                return self._fail_with_artifact_mismatch(test_run_id, job, execution)
            return self._complete_queued_project_test(test_run_id, execution)
        except (DomainError, OSError) as error:
            return self.fail_queued_project_test(test_run_id, str(error))

    def _fail_with_artifact_mismatch(
        self, test_run_id: str, job: dict[str, Any], execution: TestExecution
    ) -> dict[str, Any]:
        """Record an infrastructure-only import mismatch without a candidate verdict.

        The frozen candidate's own git identity was already verified before this ran
        (verify_frozen_candidate); what failed is that the configured command imported
        hoh_control_plane from somewhere other than that verified worktree. No
        qa_test_runs row is written -- this must never read as a candidate regression --
        but the mismatch is still durably evidenced on disk and referenced from the
        job's error_message so QA and Planner can inspect expected-vs-observed.
        """
        storage_ref, _report_path = self.project_tests.write_report(
            candidate_id=job["candidate_id"],
            test_run_id=test_run_id,
            execution=execution,
            candidate_commit=job["candidate_commit"],
        )
        message = (
            f"{execution.stderr} (verified candidate commit {job['candidate_commit']}; "
            f"durable evidence={storage_ref})"
        )
        return self.fail_queued_project_test(test_run_id, message)

    def run_project_tests(
        self, actor: Actor, ticket_id: str, lease_id: str
    ) -> dict[str, Any]:
        """Compatibility helper for direct callers; MCP callers must queue then poll."""
        queued = self.queue_project_tests(actor, ticket_id, lease_id)
        completed = self.execute_queued_project_test(queued["test_run"]["test_run_id"])
        if completed["status"] == "FAILED":
            raise ValidationError(completed["error_message"])
        return self.get_project_test_run(
            actor, ticket_id, lease_id, queued["test_run"]["test_run_id"]
        )

    def _complete_queued_project_test(
        self, test_run_id: str, execution: TestExecution
    ) -> dict[str, Any]:
        try:
            with self.store.connection(immediate=True) as connection:
                job = self._qa_test_job(connection, test_run_id)
                if job["status"] != "RUNNING":
                    return job
                actor = Actor(job["requested_by"], Role.QA, frozenset({job["project_id"]}))
                ticket = self._ticket(connection, job["ticket_id"])
                if ticket["status"] != TicketStatus.QA_IN_PROGRESS.value:
                    raise ConflictError("Ticket changed while project tests were running")
                self._require_active_lease(connection, actor, job["ticket_id"], job["lease_id"])
                candidate = self._one(
                    connection,
                    "SELECT * FROM candidates WHERE candidate_id = ? AND ticket_id = ?",
                    (job["candidate_id"], job["ticket_id"]),
                )
                if candidate["commit_sha"] != job["candidate_commit"]:
                    raise ConflictError("Candidate changed while project tests were running")
                storage_ref, report_path = self.project_tests.write_report(
                    candidate_id=job["candidate_id"],
                    test_run_id=test_run_id,
                    execution=execution,
                    candidate_commit=job["candidate_commit"],
                )
                timestamp = _iso()
                connection.execute(
                    """
                    INSERT INTO qa_test_runs
                        (test_run_id, candidate_id, ticket_id, command_digest, exit_code, timed_out,
                         duration_ms, storage_ref, report_path, executed_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        test_run_id,
                        job["candidate_id"],
                        job["ticket_id"],
                        execution.command_digest,
                        execution.exit_code,
                        int(execution.timed_out),
                        execution.duration_ms,
                        storage_ref,
                        report_path,
                        actor.agent_id,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE qa_test_jobs
                    SET status = 'COMPLETED', completed_at = ?, error_message = NULL
                    WHERE test_run_id = ?
                    """,
                    (timestamp, test_run_id),
                )
                entry_id = _id("ctx")
                outcome = "timed out" if execution.timed_out else f"exited {execution.exit_code}"
                connection.execute(
                    """
                    INSERT INTO context_entries
                        (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                         source_ref, artifact_commit, actor_id, actor_role, created_at)
                    VALUES (?, ?, ?, ?, 'qa_automated_test_run', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry_id,
                        job["project_id"],
                        ticket["feature_id"],
                        job["ticket_id"],
                        f"Planner-registered project test command {outcome} in {execution.duration_ms}ms.",
                        storage_ref,
                        job["candidate_commit"],
                        actor.agent_id,
                        actor.role.value,
                        timestamp,
                    ),
                )
                self._event(
                    connection,
                    actor,
                    job["project_id"],
                    "qa.automated_test_completed",
                    "ticket",
                    job["ticket_id"],
                    ticket["version"],
                    {
                        "test_run_id": test_run_id,
                        "exit_code": execution.exit_code,
                        "timed_out": execution.timed_out,
                    },
                )
                return self._qa_test_job(connection, test_run_id)
        except DomainError as error:
            return self.fail_queued_project_test(test_run_id, str(error))

    def fail_queued_project_test(self, test_run_id: str, message: str) -> dict[str, Any]:
        """Terminally record infrastructure or stale-state failures without blocking QA polling."""
        self._bounded_text(message, "test worker error", 2000)
        with self.store.connection(immediate=True) as connection:
            job = self._qa_test_job(connection, test_run_id)
            if job["status"] in {"COMPLETED", "FAILED", "STALE"}:
                return job
            timestamp = _iso()
            connection.execute(
                """
                UPDATE qa_test_jobs
                SET status = 'FAILED', error_message = ?, completed_at = ?
                WHERE test_run_id = ?
                """,
                (message, timestamp, test_run_id),
            )
            ticket = self._ticket(connection, job["ticket_id"])
            actor = Actor(job["requested_by"], Role.QA, frozenset({job["project_id"]}))
            self._event(
                connection,
                actor,
                job["project_id"],
                "qa.automated_test_failed",
                "ticket",
                job["ticket_id"],
                ticket["version"],
                {"test_run_id": test_run_id},
            )
            return self._qa_test_job(connection, test_run_id)

    def _qa_test_job(self, connection: sqlite3.Connection, test_run_id: str) -> dict[str, Any]:
        job = self._one(
            connection, "SELECT * FROM qa_test_jobs WHERE test_run_id = ?", (test_run_id,)
        )
        test_run = _row(
            connection.execute(
                "SELECT * FROM qa_test_runs WHERE test_run_id = ?", (test_run_id,)
            ).fetchone()
        )
        job["test_run"] = test_run
        if test_run is not None:
            job.update({
                key: test_run[key]
                for key in ("exit_code", "timed_out", "duration_ms", "storage_ref", "report_path")
            })
        return job

    def _worktree_provision_job(
        self, connection: sqlite3.Connection, provision_id: str
    ) -> dict[str, Any]:
        return self._one(
            connection,
            "SELECT * FROM worktree_provision_jobs WHERE provision_id = ?",
            (provision_id,),
        )

    def queue_candidate_submission(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        expected_version: int,
        commit_sha: str,
        base_commit: str,
        branch_ref: str,
        developer_claims: list[str],
    ) -> dict[str, Any]:
        """Durably acknowledge a candidate before detached Git validation begins."""
        require_role(actor, Role.DEVELOPER)
        self._valid_commit(commit_sha, "commit_sha")
        self._valid_commit(base_commit, "base_commit")
        self._bounded_text(branch_ref, "branch_ref", 300)
        self._bounded_list(developer_claims, "developer_claims", 50, 2000)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
                raise ConflictError("Ticket is not in development")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            repository = _row(connection.execute(
                "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
            ).fetchone())
            if repository is not None:
                worktree = _row(connection.execute(
                    "SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)
                ).fetchone())
                if worktree is None or worktree["agent_id"] != actor.agent_id:
                    raise ValidationError("Developer must own an assigned Git worktree before submitting")
                if worktree["branch_ref"] != branch_ref:
                    raise ValidationError("Submitted branch does not match the assigned worktree branch")
            active = _row(connection.execute(
                """SELECT * FROM candidate_submission_jobs
                   WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING')
                   ORDER BY created_at DESC LIMIT 1""", (ticket_id,)
            ).fetchone())
            if active is not None:
                if (
                    active["requested_by"] == actor.agent_id
                    and active["lease_id"] == lease_id
                    and active["commit_sha"] == commit_sha
                    and active["base_commit"] == base_commit
                    and active["branch_ref"] == branch_ref
                ):
                    return {"ticket": ticket, "submission": self._candidate_submission_job(connection, active["submission_id"])}
                raise ConflictError("Ticket already has an active candidate submission")
            submission_id = _id("candidate_job")
            timestamp = _iso()
            connection.execute(
                """INSERT INTO candidate_submission_jobs
                   (submission_id, project_id, ticket_id, lease_id, expected_version, commit_sha,
                    base_commit, branch_ref, developer_claims, requested_by, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?)""",
                (submission_id, project_id, ticket_id, lease_id, expected_version, commit_sha,
                 base_commit, branch_ref, json.dumps(developer_claims), actor.agent_id, timestamp),
            )
            self._event(connection, actor, project_id, "candidate.submission_queued", "ticket",
                        ticket_id, ticket["version"], {"submission_id": submission_id})
            return {"ticket": self._ticket(connection, ticket_id),
                    "submission": self._candidate_submission_job(connection, submission_id)}

    def candidate_submission_requires_worker(self, actor: Actor, ticket_id: str) -> bool:
        """Compatibility path for repository-free in-memory workflow demonstrations."""
        require_role(actor, Role.DEVELOPER)
        with self.store.connection() as connection:
            _ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            return connection.execute(
                "SELECT 1 FROM project_repositories WHERE project_id = ?", (project_id,)
            ).fetchone() is not None

    def get_candidate_submission(
        self, actor: Actor, ticket_id: str, lease_id: str, submission_id: str,
        lease_seconds: int = 900,
    ) -> dict[str, Any]:
        """Poll one candidate submission and renew its Developer lease while active."""
        require_role(actor, Role.DEVELOPER)
        if not 30 <= lease_seconds <= 3600:
            raise ValidationError("Lease duration must be between 30 and 3600 seconds")
        with self.store.connection(immediate=True) as connection:
            ticket, _project_id = self._authorized_ticket(connection, actor, ticket_id)
            job = self._candidate_submission_job(connection, submission_id)
            if job["ticket_id"] != ticket_id or job["lease_id"] != lease_id:
                raise AuthorizationError("Candidate submission does not belong to this Developer lease")
            if job["requested_by"] != actor.agent_id:
                raise AuthorizationError("Candidate submission belongs to another Developer")
            lease: dict[str, Any] | None = None
            if job["status"] in {"QUEUED", "RUNNING"}:
                if ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
                    raise ConflictError("Ticket is not in active development")
                self._require_active_lease(connection, actor, ticket_id, lease_id)
                timestamp = _now()
                connection.execute("UPDATE leases SET heartbeat_at = ?, expires_at = ? WHERE lease_id = ?",
                    (_iso(timestamp), _iso(timestamp + timedelta(seconds=lease_seconds)), lease_id))
                lease = self._lease(connection, lease_id)
            return {"ticket": self._ticket(connection, ticket_id), "lease": lease,
                    "submission": self._candidate_submission_job(connection, submission_id)}

    def execute_queued_candidate_submission(self, submission_id: str) -> dict[str, Any]:
        """Worker-only Git validation and immutable candidate freeze."""
        preflight_error: str | None = None
        with self.store.connection(immediate=True) as connection:
            job = self._candidate_submission_job(connection, submission_id)
            if job["status"] != "QUEUED":
                return job
            actor = Actor(job["requested_by"], Role.DEVELOPER, frozenset({job["project_id"]}))
            ticket = self._ticket(connection, job["ticket_id"])
            try:
                if ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
                    raise ConflictError("Ticket is no longer in active development before candidate validation")
                self._expect_version(ticket, job["expected_version"])
                self._require_active_lease(connection, actor, job["ticket_id"], job["lease_id"])
            except DomainError as error:
                preflight_error = str(error)
            if preflight_error is None:
                connection.execute("UPDATE candidate_submission_jobs SET status = 'RUNNING', started_at = ? WHERE submission_id = ?",
                                   (_iso(), submission_id))
        if preflight_error is not None:
            return self.fail_queued_candidate_submission(submission_id, preflight_error)
        try:
            repository, worktree = self._candidate_submission_git_inputs(submission_id)
            commit_sha, base_commit, changed_paths = job["commit_sha"], job["base_commit"], []
            if repository is not None:
                assert worktree is not None
                commit_sha, base_commit = self.git_worktrees.verify_candidate(
                    repository_path=repository["repository_path"], worktree_path=worktree["worktree_path"],
                    expected_branch=worktree["branch_ref"], expected_base_commit=worktree["base_commit"],
                    candidate_commit=job["commit_sha"], submitted_base_commit=job["base_commit"],
                )
                changed_paths = self.git_worktrees.changed_paths(
                    repository_path=repository["repository_path"], base_commit=base_commit, candidate_commit=commit_sha
                )
            with self.store.connection() as connection:
                ticket = self._ticket(connection, job["ticket_id"])
                outside_scope = [path for path in changed_paths if not self._path_is_in_any_scope(path, ticket["file_scope"])]
            if outside_scope:
                raise ValidationError("Candidate changes paths outside ticket write scope: " + ", ".join(outside_scope))
            return self._complete_queued_candidate_submission(submission_id, commit_sha, base_commit, changed_paths)
        except DomainError as error:
            return self.fail_queued_candidate_submission(submission_id, str(error))

    def _candidate_submission_git_inputs(self, submission_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        with self.store.connection() as connection:
            job = self._candidate_submission_job(connection, submission_id)
            repository = _row(connection.execute("SELECT * FROM project_repositories WHERE project_id = ?", (job["project_id"],)).fetchone())
            worktree = _row(connection.execute("SELECT * FROM worktrees WHERE ticket_id = ?", (job["ticket_id"],)).fetchone())
            return repository, worktree

    def _complete_queued_candidate_submission(self, submission_id: str, commit_sha: str, base_commit: str,
                                              changed_paths: list[str]) -> dict[str, Any]:
        with self.store.connection(immediate=True) as connection:
            job = self._candidate_submission_job(connection, submission_id)
            if job["status"] != "RUNNING":
                return job
            actor = Actor(job["requested_by"], Role.DEVELOPER, frozenset({job["project_id"]}))
            ticket = self._ticket(connection, job["ticket_id"])
            if ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
                raise ConflictError("Ticket changed while candidate validation ran")
            self._expect_version(ticket, job["expected_version"])
            self._require_active_lease(connection, actor, job["ticket_id"], job["lease_id"])
            candidate_id = _id("cand")
            timestamp = _iso()
            connection.execute("""INSERT INTO candidates
                (candidate_id, ticket_id, base_commit, commit_sha, branch_ref, developer_claims, submitted_by, submitted_at, frozen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (candidate_id, job["ticket_id"], base_commit, commit_sha, job["branch_ref"], json.dumps(job["developer_claims"]), actor.agent_id, timestamp, timestamp))
            for path in changed_paths:
                connection.execute("INSERT INTO candidate_changed_paths (candidate_id, path) VALUES (?, ?)", (candidate_id, path))
            profile = _row(connection.execute("SELECT * FROM project_test_profiles WHERE project_id = ?", (job["project_id"],)).fetchone())
            if profile is not None:
                connection.execute("""INSERT INTO candidate_test_profiles (candidate_id, test_command, timeout_seconds, configured_at)
                    VALUES (?, ?, ?, ?)""", (candidate_id, json.dumps(profile["test_command"]), profile["timeout_seconds"], timestamp))
            next_version = ticket["version"] + 1
            connection.execute("""UPDATE tickets SET status = ?, base_commit = ?, candidate_commit = ?, version = ?, updated_at = ?
                WHERE ticket_id = ?""", (TicketStatus.QA_READY.value, base_commit, commit_sha, next_version, timestamp, job["ticket_id"]))
            connection.execute("DELETE FROM leases WHERE lease_id = ?", (job["lease_id"],))
            connection.execute("UPDATE worktrees SET last_verified_at = ? WHERE ticket_id = ?", (timestamp, job["ticket_id"]))
            connection.execute("""UPDATE candidate_submission_jobs SET status = 'COMPLETED', candidate_id = ?, error_message = NULL, completed_at = ?
                WHERE submission_id = ?""", (candidate_id, timestamp, submission_id))
            self._event(connection, actor, job["project_id"], "candidate.frozen", "ticket", job["ticket_id"], next_version,
                        {"candidate_id": candidate_id, "commit_sha": commit_sha, "submission_id": submission_id})
            qa_ready = self._ticket(connection, job["ticket_id"])
            self._queue_ticket(connection, actor, job["project_id"], qa_ready)
            return self._candidate_submission_job(connection, submission_id)

    def fail_queued_candidate_submission(self, submission_id: str, error_message: str) -> dict[str, Any]:
        self._bounded_text(error_message, "candidate_submission.error_message", 4000)
        with self.store.connection(immediate=True) as connection:
            job = self._candidate_submission_job(connection, submission_id)
            if job["status"] in {"COMPLETED", "FAILED"}:
                return job
            connection.execute("UPDATE candidate_submission_jobs SET status = 'FAILED', error_message = ?, completed_at = ? WHERE submission_id = ?",
                               (error_message, _iso(), submission_id))
            actor = Actor(job["requested_by"], Role.DEVELOPER, frozenset({job["project_id"]}))
            ticket = self._ticket(connection, job["ticket_id"])
            self._event(connection, actor, job["project_id"], "candidate.submission_failed", "ticket", job["ticket_id"], ticket["version"],
                        {"submission_id": submission_id})
            return self._candidate_submission_job(connection, submission_id)

    def _candidate_submission_job(self, connection: sqlite3.Connection, submission_id: str) -> dict[str, Any]:
        job = self._one(connection, "SELECT * FROM candidate_submission_jobs WHERE submission_id = ?", (submission_id,))
        if isinstance(job["developer_claims"], str):
            job["developer_claims"] = json.loads(job["developer_claims"])
        if job["candidate_id"] is not None:
            job["candidate"] = self._candidate_with_changed_paths(connection, self._one(
                connection, "SELECT * FROM candidates WHERE candidate_id = ?", (job["candidate_id"],)))
        else:
            job["candidate"] = None
        return job

    def submit_candidate(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        expected_version: int,
        commit_sha: str,
        base_commit: str,
        branch_ref: str,
        developer_claims: list[str],
    ) -> dict[str, Any]:
        require_role(actor, Role.DEVELOPER)
        self._valid_commit(commit_sha, "commit_sha")
        self._valid_commit(base_commit, "base_commit")
        self._bounded_text(branch_ref, "branch_ref", 300)
        self._bounded_list(developer_claims, "developer_claims", 50, 2000)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
                raise ConflictError("Ticket is not in development")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            repository = _row(
                connection.execute(
                    "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            changed_paths: list[str] = []
            if repository is not None:
                worktree = _row(
                    connection.execute(
                        "SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)
                    ).fetchone()
                )
                if worktree is None:
                    raise ValidationError(
                        "Developer must prepare the assigned Git worktree before submitting a candidate"
                    )
                if worktree["agent_id"] != actor.agent_id:
                    raise AuthorizationError("Ticket worktree belongs to another Developer")
                if branch_ref != worktree["branch_ref"]:
                    raise ValidationError("Submitted branch does not match the assigned worktree branch")
                commit_sha, base_commit = self.git_worktrees.verify_candidate(
                    repository_path=repository["repository_path"],
                    worktree_path=worktree["worktree_path"],
                    expected_branch=worktree["branch_ref"],
                    expected_base_commit=worktree["base_commit"],
                    candidate_commit=commit_sha,
                    submitted_base_commit=base_commit,
                )
                changed_paths = self.git_worktrees.changed_paths(
                    repository_path=repository["repository_path"],
                    base_commit=base_commit,
                    candidate_commit=commit_sha,
                )
                paths_outside_scope = [
                    path
                    for path in changed_paths
                    if not self._path_is_in_any_scope(path, ticket["file_scope"])
                ]
                if paths_outside_scope:
                    raise ValidationError(
                        "Candidate changes paths outside ticket write scope: "
                        + ", ".join(paths_outside_scope)
                    )
                connection.execute(
                    "UPDATE worktrees SET last_verified_at = ? WHERE ticket_id = ?",
                    (_iso(), ticket_id),
                )
            candidate_id = _id("cand")
            timestamp = _iso()
            connection.execute(
                """
                INSERT INTO candidates
                    (candidate_id, ticket_id, base_commit, commit_sha, branch_ref,
                     developer_claims, submitted_by, submitted_at, frozen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    ticket_id,
                    base_commit,
                    commit_sha,
                    branch_ref,
                    json.dumps(developer_claims),
                    actor.agent_id,
                    timestamp,
                    timestamp,
                ),
            )
            for path in changed_paths:
                connection.execute(
                    "INSERT INTO candidate_changed_paths (candidate_id, path) VALUES (?, ?)",
                    (candidate_id, path),
                )
            test_profile = _row(
                connection.execute(
                    "SELECT * FROM project_test_profiles WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            if test_profile is not None:
                connection.execute(
                    """
                    INSERT INTO candidate_test_profiles
                        (candidate_id, test_command, timeout_seconds, configured_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        json.dumps(test_profile["test_command"]),
                        test_profile["timeout_seconds"],
                        timestamp,
                    ),
                )
            next_version = ticket["version"] + 1
            connection.execute(
                """
                UPDATE tickets
                SET status = ?, base_commit = ?, candidate_commit = ?, version = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (
                    TicketStatus.QA_READY.value,
                    base_commit,
                    commit_sha,
                    next_version,
                    timestamp,
                    ticket_id,
                ),
            )
            connection.execute("DELETE FROM leases WHERE lease_id = ?", (lease_id,))
            self._event(
                connection,
                actor,
                project_id,
                "candidate.frozen",
                "ticket",
                ticket_id,
                next_version,
                {"candidate_id": candidate_id, "commit_sha": commit_sha},
            )
            qa_ready_ticket = self._ticket(connection, ticket_id)
            self._queue_ticket(connection, actor, project_id, qa_ready_ticket)
            return {
                "ticket": qa_ready_ticket,
                "candidate": self._candidate_with_changed_paths(connection, self._one(
                    connection,
                    "SELECT * FROM candidates WHERE candidate_id = ?",
                    (candidate_id,),
                )),
            }

    def submit_qa_verdict(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        candidate_id: str,
        expected_version: int,
        verdict: str,
        findings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        require_role(actor, Role.QA)
        if verdict not in {"passed", "failed"}:
            raise ValidationError("Verdict must be 'passed' or 'failed'")
        self._bounded_list(findings, "findings", 50, 1, required=True, strings_only=False)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] != TicketStatus.QA_IN_PROGRESS.value:
                raise ConflictError("Ticket is not in QA")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            candidate = self._one(
                connection,
                "SELECT * FROM candidates WHERE candidate_id = ? AND ticket_id = ?",
                (candidate_id, ticket_id),
            )
            if candidate["commit_sha"] != ticket["candidate_commit"]:
                raise ConflictError("Candidate does not match the frozen ticket artifact")
            verified_criteria: set[str] = set()
            timestamp = _iso()
            for finding_data in findings:
                evidence_items = finding_data.get("evidence") or []
                self._bounded_text(str(finding_data.get("criterion", "")), "finding.criterion", 2000)
                self._bounded_text(str(finding_data.get("observation", "")), "finding.observation", 8000)
                self._bounded_text(str(finding_data.get("severity", "unknown")), "finding.severity", 30)
                self._bounded_list(evidence_items, "finding.evidence", 30, 1, required=True, strings_only=False)
                status = str(finding_data.get("status", ""))
                if status not in {"verified", "failed", "insufficient_evidence", "flaky"}:
                    raise ValidationError("QA finding has an invalid status")
                criterion = str(finding_data.get("criterion", ""))
                if status == "verified":
                    verified_criteria.add(criterion)
                evidence_ids = []
                for item in evidence_items:
                    self._bounded_text(str(item.get("type", "")), "evidence.type", 50)
                    self._bounded_text(str(item.get("observation", "")), "evidence.observation", 8000)
                    self._bounded_text(str(item.get("storage_ref", "")), "evidence.storage_ref", 1000)
                    evidence_id = _id("ev")
                    evidence_ids.append(evidence_id)
                    connection.execute(
                        """
                        INSERT INTO evidence
                            (evidence_id, candidate_id, criterion, evidence_type,
                             observation, storage_ref, created_by, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            evidence_id,
                            candidate_id,
                            criterion,
                            item.get("type", "observation"),
                            item.get("observation", ""),
                            item.get("storage_ref", "evidence://inline"),
                            actor.agent_id,
                            timestamp,
                        ),
                    )
                finding_id = _id("finding")
                connection.execute(
                    """
                    INSERT INTO qa_findings
                        (finding_id, candidate_id, criterion, status, severity,
                         observation, evidence_refs, created_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        finding_id,
                        candidate_id,
                        criterion,
                        status,
                        finding_data.get("severity", "unknown"),
                        finding_data.get("observation", ""),
                        json.dumps(evidence_ids),
                        actor.agent_id,
                        timestamp,
                    ),
                )
            if verdict == "passed":
                missing = set(ticket["acceptance_criteria"]) - verified_criteria
                if missing:
                    raise ValidationError(
                        "Passed verdict requires verified evidence for every criterion",
                        {"missing_criteria": sorted(missing)},
                    )
                next_status = TicketStatus.VERIFIED
            else:
                next_status = TicketStatus.REWORK
                known_failure_entry_id = _id("ctx")
                connection.execute(
                    """
                    INSERT INTO context_entries
                        (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                         source_ref, artifact_commit, actor_id, actor_role, created_at)
                    VALUES (?, ?, ?, ?, 'known_failure', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        known_failure_entry_id,
                        project_id,
                        ticket["feature_id"],
                        ticket_id,
                        "QA rejected candidate; inspect linked findings and evidence",
                        f"candidate:{candidate_id}",
                        candidate["commit_sha"],
                        actor.agent_id,
                        actor.role.value,
                        timestamp,
                    ),
                )
            next_version = ticket["version"] + 1
            connection.execute(
                "UPDATE tickets SET status = ?, version = ?, updated_at = ? WHERE ticket_id = ?",
                (next_status.value, next_version, timestamp, ticket_id),
            )
            connection.execute("DELETE FROM leases WHERE lease_id = ?", (lease_id,))
            self._event(
                connection,
                actor,
                project_id,
                "ticket.verified" if verdict == "passed" else "ticket.rework_requested",
                "ticket",
                ticket_id,
                next_version,
                {"candidate_id": candidate_id, "verdict": verdict},
            )
            verdict_ticket = self._ticket(connection, ticket_id)
            if verdict in {"passed", "failed"}:
                self._queue_ticket(connection, actor, project_id, verdict_ticket)
            planner_job = None
            if verdict == "failed":
                failed_findings = [
                    row["finding_id"]
                    for row in connection.execute(
                        """
                        SELECT finding_id FROM qa_findings
                        WHERE candidate_id = ? AND status != 'verified'
                        ORDER BY created_at ASC
                        """,
                        (candidate_id,),
                    ).fetchall()
                ]
                planner_job = self._open_planner_job(
                    connection,
                    actor,
                    project_id,
                    verdict_ticket,
                    job_type=PlannerJobType.QA_FAILURE,
                    summary="QA rejected the frozen candidate; inspect the linked findings.",
                    evidence_refs=[
                        f"candidate:{candidate_id}",
                        f"context:{known_failure_entry_id}",
                        *(f"finding:{finding_id}" for finding_id in failed_findings),
                    ],
                    candidate_id=candidate_id,
                    context_entry_id=known_failure_entry_id,
                    # A contradicted criterion is urgent; missing or inconsistent
                    # evidence is a slower question about how to test it.
                    severity=(
                        InterventionSeverity.HIGH
                        if any(
                            str(finding.get("status")) == "failed" for finding in findings
                        )
                        else InterventionSeverity.NORMAL
                    ),
                    artifact_commit=candidate["commit_sha"],
                )
            return {
                "ticket": verdict_ticket,
                "candidate": candidate,
                "verdict": verdict,
                "planner_job": planner_job,
            }

    def board(self, project_id: str) -> dict[str, Any]:
        with self.store.connection() as connection:
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            rows = connection.execute(
                """
                SELECT t.*, f.title AS feature_title, f.feature_id,
                       l.agent_id AS lease_agent_id, l.role AS lease_role,
                       l.expires_at AS lease_expires_at,
                       (SELECT COUNT(*) FROM ticket_dependencies d
                        WHERE d.ticket_id = t.ticket_id) AS dependency_count,
                       (SELECT COUNT(*) FROM evidence e JOIN candidates c
                        ON c.candidate_id = e.candidate_id
                        WHERE c.ticket_id = t.ticket_id) AS evidence_count
                FROM tickets t
                JOIN features f ON f.feature_id = t.feature_id
                LEFT JOIN leases l ON l.ticket_id = t.ticket_id
                WHERE f.project_id = ?
                ORDER BY t.priority DESC, t.created_at ASC
                """,
                (project_id,),
            ).fetchall()
            tickets = [_row(row) for row in rows]
            for ticket in tickets:
                assert ticket is not None
                ticket["owner"] = self._board_ticket_owner(ticket)
            features = [
                _row(row)
                for row in connection.execute(
                    "SELECT * FROM features WHERE project_id = ? ORDER BY priority DESC",
                    (project_id,),
                ).fetchall()
            ]
            events = [
                _row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM audit_events WHERE project_id = ?
                    ORDER BY sequence DESC LIMIT 20
                    """,
                    (project_id,),
                ).fetchall()
            ]
        columns: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ticket in tickets:
            assert ticket is not None
            columns[ticket["status"]].append(ticket)
        return {
            "project": project,
            "features": features,
            "columns": dict(columns),
            "recent_events": events,
        }

    @staticmethod
    def _board_ticket_owner(ticket: dict[str, Any]) -> dict[str, Any]:
        """Describe a ticket's live exclusive lease without treating stale data as ownership."""
        agent_id = ticket["lease_agent_id"]
        role = ticket["lease_role"]
        expires_at = ticket["lease_expires_at"]
        if agent_id is None:
            status = TicketStatus(ticket["status"])
            return {
                "state": (
                    "blocked"
                    if status is TicketStatus.BLOCKED
                    else "planning"
                    if status is TicketStatus.BACKLOG
                    else "unassigned"
                ),
                "agent_id": None,
                "role": None,
                "lease_expires_at": None,
                "awaiting_role": ControlPlaneService._board_awaiting_role(ticket),
            }
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        return {
            "state": "active" if expiry > _now() else "expired",
            "agent_id": agent_id,
            "role": role,
            "lease_expires_at": expires_at,
            "awaiting_role": None,
        }

    @staticmethod
    def _board_awaiting_role(ticket: dict[str, Any]) -> str | None:
        """Return the workflow role that can move an unleased ticket forward."""
        status = TicketStatus(ticket["status"])
        if status in {TicketStatus.BACKLOG, TicketStatus.BLOCKED}:
            return Role.PLANNER.value
        if status in {TicketStatus.QA_READY, TicketStatus.QA_IN_PROGRESS}:
            return Role.QA.value
        if status in {TicketStatus.VERIFIED, TicketStatus.MERGE_READY}:
            return Role.INTEGRATOR.value
        if status in {TicketStatus.READY, TicketStatus.REWORK, TicketStatus.DEV_IN_PROGRESS}:
            return ticket["required_role"]
        return None

    def list_events(self, actor: Actor, project_id: str, after: int = 0) -> dict[str, Any]:
        require_project(actor, project_id)
        with self.store.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM audit_events
                WHERE project_id = ? AND sequence > ?
                ORDER BY sequence ASC LIMIT 100
                """,
                (project_id, after),
            ).fetchall()
        events = [_row(row) for row in rows]
        return {
            "events": events,
            "next_cursor": events[-1]["sequence"] if events else after,
        }

    def list_agent_activity(
        self, actor: Actor, project_id: str, after: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        """Read the redacted MCP invocation log for a project."""
        require_project(actor, project_id)
        limit = min(max(limit, 1), 100)
        with self.store.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_tool_calls
                WHERE project_id = ? AND sequence > ?
                ORDER BY sequence ASC LIMIT ?
                """,
                (project_id, after, limit),
            ).fetchall()
        calls = [_row(row) for row in rows]
        return {
            "calls": calls,
            "next_cursor": calls[-1]["sequence"] if calls else after,
        }

    def project_id_for_ticket(self, actor: Actor, ticket_id: str) -> str:
        with self.store.connection() as connection:
            project_id = self._project_for_ticket(connection, ticket_id)
        require_project(actor, project_id)
        return project_id

    def project_id_for_feature(self, actor: Actor, feature_id: str) -> str:
        with self.store.connection() as connection:
            project_id = self._project_for_feature(connection, feature_id)
        require_project(actor, project_id)
        return project_id

    def run_idempotent(
        self,
        actor: Actor,
        *,
        operation: str,
        idempotency_key: str | None,
        payload: dict[str, Any],
        run: Callable[[], Any],
        project_id: str | None = None,
    ) -> Any:
        """Run one consequential command so an identical retry replays its result.

        The reservation and the completion are separate short transactions because
        ``run`` opens its own write transaction; nothing here may hold one while the
        command executes. A command that fails releases its key rather than storing
        the failure, so a retry is re-executed and a failed transaction can never be
        replayed as a success.
        """
        if not idempotency_key:
            return run()
        self._bounded_text(idempotency_key, "idempotency_key", _MAX_IDEMPOTENCY_KEY_CHARS)
        self._bounded_text(operation, "operation", 120)
        scope_project = (
            project_id if project_id is not None else self._project_for_payload(actor, payload)
        )
        digest = hashlib.sha256(_serialize(payload).encode("utf-8")).hexdigest()
        scope = (actor.agent_id, scope_project, operation, idempotency_key)

        replay = self._reserve_idempotency_record(actor, scope, digest)
        if replay is not None:
            return replay
        try:
            result = run()
        except BaseException:
            self._release_idempotency_record(scope)
            raise
        self._complete_idempotency_record(scope, result)
        return result

    def _reserve_idempotency_record(
        self, actor: Actor, scope: tuple[str, str, str, str], digest: str
    ) -> Any | None:
        """Claim the key, or return the stored response when this is a true replay."""
        agent_id, project_id, operation, idempotency_key = scope
        with self.store.connection(immediate=True) as connection:
            existing = _row(
                connection.execute(
                    """
                    SELECT * FROM idempotency_records
                    WHERE actor_id = ? AND project_id = ? AND operation = ? AND idempotency_key = ?
                    """,
                    scope,
                ).fetchone()
            )
            timestamp = _now()
            if existing is not None:
                expired = (
                    datetime.fromisoformat(existing["expires_at"].replace("Z", "+00:00"))
                    <= timestamp
                )
                if not expired:
                    if existing["payload_digest"] != digest:
                        raise IdempotencyConflictError(
                            "Idempotency key was already used with a different request",
                            {"reason": "payload_mismatch", "operation": operation},
                        )
                    if existing["status"] == "IN_PROGRESS":
                        raise IdempotencyConflictError(
                            "A request with this idempotency key is still in progress",
                            {"reason": "in_progress", "operation": operation},
                        )
                    return json.loads(existing["response"])
                connection.execute(
                    "DELETE FROM idempotency_records WHERE record_id = ?",
                    (existing["record_id"],),
                )
            connection.execute(
                """
                INSERT INTO idempotency_records
                    (record_id, idempotency_key, actor_id, actor_role, project_id, operation,
                     payload_digest, status, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'IN_PROGRESS', ?, ?)
                """,
                (
                    _id("idem"),
                    idempotency_key,
                    agent_id,
                    actor.role.value,
                    project_id,
                    operation,
                    digest,
                    _iso(timestamp),
                    _iso(timestamp + timedelta(seconds=IDEMPOTENCY_RETENTION_SECONDS)),
                ),
            )
            return None

    def _complete_idempotency_record(self, scope: tuple[str, str, str, str], result: Any) -> None:
        with self.store.connection(immediate=True) as connection:
            connection.execute(
                """
                UPDATE idempotency_records
                SET status = 'COMPLETED', response = ?, completed_at = ?
                WHERE actor_id = ? AND project_id = ? AND operation = ? AND idempotency_key = ?
                """,
                (_serialize(result), _iso(), *scope),
            )

    def _release_idempotency_record(self, scope: tuple[str, str, str, str]) -> None:
        """Free a key whose command failed so the caller may retry a corrected request."""
        with self.store.connection(immediate=True) as connection:
            connection.execute(
                """
                DELETE FROM idempotency_records
                WHERE actor_id = ? AND project_id = ? AND operation = ? AND idempotency_key = ?
                  AND status = 'IN_PROGRESS'
                """,
                scope,
            )

    def _project_for_payload(self, actor: Actor, payload: dict[str, Any]) -> str:
        """Bind a record to its project when the payload identifies one; '' otherwise."""
        project_id = payload.get("project_id")
        if isinstance(project_id, str) and project_id:
            return project_id
        try:
            feature_id = payload.get("feature_id")
            if isinstance(feature_id, str) and feature_id:
                return self.project_id_for_feature(actor, feature_id)
            ticket_id = payload.get("ticket_id")
            if isinstance(ticket_id, str) and ticket_id:
                return self.project_id_for_ticket(actor, ticket_id)
        except DomainError:
            return ""
        return ""

    def purge_expired_idempotency_records(self, actor: Actor) -> dict[str, Any]:
        """Apply the retention policy; expired keys stop replaying and are removed."""
        require_role(actor, Role.ADMIN)
        with self.store.connection(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM idempotency_records WHERE expires_at <= ?", (_iso(),)
            )
            purged = cursor.rowcount
        return {"purged": max(purged, 0), "retention_seconds": IDEMPOTENCY_RETENTION_SECONDS}

    def _open_planner_job(
        self,
        connection: sqlite3.Connection,
        actor: Actor,
        project_id: str,
        ticket: dict[str, Any],
        *,
        job_type: PlannerJobType,
        summary: str,
        evidence_refs: list[str],
        candidate_id: str | None = None,
        context_entry_id: str | None = None,
        severity: InterventionSeverity = InterventionSeverity.NORMAL,
        artifact_commit: str | None = None,
    ) -> dict[str, Any]:
        """Turn one piece of failure evidence into durable, claimable Planner work.

        A ticket carries at most one open planning problem: when new evidence
        arrives while a job is still open, it updates that job instead of queueing
        a second Planner into the same decision.
        """
        timestamp = _iso()
        existing = _row(
            connection.execute(
                """
                SELECT * FROM planner_jobs
                WHERE ticket_id = ? AND status IN ('READY', 'IN_PROGRESS')
                """,
                (ticket["ticket_id"],),
            ).fetchone()
        )
        if existing is not None:
            merged = list(dict.fromkeys([*existing["evidence_refs"], *evidence_refs]))
            connection.execute(
                """
                UPDATE planner_jobs
                SET summary = ?, evidence_refs = ?, candidate_id = COALESCE(?, candidate_id),
                    context_entry_id = COALESCE(?, context_entry_id), updated_at = ?
                WHERE planner_job_id = ?
                """,
                (
                    summary,
                    json.dumps(merged),
                    candidate_id,
                    context_entry_id,
                    timestamp,
                    existing["planner_job_id"],
                ),
            )
            self._event(
                connection, actor, project_id, "planner_job.evidence_added", "planner_job",
                existing["planner_job_id"], ticket["version"],
                {"ticket_id": ticket["ticket_id"], "job_type": job_type.value},
            )
            refreshed = self._planner_job(connection, existing["planner_job_id"])
            self._record_planner_intervention(
                connection, actor, refreshed, event_class=job_type, severity=severity,
                artifact_commit=artifact_commit,
            )
            return refreshed
        planner_job_id = _id("plan")
        connection.execute(
            """
            INSERT INTO planner_jobs
                (planner_job_id, project_id, ticket_id, job_type, status, summary,
                 candidate_id, evidence_refs, context_entry_id, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'READY', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                planner_job_id,
                project_id,
                ticket["ticket_id"],
                job_type.value,
                summary,
                candidate_id,
                json.dumps(evidence_refs),
                context_entry_id,
                actor.agent_id,
                timestamp,
                timestamp,
            ),
        )
        self._event(
            connection, actor, project_id, "planner_job.created", "planner_job", planner_job_id,
            ticket["version"],
            {
                "ticket_id": ticket["ticket_id"],
                "job_type": job_type.value,
                "candidate_id": candidate_id,
            },
        )
        opened = self._planner_job(connection, planner_job_id)
        self._record_planner_intervention(
            connection, actor, opened, event_class=job_type, severity=severity,
            artifact_commit=artifact_commit,
        )
        return opened

    def _record_planner_intervention(
        self,
        connection: sqlite3.Connection,
        actor: Actor,
        planner_job: dict[str, Any],
        *,
        event_class: PlannerJobType,
        severity: InterventionSeverity,
        artifact_commit: str | None,
    ) -> dict[str, Any]:
        """Raise or refresh the single wakeup request for one open planning problem.

        The stored row is deliberately only a pointer: identifiers, class, severity
        and evidence references. Raw claims, logs, observations and lease ids stay
        in the durable ticket record where an authorized Planner reads them.
        """
        timestamp = _iso()
        evidence_refs = list(planner_job["evidence_refs"])
        existing = _row(
            connection.execute(
                """
                SELECT * FROM planner_interventions
                WHERE planner_job_id = ? AND status IN ('PENDING', 'DELIVERED')
                """,
                (planner_job["planner_job_id"],),
            ).fetchone()
        )
        if existing is not None:
            # Only materially new evidence is worth another wakeup; a repeated
            # blocker, poll or progress note must not page a Planner again.
            unchanged = (
                existing["evidence_refs"] == evidence_refs
                and existing["severity"] == severity.value
                and existing["event_class"] == event_class.value
                and existing["artifact_commit"] == artifact_commit
            )
            if unchanged:
                return self._planner_intervention(connection, existing["intervention_id"])
            connection.execute(
                """
                UPDATE planner_interventions
                SET event_class = ?, severity = ?, evidence_refs = ?, artifact_commit = ?,
                    candidate_id = COALESCE(?, candidate_id), status = 'PENDING',
                    updated_at = ?
                WHERE intervention_id = ?
                """,
                (
                    event_class.value,
                    severity.value,
                    json.dumps(evidence_refs),
                    artifact_commit,
                    planner_job["candidate_id"],
                    timestamp,
                    existing["intervention_id"],
                ),
            )
            self._event(
                connection, actor, planner_job["project_id"], "planner_intervention.updated",
                "planner_intervention", existing["intervention_id"], 1,
                {"planner_job_id": planner_job["planner_job_id"], "severity": severity.value},
            )
            return self._planner_intervention(connection, existing["intervention_id"])
        intervention_id = _id("wake")
        connection.execute(
            """
            INSERT INTO planner_interventions
                (intervention_id, planner_job_id, project_id, ticket_id, event_class, severity,
                 context_ref, artifact_commit, candidate_id, evidence_refs, status, nonce,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
            """,
            (
                intervention_id,
                planner_job["planner_job_id"],
                planner_job["project_id"],
                planner_job["ticket_id"],
                event_class.value,
                severity.value,
                f"ctx:ticket:{planner_job['ticket_id']}",
                artifact_commit,
                planner_job["candidate_id"],
                json.dumps(evidence_refs),
                uuid4().hex,
                timestamp,
                timestamp,
            ),
        )
        self._event(
            connection, actor, planner_job["project_id"], "planner_intervention.raised",
            "planner_intervention", intervention_id, 1,
            {
                "planner_job_id": planner_job["planner_job_id"],
                "ticket_id": planner_job["ticket_id"],
                "event_class": event_class.value,
                "severity": severity.value,
            },
        )
        return self._planner_intervention(connection, intervention_id)

    def _planner_intervention(
        self, connection: sqlite3.Connection, intervention_id: str
    ) -> dict[str, Any]:
        return self._one(
            connection,
            "SELECT * FROM planner_interventions WHERE intervention_id = ?",
            (intervention_id,),
        )

    @staticmethod
    def intervention_payload(intervention: dict[str, Any]) -> dict[str, Any]:
        """The only shape that may leave the control plane for a wakeup."""
        return {
            "intervention_id": intervention["intervention_id"],
            "project_id": intervention["project_id"],
            "ticket_id": intervention["ticket_id"],
            "context_ref": intervention["context_ref"],
            "planner_job_id": intervention["planner_job_id"],
            "event_class": intervention["event_class"],
            "severity": intervention["severity"],
            "artifact_commit": intervention["artifact_commit"],
            "candidate_id": intervention["candidate_id"],
            "evidence_refs": list(intervention["evidence_refs"]),
        }

    def list_planner_interventions(
        self, actor: Actor, project_id: str, *, include_acknowledged: bool = False
    ) -> list[dict[str, Any]]:
        """Read-only operational state; it grants no claim over ticket or decision."""
        require_project(actor, project_id)
        statuses = (
            ("PENDING", "DELIVERED", "ACKNOWLEDGED")
            if include_acknowledged
            else ("PENDING", "DELIVERED")
        )
        with self.store.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT intervention_id FROM planner_interventions
                WHERE project_id = ? AND status IN ({', '.join('?' for _ in statuses)})
                ORDER BY created_at ASC
                """,
                (project_id, *statuses),
            ).fetchall()
            return [
                self._planner_intervention(connection, str(row["intervention_id"]))
                for row in rows
            ]

    def pending_planner_interventions(self, limit: int = 20) -> list[dict[str, Any]]:
        """Adapter-facing queue of undelivered wakeups; trusted-local worker only."""
        with self.store.connection() as connection:
            rows = connection.execute(
                """
                SELECT intervention_id FROM planner_interventions
                WHERE status = 'PENDING' ORDER BY created_at ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                self._planner_intervention(connection, str(row["intervention_id"]))
                for row in rows
            ]

    def record_intervention_delivery(
        self, intervention_id: str, *, delivered: bool, detail: str | None = None
    ) -> dict[str, Any]:
        """Record one delivery attempt. A failure keeps the request pending forever."""
        if detail is not None:
            self._bounded_text(detail, "delivery detail", 2000)
        with self.store.connection(immediate=True) as connection:
            intervention = self._planner_intervention(connection, intervention_id)
            if intervention["status"] == InterventionStatus.ACKNOWLEDGED.value:
                return intervention
            timestamp = _iso()
            connection.execute(
                """
                UPDATE planner_interventions
                SET status = ?, delivery_attempts = delivery_attempts + 1, last_error = ?,
                    delivered_at = CASE WHEN ? THEN ? ELSE delivered_at END, updated_at = ?
                WHERE intervention_id = ?
                """,
                (
                    InterventionStatus.DELIVERED.value if delivered else InterventionStatus.PENDING.value,
                    None if delivered else detail,
                    delivered,
                    timestamp,
                    timestamp,
                    intervention_id,
                ),
            )
            return self._planner_intervention(connection, intervention_id)

    def acknowledge_planner_intervention(
        self, actor: Actor, intervention_id: str
    ) -> dict[str, Any]:
        """A Planner confirms receipt. This is not a planning decision.

        The Planner job stays open until an authorized Planner resolves it, so an
        acknowledgment can never close a decision or take a ticket.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        with self.store.connection(immediate=True) as connection:
            intervention = self._planner_intervention(connection, intervention_id)
            require_project(actor, intervention["project_id"])
            timestamp = _iso()
            connection.execute(
                """
                UPDATE planner_interventions
                SET status = 'ACKNOWLEDGED', acknowledged_at = ?, acknowledged_by = ?,
                    updated_at = ?
                WHERE intervention_id = ?
                """,
                (timestamp, actor.agent_id, timestamp, intervention_id),
            )
            self._event(
                connection, actor, intervention["project_id"], "planner_intervention.acknowledged",
                "planner_intervention", intervention_id, 1,
                {"planner_job_id": intervention["planner_job_id"]},
            )
            return self._planner_intervention(connection, intervention_id)

    def discover_planner_jobs(
        self, actor: Actor, project_id: str, *, include_resolved: bool = False
    ) -> list[dict[str, Any]]:
        """List planning work separately from the executable Developer/QA queue."""
        require_project(actor, project_id)
        with self.store.connection() as connection:
            self._expire_planner_job_leases(connection)
            statuses = (
                ("READY", "IN_PROGRESS", "RESOLVED")
                if include_resolved
                else ("READY", "IN_PROGRESS")
            )
            rows = connection.execute(
                f"""
                SELECT planner_job_id FROM planner_jobs
                WHERE project_id = ? AND status IN ({', '.join('?' for _ in statuses)})
                ORDER BY created_at ASC
                """,
                (project_id, *statuses),
            ).fetchall()
            return [
                self._planner_job(connection, str(row["planner_job_id"])) for row in rows
            ]

    def claim_planner_job(
        self, actor: Actor, planner_job_id: str, lease_seconds: int = 900
    ) -> dict[str, Any]:
        """Take the single exclusive, timed lease on one planning decision."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        if not 30 <= lease_seconds <= 3600:
            raise ValidationError("Lease duration must be between 30 and 3600 seconds")
        with self.store.connection(immediate=True) as connection:
            self._expire_planner_job_leases(connection)
            job = self._planner_job(connection, planner_job_id)
            require_project(actor, job["project_id"])
            if job["status"] == PlannerJobStatus.RESOLVED.value:
                raise ConflictError("Planner job is already resolved")
            existing = connection.execute(
                "SELECT 1 FROM planner_job_leases WHERE planner_job_id = ?", (planner_job_id,)
            ).fetchone()
            if existing is not None:
                raise ConflictError("Planner job is already leased by another Planner")
            timestamp = _now()
            lease_id = _id("planlease")
            connection.execute(
                """
                INSERT INTO planner_job_leases
                    (lease_id, planner_job_id, agent_id, acquired_at, heartbeat_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    planner_job_id,
                    actor.agent_id,
                    _iso(timestamp),
                    _iso(timestamp),
                    _iso(timestamp + timedelta(seconds=lease_seconds)),
                ),
            )
            connection.execute(
                "UPDATE planner_jobs SET status = 'IN_PROGRESS', updated_at = ? WHERE planner_job_id = ?",
                (_iso(timestamp), planner_job_id),
            )
            self._event(
                connection, actor, job["project_id"], "planner_job.claimed", "planner_job",
                planner_job_id, 1, {"lease_id": lease_id},
            )
            return {
                "planner_job": self._planner_job(connection, planner_job_id),
                "lease": self._one(
                    connection,
                    "SELECT * FROM planner_job_leases WHERE lease_id = ?",
                    (lease_id,),
                ),
            }

    def resolve_planner_job(
        self, actor: Actor, planner_job_id: str, lease_id: str, resolution: str
    ) -> dict[str, Any]:
        """Close a planning decision and release its exclusive lease."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(resolution, "resolution", 4000)
        with self.store.connection(immediate=True) as connection:
            job = self._planner_job(connection, planner_job_id)
            require_project(actor, job["project_id"])
            lease = self._one(
                connection, "SELECT * FROM planner_job_leases WHERE lease_id = ?", (lease_id,)
            )
            if lease["planner_job_id"] != planner_job_id:
                raise AuthorizationError("Lease does not cover this planner job")
            if lease["agent_id"] != actor.agent_id:
                raise AuthorizationError("Planner job lease belongs to another actor")
            if datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00")) <= _now():
                raise ConflictError("Planner job lease has expired")
            timestamp = _iso()
            connection.execute(
                """
                UPDATE planner_jobs
                SET status = 'RESOLVED', resolution = ?, resolved_by = ?, resolved_at = ?,
                    updated_at = ?
                WHERE planner_job_id = ?
                """,
                (resolution, actor.agent_id, timestamp, timestamp, planner_job_id),
            )
            connection.execute(
                "DELETE FROM planner_job_leases WHERE lease_id = ?", (lease_id,)
            )
            self._event(
                connection, actor, job["project_id"], "planner_job.resolved", "planner_job",
                planner_job_id, 1, {"ticket_id": job["ticket_id"]},
            )
            return {"planner_job": self._planner_job(connection, planner_job_id)}

    def _expire_planner_job_leases(self, connection: sqlite3.Connection) -> None:
        """Return abandoned planning decisions to the ready queue."""
        expired = connection.execute(
            "SELECT planner_job_id FROM planner_job_leases WHERE expires_at <= ?", (_iso(),)
        ).fetchall()
        for row in expired:
            connection.execute(
                "DELETE FROM planner_job_leases WHERE planner_job_id = ?", (row["planner_job_id"],)
            )
            connection.execute(
                """
                UPDATE planner_jobs SET status = 'READY', updated_at = ?
                WHERE planner_job_id = ? AND status = 'IN_PROGRESS'
                """,
                (_iso(), row["planner_job_id"]),
            )

    def _planner_job(self, connection: sqlite3.Connection, planner_job_id: str) -> dict[str, Any]:
        job = self._one(
            connection, "SELECT * FROM planner_jobs WHERE planner_job_id = ?", (planner_job_id,)
        )
        job["lease"] = _row(
            connection.execute(
                "SELECT * FROM planner_job_leases WHERE planner_job_id = ?", (planner_job_id,)
            ).fetchone()
        )
        return job

    def record_tool_call(
        self,
        actor: Actor,
        *,
        tool_name: str,
        input_summary: dict[str, Any],
        outcome: str,
        project_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """Store redacted transport-level MCP activity, separate from domain audit events."""
        if outcome not in {"succeeded", "rejected", "failed"}:
            raise ValidationError("Tool-call outcome is invalid")
        self._bounded_text(tool_name, "tool_name", 120)
        with self.store.connection(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO agent_tool_calls
                    (call_id, project_id, actor_id, actor_role, tool_name, input_summary,
                     outcome, error_code, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _id("toolcall"), project_id, actor.agent_id, actor.role.value, tool_name,
                    json.dumps(input_summary), outcome, error_code, _iso(),
                ),
            )

    def _claim(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        lease_seconds: int,
        allowed_statuses: set[TicketStatus],
        next_status: TicketStatus,
        event_type: str,
    ) -> dict[str, Any]:
        if not 30 <= lease_seconds <= 3600:
            raise ValidationError("Lease duration must be between 30 and 3600 seconds")
        with self.store.connection(immediate=True) as connection:
            self._expire_lease(connection, ticket_id)
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            existing = connection.execute(
                "SELECT * FROM leases WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if existing:
                raise ConflictError("Ticket is already leased")
            if TicketStatus(ticket["status"]) not in allowed_statuses:
                raise ConflictError(f"Ticket cannot be claimed from {ticket['status']}")
            if ticket["required_role"] != actor.role.value and actor.role not in {
                Role.QA,
                Role.INTEGRATOR,
            }:
                raise AuthorizationError("Ticket is assigned to another role")
            timestamp = _now()
            if actor.role is Role.DEVELOPER:
                unfinished_dependencies = self._unfinished_dependency_ids(connection, ticket_id)
                if unfinished_dependencies:
                    raise ConflictError(
                        "Ticket dependencies are not complete: "
                        + ", ".join(unfinished_dependencies)
                    )
                conflicting_ticket_id = self._active_write_scope_conflict(
                    connection, project_id, ticket, timestamp
                )
                if conflicting_ticket_id is not None:
                    raise ConflictError(
                        "Ticket write scope conflicts with active ticket " + conflicting_ticket_id
                    )
            lease_id = _id("lease")
            connection.execute(
                """
                INSERT INTO leases
                    (lease_id, ticket_id, agent_id, role, acquired_at, heartbeat_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    ticket_id,
                    actor.agent_id,
                    actor.role.value,
                    _iso(timestamp),
                    _iso(timestamp),
                    _iso(timestamp + timedelta(seconds=lease_seconds)),
                ),
            )
            next_version = ticket["version"] + 1
            connection.execute(
                "UPDATE tickets SET status = ?, version = ?, updated_at = ? WHERE ticket_id = ?",
                (next_status.value, next_version, _iso(timestamp), ticket_id),
            )
            self._event(
                connection,
                actor,
                project_id,
                event_type,
                "ticket",
                ticket_id,
                next_version,
                {"lease_id": lease_id},
            )
            self._mark_queue_claimed(connection, ticket_id, actor.agent_id, timestamp)
            return {
                "ticket": self._ticket(connection, ticket_id),
                "lease": self._lease(connection, lease_id),
                "context_snapshot_id": _id("ctxsnap"),
            }

    def _queue_ticket(
        self,
        connection: sqlite3.Connection,
        actor: Actor,
        project_id: str,
        ticket: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Persist a hand-off before any notifier or polling loop sees it."""
        target_role = self._dispatch_target_role(ticket)
        if target_role is None:
            return None
        existing = _row(
            connection.execute(
                """
                SELECT * FROM work_queue
                WHERE ticket_id = ? AND status IN ('pending', 'offered')
                """,
                (ticket["ticket_id"],),
            ).fetchone()
        )
        if existing is not None:
            return existing
        timestamp = _iso()
        queue_id = _id("queue")
        connection.execute(
            """
            INSERT INTO work_queue
                (queue_id, ticket_id, project_id, target_role, context_version, priority,
                 status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                queue_id,
                ticket["ticket_id"],
                project_id,
                target_role.value,
                ticket["version"],
                ticket["priority"],
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO dispatch_outbox
                (outbox_id, queue_id, project_id, target_role, event_type, payload,
                 status, created_at)
            VALUES (?, ?, ?, ?, 'work.queued', ?, 'pending', ?)
            """,
            (
                _id("outbox"),
                queue_id,
                project_id,
                target_role.value,
                json.dumps(
                    {
                        "ticket_id": ticket["ticket_id"],
                        "context_ref": f"ctx:ticket:{ticket['ticket_id']}:v{ticket['version']}",
                    }
                ),
                timestamp,
            ),
        )
        self._event(
            connection,
            actor,
            project_id,
            "work.queued",
            "ticket",
            ticket["ticket_id"],
            ticket["version"],
            {"queue_id": queue_id, "target_role": target_role.value},
        )
        return self._one(connection, "SELECT * FROM work_queue WHERE queue_id = ?", (queue_id,))

    def _reconcile_work_queue(self, connection: sqlite3.Connection, actor: Actor) -> None:
        """Backfill durable queues for existing tickets created before the dispatcher."""
        rows = connection.execute(
            """
            SELECT t.ticket_id
            FROM tickets t
            JOIN features f ON f.feature_id = t.feature_id
            LEFT JOIN leases l ON l.ticket_id = t.ticket_id
            WHERE t.status IN ('READY', 'REWORK', 'QA_READY', 'VERIFIED', 'MERGE_READY')
              AND l.lease_id IS NULL
            ORDER BY t.priority DESC, t.created_at ASC
            """
        ).fetchall()
        for row in rows:
            ticket_id = str(row["ticket_id"])
            project_id = self._project_for_ticket(connection, ticket_id)
            self._queue_ticket(connection, actor, project_id, self._ticket(connection, ticket_id))

    @staticmethod
    def _dispatch_target_role(ticket: dict[str, Any]) -> Role | None:
        status = TicketStatus(ticket["status"])
        if status is TicketStatus.QA_READY:
            return Role.QA
        if status in {TicketStatus.VERIFIED, TicketStatus.MERGE_READY}:
            return Role.INTEGRATOR
        if status in {TicketStatus.READY, TicketStatus.REWORK}:
            role = Role(ticket["required_role"])
            return role if role in {Role.DEVELOPER, Role.INTEGRATOR} else None
        return None

    def _offer_queue(
        self,
        connection: sqlite3.Connection,
        queue: dict[str, Any] | None,
        now: datetime,
        offer_seconds: int,
        *,
        fanout: int,
        only_agent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if queue is None or queue["status"] not in {"pending", "offered"}:
            return []
        existing_offer_count = connection.execute(
            "SELECT COUNT(*) FROM work_offers WHERE queue_id = ? AND status = 'offered'",
            (queue["queue_id"],),
        ).fetchone()[0]
        available_slots = 1 if only_agent_id is not None else max(0, fanout - existing_offer_count)
        if available_slots == 0:
            return []
        candidate_rows = connection.execute(
            "SELECT * FROM agent_presence WHERE role = ? AND expires_at > ? ORDER BY last_seen_at DESC, agent_id ASC",
            (queue["target_role"], _iso(now)),
        ).fetchall()
        offers: list[dict[str, Any]] = []
        for candidate_row in candidate_rows:
            presence = _row(candidate_row)
            assert presence is not None
            if only_agent_id is not None and presence["agent_id"] != only_agent_id:
                continue
            if not self._presence_can_access(presence, queue["project_id"]):
                continue
            if len(offers) >= available_slots:
                break
            active_leases = connection.execute(
                "SELECT COUNT(*) FROM leases WHERE agent_id = ? AND expires_at > ?",
                (presence["agent_id"], _iso(now)),
            ).fetchone()[0]
            if active_leases >= presence["capacity"]:
                continue
            duplicate = connection.execute(
                """
                SELECT 1 FROM work_offers
                WHERE queue_id = ? AND agent_id = ? AND status = 'offered'
                """,
                (queue["queue_id"], presence["agent_id"]),
            ).fetchone()
            if duplicate is not None:
                continue
            timestamp = _iso(now)
            offer_id = _id("offer")
            connection.execute(
                """
                INSERT INTO work_offers
                    (offer_id, queue_id, agent_id, status, offered_at, expires_at)
                VALUES (?, ?, ?, 'offered', ?, ?)
                """,
                (
                    offer_id,
                    queue["queue_id"],
                    presence["agent_id"],
                    timestamp,
                    _iso(now + timedelta(seconds=offer_seconds)),
                ),
            )
            offers.append(self._one(connection, "SELECT * FROM work_offers WHERE offer_id = ?", (offer_id,)))
        if offers:
            timestamp = _iso(now)
            connection.execute(
                "UPDATE work_queue SET status = 'offered', updated_at = ? WHERE queue_id = ?",
                (timestamp, queue["queue_id"]),
            )
            connection.execute(
                """
                UPDATE dispatch_outbox SET status = 'dispatched', dispatched_at = ?
                WHERE queue_id = ? AND status = 'pending'
                """,
                (timestamp, queue["queue_id"]),
            )
        return offers

    def _expire_offers(self, connection: sqlite3.Connection, now: datetime) -> None:
        timestamp = _iso(now)
        connection.execute(
            """
            UPDATE work_offers SET status = 'expired', closed_at = ?
            WHERE status = 'offered' AND expires_at <= ?
            """,
            (timestamp, timestamp),
        )
        connection.execute(
            """
            UPDATE work_queue SET status = 'pending', updated_at = ?
            WHERE status = 'offered'
              AND NOT EXISTS (
                  SELECT 1 FROM work_offers o
                  WHERE o.queue_id = work_queue.queue_id AND o.status = 'offered'
              )
            """,
            (timestamp,),
        )

    def _mark_queue_claimed(
        self, connection: sqlite3.Connection, ticket_id: str, agent_id: str, now: datetime
    ) -> None:
        queue_rows = connection.execute(
            """
            SELECT queue_id FROM work_queue
            WHERE ticket_id = ? AND status IN ('pending', 'offered')
            """,
            (ticket_id,),
        ).fetchall()
        if not queue_rows:
            return
        timestamp = _iso(now)
        for row in queue_rows:
            queue_id = str(row["queue_id"])
            connection.execute(
                """
                UPDATE work_queue
                SET status = 'claimed', claimed_by = ?, claimed_at = ?, updated_at = ?
                WHERE queue_id = ?
                """,
                (agent_id, timestamp, timestamp, queue_id),
            )
            connection.execute(
                """
                UPDATE work_offers
                SET status = CASE WHEN agent_id = ? THEN 'claimed' ELSE 'cancelled' END,
                    closed_at = ?
                WHERE queue_id = ? AND status = 'offered'
                """,
                (agent_id, timestamp, queue_id),
            )

    def _refresh_presence(
        self,
        connection: sqlite3.Connection,
        actor: Actor,
        presence_seconds: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        presence = _row(
            connection.execute(
                "SELECT * FROM agent_presence WHERE agent_id = ?", (actor.agent_id,)
            ).fetchone()
        )
        if presence is None:
            raise ValidationError("Register agent presence before reading the work inbox")
        if presence["role"] != actor.role.value:
            raise AuthorizationError("Agent presence role does not match authenticated identity")
        timestamp = now or _now()
        connection.execute(
            """
            UPDATE agent_presence
            SET project_ids = ?, last_seen_at = ?, expires_at = ?, updated_at = ?
            WHERE agent_id = ?
            """,
            (
                json.dumps(sorted(actor.project_ids)),
                _iso(timestamp),
                _iso(timestamp + timedelta(seconds=presence_seconds)),
                _iso(timestamp),
                actor.agent_id,
            ),
        )
        return self._presence(connection, actor.agent_id)

    def _presence(self, connection: sqlite3.Connection, agent_id: str) -> dict[str, Any]:
        return self._one(
            connection, "SELECT * FROM agent_presence WHERE agent_id = ?", (agent_id,)
        )

    def _inbox_offer(
        self, connection: sqlite3.Connection, offer: dict[str, Any] | None
    ) -> dict[str, Any]:
        assert offer is not None
        ticket = self._ticket(connection, offer["ticket_id"])
        return {
            "offer_id": offer["offer_id"],
            "queue_id": offer["queue_id"],
            "target_role": offer["target_role"],
            "expires_at": offer["expires_at"],
            "context_ref": f"ctx:ticket:{ticket['ticket_id']}:v{ticket['version']}",
            "ticket": ticket,
        }

    @staticmethod
    def _presence_can_access(presence: dict[str, Any], project_id: str) -> bool:
        return "*" in presence["project_ids"] or project_id in presence["project_ids"]

    def _authorized_ticket(
        self, connection: sqlite3.Connection, actor: Actor, ticket_id: str
    ) -> tuple[dict[str, Any], str]:
        project_id = self._project_for_ticket(connection, ticket_id)
        require_project(actor, project_id)
        return self._ticket(connection, ticket_id), project_id

    def _expire_lease(self, connection: sqlite3.Connection, ticket_id: str) -> None:
        lease = connection.execute(
            "SELECT * FROM leases WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        if lease and datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00")) <= _now():
            connection.execute("DELETE FROM leases WHERE ticket_id = ?", (ticket_id,))
            current = self._ticket(connection, ticket_id)
            fallback = (
                TicketStatus.MERGE_READY
                if lease["role"] == Role.INTEGRATOR.value
                else TicketStatus.QA_READY
                if lease["role"] == Role.QA.value
                else TicketStatus.READY
            )
            connection.execute(
                "UPDATE tickets SET status = ?, version = version + 1, updated_at = ? WHERE ticket_id = ?",
                (fallback.value, _iso(), ticket_id),
            )
            project_id = self._project_for_ticket(connection, ticket_id)
            self._queue_ticket(
                connection,
                Actor("lease-recovery", Role.ADMIN, frozenset({"*"})),
                project_id,
                self._ticket(connection, ticket_id),
            )

    def _require_active_lease(
        self, connection: sqlite3.Connection, actor: Actor, ticket_id: str, lease_id: str
    ) -> dict[str, Any]:
        lease = self._lease(connection, lease_id)
        if lease["ticket_id"] != ticket_id:
            raise AuthorizationError("Lease does not cover this ticket")
        if lease["agent_id"] != actor.agent_id or lease["role"] != actor.role.value:
            raise AuthorizationError("Lease belongs to another actor")
        expires_at = datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00"))
        if expires_at <= _now():
            raise ConflictError("Lease has expired")
        return lease

    def _lease(self, connection: sqlite3.Connection, lease_id: str) -> dict[str, Any]:
        return self._one(
            connection, "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
        )

    def _ticket(self, connection: sqlite3.Connection, ticket_id: str) -> dict[str, Any]:
        ticket = self._one(
            connection, "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
        )
        ticket["dependency_ids"] = [
            row["depends_on_ticket_id"]
            for row in connection.execute(
                "SELECT depends_on_ticket_id FROM ticket_dependencies WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchall()
        ]
        return ticket

    def _candidate_with_changed_paths(
        self, connection: sqlite3.Connection, candidate: dict[str, Any]
    ) -> dict[str, Any]:
        candidate["changed_paths"] = [
            row["path"]
            for row in connection.execute(
                "SELECT path FROM candidate_changed_paths WHERE candidate_id = ? ORDER BY path",
                (candidate["candidate_id"],),
            ).fetchall()
        ]
        return candidate

    @staticmethod
    def _unfinished_dependency_ids(connection: sqlite3.Connection, ticket_id: str) -> list[str]:
        return [
            row["ticket_id"]
            for row in connection.execute(
                """
                SELECT dependency.ticket_id
                FROM ticket_dependencies dependency_link
                JOIN tickets dependency ON dependency.ticket_id = dependency_link.depends_on_ticket_id
                WHERE dependency_link.ticket_id = ? AND dependency.status != ?
                ORDER BY dependency.ticket_id
                """,
                (ticket_id, TicketStatus.DONE.value),
            ).fetchall()
        ]

    def _active_write_scope_conflict(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        ticket: dict[str, Any],
        now: datetime,
    ) -> str | None:
        active_tickets = connection.execute(
            """
            SELECT active_ticket.ticket_id, active_ticket.file_scope
            FROM leases lease
            JOIN tickets active_ticket ON active_ticket.ticket_id = lease.ticket_id
            JOIN features feature ON feature.feature_id = active_ticket.feature_id
            WHERE feature.project_id = ?
              AND lease.role = ?
              AND lease.expires_at > ?
              AND active_ticket.ticket_id != ?
            ORDER BY active_ticket.ticket_id
            """,
            (project_id, Role.DEVELOPER.value, _iso(now), ticket["ticket_id"]),
        ).fetchall()
        for active_ticket in active_tickets:
            active_scope = json.loads(active_ticket["file_scope"])
            if self._scopes_overlap(ticket["file_scope"], active_scope):
                return str(active_ticket["ticket_id"])
        return None

    @staticmethod
    def _scopes_overlap(left_scopes: list[str], right_scopes: list[str]) -> bool:
        if not left_scopes or not right_scopes:
            return True
        return any(
            ControlPlaneService._scope_roots_overlap(left_scope, right_scope)
            for left_scope in left_scopes
            for right_scope in right_scopes
        )

    @staticmethod
    def _scope_roots_overlap(left_scope: str, right_scope: str) -> bool:
        left_root = ControlPlaneService._scope_root(left_scope)
        right_root = ControlPlaneService._scope_root(right_scope)
        if not left_root or not right_root:
            return True
        return (
            left_root == right_root
            or left_root.startswith(f"{right_root}/")
            or right_root.startswith(f"{left_root}/")
        )

    @staticmethod
    def _scope_root(scope: str) -> str:
        normalized = scope.replace("\\", "/").strip("/")
        static_parts = []
        for part in normalized.split("/"):
            if any(character in part for character in "*?["):
                break
            if part:
                static_parts.append(part)
        return "/".join(static_parts)

    @staticmethod
    def _path_is_in_any_scope(path: str, scopes: list[str]) -> bool:
        return any(ControlPlaneService._path_matches_scope(path, scope) for scope in scopes)

    @staticmethod
    def _repository_relative_scope(scope: str) -> str:
        raw_scope = scope.replace("\\", "/").strip()
        if not raw_scope or raw_scope.startswith("/") or re.match(r"^[A-Za-z]:", raw_scope):
            raise ValidationError("file_scope entries must be repository-relative paths")
        normalized = raw_scope.strip("/")
        if any(part in {".", ".."} for part in normalized.split("/")):
            raise ValidationError("file_scope entries must be repository-relative paths")
        if any(character in normalized for character in ("\x00", "\r", "\n")):
            raise ValidationError("file_scope entries contain invalid control characters")
        return normalized

    @staticmethod
    def _path_matches_scope(path: str, scope: str) -> bool:
        normalized_path = path.replace("\\", "/").lstrip("/")
        normalized_scope = scope.replace("\\", "/").strip("/")
        pattern = []
        index = 0
        while index < len(normalized_scope):
            character = normalized_scope[index]
            if (
                character == "*"
                and index + 1 < len(normalized_scope)
                and normalized_scope[index + 1] == "*"
            ):
                if index + 2 < len(normalized_scope) and normalized_scope[index + 2] == "/":
                    pattern.append("(?:.*/)?")
                    index += 3
                else:
                    pattern.append(".*")
                    index += 2
            elif character == "*":
                pattern.append("[^/]*")
                index += 1
            elif character == "?":
                pattern.append("[^/]")
                index += 1
            else:
                pattern.append(re.escape(character))
                index += 1
        return re.fullmatch("".join(pattern), normalized_path) is not None

    def _project_for_feature(self, connection: sqlite3.Connection, feature_id: str) -> str:
        row = connection.execute(
            "SELECT project_id FROM features WHERE feature_id = ?", (feature_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("Feature not found")
        return str(row["project_id"])

    def _project_for_ticket(self, connection: sqlite3.Connection, ticket_id: str) -> str:
        row = connection.execute(
            """
            SELECT f.project_id FROM tickets t
            JOIN features f ON f.feature_id = t.feature_id
            WHERE t.ticket_id = ?
            """,
            (ticket_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("Ticket not found")
        return str(row["project_id"])

    @staticmethod
    def _one(
        connection: sqlite3.Connection, query: str, parameters: tuple[Any, ...]
    ) -> dict[str, Any]:
        result = _row(connection.execute(query, parameters).fetchone())
        if result is None:
            raise NotFoundError("Resource not found")
        return result

    @staticmethod
    def _expect_version(resource: dict[str, Any], expected_version: int) -> None:
        if resource["version"] != expected_version:
            raise ConflictError(
                "Ticket version is stale",
                {"expected": expected_version, "current": resource["version"]},
            )

    @staticmethod
    def _not_blank(value: str, field: str) -> None:
        if not value or not value.strip():
            raise ValidationError(f"{field} cannot be blank")

    @classmethod
    def _bounded_text(
        cls, value: str, field: str, maximum: int, *, allow_blank: bool = False
    ) -> None:
        if not isinstance(value, str):
            raise ValidationError(f"{field} must be text")
        if not allow_blank:
            cls._not_blank(value, field)
        if len(value) > maximum:
            raise ValidationError(f"{field} must be at most {maximum} characters")

    @classmethod
    def _bounded_list(
        cls,
        values: list[Any],
        field: str,
        maximum_items: int,
        maximum_item_length: int,
        *,
        required: bool = False,
        strings_only: bool = True,
    ) -> None:
        if not isinstance(values, list):
            raise ValidationError(f"{field} must be a list")
        if required and not values:
            raise ValidationError(f"{field} cannot be empty")
        if len(values) > maximum_items:
            raise ValidationError(f"{field} supports at most {maximum_items} items")
        if strings_only:
            for index, value in enumerate(values):
                cls._bounded_text(value, f"{field}[{index}]", maximum_item_length)

    @classmethod
    def _test_command(cls, command: list[str]) -> None:
        cls._bounded_list(command, "test_command", 20, 500, required=True)
        for index, part in enumerate(command):
            if any(character in part for character in ("\x00", "\r", "\n")):
                raise ValidationError(f"test_command[{index}] contains an invalid control character")

    @staticmethod
    def _priority(value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
            raise ValidationError("priority must be an integer from 0 to 100")

    @staticmethod
    def _capacity(value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 20:
            raise ValidationError("capacity must be an integer from 1 to 20")

    @staticmethod
    def _presence_seconds(value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or not 30 <= value <= 3600:
            raise ValidationError("presence_seconds must be an integer from 30 to 3600")

    @staticmethod
    def _offer_seconds(value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or not 30 <= value <= 3600:
            raise ValidationError("offer_seconds must be an integer from 30 to 3600")

    @staticmethod
    def _valid_commit(value: str, field: str) -> None:
        if not COMMIT_RE.fullmatch(value):
            raise ValidationError(f"{field} must be a 40-64 character hexadecimal digest")

    @staticmethod
    def _strip_ref(reference: str, kind: str) -> str:
        prefix = f"{kind}:"
        context_prefix = f"ctx:{kind}:"
        if reference.startswith(context_prefix):
            return reference[len(context_prefix) :].rsplit(":v", 1)[0]
        if reference.startswith(prefix):
            return reference[len(prefix) :]
        return reference

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        actor: Actor,
        project_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events
                (event_id, event_type, aggregate_type, aggregate_id, aggregate_version,
                 project_id, actor_id, actor_role, payload, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _id("event"),
                event_type,
                aggregate_type,
                aggregate_id,
                aggregate_version,
                project_id,
                actor.agent_id,
                actor.role.value,
                json.dumps(payload),
                _iso(),
            ),
        )
