from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    Role,
    TicketStatus,
    _now,
    _row,
    datetime,
    defaultdict,
    require_project,
)


class BoardMixin:
    """Read-only board, event and activity projections."""

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
                "awaiting_role": BoardMixin._board_awaiting_role(ticket),
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
