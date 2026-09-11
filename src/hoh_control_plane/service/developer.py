from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    ConflictError,
    InterventionSeverity,
    PlannerJobType,
    Role,
    TicketStatus,
    _id,
    _iso,
    require_role,
)


class DeveloperMixin:
    """Developer progress checkpoints and blocker reports."""

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
