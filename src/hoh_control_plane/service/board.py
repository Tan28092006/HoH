from __future__ import annotations

from hoh_control_plane import contracts
from hoh_control_plane.service._common import (
    BARE_REFERENCE_KINDS,
    Actor,
    Any,
    Role,
    ValidationError,
    TicketStatus,
    _iso,
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
                        "events": 0,
                        "last_event": row["event_type"],
                        "at": row["occurred_at"],
                    }
                    # A ref is offered only where context.resolve can follow it.
                    if row["aggregate_type"] in BARE_REFERENCE_KINDS:
                        entry["ref"] = f"{row['aggregate_type']}:{row['aggregate_id']}"
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

    # Phase name -> (kind, start marker, end marker). Markers are resolved per
    # ticket by _cycle_markers from audit events the workflow already writes.
    # A wait phase is a ticket sitting in a queue for the next role; a service
    # phase is that role holding the lease and working.
    _CYCLE_PHASES = (
        ("ready_to_dev_claim", "wait", "ready", "dev_claim"),
        ("dev_claim_to_submit", "service", "dev_claim", "submit"),
        ("submit_to_qa_claim", "wait", "submit", "qa_claim"),
        ("qa_claim_to_verdict", "service", "qa_claim", "verdict"),
        ("verdict_to_integrator_claim", "wait", "verdict", "integrator_claim"),
        ("integrator_claim_to_merged", "service", "integrator_claim", "delivered"),
    )
    _DELIVERY_EVENTS = ("integration.completed", "integration.landed_ungated")
    _MARKER_EVENTS = (
        "ticket.ready", "ticket.claimed", "candidate.frozen", "qa.started",
        "ticket.verified", "integration.started", *_DELIVERY_EVENTS,
    )

    def cycle_metrics(self, actor: Actor, project_id: str, limit: int = 20) -> dict[str, Any]:
        """How long the loop takes and where the time goes, as p50/p90.

        Read-only: computed from audit_events, the job tables and
        repository_reconciliations, so measuring the loop adds nothing to it.

        Per delivered ticket (the most recent ``limit``), each phase is split
        into waiting for a role and that role's service time. Markers are the
        first READY, the first Developer claim, the last candidate freeze, the
        last QA claim and passing verdict of that candidate, the last Integrator
        claim after it, and the delivery; a rework round therefore shows up in
        Developer service time. Seconds; None where a phase did not occur (an
        ungated landing has no QA). ``suite_runs`` counts project-suite
        executions on the host: QA runs and gates that ran rather than reused.

        Project-level figures read the most recent reconciliations and jobs:
        how long barriers stayed open, how long QA and merge jobs queued for a
        worker, and the merge shadow partition's agreement with Planner.
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
            marker_types = ", ".join("?" for _ in self._MARKER_EVENTS)
            for row in delivered:
                ticket_id = row["ticket_id"]
                events = connection.execute(
                    f"""
                    SELECT event_type, actor_role, payload, occurred_at FROM audit_events
                    WHERE project_id = ? AND aggregate_id = ?
                      AND event_type IN ({marker_types})
                    ORDER BY sequence ASC
                    """,
                    (project_id, ticket_id, *self._MARKER_EVENTS),
                ).fetchall()
                markers = self._cycle_markers([dict(event) for event in events])
                suite_total = int(connection.execute(
                    "SELECT COUNT(*) FROM qa_test_runs WHERE ticket_id = ?", (ticket_id,)
                ).fetchone()[0])
                suite_qa = int(connection.execute(
                    """
                    SELECT COUNT(*) FROM qa_test_runs r
                    JOIN qa_test_jobs j ON j.test_run_id = r.test_run_id
                    WHERE r.ticket_id = ?
                    """,
                    (ticket_id,),
                ).fetchone()[0])
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
                    for name, _kind, start, end in self._CYCLE_PHASES
                }
                tickets.append(
                    {
                        "ticket_id": ticket_id,
                        "delivered_at": markers.get("delivered") or row["delivered_at"],
                        "gated": markers.get("verdict") is not None,
                        "phases_seconds": phases,
                        "wait_seconds": self._phase_total(phases, "wait"),
                        "service_seconds": self._phase_total(phases, "service"),
                        "ready_to_merged_seconds": self._seconds_between(
                            markers.get("ready"), markers.get("delivered")
                        ),
                        "suite_runs": {
                            "qa": suite_qa, "gate": suite_total - suite_qa, "total": suite_total,
                        },
                        "gate_runs": {"executed": len(gate_modes) - reused, "reused": reused},
                        "usage": self._ticket_usage(connection, ticket_id),
                    }
                )
            shadow = self._barrier_shadow_summary(connection, project_id)
            barriers = self._barrier_open_summary(connection, project_id)
            queues = self._job_queue_summary(connection, project_id)
        summary = {
            name: {"kind": kind, **self._distribution(
                [ticket["phases_seconds"][name] for ticket in tickets]
            )}
            for name, kind, _start, _end in self._CYCLE_PHASES
        }
        for total in ("wait_seconds", "service_seconds", "ready_to_merged_seconds"):
            summary[total] = self._distribution([ticket[total] for ticket in tickets])
        summary["suite_runs_per_landed_ticket"] = {
            **self._distribution([ticket["suite_runs"]["total"] for ticket in tickets]),
            "total": sum(ticket["suite_runs"]["total"] for ticket in tickets),
        }
        return {
            "project_id": project_id,
            "delivered_tickets": len(tickets),
            "summary": summary,
            "worker_usage": self._worker_usage_summary(tickets),
            "barrier_open_seconds": barriers,
            "job_queue_seconds": queues,
            "barrier_shadow": shadow,
            "tickets": tickets,
        }

    @staticmethod
    def _ticket_usage(connection: Any, ticket_id: str) -> dict[str, Any]:
        """What the roles reported about how they worked one ticket (work.report_usage)."""
        rows = connection.execute(
            "SELECT worker, tokens_in, tokens_out FROM ticket_usage_reports WHERE ticket_id = ?",
            (ticket_id,),
        ).fetchall()
        if not rows:
            return {"reports": 0, "fresh_workers": None, "tokens_in": None, "tokens_out": None}

        def total(field: str) -> int | None:
            values = [row[field] for row in rows if row[field] is not None]
            return sum(values) if values else None

        return {
            "reports": len(rows),
            # Fresh only if every reported round kept its transcript to itself.
            "fresh_workers": all(row["worker"] in contracts.FRESH_WORKER_KINDS for row in rows),
            "by_worker": {
                kind: sum(1 for row in rows if row["worker"] == kind)
                for kind in sorted({row["worker"] for row in rows})
            },
            "tokens_in": total("tokens_in"),
            "tokens_out": total("tokens_out"),
        }

    def _worker_usage_summary(self, tickets: list[dict[str, Any]]) -> dict[str, Any]:
        """Dispatcher discipline over the delivered tickets: who did the work, at what cost."""
        reported = [ticket["usage"] for ticket in tickets if ticket["usage"]["reports"]]
        fresh = sum(1 for usage in reported if usage["fresh_workers"])

        def tokens(usage: dict[str, Any]) -> int | None:
            parts = [usage[field] for field in ("tokens_in", "tokens_out") if usage[field] is not None]
            return sum(parts) if parts else None

        return {
            "reported_tickets": len(reported),
            "unreported_tickets": len(tickets) - len(reported),
            "fresh_worker_share": round(fresh / len(reported), 3) if reported else None,
            "tokens_per_ticket": self._distribution([tokens(usage) for usage in reported]),
            "tokens_in_per_ticket": self._distribution([usage["tokens_in"] for usage in reported]),
            "tokens_out_per_ticket": self._distribution([usage["tokens_out"] for usage in reported]),
        }

    def _phase_total(self, phases: dict[str, float | None], kind: str) -> float | None:
        values = [
            phases[name] for name, phase_kind, _start, _end in self._CYCLE_PHASES
            if phase_kind == kind and phases[name] is not None
        ]
        return round(sum(values), 3) if values else None

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

    def _barrier_open_summary(self, connection: Any, project_id: str) -> dict[str, Any]:
        """How long reconciliations kept work waiting for Planner: created to resolved.

        A reconciliation the server resolved in the same transaction (a
        scope-disjoint merge) never blocked anything, so it is counted apart
        rather than pulling the percentiles to zero. ``by_scope`` separates the
        project-wide barrier from ticket-scoped freezes.
        """
        rows = connection.execute(
            """
            SELECT scope, status, resolved_by, created_at, resolved_at
            FROM repository_reconciliations
            WHERE project_id = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (project_id, self._RECONCILIATION_WINDOW),
        ).fetchall()
        reviewed = [
            row for row in rows
            if row["status"] == "RESOLVED" and row["resolved_by"] != "system:scope-disjoint"
        ]
        durations = {
            "project": [], "tickets": [],
        }
        for row in reviewed:
            durations.setdefault(row["scope"] or "project", []).append(
                self._seconds_between(row["created_at"], row["resolved_at"])
            )
        pending = [row for row in rows if row["status"] == "PENDING"]
        now = _iso()
        return {
            **self._distribution([value for values in durations.values() for value in values]),
            "by_scope": {scope: self._distribution(values) for scope, values in durations.items()},
            "auto_resolved": sum(
                1 for row in rows if row["resolved_by"] == "system:scope-disjoint"
            ),
            "pending": len(pending),
            "oldest_pending_seconds": max(
                (self._seconds_between(row["created_at"], now) for row in pending), default=None
            ),
        }

    _QUEUE_WINDOW = 500

    def _job_queue_summary(self, connection: Any, project_id: str) -> dict[str, Any]:
        """How long QA and merge jobs waited for a worker: queued to started.

        This is the test-slot wait the supervisor imposes. Merges are also split
        by how their gate ran, since a merge that reuses a QA run no longer
        waits for the slot (wave 6E).
        """
        summary: dict[str, Any] = {}
        for kind, table in (("qa", "qa_test_jobs"), ("merge", "merge_jobs")):
            rows = connection.execute(
                f"""
                SELECT created_at, started_at FROM {table}
                WHERE project_id = ? AND started_at IS NOT NULL
                ORDER BY created_at DESC LIMIT ?
                """,
                (project_id, self._QUEUE_WINDOW),
            ).fetchall()
            queued = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE project_id = ? AND status = 'QUEUED'",
                (project_id,),
            ).fetchone()[0]
            summary[kind] = {
                **self._distribution([
                    self._seconds_between(row["created_at"], row["started_at"]) for row in rows
                ]),
                "queued_now": int(queued),
            }
        by_mode: dict[str, list[float | None]] = {}
        for row in connection.execute(
            """
            SELECT m.created_at, m.started_at, e.payload FROM merge_jobs m
            JOIN audit_events e
              ON e.aggregate_id = m.merge_job_id AND e.event_type = 'integration.gate_passed'
            WHERE m.project_id = ? AND m.started_at IS NOT NULL
            ORDER BY m.created_at DESC LIMIT ?
            """,
            (project_id, self._QUEUE_WINDOW),
        ).fetchall():
            mode = str(self._payload_field(row["payload"], "gate_mode") or "unknown")
            by_mode.setdefault(mode, []).append(
                self._seconds_between(row["created_at"], row["started_at"])
            )
        summary["merge"]["by_gate_mode"] = {
            mode: self._distribution(values) for mode, values in sorted(by_mode.items())
        }
        return summary

    @staticmethod
    def _cycle_markers(events: list[dict[str, Any]]) -> dict[str, str]:
        """Phase boundaries from a ticket's events, read in order.

        A new candidate restarts everything after the freeze, and a new QA
        claim everything after it, so the downstream markers always belong to
        the candidate that was finally delivered.
        """
        markers: dict[str, str] = {}
        for event in events:
            kind, at = event["event_type"], event["occurred_at"]
            if kind == "ticket.ready":
                markers.setdefault("ready", at)
            elif kind == "ticket.claimed" and event["actor_role"] == Role.DEVELOPER.value:
                markers.setdefault("dev_claim", at)
            elif kind == "candidate.frozen":
                markers["submit"] = at
                for later in ("qa_claim", "verdict", "integrator_claim"):
                    markers.pop(later, None)
            elif kind == "qa.started":
                markers["qa_claim"] = at
                for later in ("verdict", "integrator_claim"):
                    markers.pop(later, None)
            elif kind == "ticket.verified":
                markers["verdict"] = at
                markers.pop("integrator_claim", None)
            elif kind == "integration.started":
                markers["integrator_claim"] = at
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
            return {"count": 0, "p50": None, "p90": None}

        def percentile(fraction: float) -> float:
            # Linear interpolation between closest ranks.
            position = (len(present) - 1) * fraction
            lower = int(position)
            upper = min(lower + 1, len(present) - 1)
            return round(present[lower] + (present[upper] - present[lower]) * (position - lower), 3)

        return {"count": len(present), "p50": percentile(0.5), "p90": percentile(0.9)}
