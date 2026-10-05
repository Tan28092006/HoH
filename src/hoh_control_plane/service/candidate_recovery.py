"""Planner recovery of a candidate: requeue or return it to QA."""
from __future__ import annotations

from hoh_control_plane.service._common import (
    _audit_event_has_field,
    Actor,
    Any,
    ConflictError,
    Role,
    TicketStatus,
    ValidationError,
    _id,
    _iso,
    _row,
    _transition_ticket,
    require_role,
    Connection,
)


class CandidateRecoveryMixin:
    """Planner recovery of a candidate: requeue or return it to QA."""

    def requeue_candidate(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        reason: str = "base_drift",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Requeue a stale QA-ready candidate whose base commit has drifted from main."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(ticket_id, "ticket_id", 100)
        self._bounded_text(reason, "reason", 2000)

        if idempotency_key:
            payload = {
                "ticket_id": ticket_id,
                "expected_version": expected_version,
                "reason": reason,
            }
            return self.run_idempotent(
                actor,
                operation="candidate:requeue",
                idempotency_key=idempotency_key,
                payload=payload,
                run=lambda: self._requeue_candidate(actor, ticket_id, expected_version, reason),
            )
        return self._requeue_candidate(actor, ticket_id, expected_version, reason)

    def _requeue_candidate(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        reason: str,
    ) -> dict[str, Any]:
        with self.store.connection() as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] != TicketStatus.QA_READY.value:
                raise ConflictError(
                    f"Only QA_READY tickets can have their candidate requeued (ticket is {ticket['status']})"
                )
            repository = _row(
                connection.execute(
                    "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            if repository is None:
                raise ValidationError("Project repository is required before requeuing a candidate")
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            candidate = _row(
                connection.execute(
                    "SELECT * FROM candidates WHERE ticket_id = ? ORDER BY submitted_at DESC LIMIT 1",
                    (ticket_id,),
                ).fetchone()
            )
            if candidate is None:
                raise ConflictError("Ticket has no frozen candidate to requeue")

        repository_path = repository["repository_path"]
        default_branch = project["default_branch"]
        mainline = f"refs/heads/{default_branch}"
        base = candidate["base_commit"]
        base_is_current_main = self.git_worktrees.commit_descends_from(
            repository_path=repository_path,
            ancestor_commit=mainline,
            descendant_commit=base,
        ) and self.git_worktrees.commit_descends_from(
            repository_path=repository_path,
            ancestor_commit=base,
            descendant_commit=mainline,
        )
        if base_is_current_main:
            raise ConflictError(
                "Candidate base commit has not drifted from current main; candidate is not stale"
            )

        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] != TicketStatus.QA_READY.value:
                raise ConflictError(
                    f"Only QA_READY tickets can have their candidate requeued (ticket is {ticket['status']})"
                )

            active_lease = connection.execute(
                "SELECT 1 FROM leases WHERE ticket_id = ? AND expires_at > ?",
                (ticket_id, _iso()),
            ).fetchone()
            if active_lease is not None:
                raise ConflictError(
                    "A ticket with an active lease cannot have its candidate requeued"
                )
            connection.execute("DELETE FROM leases WHERE ticket_id = ?", (ticket_id,))

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
                    raise ConflictError(
                        f"A ticket with a running job ({table}) cannot have its candidate requeued"
                    )

            latest_candidate = _row(
                connection.execute(
                    "SELECT * FROM candidates WHERE ticket_id = ? ORDER BY submitted_at DESC LIMIT 1",
                    (ticket_id,),
                ).fetchone()
            )
            if latest_candidate is None or latest_candidate["candidate_id"] != candidate["candidate_id"]:
                raise ConflictError("Candidate changed during requeue")

            timestamp = _iso()
            next_version = ticket["version"] + 1

            closed_offers = self._close_ticket_dispatch(connection, ticket_id, timestamp)
            _transition_ticket(
                connection, ticket_id, actor=actor, expected_version=ticket["version"],
                from_statuses=(TicketStatus.QA_READY,),
                changes={"status": TicketStatus.REWORK, "blocked_reason": reason,
                         "updated_at": timestamp},
            )

            entry_id = _id(connection, "ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     source_ref, actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'candidate_requeued', ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    project_id,
                    ticket["feature_id"],
                    ticket_id,
                    reason,
                    f"candidate:{candidate['candidate_id']}",
                    actor.agent_id,
                    actor.role.value,
                    timestamp,
                ),
            )

            self._event(
                connection,
                actor,
                project_id,
                "candidate.requeued",
                "ticket",
                ticket_id,
                next_version,
                {
                    "previous_status": ticket["status"],
                    "reason": reason,
                    "candidate_id": candidate["candidate_id"],
                    "candidate_commit": candidate["commit_sha"],
                    "base_commit": candidate["base_commit"],
                    "closed_offers": closed_offers,
                },
            )

            rework_ticket = self._ticket(connection, ticket_id)
            self._queue_ticket(connection, actor, project_id, rework_ticket)

            return {
                "ticket": rework_ticket,
                "candidate": candidate,
                "closed_offers": closed_offers,
                "context_entry": self._one(
                    connection, "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
                ),
            }

    def recover_candidate_for_qa(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        evidence_reference: str,
        readiness_reference: str | None = None,
    ) -> dict[str, Any]:
        """Requeue a frozen candidate for independent QA after an infra-only rejection.

        This never marks a candidate verified, never re-runs Developer submission, and
        never edits or duplicates the immutable candidate: it only proves, from durable
        evidence already on file, that a candidate's prior QA rejection was an
        infrastructure artifact mismatch rather than a logic failure, then routes the
        exact same candidate back to a fresh, independent QA claim.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(evidence_reference, "evidence_reference", 200)
        if readiness_reference is not None:
            self._bounded_text(readiness_reference, "readiness_reference", 200)
        with self.store.connection(immediate=True) as connection:
            self._expire_lease(connection, ticket_id)
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if TicketStatus(ticket["status"]) not in {TicketStatus.BLOCKED, TicketStatus.REWORK}:
                raise ConflictError("Only a BLOCKED or REWORK ticket can be recovered for QA")
            if (ticket.get("blocked_reason") or "").startswith("qa.infrastructure/"):
                readiness = _row(connection.execute(
                    "SELECT * FROM context_entries WHERE entry_id = ? AND ticket_id = ? AND superseded_by IS NULL",
                    (readiness_reference, ticket_id)).fetchone())
                if (readiness is None or readiness["actor_role"] not in {"planner", "admin"}
                        or readiness["entry_type"] not in {"planner_decision", "planning_decision"}
                        or not readiness["source_ref"] or readiness["created_at"] < ticket["updated_at"]):
                    raise ValidationError("Infrastructure recovery requires a current Planner readiness entry with an evidence source")
            active_lease = connection.execute(
                "SELECT 1 FROM leases WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if active_lease is not None:
                raise ConflictError("Recovery requires no active lease on the ticket")
            candidate_rows = connection.execute(
                "SELECT * FROM candidates WHERE ticket_id = ? ORDER BY submitted_at DESC LIMIT 2",
                (ticket_id,),
            ).fetchall()
            # A policy refresh may submit the same SHA again with a different
            # frozen profile. Match QA's current submission, never an older row
            # selected by SHA or by the caller's preferred evidence reference.
            if not candidate_rows or (len(candidate_rows) > 1
                    and candidate_rows[0]["submitted_at"] == candidate_rows[1]["submitted_at"]):
                raise ValidationError(
                    "Recovery requires an unambiguous latest frozen candidate on this ticket"
                )
            candidate = _row(candidate_rows[0])
            if candidate is None or candidate["commit_sha"] != ticket["candidate_commit"]:
                raise ConflictError(
                    "Candidate identity no longer matches the frozen ticket artifact"
                )
            active_job = connection.execute(
                """
                SELECT 1 FROM qa_test_jobs
                WHERE candidate_id = ? AND status IN ('QUEUED', 'RUNNING')
                """,
                (candidate["candidate_id"],),
            ).fetchone()
            if active_job is not None:
                raise ConflictError("Recovery requires no active project test run")
            provenance = self._verify_infrastructure_mismatch_provenance(
                connection, candidate["candidate_id"], evidence_reference
            )
            timestamp = _iso()
            next_version = ticket["version"] + 1
            _transition_ticket(
                connection, ticket_id, actor=actor, expected_version=ticket["version"],
                from_statuses=(TicketStatus.BLOCKED, TicketStatus.REWORK),
                changes={"status": TicketStatus.QA_READY, "blocked_reason": None,
                         "updated_at": timestamp},
            )
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     source_ref, artifact_commit, actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'planner_decision', ?, ?, ?, ?, ?, ?)
                """,
                (
                    _id(connection, "ctx"),
                    project_id,
                    ticket["feature_id"],
                    ticket_id,
                    (
                        f"Recovered candidate {candidate['candidate_id']} "
                        f"({candidate['commit_sha']}) for independent QA. The prior rejection "
                        f"on this candidate was an infrastructure artifact mismatch, not a "
                        f"candidate logic failure: {provenance}. Recovered from ticket version "
                        f"{expected_version}; the immutable candidate was not edited, "
                        "duplicated, or resubmitted."
                        + (f" Readiness evidence: {readiness_reference}." if readiness_reference else "")
                    ),
                    evidence_reference,
                    candidate["commit_sha"],
                    actor.agent_id,
                    actor.role.value,
                    timestamp,
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                "candidate.qa_recovery",
                "ticket",
                ticket_id,
                next_version,
                {"candidate_id": candidate["candidate_id"], "evidence_reference": evidence_reference},
            )
            recovered_ticket = self._ticket(connection, ticket_id)
            self._queue_ticket(connection, actor, project_id, recovered_ticket)
            return {
                "ticket": recovered_ticket,
                "candidate": self._candidate_with_changed_paths(connection, candidate),
            }

    def _verify_infrastructure_mismatch_provenance(
        self,
        connection: Connection,
        candidate_id: str,
        evidence_reference: str,
    ) -> str:
        """Confirm cited evidence shows an infra-only gap, never a logic failure.

        A logical QA failure recorded anywhere against this candidate blocks recovery
        outright, regardless of which piece of evidence the Planner cites -- recovery
        must never become a generic bypass for a real QA rejection.
        """
        logical_failure = connection.execute(
            "SELECT 1 FROM qa_findings WHERE candidate_id = ? AND status = 'failed' LIMIT 1",
            (candidate_id,),
        ).fetchone()
        if logical_failure is not None:
            raise ValidationError(
                "Candidate has a logical QA failure on record and cannot be recovered"
            )
        if evidence_reference.startswith("testrun_"):
            job = _row(
                connection.execute(
                    "SELECT * FROM qa_test_jobs WHERE test_run_id = ?", (evidence_reference,)
                ).fetchone()
            )
            if job is None or job["candidate_id"] != candidate_id:
                raise ValidationError(
                    "Evidence reference does not belong to this candidate's project test runs"
                )
            recorded_infrastructure = _audit_event_has_field(
                connection, job["ticket_id"], "qa.infrastructure_blocked",
                "test_run_id", evidence_reference,
            )
            if not recorded_infrastructure and (job["status"] != "FAILED" or "candidate_artifact_mismatch" not in (
                job["error_message"] or ""
            )):
                raise ValidationError(
                    "Evidence reference does not show a candidate_artifact_mismatch"
                )
            return f"qa_test_jobs:{evidence_reference}"
        if evidence_reference.startswith("finding_"):
            finding = _row(
                connection.execute(
                    "SELECT * FROM qa_findings WHERE finding_id = ?", (evidence_reference,)
                ).fetchone()
            )
            if finding is None or finding["candidate_id"] != candidate_id:
                raise ValidationError(
                    "Evidence reference does not belong to this candidate's QA findings"
                )
            if finding["status"] != "insufficient_evidence":
                raise ValidationError(
                    "Evidence reference does not show an insufficient-evidence gap"
                )
            return f"qa_findings:{evidence_reference}"
        raise ValidationError(
            "evidence_reference must be a qa_test_jobs test_run_id or a qa_findings finding_id"
        )
