from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    Role,
    ValidationError,
    TicketStatus,
    _now,
    _row,
    datetime,
    defaultdict,
    json,
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

    # A change signal, not the change itself. payload is free-form text written
    # by whichever call emitted the event, so it is the one field that could
    # carry a reason string, a scope listing or a blocked explanation into a
    # channel nobody audits. The stream says what moved; the REST snapshot
    # remains the only source for what it now says.
    _EVENT_SIGNAL_FIELDS = (
        "sequence",
        "event_id",
        "event_type",
        "aggregate_type",
        "aggregate_id",
        "aggregate_version",
        "actor_role",
        "occurred_at",
    )

    def list_event_signals(
        self, actor: Actor, project_id: str, after: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        """Bounded change signals after a cursor, for a live view to react to.

        Reports `reset` when the cursor cannot belong to this project's history —
        a client holding a cursor from a different or rebuilt database would
        otherwise wait forever for events that will never arrive.
        """
        require_project(actor, project_id)
        limit = min(max(limit, 1), 100)
        if after < 0:
            raise ValidationError("Event cursor cannot be negative")
        with self.store.connection() as connection:
            highest = connection.execute(
                "SELECT MAX(sequence) AS top FROM audit_events WHERE project_id = ?",
                (project_id,),
            ).fetchone()["top"]
            rows = connection.execute(
                """
                SELECT * FROM audit_events
                WHERE project_id = ? AND sequence > ?
                ORDER BY sequence ASC LIMIT ?
                """,
                (project_id, after, limit),
            ).fetchall()
        reset = highest is not None and after > highest
        signals = [
            {field: _row(row)[field] for field in self._EVENT_SIGNAL_FIELDS} for row in rows
        ]
        return {
            "events": signals,
            "next_cursor": signals[-1]["sequence"] if signals else (0 if reset else after),
            "reset": reset,
        }

    def list_event_digest(
        self, actor: Actor, project_id: str, after: int = 0, limit: int = 200
    ) -> dict[str, Any]:
        """What changed since a cursor, collapsed by the thing it changed.

        An agent waking up asks "what moved", not "replay everything". Twenty
        events across three tickets read as three entries, each saying where that
        thing ended up, so the answer stays small however busy the project was.
        Nothing here carries payload text: this surface exists to be cheap and
        polled often, and the typed references are how a caller gets detail.
        """
        require_project(actor, project_id)
        limit = min(max(limit, 1), 500)
        if after < 0:
            raise ValidationError("Event cursor cannot be negative")
        with self.store.connection() as connection:
            highest = connection.execute(
                "SELECT MAX(sequence) AS top FROM audit_events WHERE project_id = ?",
                (project_id,),
            ).fetchone()["top"]
            if highest is not None and after > highest:
                # A cursor this history cannot contain will never advance, so say
                # so rather than reporting "nothing new" forever.
                return {
                    "project_id": project_id,
                    "cursor": after,
                    "next_cursor": 0,
                    "changed": [],
                    "event_count": 0,
                    "has_more": False,
                    "reset": True,
                }
            rows = connection.execute(
                """
                SELECT sequence, event_type, aggregate_type, aggregate_id, occurred_at
                FROM audit_events
                WHERE project_id = ? AND sequence > ?
                ORDER BY sequence ASC LIMIT ?
                """,
                (project_id, after, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            rows = [_row(row) for row in rows[:limit]]

            collapsed: dict[tuple[str, str], dict[str, Any]] = {}
            for row in rows:
                key = (row["aggregate_type"], row["aggregate_id"])
                entry = collapsed.get(key)
                if entry is None:
                    collapsed[key] = entry = {
                        "type": row["aggregate_type"],
                        "id": row["aggregate_id"],
                        "ref": f"{row['aggregate_type']}:{row['aggregate_id']}",
                        "events": 0,
                        "last_event": row["event_type"],
                        "at": row["occurred_at"],
                    }
                entry["events"] += 1
                # The newest wins: where it ended up is what a caller acts on.
                entry["last_event"] = row["event_type"]
                entry["at"] = row["occurred_at"]

            # Where each ticket actually landed, so a caller need not resolve one
            # context packet per entry just to learn the resulting state.
            ticket_ids = [key[1] for key in collapsed if key[0] == "ticket"]
            if ticket_ids:
                placeholders = ",".join("?" for _ in ticket_ids)
                for row in connection.execute(
                    f"SELECT ticket_id, status FROM tickets WHERE ticket_id IN ({placeholders})",
                    ticket_ids,
                ).fetchall():
                    collapsed[("ticket", row["ticket_id"])]["status"] = row["status"]

        return {
            "project_id": project_id,
            "cursor": after,
            "next_cursor": rows[-1]["sequence"] if rows else after,
            "changed": list(collapsed.values()),
            "event_count": len(rows),
            "has_more": has_more,
            "reset": False,
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

    # Phase name -> (start marker, end marker). Markers are resolved per ticket
    # by _cycle_markers from audit events that the workflow already writes.
    _CYCLE_PHASES = (
        ("ready_to_claim", "ready", "claimed"),
        ("claim_to_submit", "claimed", "submitted"),
        ("submit_to_verdict", "submitted", "verdict"),
        ("verdict_to_merge", "verdict", "delivered"),
        ("ready_to_merge", "ready", "delivered"),
    )
    _DELIVERY_EVENTS = ("integration.completed", "integration.landed_ungated")

    def cycle_metrics(self, actor: Actor, project_id: str, limit: int = 20) -> dict[str, Any]:
        """How long the loop takes, per delivered ticket and as median/p90.

        Read-only: computed from audit_events, qa_test_runs and merge_jobs, so
        measuring the loop adds nothing to it. Phases use the first READY, the
        first Developer claim, the last candidate freeze, the last passing QA
        verdict and the delivery event; a rework round therefore shows up as a
        longer claim-to-submit phase. Seconds; None where a phase did not occur
        (an ungated landing has no verdict).
        """
        require_project(actor, project_id)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValidationError("limit must be between 1 and 200")
        with self.store.connection() as connection:
            delivered = connection.execute(
                """
                SELECT aggregate_id AS ticket_id, MAX(occurred_at) AS delivered_at
                FROM audit_events
                WHERE project_id = ? AND aggregate_type = 'ticket'
                  AND event_type IN (?, ?)
                GROUP BY aggregate_id
                ORDER BY delivered_at DESC
                LIMIT ?
                """,
                (project_id, *self._DELIVERY_EVENTS, limit),
            ).fetchall()
            tickets: list[dict[str, Any]] = []
            for row in delivered:
                ticket_id = row["ticket_id"]
                events = connection.execute(
                    """
                    SELECT event_type, actor_role, payload, occurred_at FROM audit_events
                    WHERE project_id = ? AND aggregate_id = ?
                      AND event_type IN ('ticket.ready', 'ticket.claimed', 'candidate.frozen',
                                         'ticket.verified', 'integration.completed',
                                         'integration.landed_ungated')
                    ORDER BY sequence ASC
                    """,
                    (project_id, ticket_id),
                ).fetchall()
                markers = self._cycle_markers([dict(event) for event in events])
                qa_runs = connection.execute(
                    "SELECT COUNT(*) FROM qa_test_runs WHERE ticket_id = ?", (ticket_id,)
                ).fetchone()[0]
                gate_events = connection.execute(
                    """
                    SELECT audit_events.payload FROM audit_events
                    JOIN merge_jobs ON merge_jobs.merge_job_id = audit_events.aggregate_id
                    WHERE audit_events.event_type = 'integration.gate_passed'
                      AND merge_jobs.ticket_id = ?
                    """,
                    (ticket_id,),
                ).fetchall()
                gate_modes = [self._payload_field(event["payload"], "gate_mode") for event in gate_events]
                reused = sum(1 for mode in gate_modes if mode and "reused" in str(mode))
                phases = {
                    name: self._seconds_between(markers.get(start), markers.get(end))
                    for name, start, end in self._CYCLE_PHASES
                }
                tickets.append(
                    {
                        "ticket_id": ticket_id,
                        "delivered_at": markers.get("delivered") or row["delivered_at"],
                        "gated": markers.get("verdict") is not None,
                        "phases_seconds": phases,
                        "qa_test_runs": int(qa_runs),
                        "gate_runs": {"executed": len(gate_modes) - reused, "reused": reused},
                    }
                )
            shadow = self._barrier_shadow_summary(connection, project_id)
        summary = {
            name: self._distribution(
                [ticket["phases_seconds"][name] for ticket in tickets]
            )
            for name, _start, _end in self._CYCLE_PHASES
        }
        return {
            "project_id": project_id,
            "delivered_tickets": len(tickets),
            "summary": summary,
            "barrier_shadow": shadow,
            "tickets": tickets,
        }

    # The most recent reconciliations a project-level figure reads, so the
    # metric's cost stays bounded however long a project lives.
    _RECONCILIATION_WINDOW = 500

    def _barrier_shadow_summary(self, connection: Any, project_id: str) -> dict[str, Any]:
        """How often a scope-partitioned merge barrier would have agreed with Planner.

        Shadow mode only (wave 6D): each merge Planner had to review recorded the
        tickets the scope rule would have frozen. A disagreement is a ticket
        Planner revised, cancelled, superseded, completed or reworked outside
        that set. ``partitioned`` counts the reviews where the rule would have
        kept some tickets flowing rather than falling back to the whole project.
        """
        rows = connection.execute(
            """
            SELECT reconciliation_id, status, shadow_partition, shadow_agreed
            FROM repository_reconciliations
            WHERE project_id = ? AND shadow_partition IS NOT NULL
            ORDER BY created_at DESC LIMIT ?
            """,
            (project_id, self._RECONCILIATION_WINDOW),
        ).fetchall()
        resolved = [row for row in rows if row["status"] == "RESOLVED"]
        agreed = sum(1 for row in resolved if row["shadow_agreed"] == 1)
        disagreements = []
        partitioned = 0
        flowing = 0
        for row in resolved:
            partition = row["shadow_partition"]
            if isinstance(partition, str):
                partition = json.loads(partition)
            if not partition.get("project_wide"):
                partitioned += 1
            flowing += len(partition.get("flowing_ticket_ids") or [])
            if row["shadow_agreed"] == 0:
                disagreements.append({
                    "reconciliation_id": row["reconciliation_id"],
                    "missed_ticket_ids": (partition.get("outcome") or {}).get("missed_ticket_ids", []),
                })
        return {
            "rule": self.SHADOW_RULE,
            "resolved": len(resolved),
            "pending": len(rows) - len(resolved),
            "agreed": agreed,
            "disagreed": len(resolved) - agreed,
            "agreement_rate": round(agreed / len(resolved), 3) if resolved else None,
            "partitioned": partitioned,
            "tickets_that_would_have_flowed": flowing,
            "disagreements": disagreements[:10],
        }

    @staticmethod
    def _cycle_markers(events: list[dict[str, Any]]) -> dict[str, str]:
        markers: dict[str, str] = {}
        for event in events:
            kind, at = event["event_type"], event["occurred_at"]
            if kind == "ticket.ready":
                markers.setdefault("ready", at)
            elif kind == "ticket.claimed" and event["actor_role"] == Role.DEVELOPER.value:
                markers.setdefault("claimed", at)
            elif kind == "candidate.frozen":
                markers["submitted"] = at
            elif kind == "ticket.verified":
                markers["verdict"] = at
            elif kind in BoardMixin._DELIVERY_EVENTS:
                markers["delivered"] = at
        return markers

    @staticmethod
    def _payload_field(payload: Any, field: str) -> Any:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                return None
        return payload.get(field) if isinstance(payload, dict) else None

    @staticmethod
    def _seconds_between(start: str | None, end: str | None) -> float | None:
        if not start or not end:
            return None
        begin = datetime.fromisoformat(start.replace("Z", "+00:00"))
        finish = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return max(0.0, round((finish - begin).total_seconds(), 3))

    @staticmethod
    def _distribution(values: list[float | None]) -> dict[str, Any]:
        present = sorted(value for value in values if value is not None)
        if not present:
            return {"count": 0, "median": None, "p90": None}

        def percentile(fraction: float) -> float:
            # Linear interpolation between closest ranks.
            position = (len(present) - 1) * fraction
            lower = int(position)
            upper = min(lower + 1, len(present) - 1)
            return round(present[lower] + (present[upper] - present[lower]) * (position - lower), 3)

        return {"count": len(present), "median": percentile(0.5), "p90": percentile(0.9)}
