"""How execution roles worked their tickets, and when a dispatcher should restart."""
from __future__ import annotations

from hoh_control_plane import contracts
from hoh_control_plane.service._common import (
    Actor,
    Any,
    AuthorizationError,
    Role,
    ValidationError,
    _id,
    _iso,
    require_role,
)

#: Tickets one agent session may claim before work.wait_for_event advises it to
#: start a new session. A dispatcher that delegates every ticket keeps a small
#: context, but each delegation still leaves a trace in it; one that does not
#: delegate carries every transcript into the next ticket. Advisory only: the
#: server never refuses a claim because of it.
DISPATCHER_TICKET_LIMIT = 5

# The claim events that start a role's work on a ticket.
_CLAIM_EVENTS = ("ticket.claimed", "qa.started", "integration.started")


class UsageMixin:
    """Self-reported worker usage and the dispatcher session soft cap."""

    def report_usage(
        self,
        actor: Actor,
        ticket_id: str,
        worker: str,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> dict[str, Any]:
        """Record how this role worked one ticket it claimed.

        ``worker`` says whether a fresh subagent, a new session or the waiting
        dispatcher chat itself did the work; token counts are optional because
        not every client can read them. Only an agent that claimed the ticket
        may report on it. A report is evidence for metrics, never authority.
        """
        require_role(actor, Role.DEVELOPER, Role.QA, Role.INTEGRATOR)
        if worker not in contracts.WORKER_KINDS:
            raise ValidationError(
                "worker must be fresh_subagent, dispatcher or new_session",
                {"allowed": sorted(contracts.WORKER_KINDS)},
            )
        for field, value in (("tokens_in", tokens_in), ("tokens_out", tokens_out)):
            if value is None:
                continue
            if (
                not isinstance(value, int) or isinstance(value, bool)
                or not 0 <= value <= contracts.MAX_REPORTED_TOKENS
            ):
                raise ValidationError(
                    f"{field} must be an integer from 0 to {contracts.MAX_REPORTED_TOKENS}"
                )
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            placeholders = ", ".join("?" for _ in _CLAIM_EVENTS)
            claimed = connection.execute(
                f"""
                SELECT 1 FROM audit_events
                WHERE aggregate_id = ? AND actor_id = ? AND event_type IN ({placeholders})
                LIMIT 1
                """,
                (ticket_id, actor.agent_id, *_CLAIM_EVENTS),
            ).fetchone()
            if claimed is None:
                raise AuthorizationError(
                    "Only an agent that claimed this ticket can report how it was worked",
                    {"reason": "usage_report_without_claim"},
                )
            report_id = _id(connection, "usage")
            timestamp = _iso()
            connection.execute(
                """
                INSERT INTO ticket_usage_reports
                    (report_id, project_id, ticket_id, agent_id, role, worker,
                     tokens_in, tokens_out, reported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (report_id, project_id, ticket_id, actor.agent_id, actor.role.value,
                 worker, tokens_in, tokens_out, timestamp),
            )
            self._event(
                connection, actor, project_id, "ticket.usage_reported", "ticket", ticket_id,
                ticket["version"],
                {"report_id": report_id, "worker": worker,
                 "tokens_in": tokens_in, "tokens_out": tokens_out},
            )
            return {
                "report_id": report_id,
                "ticket_id": ticket_id,
                "worker": worker,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "reported_at": timestamp,
                **self._dispatcher_load(connection, actor),
            }

    def dispatcher_advice(self, actor: Actor) -> dict[str, Any] | None:
        """Advice for a waiting execution chat that has handled too many tickets.

        Read-only. None until this agent's current session (since its last
        ``agent.register``) has claimed ``DISPATCHER_TICKET_LIMIT`` tickets.
        """
        if actor.role not in {Role.DEVELOPER, Role.QA, Role.INTEGRATOR}:
            return None
        with self.store.connection() as connection:
            load = self._dispatcher_load(connection, actor)
        if load["session_tickets"] is None or load["session_tickets"] < DISPATCHER_TICKET_LIMIT:
            return None
        return {
            "advice": "start_new_session",
            "session_tickets": load["session_tickets"],
            "ticket_limit": DISPATCHER_TICKET_LIMIT,
            "session_started_at": load["session_started_at"],
            "advice_reason": (
                f"This session has claimed {load['session_tickets']} tickets. Finish any "
                "assignment you hold, then end this chat and start a new one, so earlier "
                "tickets' context does not reach the next."
            ),
        }

    @staticmethod
    def _dispatcher_load(connection: Any, actor: Actor) -> dict[str, Any]:
        """Distinct tickets this agent claimed since its session began."""
        presence = connection.execute(
            "SELECT session_started_at FROM agent_presence WHERE agent_id = ?",
            (actor.agent_id,),
        ).fetchone()
        started = None if presence is None else presence["session_started_at"]
        if started is None:
            return {"session_started_at": None, "session_tickets": None}
        placeholders = ", ".join("?" for _ in _CLAIM_EVENTS)
        count = connection.execute(
            f"""
            SELECT COUNT(DISTINCT aggregate_id) FROM audit_events
            WHERE actor_id = ? AND aggregate_type = 'ticket'
              AND event_type IN ({placeholders}) AND occurred_at >= ?
            """,
            (actor.agent_id, *_CLAIM_EVENTS, started),
        ).fetchone()[0]
        return {"session_started_at": started, "session_tickets": int(count)}
