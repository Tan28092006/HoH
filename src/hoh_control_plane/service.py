from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from hoh_control_plane.domain import (
    Actor,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    Role,
    TicketStatus,
    ValidationError,
    require_project,
    require_role,
)
from hoh_control_plane.git_worktrees import GitWorktreeManager
from hoh_control_plane.qa_runner import ProjectTestRunner
from hoh_control_plane.store import SQLiteStore


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


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat().replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


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

        return {
            "agent": {
                "agent_id": actor.agent_id,
                "role": actor.role.value,
                "capabilities": self.capabilities(actor),
            },
            "project": project,
            "features": list(grouped.values()),
            "next_cursor": None,
        }

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
        return {
            "agent": presence,
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
        return {"agents": agents, "queues": queues}

    def get_feature_context(self, actor: Actor, feature_ref: str) -> dict[str, Any]:
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
        return {
            "context_snapshot_id": _id("ctxsnap"),
            "context_ref": f"ctx:feature:{feature_id}:v{feature['version']}",
            "feature": feature,
            "tickets": tickets,
            "verified_behaviors": verified,
            "open_ticket_count": sum(
                ticket["status"] not in {TicketStatus.DONE.value, TicketStatus.CANCELLED.value}
                for ticket in tickets
            ),
        }

    def get_ticket_context(self, actor: Actor, ticket_ref: str) -> dict[str, Any]:
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
        return {
            "context_snapshot_id": _id("ctxsnap"),
            "context_ref": f"ctx:ticket:{ticket_id}:v{ticket['version']}",
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
            "omitted_sections": [],
        }

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
        """Create one ticket-and-agent-bound Git worktree for active Developer work."""
        require_role(actor, Role.DEVELOPER)
        with self.store.connection(immediate=True) as connection:
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
            existing = _row(
                connection.execute(
                    "SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)
                ).fetchone()
            )
            if existing is not None:
                if existing["agent_id"] != actor.agent_id:
                    raise ConflictError("Ticket already has a worktree assigned to another Developer")
                return {"ticket": ticket, "worktree": existing, "reused": True}

            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            prepared = self.git_worktrees.prepare(
                repository_path=repository["repository_path"],
                default_branch=project["default_branch"],
                project_id=project_id,
                ticket_id=ticket_id,
                agent_id=actor.agent_id,
            )
            timestamp = _iso()
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
            self._event(
                connection,
                actor,
                project_id,
                "worktree.prepared",
                "ticket",
                ticket_id,
                ticket["version"],
                {"branch_ref": prepared.branch_ref},
            )
            worktree = self._one(
                connection, "SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)
            )
            return {"ticket": ticket, "worktree": worktree, "reused": False}

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
                    TicketStatus.QA_READY
                    if ticket["status"] == TicketStatus.QA_IN_PROGRESS.value
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
            return {"ticket": self._ticket(connection, ticket_id), "context_entry_id": entry_id}

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

    def run_project_tests(
        self, actor: Actor, ticket_id: str, lease_id: str
    ) -> dict[str, Any]:
        """Run the candidate's Planner-frozen test command from its clean ticket worktree."""
        require_role(actor, Role.QA)
        with self.store.connection() as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            if ticket["status"] != TicketStatus.QA_IN_PROGRESS.value:
                raise ConflictError("Ticket is not in QA")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            candidate = self._one(
                connection,
                "SELECT * FROM candidates WHERE ticket_id = ? ORDER BY submitted_at DESC LIMIT 1",
                (ticket_id,),
            )
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
            candidate_id = candidate["candidate_id"]
            candidate_commit = candidate["commit_sha"]
            worktree_path = self.git_worktrees.verify_frozen_candidate(
                worktree["worktree_path"], candidate_commit
            )

        execution = self.project_tests.execute(command, worktree_path, timeout_seconds)
        with self.store.connection(immediate=True) as connection:
            ticket, current_project_id = self._authorized_ticket(connection, actor, ticket_id)
            if current_project_id != project_id or ticket["status"] != TicketStatus.QA_IN_PROGRESS.value:
                raise ConflictError("Ticket changed while project tests were running")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            current_candidate = self._one(
                connection,
                "SELECT * FROM candidates WHERE candidate_id = ? AND ticket_id = ?",
                (candidate_id, ticket_id),
            )
            if current_candidate["commit_sha"] != candidate_commit:
                raise ConflictError("Candidate changed while project tests were running")
            test_run_id = _id("testrun")
            storage_ref, report_path = self.project_tests.write_report(
                candidate_id=candidate_id,
                test_run_id=test_run_id,
                execution=execution,
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
                    candidate_id,
                    ticket_id,
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
                    project_id,
                    ticket["feature_id"],
                    ticket_id,
                    f"Planner-registered project test command {outcome} in {execution.duration_ms}ms.",
                    storage_ref,
                    candidate_commit,
                    actor.agent_id,
                    actor.role.value,
                    timestamp,
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                "qa.automated_test_completed",
                "ticket",
                ticket_id,
                ticket["version"],
                {
                    "test_run_id": test_run_id,
                    "exit_code": execution.exit_code,
                    "timed_out": execution.timed_out,
                },
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "test_run": self._one(
                    connection, "SELECT * FROM qa_test_runs WHERE test_run_id = ?", (test_run_id,)
                ),
            }

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
                "candidate": self._one(
                    connection,
                    "SELECT * FROM candidates WHERE candidate_id = ?",
                    (candidate_id,),
                ),
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
                connection.execute(
                    """
                    INSERT INTO context_entries
                        (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                         source_ref, artifact_commit, actor_id, actor_role, created_at)
                    VALUES (?, ?, ?, ?, 'known_failure', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _id("ctx"),
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
            if verdict == "failed":
                self._queue_ticket(connection, actor, project_id, verdict_ticket)
            return {
                "ticket": verdict_ticket,
                "candidate": candidate,
                "verdict": verdict,
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
            if ticket["required_role"] != actor.role.value and actor.role is not Role.QA:
                raise AuthorizationError("Ticket is assigned to another role")
            timestamp = _now()
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
            WHERE t.status IN ('READY', 'REWORK', 'QA_READY') AND l.lease_id IS NULL
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
                TicketStatus.QA_READY
                if current["status"] == TicketStatus.QA_IN_PROGRESS.value
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
