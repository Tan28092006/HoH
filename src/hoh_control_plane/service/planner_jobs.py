from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    AuthorizationError,
    ConflictError,
    InterventionSeverity,
    InterventionStatus,
    PlannerJobStatus,
    PlannerJobType,
    Role,
    ValidationError,
    _id,
    _iso,
    _now,
    _row,
    datetime,
    json,
    require_project,
    require_role,
    sqlite3,
    timedelta,
    uuid4,
)


class PlannerJobMixin:
    """Planner job queue and intervention outbox."""

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
