"""Requirement registration, linking and coverage."""
from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    CLOSED_TICKET_STATUSES,
    ConflictError,
    Role,
    TicketStatus,
    ValidationError,
    _iso,
    _row,
    require_project,
    require_role,
)


class RequirementsMixin:
    """Requirement registration, linking and coverage."""

    def register_requirement(
        self,
        actor: Actor,
        project_id: str,
        requirement_id: str,
        statement: str,
        source_ref: str,
    ) -> dict[str, Any]:
        """Record something the specification asks for, so coverage can be computed.

        This records what was asked. It does not record what must be true for a
        ticket to be done: that stays on the ticket, because QA verifies a
        ticket's own criteria and a second place to author them would drift.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._bounded_text(requirement_id, "requirement_id", 100)
        self._bounded_text(statement, "statement", 2000)
        self._bounded_text(source_ref, "source_ref", 1000)
        with self.store.connection(immediate=True) as connection:
            self._one(connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,))
            existing = _row(
                connection.execute(
                    "SELECT * FROM requirements WHERE requirement_id = ?", (requirement_id,)
                ).fetchone()
            )
            if existing is not None:
                if existing["project_id"] != project_id:
                    raise ConflictError("That requirement id belongs to another project")
                return {**existing, "registered": False}
            timestamp = _iso()
            connection.execute(
                """
                INSERT INTO requirements
                    (requirement_id, project_id, statement, source_ref, registered_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (requirement_id, project_id, statement, source_ref, actor.agent_id, timestamp),
            )
            self._event(
                connection, actor, project_id, "requirement.registered", "project",
                project_id, 0, {"requirement_id": requirement_id, "source_ref": source_ref},
            )
            return {
                **self._one(
                    connection,
                    "SELECT * FROM requirements WHERE requirement_id = ?",
                    (requirement_id,),
                ),
                "registered": True,
            }

    def link_requirement(
        self,
        actor: Actor,
        requirement_id: str,
        *,
        ticket_id: str | None = None,
        feature_id: str | None = None,
    ) -> dict[str, Any]:
        """Point a piece of work at the requirement it serves."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        if bool(ticket_id) == bool(feature_id):
            raise ValidationError("Provide exactly one of ticket_id or feature_id")
        with self.store.connection(immediate=True) as connection:
            requirement = _row(
                connection.execute(
                    "SELECT * FROM requirements WHERE requirement_id = ?", (requirement_id,)
                ).fetchone()
            )
            if requirement is None:
                # Refused rather than created implicitly: a typo would otherwise
                # invent a requirement nobody asked for and report it as covered.
                raise ValidationError(
                    f"No such requirement: {requirement_id}",
                    {"reason": "requirement_unknown"},
                )
            if ticket_id:
                ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
                subject_type, subject_id = "ticket", ticket["ticket_id"]
            else:
                assert feature_id is not None
                project_id = self._project_for_feature(connection, feature_id)
                require_project(actor, project_id)
                subject_type, subject_id = "feature", feature_id
            if project_id != requirement["project_id"]:
                raise ConflictError("Work and requirement belong to different projects")
            connection.execute(
                """
                INSERT INTO requirement_links
                    (requirement_id, subject_type, subject_id, linked_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(requirement_id, subject_type, subject_id) DO NOTHING
                """,
                (requirement_id, subject_type, subject_id, actor.agent_id, _iso()),
            )
            return {
                "requirement_id": requirement_id,
                "subject_type": subject_type,
                "subject_id": subject_id,
            }

    def requirement_coverage(self, actor: Actor, project_id: str) -> dict[str, Any]:
        """What nothing is pointing at, what is in flight, and what is finished.

        Read instead of re-reading the specification: this answers "what have I
        missed" from the board's own state, which is cheaper than recalling the
        source and, unlike memory, cannot quietly disagree with it.
        """
        require_project(actor, project_id)
        with self.store.connection() as connection:
            requirements = [
                _row(row)
                for row in connection.execute(
                    "SELECT * FROM requirements WHERE project_id = ? ORDER BY requirement_id",
                    (project_id,),
                ).fetchall()
            ]
            uncovered, in_flight, satisfied = [], [], []
            for requirement in requirements:
                tickets = [
                    _row(row)
                    for row in connection.execute(
                        """
                        SELECT t.ticket_id, t.status FROM requirement_links l
                        JOIN tickets t ON t.ticket_id = l.subject_id
                        WHERE l.requirement_id = ? AND l.subject_type = 'ticket'
                        ORDER BY t.ticket_id
                        """,
                        (requirement["requirement_id"],),
                    ).fetchall()
                ]
                # Retired work does not cover anything, so a cancelled ticket
                # returns its requirement to uncovered rather than hiding it.
                live = [
                    row
                    for row in tickets
                    if TicketStatus(row["status"]) not in CLOSED_TICKET_STATUSES
                ]
                entry = {
                    "requirement_id": requirement["requirement_id"],
                    "statement": requirement["statement"],
                    "source_ref": requirement["source_ref"],
                    "tickets": [row["ticket_id"] for row in live],
                }
                if not live:
                    uncovered.append(entry)
                elif all(row["status"] == TicketStatus.DONE.value for row in live):
                    satisfied.append(entry)
                else:
                    in_flight.append(entry)
        return {
            "project_id": project_id,
            "uncovered": uncovered,
            "in_flight": in_flight,
            "satisfied": satisfied,
            "counts": {
                "total": len(requirements),
                "uncovered": len(uncovered),
                "in_flight": len(in_flight),
                "satisfied": len(satisfied),
            },
        }
