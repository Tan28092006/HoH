from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    AuthorizationError,
    ConflictError,
    CLOSED_TICKET_STATUSES,
    hashlib,
    InterventionSeverity,
    InterventionStatus,
    PlannerJobStatus,
    PlannerJobType,
    Role,
    TicketStatus,
    ValidationError,
    _id,
    _iso,
    _now,
    _row,
    datetime,
    json,
    require_project,
    require_role,
    Connection,
    timedelta,
    uuid4,
)


class PlannerJobMixin:
    """Planner job queue and intervention outbox."""

    def _open_planner_job(
        self,
        connection: Connection,
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
                SET job_type = ?, summary = ?, evidence_refs = ?,
                    candidate_id = CASE WHEN job_type != ? THEN ?
                                        ELSE COALESCE(?, candidate_id) END,
                    context_entry_id = CASE WHEN job_type != ? THEN ?
                                            ELSE COALESCE(?, context_entry_id) END,
                    updated_at = ?
                WHERE planner_job_id = ?
                """,
                (
                    job_type.value,
                    summary,
                    json.dumps(merged),
                    job_type.value,
                    candidate_id,
                    candidate_id,
                    job_type.value,
                    context_entry_id,
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
        planner_job_id = _id(connection, "plan")
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

    def report_execution_failure(
        self,
        actor: Actor,
        project_id: str,
        ticket_id: str,
        failure_class: str,
        summary: str,
        evidence_refs: list[str] | None = None,
        job_kind: str | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Escalate one durable execution failure before an agent becomes idle.

        Expected conflicts, stale context, empty inboxes and first transient
        errors do not belong here. A report needs either the caller's live lease
        or a terminal worker job owned by the caller. Identical reports reuse one
        context pointer and one open Planner job.
        """
        require_role(actor, Role.DEVELOPER, Role.QA, Role.INTEGRATOR)
        require_project(actor, project_id)
        allowed_classes = {
            "source_or_contract",
            "infrastructure",
            "dependency",
            "integration_conflict",
            "security_or_data",
            "unknown_durable",
        }
        if failure_class not in allowed_classes:
            raise ValidationError(
                "failure_class must identify a durable source/contract, infrastructure, "
                "dependency, integration, security/data or unknown failure"
            )
        self._bounded_text(summary, "summary", 4000)
        evidence_refs = evidence_refs or []
        self._bounded_list(evidence_refs, "evidence_refs", 50, 1000)
        if (job_kind is None) != (job_id is None):
            raise ValidationError("job_kind and job_id must be supplied together")
        with self.store.connection(immediate=True) as connection:
            ticket, actual_project_id = self._authorized_ticket(connection, actor, ticket_id)
            if actual_project_id != project_id:
                raise ValidationError("Ticket does not belong to project_id")
            if connection.execute(
                "SELECT 1 FROM ticket_retirement_requests "
                "WHERE ticket_id = ? AND status = 'PENDING'",
                (ticket_id,),
            ).fetchone() is not None:
                raise ConflictError("Planner retirement is already handling this ticket")
            source_ref: str
            if job_kind is not None and job_id is not None:
                jobs = {
                    "worktree_provision": ("worktree_provision_jobs", "provision_id", {"FAILED"}),
                    "candidate_submission": ("candidate_submission_jobs", "submission_id", {"FAILED"}),
                    "qa_test": ("qa_test_jobs", "test_run_id", {"FAILED", "STALE"}),
                    "merge": ("merge_jobs", "merge_job_id", {"FAILED"}),
                }
                selected = jobs.get(job_kind)
                if selected is None:
                    raise ValidationError(
                        "job_kind must be worktree_provision, candidate_submission, qa_test or merge"
                    )
                table, key, terminal = selected
                job = _row(connection.execute(
                    f"SELECT * FROM {table} WHERE {key} = ?", (job_id,)
                ).fetchone())
                if (
                    job is None
                    or job["ticket_id"] != ticket_id
                    or job["project_id"] != project_id
                    or job["requested_by"] != actor.agent_id
                ):
                    raise AuthorizationError("Failure job does not belong to this agent and ticket")
                if job["status"] not in terminal:
                    raise ConflictError("Only a terminal failed worker job can be reported")
                if "planner retir" in str(job.get("error_message") or "").lower():
                    raise ConflictError("Planner-requested retirement is not an execution failure")
                source_ref = f"{job_kind}_job:{job_id}"
            else:
                lease = _row(connection.execute(
                    """
                    SELECT * FROM leases
                    WHERE ticket_id = ? AND agent_id = ? AND role = ? AND expires_at > ?
                    """,
                    (ticket_id, actor.agent_id, actor.role.value, _iso()),
                ).fetchone())
                if lease is None:
                    raise ConflictError(
                        "A failure without a terminal job requires this agent's live ticket lease"
                    )
                active_worker = any(
                    connection.execute(
                        f"SELECT 1 FROM {table} WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING')",
                        (ticket_id,),
                    ).fetchone() is not None
                    for table in (
                        "worktree_provision_jobs",
                        "candidate_submission_jobs",
                        "qa_test_jobs",
                        "merge_jobs",
                    )
                )
                if active_worker:
                    raise ConflictError(
                        "Poll the active worker to a terminal result before reporting its failure"
                    )
                source_ref = f"lease:{lease['lease_id']}"
            existing_job = _row(connection.execute(
                """
                SELECT * FROM planner_jobs
                WHERE ticket_id = ? AND status IN ('READY', 'IN_PROGRESS')
                """,
                (ticket_id,),
            ).fetchone())
            source_aliases = {source_ref}
            if job_kind == "qa_test" and job_id is not None:
                source_aliases.add(f"test_run:{job_id}")
            if existing_job is not None and source_aliases.intersection(
                set(existing_job.get("evidence_refs") or [])
            ):
                context = (
                    _row(connection.execute(
                        "SELECT * FROM context_entries WHERE entry_id = ?",
                        (existing_job["context_entry_id"],),
                    ).fetchone())
                    if existing_job.get("context_entry_id") else None
                )
                return {
                    "ticket": ticket,
                    "failure_context": context,
                    "planner_job": self._planner_job(
                        connection, existing_job["planner_job_id"]
                    ),
                    "deduplicated": True,
                    "next_action": "wait_for_planner_direction",
                }
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "project_id": project_id,
                        "ticket_id": ticket_id,
                        "role": actor.role.value,
                        "failure_class": failure_class,
                        "summary": " ".join(summary.split()),
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()[:16]
            failure_ref = f"failure:{fingerprint}"
            context = _row(connection.execute(
                """
                SELECT * FROM context_entries
                WHERE ticket_id = ? AND entry_type = 'risk' AND source_ref = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (ticket_id, failure_ref),
            ).fetchone())
            deduplicated = context is not None
            timestamp = _iso()
            if context is None:
                if TicketStatus(ticket["status"]) in CLOSED_TICKET_STATUSES or (
                    ticket["status"] == TicketStatus.DONE.value
                ):
                    raise ConflictError("A terminal ticket cannot receive an execution failure")
                entry_id = _id(connection, "ctx")
                connection.execute(
                    """
                    INSERT INTO context_entries
                        (entry_id, project_id, feature_id, ticket_id, entry_type,
                         content, source_ref, artifact_commit, actor_id, actor_role, created_at)
                    VALUES (?, ?, ?, ?, 'risk', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry_id, project_id, ticket["feature_id"], ticket_id,
                        f"{failure_class}: {summary}", failure_ref,
                        ticket.get("candidate_commit") or ticket.get("base_commit"),
                        actor.agent_id, actor.role.value, timestamp,
                    ),
                )
                context = self._one(
                    connection, "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
                )
                if ticket["status"] not in {
                    TicketStatus.BLOCKED.value,
                    TicketStatus.REWORK.value,
                }:
                    self._close_ticket_dispatch(connection, ticket_id, timestamp)
                    connection.execute("DELETE FROM worktree_ownership WHERE ticket_id = ?", (ticket_id,))
                    connection.execute("DELETE FROM leases WHERE ticket_id = ?", (ticket_id,))
                    next_version = int(ticket["version"]) + 1
                    connection.execute(
                        """
                        UPDATE tickets SET status = 'BLOCKED', blocked_reason = ?,
                            version = ?, updated_at = ? WHERE ticket_id = ?
                        """,
                        (summary, next_version, timestamp, ticket_id),
                    )
                    ticket = self._ticket(connection, ticket_id)
                    self._event(
                        connection, actor, project_id, "ticket.execution_failure_reported",
                        "ticket", ticket_id, next_version,
                        {"context_ref": f"ctx:entry:{entry_id}", "failure_class": failure_class},
                    )
            job_type = {
                Role.DEVELOPER: PlannerJobType.DEVELOPER_BLOCKER,
                Role.QA: PlannerJobType.QA_FAILURE,
                Role.INTEGRATOR: PlannerJobType.INTEGRATION_CONFLICT,
            }[actor.role]
            planner_job = self._open_planner_job(
                connection,
                actor,
                project_id,
                self._ticket(connection, ticket_id),
                job_type=job_type,
                summary=summary,
                evidence_refs=list(dict.fromkeys([
                    f"context:{context['entry_id']}", source_ref, *evidence_refs
                ])),
                context_entry_id=context["entry_id"],
                severity=(
                    InterventionSeverity.HIGH
                    if failure_class in {
                        "source_or_contract", "integration_conflict", "security_or_data"
                    }
                    else InterventionSeverity.NORMAL
                ),
                artifact_commit=ticket.get("candidate_commit") or ticket.get("base_commit"),
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "failure_context": context,
                "planner_job": planner_job,
                "deduplicated": deduplicated,
                "next_action": "wait_for_planner_direction",
            }

    def resume_ticket_after_failure(
        self,
        actor: Actor,
        planner_job_id: str,
        lease_id: str,
        expected_version: int,
        resume_role: str,
        resolution: str,
    ) -> dict[str, Any]:
        """Resolve one failure decision and requeue the ticket to the chosen role.

        The Planner chooses the next role only after reconciling current Git,
        specifications and the failure evidence. The existing immutable candidate
        is retained for QA or Integrator recovery; routing back to Developer marks
        the ticket REWORK when such a candidate exists.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(resolution, "resolution", 4000)
        try:
            target_role = Role(resume_role)
        except ValueError as error:
            raise ValidationError(
                "resume_role must be developer, qa or integrator"
            ) from error
        if target_role not in {Role.DEVELOPER, Role.QA, Role.INTEGRATOR}:
            raise ValidationError("resume_role must be developer, qa or integrator")
        with self.store.connection(immediate=True) as connection:
            job = self._planner_job(connection, planner_job_id)
            require_project(actor, job["project_id"])
            if job["job_type"] == PlannerJobType.DELIVERY_COMPLETED.value:
                raise ConflictError("A delivery-completed review cannot resume execution")
            planner_lease = self._one(
                connection,
                "SELECT * FROM planner_job_leases WHERE lease_id = ?",
                (lease_id,),
            )
            if planner_lease["planner_job_id"] != planner_job_id:
                raise AuthorizationError("Lease does not cover this Planner job")
            if planner_lease["agent_id"] != actor.agent_id:
                raise AuthorizationError("Planner job lease belongs to another actor")
            if datetime.fromisoformat(
                planner_lease["expires_at"].replace("Z", "+00:00")
            ) <= _now():
                raise ConflictError("Planner job lease has expired")

            ticket = self._ticket(connection, job["ticket_id"])
            self._expect_version(ticket, expected_version)
            if ticket["status"] not in {
                TicketStatus.BLOCKED.value,
                TicketStatus.REWORK.value,
            }:
                raise ConflictError("Only BLOCKED or REWORK work can be resumed after failure")
            if connection.execute(
                "SELECT 1 FROM ticket_retirement_requests "
                "WHERE ticket_id = ? AND status = 'PENDING'",
                (ticket["ticket_id"],),
            ).fetchone() is not None:
                raise ConflictError("Ticket has a pending Planner retirement")
            if connection.execute(
                "SELECT 1 FROM leases WHERE ticket_id = ?",
                (ticket["ticket_id"],),
            ).fetchone() is not None:
                raise ConflictError("Ticket still has an active execution lease")
            for table in (
                "worktree_provision_jobs",
                "candidate_submission_jobs",
                "qa_test_jobs",
                "merge_jobs",
            ):
                if connection.execute(
                    f"SELECT 1 FROM {table} WHERE ticket_id = ? "
                    "AND status IN ('QUEUED', 'RUNNING')",
                    (ticket["ticket_id"],),
                ).fetchone() is not None:
                    raise ConflictError("Ticket still has an active worker job")

            candidate = _row(connection.execute(
                "SELECT * FROM candidates WHERE ticket_id = ? "
                "ORDER BY submitted_at DESC LIMIT 1",
                (ticket["ticket_id"],),
            ).fetchone())
            if target_role is Role.DEVELOPER:
                next_status = (
                    TicketStatus.REWORK if candidate is not None else TicketStatus.READY
                )
            elif target_role is Role.QA:
                if candidate is None or candidate["commit_sha"] != ticket.get("candidate_commit"):
                    raise ConflictError(
                        "QA recovery requires the ticket's current immutable candidate"
                    )
                next_status = TicketStatus.QA_READY
            else:
                if candidate is None or candidate["commit_sha"] != ticket.get("candidate_commit"):
                    raise ConflictError(
                        "Integrator recovery requires the ticket's current immutable candidate"
                    )
                verified = connection.execute(
                    "SELECT 1 FROM audit_events WHERE aggregate_id = ? "
                    "AND event_type = 'ticket.verified' AND payload LIKE ?",
                    (ticket["ticket_id"], f'%"{candidate["candidate_id"]}"%'),
                ).fetchone()
                if verified is None:
                    raise ConflictError(
                        "Integrator recovery requires QA verification for the current candidate"
                    )
                next_status = TicketStatus.VERIFIED

            timestamp = _iso()
            next_version = int(ticket["version"]) + 1
            entry_id = _id(connection, "ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type,
                     content, source_ref, artifact_commit, actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'planner_decision', ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    job["project_id"],
                    ticket["feature_id"],
                    ticket["ticket_id"],
                    (
                        f"Planner resumed ticket for {target_role.value} after failure review: "
                        f"{resolution}"
                    ),
                    f"planner_job:{planner_job_id}",
                    ticket.get("candidate_commit") or ticket.get("base_commit"),
                    actor.agent_id,
                    actor.role.value,
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE tickets SET status = ?, blocked_reason = NULL,
                    version = ?, updated_at = ? WHERE ticket_id = ?
                """,
                (next_status.value, next_version, timestamp, ticket["ticket_id"]),
            )
            connection.execute(
                """
                UPDATE planner_jobs
                SET status = 'RESOLVED', resolution = ?, resolved_by = ?,
                    resolved_at = ?, updated_at = ? WHERE planner_job_id = ?
                """,
                (resolution, actor.agent_id, timestamp, timestamp, planner_job_id),
            )
            connection.execute(
                "DELETE FROM planner_job_leases WHERE lease_id = ?", (lease_id,)
            )
            self._event(
                connection,
                actor,
                job["project_id"],
                "ticket.failure_resolved",
                "ticket",
                ticket["ticket_id"],
                next_version,
                {
                    "planner_job_id": planner_job_id,
                    "resume_role": target_role.value,
                    "context_ref": f"ctx:entry:{entry_id}",
                },
            )
            resumed = self._ticket(connection, ticket["ticket_id"])
            self._queue_ticket(connection, actor, job["project_id"], resumed)
            return {
                "ticket": resumed,
                "planner_job": self._planner_job(connection, planner_job_id),
                "context_ref": f"ctx:entry:{entry_id}",
                "resume_role": target_role.value,
            }

    def _report_delivery_completed(
        self,
        connection: Connection,
        actor: Actor,
        project_id: str,
        ticket: dict[str, Any],
        *,
        candidate_id: str | None,
        mainline_commit: str,
        merge_job_id: str | None = None,
        gated: bool = True,
    ) -> dict[str, Any]:
        """Close the engineering feedback loop with one durable Planner report."""
        evidence_refs = [f"candidate:{candidate_id}"] if candidate_id is not None else []
        if merge_job_id is not None:
            evidence_refs.append(f"merge_job:{merge_job_id}")
        evidence_refs.append(f"mainline:{mainline_commit}")
        if candidate_id is not None:
            existing = _row(
                connection.execute(
                    """SELECT * FROM planner_jobs
                       WHERE ticket_id = ? AND job_type = 'delivery_completed'
                         AND candidate_id = ?
                       ORDER BY created_at DESC LIMIT 1""",
                    (ticket["ticket_id"], candidate_id),
                ).fetchone()
            )
        else:
            existing = _row(
                connection.execute(
                    """SELECT j.* FROM planner_jobs j
                       JOIN planner_interventions i ON i.planner_job_id = j.planner_job_id
                       WHERE j.ticket_id = ? AND j.job_type = 'delivery_completed'
                         AND i.artifact_commit = ?
                       ORDER BY j.created_at DESC LIMIT 1""",
                    (ticket["ticket_id"], mainline_commit),
                ).fetchone()
            )
        if existing is not None:
            return self._planner_job(connection, existing["planner_job_id"])
        return self._open_planner_job(
            connection,
            actor,
            project_id,
            ticket,
            job_type=PlannerJobType.DELIVERY_COMPLETED,
            summary=(
                f"{ticket['ticket_id']} landed on mainline at {mainline_commit}. "
                + (
                    "Review feature progress, unblock satisfied dependencies, and select the next bounded work."
                    if gated else
                    "This landing was recorded outside the gate and certifies no QA criterion; "
                    "reconcile scope and verification needs before selecting the next bounded work."
                )
            ),
            evidence_refs=evidence_refs,
            candidate_id=candidate_id,
            artifact_commit=mainline_commit,
        )

    def _record_planner_intervention(
        self,
        connection: Connection,
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
        intervention_id = _id(connection, "wake")
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
        self, connection: Connection, intervention_id: str
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
        with self.store.connection(immediate=True) as connection:
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
            lease_id = _id(connection, "planlease")
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

    def _expire_planner_job_leases(self, connection: Connection) -> list[str]:
        """Return abandoned planning decisions to the ready queue.

        Called by the periodic lease sweep and by the Planner wait loop as well as
        by discover/claim, so a job whose Planner chat died becomes READY (and
        wakes a waiting Planner) without anyone having to discover it first.
        """
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
        return [str(row["planner_job_id"]) for row in expired]

    def _planner_job(self, connection: Connection, planner_job_id: str) -> dict[str, Any]:
        job = self._one(
            connection, "SELECT * FROM planner_jobs WHERE planner_job_id = ?", (planner_job_id,)
        )
        job["lease"] = _row(
            connection.execute(
                "SELECT * FROM planner_job_leases WHERE planner_job_id = ?", (planner_job_id,)
            ).fetchone()
        )
        return job
