"""Stopping, cancelling, superseding and retiring tickets through a pending reconciliation."""
from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    ConflictError,
    InterventionSeverity,
    NotFoundError,
    PlannerJobType,
    RETIRABLE_TICKET_STATUSES,
    Role,
    TicketStatus,
    ValidationError,
    _id,
    _iso,
    _row,
    _transition_ticket,
    require_project,
    require_role,
    Connection,
)


class TicketRetirementMixin:
    """Stopping, cancelling, superseding and retiring tickets through a pending reconciliation."""

    def stop_ticket_for_reconciliation(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        reconciliation_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Revoke stale execution while preserving its worktree and frozen artifacts."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(reason, "reason", 2000)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            reconciliation = self._require_pending_reconciliation(
                connection, project_id, reconciliation_id
            )
            if ticket["status"] not in {
                TicketStatus.DEV_IN_PROGRESS.value,
                TicketStatus.QA_IN_PROGRESS.value,
                TicketStatus.MERGE_READY.value,
            }:
                raise ConflictError(
                    "Only active Development, QA or Integration work can be stopped for reconciliation"
                )
            lease = _row(
                connection.execute(
                    "SELECT * FROM leases WHERE ticket_id = ? AND expires_at > ?",
                    (ticket_id, _iso()),
                ).fetchone()
            )
            if lease is None:
                raise ConflictError("Ticket has no live execution lease to stop")
            for table in (
                "worktree_provision_jobs",
                "candidate_submission_jobs",
                "qa_test_jobs",
                "merge_jobs",
            ):
                if connection.execute(
                    f"SELECT 1 FROM {table} WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING')",
                    (ticket_id,),
                ).fetchone() is not None:
                    raise ConflictError(
                        "A detached worker is still running; wait for its guarded terminal result before stopping the ticket",
                        {"reason": "reconciliation_worker_active", "table": table},
                    )
            timestamp = _iso()
            next_version = ticket["version"] + 1
            blocked_reason = f"repository_reconciliation/{reconciliation_id}: {reason}"
            self._close_ticket_dispatch(connection, ticket_id, timestamp)
            connection.execute("DELETE FROM leases WHERE lease_id = ?", (lease["lease_id"],))
            _transition_ticket(
                connection, ticket_id, actor=actor, expected_version=ticket["version"],
                from_statuses=(
                    TicketStatus.DEV_IN_PROGRESS,
                    TicketStatus.QA_IN_PROGRESS,
                    TicketStatus.MERGE_READY,
                ),
                changes={"status": TicketStatus.BLOCKED, "blocked_reason": blocked_reason,
                         "updated_at": timestamp},
            )
            entry_id = _id(connection, "ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     source_ref, artifact_commit, actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'planner_decision', ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    project_id,
                    ticket["feature_id"],
                    ticket_id,
                    blocked_reason,
                    reconciliation["observation_ref"],
                    ticket.get("candidate_commit") or ticket.get("base_commit"),
                    actor.agent_id,
                    actor.role.value,
                    timestamp,
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                "ticket.reconciliation_stopped",
                "ticket",
                ticket_id,
                next_version,
                {
                    "reconciliation_id": reconciliation_id,
                    "previous_status": ticket["status"],
                    "revoked_lease_id": lease["lease_id"],
                    "previous_agent_id": lease["agent_id"],
                    "context_ref": f"ctx:entry:{entry_id}",
                },
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "preserved_worktree": _row(
                    connection.execute(
                        "SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)
                    ).fetchone()
                ),
                "preserved_candidate_commit": ticket.get("candidate_commit"),
                "context_ref": f"ctx:entry:{entry_id}",
            }

    def cancel_ticket(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        reason: str,
        reconciliation_id: str | None = None,
    ) -> dict[str, Any]:
        """Retire obsolete unclaimed work so dispatch stops offering it."""
        return self._retire_ticket(
            actor,
            ticket_id,
            expected_version,
            reason,
            next_status=TicketStatus.CANCELLED,
            entry_type="ticket_cancelled",
            event_type="ticket.cancelled",
            reconciliation_id=reconciliation_id,
        )

    def retire_ticket_for_reconciliation(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        reconciliation_id: str,
        reason: str,
        target_status: str = TicketStatus.CANCELLED.value,
        replacement_ticket_id: str | None = None,
    ) -> dict[str, Any]:
        """Fence obsolete work in any phase and retire it after Git is certain.

        Non-merge workers can be revoked immediately because they reauthorize
        before publishing. A RUNNING merge may already have moved the default
        branch, so its retirement remains PENDING until the supervisor reconciles
        the durable merge intent. Artifacts and worktrees are never deleted.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(reason, "reason", 2000)
        if target_status not in {
            TicketStatus.CANCELLED.value,
            TicketStatus.SUPERSEDED.value,
        }:
            raise ValidationError("target_status must be CANCELLED or SUPERSEDED")
        if target_status == TicketStatus.SUPERSEDED.value and not replacement_ticket_id:
            raise ValidationError("SUPERSEDED retirement requires replacement_ticket_id")
        if target_status == TicketStatus.CANCELLED.value and replacement_ticket_id is not None:
            raise ValidationError("CANCELLED retirement cannot name a replacement ticket")
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            reconciliation = self._require_pending_reconciliation(
                connection, project_id, reconciliation_id
            )
            if ticket["status"] in {
                TicketStatus.DONE.value,
                TicketStatus.CANCELLED.value,
                TicketStatus.SUPERSEDED.value,
            }:
                raise ConflictError("A terminal ticket cannot start a retirement request")
            self._retirement_replacement(
                connection, ticket_id, project_id, replacement_ticket_id
            )
            existing = _row(
                connection.execute(
                    """
                    SELECT * FROM ticket_retirement_requests
                    WHERE ticket_id = ? AND status = 'PENDING'
                    """,
                    (ticket_id,),
                ).fetchone()
            )
            if existing is not None:
                same = (
                    existing["reconciliation_id"] == reconciliation_id
                    and existing["target_status"] == target_status
                    and existing.get("replacement_ticket_id") == replacement_ticket_id
                    and existing["reason"] == reason
                )
                if not same:
                    raise ConflictError("Ticket already has a different pending retirement")
                return {
                    "retirement": existing,
                    "ticket": ticket,
                    "pending_merge_reconciliation": self._ticket_has_running_merge(
                        connection, ticket_id
                    ),
                }
            timestamp = _iso()
            request_id = _id(connection, "retire")
            connection.execute(
                """
                INSERT INTO ticket_retirement_requests
                    (request_id, project_id, ticket_id, reconciliation_id, target_status,
                     replacement_ticket_id, reason, requested_by, status, requested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
                """,
                (
                    request_id,
                    project_id,
                    ticket_id,
                    reconciliation_id,
                    target_status,
                    replacement_ticket_id,
                    reason,
                    actor.agent_id,
                    timestamp,
                ),
            )
            self._close_ticket_dispatch(connection, ticket_id, timestamp)
            self._fence_ticket_for_retirement(connection, ticket_id, request_id, timestamp)
            self._event(
                connection,
                actor,
                project_id,
                "ticket.retirement_requested",
                "ticket",
                ticket_id,
                ticket["version"],
                {
                    "request_id": request_id,
                    "reconciliation_id": reconciliation["reconciliation_id"],
                    "target_status": target_status,
                    "previous_status": ticket["status"],
                },
            )
        self.finalize_pending_ticket_retirements(
            Actor("retirement-finalizer", Role.ADMIN, frozenset({"*"})),
            request_id=request_id,
        )
        return self.get_ticket_retirement(actor, request_id)

    def get_ticket_retirement(self, actor: Actor, request_id: str) -> dict[str, Any]:
        """Read one retirement request and opportunistically settle it."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        with self.store.connection() as connection:
            request = self._one(
                connection,
                "SELECT * FROM ticket_retirement_requests WHERE request_id = ?",
                (request_id,),
            )
            require_project(actor, request["project_id"])
        if request["status"] == "PENDING":
            self.finalize_pending_ticket_retirements(
                Actor("retirement-finalizer", Role.ADMIN, frozenset({"*"})),
                request_id=request_id,
            )
        with self.store.connection() as connection:
            request = self._one(
                connection,
                "SELECT * FROM ticket_retirement_requests WHERE request_id = ?",
                (request_id,),
            )
            return {
                "retirement": request,
                "ticket": self._ticket(connection, request["ticket_id"]),
                "pending_merge_reconciliation": self._ticket_has_running_merge(
                    connection, request["ticket_id"]
                ),
            }

    def finalize_pending_ticket_retirements(
        self, actor: Actor, request_id: str | None = None
    ) -> dict[str, Any]:
        """Supervisor/operator settlement after every possibly-running merge stops."""
        require_role(actor, Role.ADMIN)
        settled: list[dict[str, Any]] = []
        with self.store.connection(immediate=True) as connection:
            parameters: tuple[Any, ...] = ()
            where = "status = 'PENDING'"
            if request_id is not None:
                where += " AND request_id = ?"
                parameters = (request_id,)
            requests = [
                _row(row) or {}
                for row in connection.execute(
                    f"""
                    SELECT * FROM ticket_retirement_requests
                    WHERE {where} ORDER BY requested_at ASC LIMIT 100
                    """,
                    parameters,
                ).fetchall()
            ]
            for request in requests:
                ticket_id = request["ticket_id"]
                if self._ticket_has_running_merge(connection, ticket_id):
                    continue
                ticket = self._ticket(connection, ticket_id)
                timestamp = _iso()
                if ticket["status"] == TicketStatus.DONE.value:
                    outcome = (
                        "The merge reached the default branch before cancellation completed; "
                        "the ticket remains DONE and Planner must decide whether a revert is needed."
                    )
                    connection.execute(
                        """
                        UPDATE ticket_retirement_requests
                        SET status = 'LANDED', completed_at = ?, outcome = ?
                        WHERE request_id = ?
                        """,
                        (timestamp, outcome, request["request_id"]),
                    )
                    self._event(
                        connection, actor, request["project_id"],
                        "ticket.retirement_landed", "ticket", ticket_id,
                        ticket["version"], {"request_id": request["request_id"]},
                    )
                    settled.append(self._one(
                        connection,
                        "SELECT * FROM ticket_retirement_requests WHERE request_id = ?",
                        (request["request_id"],),
                    ))
                    continue
                if ticket["status"] in {
                    TicketStatus.CANCELLED.value,
                    TicketStatus.SUPERSEDED.value,
                }:
                    final_status = (
                        "APPLIED" if ticket["status"] == request["target_status"] else "FAILED"
                    )
                    connection.execute(
                        """
                        UPDATE ticket_retirement_requests
                        SET status = ?, completed_at = ?, outcome = ? WHERE request_id = ?
                        """,
                        (
                            final_status,
                            timestamp,
                            f"Ticket was already terminal as {ticket['status']}.",
                            request["request_id"],
                        ),
                    )
                    settled.append(self._one(
                        connection,
                        "SELECT * FROM ticket_retirement_requests WHERE request_id = ?",
                        (request["request_id"],),
                    ))
                    continue
                self._fence_ticket_for_retirement(
                    connection, ticket_id, request["request_id"], timestamp
                )
                next_version = _transition_ticket(
                    connection, ticket_id, actor=actor, expected_version=ticket["version"],
                    from_statuses=(ticket["status"],),
                    changes={"status": request["target_status"], "blocked_reason": None,
                             "updated_at": timestamp},
                )
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
                        request["project_id"],
                        ticket["feature_id"],
                        ticket_id,
                        (
                            request["reason"]
                            + (
                                f" Replacement ticket: {request['replacement_ticket_id']}."
                                if request.get("replacement_ticket_id") else ""
                            )
                        ),
                        f"retirement:{request['request_id']}",
                        ticket.get("candidate_commit") or ticket.get("base_commit"),
                        request["requested_by"],
                        Role.PLANNER.value,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE ticket_retirement_requests
                    SET status = 'APPLIED', completed_at = ?, outcome = ?
                    WHERE request_id = ?
                    """,
                    (
                        timestamp,
                        f"Ticket retired as {request['target_status']}; mutable artifacts were preserved.",
                        request["request_id"],
                    ),
                )
                open_planner_jobs = [
                    str(row["planner_job_id"])
                    for row in connection.execute(
                        """
                        SELECT planner_job_id FROM planner_jobs
                        WHERE ticket_id = ? AND status IN ('READY', 'IN_PROGRESS')
                        """,
                        (ticket_id,),
                    ).fetchall()
                ]
                if open_planner_jobs:
                    placeholders = ",".join("?" for _ in open_planner_jobs)
                    connection.execute(
                        f"DELETE FROM planner_job_leases WHERE planner_job_id IN ({placeholders})",
                        tuple(open_planner_jobs),
                    )
                    connection.execute(
                        f"""
                        UPDATE planner_jobs SET status = 'RESOLVED', resolution = ?,
                            resolved_by = ?, resolved_at = ?, updated_at = ?
                        WHERE planner_job_id IN ({placeholders})
                        """,
                        (
                            f"Ticket retired by {request['request_id']}.",
                            request["requested_by"],
                            timestamp,
                            timestamp,
                            *open_planner_jobs,
                        ),
                    )
                rescue = self._rescue_dependents_of_retired(
                    connection,
                    Actor(request["requested_by"], Role.PLANNER, frozenset({request["project_id"]})),
                    request["project_id"], ticket_id, request["target_status"],
                    [request["replacement_ticket_id"]] if request.get("replacement_ticket_id") else [],
                )
                self._event(
                    connection,
                    actor,
                    request["project_id"],
                    "ticket.retirement_applied",
                    "ticket",
                    ticket_id,
                    next_version,
                    {
                        "request_id": request["request_id"],
                        "target_status": request["target_status"],
                        "context_ref": f"ctx:entry:{entry_id}",
                        "resolved_planner_jobs": open_planner_jobs,
                        "rewired_dependent_ticket_ids": rescue["rewired"],
                        "escalated_dependent_ticket_ids": rescue["escalated"],
                    },
                )
                settled.append(self._one(
                    connection,
                    "SELECT * FROM ticket_retirement_requests WHERE request_id = ?",
                    (request["request_id"],),
                ))
        return {"settled": settled, "count": len(settled)}

    def _rescue_dependents_of_retired(
        self,
        connection: Connection,
        actor: Actor,
        project_id: str,
        retired_ticket_id: str,
        retired_status: str,
        replacement_ticket_ids: list[str],
    ) -> dict[str, list[str]]:
        """Give every open dependent of retired work a way out, in the same transaction.

        Only DONE satisfies a dependency, so a dependent of CANCELLED or
        SUPERSEDED work could never be offered again (review R2). With a
        replacement, the edge moves to it: the replacement carries the work the
        dependent was waiting for. Without one, or when moving the edge would
        close a cycle or point at retired work, the dependent gets its own
        Planner job naming the retired dependency, so the decision is queued
        instead of left to someone noticing a ticket that never moves.
        """
        closed = {
            TicketStatus.DONE.value,
            TicketStatus.CANCELLED.value,
            TicketStatus.SUPERSEDED.value,
        }
        rows = connection.execute(
            """
            SELECT d.ticket_id FROM ticket_dependencies d
            JOIN tickets t ON t.ticket_id = d.ticket_id
            WHERE d.depends_on_ticket_id = ? ORDER BY d.ticket_id
            """,
            (retired_ticket_id,),
        ).fetchall()
        rewired: list[str] = []
        escalated: list[str] = []
        planner_job_ids: list[str] = []
        for row in rows:
            dependent = self._ticket(connection, str(row["ticket_id"]))
            if dependent["status"] in closed:
                continue
            dependent_id = dependent["ticket_id"]
            targets: list[str] = []
            usable = bool(replacement_ticket_ids)
            for replacement_id in replacement_ticket_ids:
                replacement = _row(connection.execute(
                    "SELECT status FROM tickets WHERE ticket_id = ?", (replacement_id,)
                ).fetchone())
                if (
                    replacement is None
                    or replacement_id == dependent_id
                    or replacement["status"] in {
                        TicketStatus.CANCELLED.value, TicketStatus.SUPERSEDED.value
                    }
                    or self._dependency_would_cycle(connection, dependent_id, replacement_id)
                ):
                    usable = False
                    break
                targets.append(replacement_id)
            if usable:
                connection.execute(
                    "DELETE FROM ticket_dependencies WHERE ticket_id = ? AND depends_on_ticket_id = ?",
                    (dependent_id, retired_ticket_id),
                )
                for target in targets:
                    if connection.execute(
                        "SELECT 1 FROM ticket_dependencies "
                        "WHERE ticket_id = ? AND depends_on_ticket_id = ?",
                        (dependent_id, target),
                    ).fetchone() is None:
                        connection.execute(
                            "INSERT INTO ticket_dependencies (ticket_id, depends_on_ticket_id) "
                            "VALUES (?, ?)",
                            (dependent_id, target),
                        )
                self._event(
                    connection, actor, project_id, "ticket.dependency_rewired", "ticket",
                    dependent_id, dependent["version"],
                    {"retired_ticket_id": retired_ticket_id, "retired_status": retired_status,
                     "replacement_ticket_ids": targets},
                )
                rewired.append(dependent_id)
                continue
            job = self._open_planner_job(
                connection, actor, project_id, dependent,
                job_type=PlannerJobType.DEVELOPER_BLOCKER,
                summary=(
                    f"dependency_retired: {dependent_id} ({dependent['status']}) depends on "
                    f"{retired_ticket_id}, which was {retired_status}"
                    + (
                        f" with replacement(s) {', '.join(replacement_ticket_ids)} that cannot "
                        "carry the edge (retired, the dependent itself, or a cycle)"
                        if replacement_ticket_ids else " with no replacement"
                    )
                    + ". Only DONE satisfies a dependency, so it will never be offered as is. "
                    "Rewire it with planner.update_ticket_fields(dependency_ids=...) or "
                    "retire it."
                ),
                evidence_refs=[f"ticket:{retired_ticket_id}", "dependency_retired"],
                severity=InterventionSeverity.NORMAL,
            )
            escalated.append(dependent_id)
            planner_job_ids.append(job["planner_job_id"])
        return {"rewired": rewired, "escalated": escalated, "planner_job_ids": planner_job_ids}

    @staticmethod
    def _ticket_has_running_merge(connection: Connection, ticket_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM merge_jobs WHERE ticket_id = ? AND status = 'RUNNING'",
            (ticket_id,),
        ).fetchone() is not None

    def _fence_ticket_for_retirement(
        self, connection: Connection, ticket_id: str, request_id: str, timestamp: str
    ) -> None:
        message = (
            f"Planner retirement {request_id} revoked execution; mutable artifacts remain preserved."
        )
        for table in ("worktree_provision_jobs", "candidate_submission_jobs"):
            connection.execute(
                f"""
                UPDATE {table} SET status = 'FAILED', completed_at = ?, error_message = ?
                WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING')
                """,
                (timestamp, message, ticket_id),
            )
        connection.execute(
            """
            UPDATE qa_test_jobs SET status = 'STALE', completed_at = ?, error_message = ?
            WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING')
            """,
            (timestamp, message, ticket_id),
        )
        connection.execute(
            """
            UPDATE merge_jobs SET status = 'FAILED', completed_at = ?, error_message = ?
            WHERE ticket_id = ? AND status = 'QUEUED'
            """,
            (timestamp, message, ticket_id),
        )
        connection.execute("DELETE FROM worktree_ownership WHERE ticket_id = ?", (ticket_id,))
        connection.execute("DELETE FROM leases WHERE ticket_id = ?", (ticket_id,))

    def supersede_ticket(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        replacement_ticket_id: str,
        reason: str,
        reconciliation_id: str | None = None,
    ) -> dict[str, Any]:
        """Retire work that a named same-project replacement ticket now carries."""
        self._bounded_text(replacement_ticket_id, "replacement_ticket_id", 100)
        return self._retire_ticket(
            actor,
            ticket_id,
            expected_version,
            reason,
            next_status=TicketStatus.SUPERSEDED,
            entry_type="ticket_superseded",
            event_type="ticket.superseded",
            replacement_ticket_id=replacement_ticket_id,
            reconciliation_id=reconciliation_id,
        )

    def _retire_ticket(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        reason: str,
        *,
        next_status: TicketStatus,
        entry_type: str,
        event_type: str,
        replacement_ticket_id: str | None = None,
        reconciliation_id: str | None = None,
    ) -> dict[str, Any]:
        """Close one ticket's dispatch without rewriting active or evidenced work.

        Nothing is deleted: the ticket, its context, candidates and audit history stay
        readable. Only the queue rows that would keep offering it are closed, in the
        same transaction as the status change, so no agent can be handed retired work.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(reason, "reason", 2000)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            reconciliation = None
            if reconciliation_id is not None:
                reconciliation = self._require_pending_reconciliation(
                    connection, project_id, reconciliation_id
                )
            reconciliation_retirable = {
                TicketStatus.BACKLOG,
                TicketStatus.READY,
                TicketStatus.BLOCKED,
                TicketStatus.REWORK,
                TicketStatus.QA_READY,
                TicketStatus.VERIFIED,
                TicketStatus.MERGE_READY,
            }
            allowed_statuses = (
                reconciliation_retirable
                if reconciliation is not None
                else RETIRABLE_TICKET_STATUSES
            )
            if TicketStatus(ticket["status"]) not in allowed_statuses:
                raise ConflictError(
                    "Ticket cannot be cancelled or superseded from its current lifecycle state"
                )
            self._require_no_active_work(
                connection, ticket_id, allow_frozen=reconciliation is not None
            )
            replacement = self._retirement_replacement(
                connection, ticket_id, project_id, replacement_ticket_id
            )
            timestamp = _iso()
            closed_offers = self._close_ticket_dispatch(connection, ticket_id, timestamp)
            next_version = _transition_ticket(
                connection, ticket_id, actor=actor, expected_version=ticket["version"],
                from_statuses=allowed_statuses,
                changes={"status": next_status, "updated_at": timestamp},
            )
            entry_id = _id(connection, "ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     source_ref, actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    project_id,
                    ticket["feature_id"],
                    ticket_id,
                    entry_type,
                    reason,
                    (
                        reconciliation["observation_ref"]
                        if reconciliation is not None
                        else f"ticket:{replacement_ticket_id}" if replacement_ticket_id else None
                    ),
                    actor.agent_id,
                    actor.role.value,
                    timestamp,
                ),
            )
            dependents = [
                str(row["ticket_id"])
                for row in connection.execute(
                    """
                    SELECT ticket_id FROM ticket_dependencies
                    WHERE depends_on_ticket_id = ? ORDER BY ticket_id
                    """,
                    (ticket_id,),
                ).fetchall()
            ]
            rescue = self._rescue_dependents_of_retired(
                connection, actor, project_id, ticket_id, next_status.value,
                [replacement_ticket_id] if replacement_ticket_id else [],
            )
            self._event(
                connection,
                actor,
                project_id,
                event_type,
                "ticket",
                ticket_id,
                next_version,
                {
                    "previous_status": ticket["status"],
                    "reason": reason,
                    "replacement_ticket_id": replacement_ticket_id,
                    "closed_offers": closed_offers,
                    "dependent_ticket_ids": dependents,
                    "rewired_dependent_ticket_ids": rescue["rewired"],
                    "escalated_dependent_ticket_ids": rescue["escalated"],
                },
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "replacement": replacement,
                "closed_offers": closed_offers,
                "dependent_ticket_ids": dependents,
                "rewired_dependent_ticket_ids": rescue["rewired"],
                "dependent_planner_job_ids": rescue["planner_job_ids"],
                "context_entry": self._one(
                    connection, "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
                ),
            }

    def _require_no_active_work(
        self, connection: Connection, ticket_id: str, *, allow_frozen: bool = False
    ) -> None:
        """Refuse retirement while any agent, worker or frozen artifact still owns this."""
        active_lease = connection.execute(
            "SELECT 1 FROM leases WHERE ticket_id = ? AND expires_at > ?",
            (ticket_id, _iso()),
        ).fetchone()
        if active_lease is not None:
            raise ConflictError("A ticket with an active lease cannot be cancelled or superseded")
        for table in (
            "worktree_provision_jobs",
            "candidate_submission_jobs",
            "qa_test_jobs",
            "merge_jobs",
        ):
            running = connection.execute(
                f"SELECT 1 FROM {table} WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING')",
                (ticket_id,),
            ).fetchone()
            if running is not None:
                raise ConflictError("A ticket with a running job cannot be cancelled or superseded")
        candidate = connection.execute(
            "SELECT 1 FROM candidates WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        if candidate is not None and not allow_frozen:
            raise ConflictError(
                "A ticket with a frozen candidate cannot be cancelled or superseded"
            )

    def _require_pending_reconciliation(
        self, connection: Connection, project_id: str, reconciliation_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT * FROM repository_reconciliations
            WHERE reconciliation_id = ? AND project_id = ? AND status = 'PENDING'
            """,
            (reconciliation_id, project_id),
        ).fetchone()
        if row is None:
            raise ConflictError(
                "The repository reconciliation is not pending for this project",
                {"reason": "repository_reconciliation_not_pending"},
            )
        return self._repository_reconciliation(row)

    def _retirement_replacement(
        self,
        connection: Connection,
        ticket_id: str,
        project_id: str,
        replacement_ticket_id: str | None,
    ) -> dict[str, Any] | None:
        """Resolve the replacement reference that makes a supersede auditable."""
        if replacement_ticket_id is None:
            return None
        if replacement_ticket_id == ticket_id:
            raise ValidationError("A ticket cannot supersede itself")
        replacement = _row(
            connection.execute(
                "SELECT * FROM tickets WHERE ticket_id = ?", (replacement_ticket_id,)
            ).fetchone()
        )
        if replacement is None:
            raise NotFoundError("Replacement ticket not found")
        if self._project_for_ticket(connection, replacement_ticket_id) != project_id:
            raise ValidationError("Replacement ticket must belong to the same project")
        return replacement

    def _close_ticket_dispatch(
        self, connection: Connection, ticket_id: str, timestamp: str
    ) -> int:
        """Close queue, offer and outbox rows together so no live hand-off survives."""
        queue_ids = [
            str(row["queue_id"])
            for row in connection.execute(
                "SELECT queue_id FROM work_queue WHERE ticket_id = ? AND status IN ('pending', 'offered')",
                (ticket_id,),
            ).fetchall()
        ]
        if not queue_ids:
            return 0
        placeholders = ", ".join("?" for _ in queue_ids)
        closed_offers = connection.execute(
            f"""
            UPDATE work_offers SET status = 'cancelled', closed_at = ?
            WHERE queue_id IN ({placeholders}) AND status = 'offered'
            """,
            (timestamp, *queue_ids),
        ).rowcount
        connection.execute(
            f"""
            UPDATE dispatch_outbox SET status = 'cancelled'
            WHERE queue_id IN ({placeholders}) AND status = 'pending'
            """,
            queue_ids,
        )
        connection.execute(
            f"""
            UPDATE work_queue SET status = 'cancelled', updated_at = ?
            WHERE queue_id IN ({placeholders})
            """,
            (timestamp, *queue_ids),
        )
        return max(closed_offers, 0)
