"""Workflow measurement derived from the audit trail, not counted alongside it.

Every metric here is computed from append-only events and durable rows that the
workflow already writes. Nothing increments a counter on the side, which is what
makes a retry safe to measure: an idempotent replay writes no new event, so it
cannot move a number that is only ever read from events.

Nothing here may change workflow state. A failure to measure is allowed to lose
a measurement and nothing else.
"""
from __future__ import annotations

import contextvars
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import uuid4

from hoh_control_plane.domain import Actor, Role, require_project


# The request a write belongs to. A contextvar rather than a threaded argument
# because an event is written deep inside a service call that has no business
# knowing about transports, and because each worker process and each request
# needs its own value without passing one through every signature.
_CORRELATION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "hoh_correlation_id", default=None
)

# Bounded so a label can never carry free-form agent content into a metric.
_MAX_LABEL_CHARS = 120


def new_correlation_id() -> str:
    return f"corr_{uuid4().hex[:16]}"


def current_correlation_id() -> str | None:
    return _CORRELATION.get()


@contextmanager
def correlation_scope(correlation_id: str | None = None) -> Iterator[str]:
    """Mark every event written inside this block as one request's work."""
    value = correlation_id or new_correlation_id()
    token = _CORRELATION.set(value)
    try:
        yield value
    finally:
        _CORRELATION.reset(token)


def _parsed(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _elapsed_seconds(start: Any, end: Any) -> float | None:
    first, last = _parsed(start), _parsed(end)
    if first is None or last is None:
        return None
    return round((last - first).total_seconds(), 3)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


class WorkflowMetrics:
    """Reads the workflow's own record and reports where work waits or is lost."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def project_metrics(self, actor: Actor, project_id: str) -> dict[str, Any]:
        """Bounded operational measures for one project.

        Every value is an aggregate. No ticket title, objective, evidence
        observation, branch name, worktree path or lease identifier appears
        here, because a metric surface is read by tooling that does not have
        the project's confidentiality expectations.
        """
        require_project(actor, project_id)
        with self.store.connection() as connection:
            return {
                "project_id": project_id,
                "throughput": self._throughput(connection, project_id),
                "waiting": self._waiting(connection, project_id),
                "rework": self._rework(connection, project_id),
                "evidence": self._evidence(connection, project_id),
                "leases": self._leases(connection, project_id),
                "context": self._context(connection, project_id),
                "mainline": self._mainline(connection, project_id),
            }

    def _throughput(self, connection: sqlite3.Connection, project_id: str) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT t.ticket_id, t.status, t.created_at, t.updated_at
            FROM tickets t JOIN features f ON f.feature_id = t.feature_id
            WHERE f.project_id = ?
            """,
            (project_id,),
        ).fetchall()
        lead_times = []
        for row in rows:
            if row["status"] == "DONE":
                elapsed = _elapsed_seconds(row["created_at"], row["updated_at"])
                if elapsed is not None:
                    lead_times.append(elapsed)
        by_status: dict[str, int] = {}
        for row in rows:
            by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        return {
            "tickets": len(rows),
            "by_status": by_status,
            "completed": len(lead_times),
            "mean_lead_time_seconds": _mean(lead_times),
        }

    def _waiting(self, connection: sqlite3.Connection, project_id: str) -> dict[str, Any]:
        """How long work sits in each role's queue before anyone claims it."""
        rows = connection.execute(
            """
            SELECT q.target_role, q.status, q.created_at, q.updated_at
            FROM work_queue q WHERE q.project_id = ?
            """,
            (project_id,),
        ).fetchall()
        waits: dict[str, list[float]] = {}
        pending: dict[str, int] = {}
        for row in rows:
            role = row["target_role"]
            if row["status"] == "claimed":
                elapsed = _elapsed_seconds(row["created_at"], row["updated_at"])
                if elapsed is not None:
                    waits.setdefault(role, []).append(elapsed)
            elif row["status"] in {"pending", "offered"}:
                pending[role] = pending.get(role, 0) + 1
        return {
            "mean_wait_seconds": {role: _mean(values) for role, values in waits.items()},
            "pending": pending,
        }

    def _rework(self, connection: sqlite3.Connection, project_id: str) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT e.event_type, COUNT(*) AS total FROM audit_events e
            WHERE e.project_id = ? GROUP BY e.event_type
            """,
            (project_id,),
        ).fetchall()
        counts = {row["event_type"]: row["total"] for row in rows}
        verdicts = counts.get("qa.verdict_passed", 0) + counts.get("qa.verdict_failed", 0)
        return {
            "qa_failures": counts.get("qa.verdict_failed", 0),
            "qa_passes": counts.get("qa.verdict_passed", 0),
            "candidate_pass_rate": (
                round(counts.get("qa.verdict_passed", 0) / verdicts, 3) if verdicts else None
            ),
            "base_drift": counts.get("integration.base_drift", 0),
            "blocked": counts.get("ticket.blocked", 0),
        }

    def _evidence(self, connection: sqlite3.Connection, project_id: str) -> dict[str, Any]:
        """Whether verdicts are actually carrying evidence, not just verdicts."""
        row = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM qa_findings fi
                   JOIN candidates c ON c.candidate_id = fi.candidate_id
                   JOIN tickets t ON t.ticket_id = c.ticket_id
                   JOIN features f ON f.feature_id = t.feature_id
                  WHERE f.project_id = ?) AS findings,
                (SELECT COUNT(*) FROM evidence ev
                   JOIN candidates c ON c.candidate_id = ev.candidate_id
                   JOIN tickets t ON t.ticket_id = c.ticket_id
                   JOIN features f ON f.feature_id = t.feature_id
                  WHERE f.project_id = ?) AS evidence_rows,
                (SELECT COUNT(*) FROM evidence ev
                   JOIN candidates c ON c.candidate_id = ev.candidate_id
                   JOIN tickets t ON t.ticket_id = c.ticket_id
                   JOIN features f ON f.feature_id = t.feature_id
                  WHERE f.project_id = ? AND ev.checksum_source = 'artifact')
                  AS proved_rows
            """,
            (project_id, project_id, project_id),
        ).fetchone()
        findings = row["findings"] or 0
        items = row["evidence_rows"] or 0
        proved = row["proved_rows"] or 0
        return {
            "findings": findings,
            "evidence_items": items,
            "evidence_per_finding": (round(items / findings, 3) if findings else None),
            # Section 25 asks that no verdict hang off an unidentified artifact.
            # A digest read from bytes proves one; a digest covering the retained
            # record is the agent's claim about a file kept somewhere else. Both
            # are permitted, so the only way an operator sees storage that no
            # longer reaches the machine writing evidence is this ratio falling.
            "artifact_backed_items": proved,
            "claimed_items": items - proved,
            "artifact_backed_ratio": (round(proved / items, 3) if items else None),
        }

    def _leases(self, connection: sqlite3.Connection, project_id: str) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT event_type, COUNT(*) AS total FROM audit_events
            WHERE project_id = ? AND event_type LIKE 'lease.%' GROUP BY event_type
            """,
            (project_id,),
        ).fetchall()
        counts = {row["event_type"]: row["total"] for row in rows}
        active = connection.execute(
            """
            SELECT COUNT(*) FROM leases l
            JOIN tickets t ON t.ticket_id = l.ticket_id
            JOIN features f ON f.feature_id = t.feature_id
            WHERE f.project_id = ?
            """,
            (project_id,),
        ).fetchone()[0]
        return {"active": active, "expired": counts.get("lease.expired", 0)}

    def _context(self, connection: sqlite3.Connection, project_id: str) -> dict[str, Any]:
        """How much context the workflow is compiling, and how often it goes stale."""
        snapshots = connection.execute(
            "SELECT COUNT(*) FROM context_snapshots WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        entries = connection.execute(
            "SELECT COUNT(*) FROM context_entries WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        stale = connection.execute(
            """
            SELECT COUNT(*) FROM agent_tool_calls
            WHERE project_id = ? AND error_code = 'context_stale'
            """,
            (project_id,),
        ).fetchone()[0]
        return {
            "snapshots": snapshots,
            "context_entries": entries,
            "stale_refusals": stale,
        }

    def _mainline(self, connection: sqlite3.Connection, project_id: str) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT merge_status, COUNT(*) AS total FROM merge_jobs
            WHERE project_id = ? AND merge_status IS NOT NULL GROUP BY merge_status
            """,
            (project_id,),
        ).fetchall()
        counts = {row["merge_status"]: row["total"] for row in rows}
        merged = counts.get("merged", 0)
        attempted = merged + counts.get("base_drift", 0)
        return {
            "merged": merged,
            "base_drift": counts.get("base_drift", 0),
            "merge_success_rate": round(merged / attempted, 3) if attempted else None,
        }

    def trace(self, actor: Actor, project_id: str, correlation_id: str) -> dict[str, Any]:
        """The bounded event chain one request produced.

        A trace names what moved and when. It deliberately carries no payload:
        that field is free-form text written by whichever call emitted the event,
        which is exactly what must not reach an operational surface.
        """
        require_project(actor, project_id)
        if len(correlation_id) > _MAX_LABEL_CHARS:
            correlation_id = correlation_id[:_MAX_LABEL_CHARS]
        with self.store.connection() as connection:
            rows = connection.execute(
                """
                SELECT sequence, event_id, event_type, aggregate_type, aggregate_id,
                       aggregate_version, actor_role, correlation_id, causation_id,
                       occurred_at
                FROM audit_events
                WHERE project_id = ? AND correlation_id = ?
                ORDER BY sequence ASC LIMIT 200
                """,
                (project_id, correlation_id),
            ).fetchall()
        events = [dict(row) for row in rows]
        return {
            "correlation_id": correlation_id,
            "events": events,
            "elapsed_seconds": (
                _elapsed_seconds(events[0]["occurred_at"], events[-1]["occurred_at"])
                if len(events) > 1
                else None
            ),
        }
