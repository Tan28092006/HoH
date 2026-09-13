from __future__ import annotations

from dataclasses import replace

from hoh_control_plane import semantic_record
from hoh_control_plane.service.qa import _INFRASTRUCTURE_OUTCOMES
from hoh_control_plane.service._common import (
    Actor,
    Any,
    AuthorizationError,
    CLOSED_TICKET_STATUSES,
    ConflictError,
    DomainError,
    PlannerJobType,
    ReleasePolicyError,
    Role,
    TicketStatus,
    ValidationError,
    _id,
    _iso,
    _now,
    _row,
    hashlib,
    re,
    require_role,
    Connection,
    timedelta,
)


class IntegrationMixin:
    """Protected merge gating, release policy and merge jobs."""

    @staticmethod
    def _current_integration_candidate(connection: Connection, ticket: dict[str, Any]) -> dict[str, Any]:
        rows = connection.execute(
            "SELECT * FROM candidates WHERE ticket_id = ? ORDER BY submitted_at DESC LIMIT 2",
            (ticket["ticket_id"],),
        ).fetchall()
        candidate = _row(rows[0]) if rows else None
        if (candidate is None or candidate["commit_sha"] != ticket["candidate_commit"]
                or (len(rows) > 1 and rows[0]["submitted_at"] == rows[1]["submitted_at"])):
            raise ReleasePolicyError("No unambiguous current candidate matches the ticket artifact",
                                     {"reason": "candidate_identity_mismatch"})
        return candidate

    def claim_qa(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        lease_seconds: int,
        context_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        require_role(actor, Role.QA)
        return self._claim(
            actor,
            ticket_id,
            expected_version,
            lease_seconds,
            allowed_statuses={TicketStatus.QA_READY},
            next_status=TicketStatus.QA_IN_PROGRESS,
            event_type="qa.started",
            context_snapshot_id=context_snapshot_id,
        )

    def claim_integration(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        lease_seconds: int,
        context_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        require_role(actor, Role.INTEGRATOR)
        return self._claim(
            actor,
            ticket_id,
            expected_version,
            lease_seconds,
            allowed_statuses={TicketStatus.VERIFIED, TicketStatus.MERGE_READY},
            next_status=TicketStatus.MERGE_READY,
            event_type="integration.started",
            context_snapshot_id=context_snapshot_id,
        )

    def merge_candidate(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        expected_version: int,
    ) -> dict[str, Any]:
        """Gate a QA-verified candidate on an isolated regression run, then fast-forward.

        Neither the regression command nor the git fast-forward holds the SQLite
        write lock: only the initial read and the final outcome are short, atomic
        mutations (see HOH-214). A failing or mismatched gate leaves the default
        branch and the frozen candidate untouched; a passing gate falls through to
        the unchanged Integrator-only fast-forward and base-drift guard.
        """
        require_role(actor, Role.INTEGRATOR)
        with self.store.connection() as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] != TicketStatus.MERGE_READY.value:
                raise ConflictError("Ticket is not ready for integration")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            repository = _row(
                connection.execute(
                    "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            candidate = self._current_integration_candidate(connection, ticket)
            if repository is None:
                raise ValidationError("Project repository is required before integrating a candidate")
            gate_profile = _row(
                connection.execute(
                    "SELECT * FROM candidate_test_profiles WHERE candidate_id = ?",
                    (candidate["candidate_id"],),
                ).fetchone()
            )
            repository_path = repository["repository_path"]
            default_branch = project["default_branch"]
            candidate_id = candidate["candidate_id"]
            candidate_commit = candidate["commit_sha"]
            candidate_base = candidate["base_commit"]
            gate_command: list[str] | None = None
            gate_timeout = 0
            if gate_profile is not None:
                gate_command = gate_profile["test_command"]
                self._test_command(gate_command)
                gate_timeout = gate_profile["timeout_seconds"]
                if not isinstance(gate_timeout, int) or not 5 <= gate_timeout <= 1800:
                    raise ValidationError("Candidate test command timeout is invalid")

        # Neither this regression run nor the fast-forward below runs inside a write
        # transaction: a long-running external command must never hold the database
        # write lock and block every other Developer/QA/Planner operation (HOH-214).
        execution: TestExecution | None = None
        if gate_command is not None:
            with self.git_worktrees.integration_checkout(
                repository_path=repository_path,
                project_id=project_id,
                ticket_id=ticket_id,
                candidate_commit=candidate_commit,
            ) as integration_path:
                execution = self.project_tests.execute(gate_command, integration_path, gate_timeout)
            if execution.artifact_mismatch:
                raise ValidationError(
                    "Integration regression command did not resolve the candidate's own "
                    f"source: {execution.stderr}. This is an infrastructure fault, not a "
                    "candidate verdict; the ticket and lease are unchanged, retry once fixed."
                )

        if execution is not None and (
            execution.termination_reason in _INFRASTRUCTURE_OUTCOMES
            or execution.capture_errors or execution.stdout_truncated or execution.stderr_truncated
        ):
            raise ValidationError(
                f"Integration infrastructure failure: {execution.termination_reason}. "
                "Preserve the verified candidate and retry after infrastructure recovery; no source rework is requested."
            )
        gate_passed = execution is None or (not execution.timed_out and execution.exit_code == 0)
        merged = False
        mainline_commit = ""
        if gate_passed:
            merged, mainline_commit = self.git_worktrees.fast_forward_default_branch(
                repository_path=repository_path,
                default_branch=default_branch,
                expected_base_commit=candidate_base,
                candidate_commit=candidate_commit,
            )

        with self.store.connection(immediate=True) as connection:
            ticket, current_project_id = self._authorized_ticket(connection, actor, ticket_id)
            if (
                current_project_id != project_id
                or ticket["status"] != TicketStatus.MERGE_READY.value
            ):
                raise ConflictError("Ticket changed while integration was running")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            current_candidate = self._one(
                connection,
                "SELECT * FROM candidates WHERE candidate_id = ? AND ticket_id = ?",
                (candidate_id, ticket_id),
            )
            if current_candidate["commit_sha"] != candidate_commit:
                raise ConflictError("Candidate changed while integration was running")
            timestamp = _iso()
            next_version = ticket["version"] + 1
            gate_test_run: dict[str, Any] | None = None
            if execution is not None:
                gate_test_run = self._record_integration_gate(
                    connection,
                    actor,
                    project_id=project_id,
                    ticket=ticket,
                    candidate_id=candidate_id,
                    candidate_commit=candidate_commit,
                    execution=execution,
                    timestamp=timestamp,
                )

            if not gate_passed:
                outcome = "timed out" if execution.timed_out else f"exited {execution.exit_code}"
                reason = (
                    f"Integration regression {outcome} on the frozen candidate; mainline "
                    "was left unchanged. Repair the failure and submit a new QA candidate."
                )
                connection.execute("DELETE FROM leases WHERE lease_id = ?", (lease_id,))
                connection.execute(
                    """
                    INSERT INTO context_entries
                        (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                         source_ref, artifact_commit, actor_id, actor_role, created_at)
                    VALUES (?, ?, ?, ?, 'integration_regression_failed', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _id("ctx"),
                        project_id,
                        ticket["feature_id"],
                        ticket_id,
                        reason,
                        gate_test_run["storage_ref"],
                        candidate_commit,
                        actor.agent_id,
                        actor.role.value,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE tickets
                    SET status = ?, blocked_reason = ?, version = ?, updated_at = ?
                    WHERE ticket_id = ?
                    """,
                    (TicketStatus.REWORK.value, reason, next_version, timestamp, ticket_id),
                )
                self._event(
                    connection,
                    actor,
                    project_id,
                    "integration.regression_failed",
                    "ticket",
                    ticket_id,
                    next_version,
                    {
                        "candidate_id": candidate_id,
                        "test_run_id": gate_test_run["test_run_id"],
                        "exit_code": execution.exit_code,
                        "timed_out": execution.timed_out,
                    },
                )
                rework_ticket = self._ticket(connection, ticket_id)
                self._queue_ticket(connection, actor, project_id, rework_ticket)
                return {
                    "ticket": rework_ticket,
                    "merge_status": "regression_failed",
                    "test_run": gate_test_run,
                }

            connection.execute("DELETE FROM leases WHERE lease_id = ?", (lease_id,))
            if not merged:
                reason = (
                    "Mainline advanced after this candidate was based; rebase the candidate "
                    "and submit a new QA candidate."
                )
                connection.execute(
                    """
                    INSERT INTO context_entries
                        (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                         source_ref, artifact_commit, actor_id, actor_role, created_at)
                    VALUES (?, ?, ?, ?, 'integration_base_drift', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _id("ctx"),
                        project_id,
                        ticket["feature_id"],
                        ticket_id,
                        reason,
                        f"candidate:{candidate_id}",
                        mainline_commit,
                        actor.agent_id,
                        actor.role.value,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE tickets
                    SET status = ?, blocked_reason = ?, version = ?, updated_at = ?
                    WHERE ticket_id = ?
                    """,
                    (TicketStatus.REWORK.value, reason, next_version, timestamp, ticket_id),
                )
                self._event(
                    connection,
                    actor,
                    project_id,
                    "integration.base_drift",
                    "ticket",
                    ticket_id,
                    next_version,
                    {"candidate_id": candidate_id, "mainline_commit": mainline_commit},
                )
                rework_ticket = self._ticket(connection, ticket_id)
                self._queue_ticket(connection, actor, project_id, rework_ticket)
                return {
                    "ticket": rework_ticket,
                    "merge_status": "base_drift",
                    "test_run": gate_test_run,
                }
            connection.execute(
                """
                UPDATE tickets
                SET status = ?, blocked_reason = NULL, version = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (TicketStatus.DONE.value, next_version, timestamp, ticket_id),
            )
            self._event(
                connection,
                actor,
                project_id,
                "integration.completed",
                "ticket",
                ticket_id,
                next_version,
                {"candidate_id": candidate_id, "commit_sha": mainline_commit},
            )
            merged_ticket = self._ticket(connection, ticket_id)
            record = self._settled_record_inputs(connection, ticket_id, candidate_id)

        # Outside the write transaction: this runs Git, and a long external
        # command must never hold the database write lock.
        record_commit = self._write_semantic_record(
            repository_path=repository_path,
            default_branch=default_branch,
            ticket=merged_ticket,
            candidate=record["candidate"],
            findings=record["findings"],
            decisions=record["decisions"],
            mainline_commit=mainline_commit,
            supersedes=record["supersedes"],
        )
        return {
            "ticket": merged_ticket,
            "merge_status": "merged",
            "test_run": gate_test_run,
            "semantic_record_commit": record_commit,
        }

    def _settled_record_inputs(
        self, connection: Any, ticket_id: str, candidate_id: str
    ) -> dict[str, Any]:
        """Read the settled facts a record is made of, inside the transaction."""
        candidate = _row(
            connection.execute(
                "SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
        )
        findings = [
            _row(row)
            for row in connection.execute(
                """
                SELECT criterion, status, observation, evidence_refs FROM qa_findings
                WHERE candidate_id = ? ORDER BY created_at ASC
                """,
                (candidate_id,),
            ).fetchall()
        ]
        decisions = [
            _row(row)
            for row in connection.execute(
                """
                SELECT content FROM context_entries
                WHERE ticket_id = ? AND superseded_by IS NULL
                  AND entry_type IN ('planner_decision', 'design_decision')
                ORDER BY created_at ASC
                """,
                (ticket_id,),
            ).fetchall()
        ]
        prior = _row(
            connection.execute(
                """
                SELECT commit_sha FROM candidates
                WHERE ticket_id = ? AND candidate_id != ?
                ORDER BY frozen_at DESC LIMIT 1
                """,
                (ticket_id, candidate_id),
            ).fetchone()
        )
        return {
            "candidate": candidate,
            "findings": findings,
            "decisions": decisions,
            "supersedes": prior["commit_sha"] if prior else None,
        }

    def _write_semantic_record(
        self,
        *,
        repository_path: str,
        default_branch: str,
        ticket: dict[str, Any],
        candidate: dict[str, Any],
        findings: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        mainline_commit: str,
        supersedes: str | None,
    ) -> str:
        """Land the record, or fail the merge rather than report a clean success.

        A reader must never be told a record exists when it does not, so a write
        that cannot complete surfaces as the merge outcome rather than being
        swallowed as a cosmetic afterthought.
        """
        body = semantic_record.render(
            ticket=ticket,
            candidate=candidate,
            findings=findings,
            decisions=decisions,
            mainline_commit=mainline_commit,
            supersedes=supersedes,
        )
        semantic_record.assert_carries_nothing_local(
            body,
            [
                str(candidate.get("branch_ref") or ""),
                str(candidate.get("submitted_by") or ""),
                repository_path,
            ],
        )
        try:
            return self.git_worktrees.commit_semantic_record(
                repository_path=repository_path,
                default_branch=default_branch,
                relative_path=semantic_record.record_path(ticket["ticket_id"]),
                body=body,
                message=(
                    f"docs({ticket['ticket_id']}): record what this change was for"
                ),
            )
        except (DomainError, OSError, ValueError) as error:
            raise ValidationError(
                "The candidate merged, but its semantic record could not be written: "
                f"{error}. The default branch has moved; record the outcome before "
                "reporting this merge as complete."
            ) from error

    def record_external_landing(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        landed_commit: str,
        reason: str,
    ) -> dict[str, Any]:
        """Record that a ticket's work is already on the default branch.

        This exists because work does land outside the gate — a hotfix, an
        emergency merge, a revert applied by hand — and a board that cannot say so
        reports finished work as outstanding forever. It records history and
        certifies nothing: no candidate becomes verified, no criterion becomes
        satisfied, and every surface it touches says the work did not pass the
        integration gate.
        """
        require_role(actor, Role.INTEGRATOR, Role.ADMIN)
        self._valid_commit(landed_commit, "landed_commit")
        self._bounded_text(reason, "reason", 2000)
        with self.store.connection() as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if TicketStatus(ticket["status"]) in CLOSED_TICKET_STATUSES or (
                ticket["status"] == TicketStatus.DONE.value
            ):
                raise ConflictError("Ticket has already reached a terminal state")
            if connection.execute(
                "SELECT 1 FROM leases WHERE ticket_id = ? AND expires_at > ?",
                (ticket_id, _iso()),
            ).fetchone():
                raise ConflictError(
                    "Work in flight must finish or be released before its outcome is recorded",
                    {"reason": "landing_lease_active"},
                )
            for table in ("merge_jobs", "qa_test_jobs", "candidate_submission_jobs"):
                if connection.execute(
                    f"SELECT 1 FROM {table} WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING')",
                    (ticket_id,),
                ).fetchone():
                    raise ConflictError(
                        "A job is still running for this ticket",
                        {"reason": "landing_job_active"},
                    )
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            repository = _row(
                connection.execute(
                    "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            if repository is None:
                raise ValidationError(
                    "Project repository is required before recording an external landing"
                )
            repository_path = repository["repository_path"]
            default_branch = project["default_branch"]

        # Outside the transaction: this asks Git, and Git does not belong inside
        # the write lock.
        if not self.git_worktrees.commit_is_on_branch(
            repository_path=repository_path,
            default_branch=default_branch,
            commit=landed_commit,
        ):
            raise ValidationError(
                "That commit is not reachable from the default branch, so this work "
                "has not landed. Nothing was recorded.",
                {"reason": "landing_commit_not_on_branch"},
            )

        with self.store.connection(immediate=True) as connection:
            ticket, current_project_id = self._authorized_ticket(connection, actor, ticket_id)
            if current_project_id != project_id:
                raise ConflictError("Ticket changed while its landing was being verified")
            self._expect_version(ticket, expected_version)
            timestamp = _iso()
            next_version = ticket["version"] + 1
            # Said in full, on the ticket, where anyone reading its history will
            # see it beside the state change rather than having to infer it.
            note = (
                "Recorded as landed outside the integration gate. This work is on "
                f"the default branch at {landed_commit}, but it did not pass the "
                "gate and carries no QA verdict: nothing here verifies a candidate "
                f"or satisfies an acceptance criterion. Reason given: {reason}"
            )
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     artifact_commit, actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'integration_landed_ungated', ?, ?, ?, ?, ?)
                """,
                (
                    _id("ctx"), project_id, ticket["feature_id"], ticket_id, note,
                    landed_commit, actor.agent_id, actor.role.value, timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE tickets SET status = ?, blocked_reason = NULL, version = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (TicketStatus.DONE.value, next_version, timestamp, ticket_id),
            )
            self._event(
                connection, actor, project_id,
                # A distinct event type, so no consumer can count this as a gated
                # merge by reading integration.merged.
                "integration.landed_ungated", "ticket", ticket_id, next_version,
                {"landed_commit": landed_commit, "gated": False, "qa_verified": False},
            )
            self._close_ticket_dispatch(connection, ticket_id, timestamp)
            # An expired lease is no longer an owner. Leaving it behind lets an
            # older recovery path put already-landed work back on the queue.
            connection.execute("DELETE FROM leases WHERE ticket_id = ?", (ticket_id,))
            return {
                "ticket": self._ticket(connection, ticket_id),
                "merge_status": "landed_ungated",
                "gated": False,
            }

    def _record_integration_gate(
        self,
        connection: Connection,
        actor: Actor,
        *,
        project_id: str,
        ticket: dict[str, Any],
        candidate_id: str,
        candidate_commit: str,
        execution: TestExecution,
        timestamp: str,
    ) -> dict[str, Any]:
        """Persist the gate run as durable candidate evidence before mainline is touched."""
        test_run_id = _id("testrun")
        storage_ref, report_path, checksum = self.project_tests.write_report(
            candidate_id=candidate_id,
            test_run_id=test_run_id,
            execution=execution,
            candidate_commit=candidate_commit,
        )
        connection.execute(
            """
            INSERT INTO qa_test_runs
                (test_run_id, candidate_id, ticket_id, command_digest, exit_code, timed_out,
                 duration_ms, storage_ref, report_path, environment_digest,
                 storage_checksum, executed_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                test_run_id,
                candidate_id,
                ticket["ticket_id"],
                execution.command_digest,
                execution.exit_code,
                int(execution.timed_out),
                execution.duration_ms,
                storage_ref,
                report_path,
                execution.environment_digest,
                checksum,
                actor.agent_id,
                timestamp,
            ),
        )
        outcome = "timed out" if execution.timed_out else f"exited {execution.exit_code}"
        self._event(
            connection,
            actor,
            project_id,
            "integration.regression_completed",
            "ticket",
            ticket["ticket_id"],
            ticket["version"],
            {
                "candidate_id": candidate_id,
                "test_run_id": test_run_id,
                "candidate_commit": candidate_commit,
                "exit_code": execution.exit_code,
                "timed_out": execution.timed_out,
                "outcome": outcome,
            },
        )
        return self._one(
            connection, "SELECT * FROM qa_test_runs WHERE test_run_id = ?", (test_run_id,)
        )

    def _release_policy_snapshot(
        self, actor: Actor, ticket_id: str
    ) -> dict[str, Any]:
        """Read everything the gate judges, without holding a write lock."""
        with self.store.connection() as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            repository = _row(
                connection.execute(
                    "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            candidate = self._current_integration_candidate(connection, ticket)
            candidate_id = candidate["candidate_id"]
            findings = [
                _row(row)
                for row in connection.execute(
                    "SELECT * FROM qa_findings WHERE candidate_id = ?", (candidate_id,)
                ).fetchall()
            ]
            # One verdict writes its complete finding batch with one timestamp.
            # A fresh full QA pass after infra recovery supersedes the earlier
            # evidence gap for gate decisions; keep every historical row intact.
            if findings:
                latest_verdict = max(finding["created_at"] for finding in findings)
                findings = [finding for finding in findings if finding["created_at"] == latest_verdict]
            current_evidence_ids = {ref for finding in findings for ref in finding["evidence_refs"]}
            evidence = [
                _row(row)
                for row in connection.execute(
                    "SELECT * FROM evidence WHERE candidate_id = ?", (candidate_id,)
                ).fetchall()
                if row["evidence_id"] in current_evidence_ids
            ]
            test_runs = [
                _row(row)
                for row in connection.execute(
                    "SELECT * FROM qa_test_runs WHERE candidate_id = ? ORDER BY created_at ASC",
                    (candidate_id,),
                ).fetchall()
            ]
            profile = _row(
                connection.execute(
                    "SELECT * FROM candidate_test_profiles WHERE candidate_id = ?",
                    (candidate_id,),
                ).fetchone()
            )
            gates = [
                _row(row)
                for row in connection.execute(
                    """
                    SELECT * FROM context_entries
                    WHERE ticket_id = ? AND superseded_by IS NULL
                      AND entry_type IN ('integration_hold', 'integration_authorization')
                    ORDER BY created_at ASC
                    """,
                    (ticket_id,),
                ).fetchall()
            ]
            verified_event = connection.execute(
                """
                SELECT 1 FROM audit_events
                WHERE aggregate_id = ? AND event_type = 'ticket.verified'
                  AND payload LIKE ?
                """,
                (ticket_id, f'%"{candidate_id}"%'),
            ).fetchone()
        return {
            "ticket": ticket,
            "project": project,
            "project_id": project_id,
            "repository": repository,
            "candidate": candidate,
            "findings": findings,
            "evidence": evidence,
            "test_runs": test_runs,
            "profile": profile,
            "gates": gates,
            "qa_verified": verified_event is not None,
        }

    def _require_release_evidence(self, snapshot: dict[str, Any]) -> None:
        """Judge release eligibility from durable evidence only, never from claims."""
        ticket = snapshot["ticket"]
        candidate = snapshot["candidate"]
        if snapshot["repository"] is None:
            raise ValidationError("Project repository is required before integrating a candidate")
        if not snapshot["qa_verified"]:
            raise ReleasePolicyError(
                "This candidate has no recorded QA verification for its ticket",
                {"reason": "ticket_not_verified"},
            )

        unverified = sorted(
            {
                str(finding["status"])
                for finding in snapshot["findings"]
                if finding["status"] != "verified"
            }
        )
        if unverified:
            raise ReleasePolicyError(
                "The candidate carries QA findings that are not verified",
                {"reason": "unverified_qa_finding", "statuses": unverified},
            )
        evidenced = {
            (str(item["criterion"]))
            for item in snapshot["evidence"]
        }
        verified_criteria = {
            str(finding["criterion"])
            for finding in snapshot["findings"]
            if finding["status"] == "verified" and str(finding["criterion"]) in evidenced
        }
        missing = sorted(set(ticket["acceptance_criteria"]) - verified_criteria)
        if missing:
            raise ReleasePolicyError(
                "Every mandatory acceptance criterion needs candidate-bound QA evidence",
                {"reason": "missing_qa_evidence", "criteria": missing},
            )

        # A qa_test_runs row is only written for a run whose resolved source was
        # the frozen candidate, so its presence is the source-identity proof.
        profile = snapshot["profile"]
        if profile is None:
            raise ReleasePolicyError(
                "The candidate has no registered regression command to satisfy",
                {"reason": "regression_run_missing"},
            )
        digest = hashlib.sha256("\0".join(profile["test_command"]).encode()).hexdigest()
        matching = [run for run in snapshot["test_runs"] if run["command_digest"] == digest]
        if not matching:
            raise ReleasePolicyError(
                "No registered regression run was recorded against this candidate",
                {"reason": "regression_run_missing"},
            )
        if not any(run["exit_code"] == 0 and not run["timed_out"] for run in matching):
            raise ReleasePolicyError(
                "The registered regression run did not complete successfully",
                {"reason": "regression_run_failed"},
            )

        holds = [
            entry
            for entry in snapshot["gates"]
            if entry["entry_type"] == "integration_hold"
            and entry["artifact_commit"] in (None, candidate["commit_sha"])
        ]
        if holds:
            latest_hold = holds[-1]["created_at"]
            # Only an authorization naming this exact SHA can clear a hold; one
            # issued for another artifact says nothing about this candidate.
            cleared = any(
                entry["entry_type"] == "integration_authorization"
                and entry["artifact_commit"] == candidate["commit_sha"]
                and entry["created_at"] > latest_hold
                for entry in snapshot["gates"]
            )
            if not cleared:
                raise ReleasePolicyError(
                    "A current integration hold blocks this candidate",
                    {"reason": "integration_hold", "entry_id": holds[-1]["entry_id"]},
                )

    def _require_mergeable_identity(self, snapshot: dict[str, Any]) -> None:
        """Confirm the exact candidate still fast-forwards current main.

        Git runs here, deliberately outside any transaction, so no write lock is
        held while the repository is inspected.
        """
        repository_path = snapshot["repository"]["repository_path"]
        default_branch = snapshot["project"]["default_branch"]
        candidate = snapshot["candidate"]
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
        if not base_is_current_main:
            raise ReleasePolicyError(
                "Mainline moved after this candidate was based",
                {"reason": "base_drift"},
            )
        if not self.git_worktrees.commit_descends_from(
            repository_path=repository_path,
            ancestor_commit=mainline,
            descendant_commit=candidate["commit_sha"],
        ):
            raise ReleasePolicyError(
                "This candidate would not fast-forward the default branch",
                {"reason": "not_fast_forward"},
            )

    def queue_merge_candidate(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        expected_version: int,
        context_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        """Durably acknowledge integration before the detached Git merge worker runs."""
        require_role(actor, Role.INTEGRATOR)
        # The standing release policy is judged before anything is queued, and the
        # Git half runs without a write lock held.
        snapshot = self._release_policy_snapshot(actor, ticket_id)
        if snapshot["ticket"]["status"] == TicketStatus.MERGE_READY.value:
            self._require_release_evidence(snapshot)
            self._require_mergeable_identity(snapshot)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            self.require_fresh_context(connection, ticket_id, context_snapshot_id)
            if ticket["status"] != TicketStatus.MERGE_READY.value:
                raise ConflictError("Ticket is not ready for integration")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            repository = _row(
                connection.execute(
                    "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            if repository is None:
                raise ValidationError("Project repository is required before integrating a candidate")
            candidate = self._current_integration_candidate(connection, ticket)
            if candidate["candidate_id"] != snapshot["candidate"]["candidate_id"]:
                raise ConflictError("Candidate changed after integration policy validation")
            active = _row(
                connection.execute(
                    """SELECT * FROM merge_jobs
                       WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING')
                       ORDER BY created_at DESC LIMIT 1""",
                    (ticket_id,),
                ).fetchone()
            )
            if active is not None:
                if (
                    active["requested_by"] == actor.agent_id
                    and active["lease_id"] == lease_id
                    and active["candidate_id"] == candidate["candidate_id"]
                ):
                    return {
                        "ticket": ticket,
                        "merge_job": self._merge_job(connection, active["merge_job_id"]),
                        "reused": True,
                    }
                raise ConflictError("Ticket already has an active merge job")
            merge_job_id = _id("merge_job")
            timestamp = _iso()
            connection.execute(
                """INSERT INTO merge_jobs
                   (merge_job_id, project_id, ticket_id, lease_id, expected_version, candidate_id,
                    requested_by, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?)""",
                (
                    merge_job_id, project_id, ticket_id, lease_id, expected_version,
                    candidate["candidate_id"], actor.agent_id, timestamp,
                ),
            )
            self._event(
                connection, actor, project_id, "integration.merge_queued", "ticket",
                ticket_id, ticket["version"], {"merge_job_id": merge_job_id},
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "merge_job": self._merge_job(connection, merge_job_id),
                "reused": False,
            }

    def get_merge_candidate(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        merge_job_id: str,
        lease_seconds: int = 900,
    ) -> dict[str, Any]:
        """Poll one merge job and renew the caller's Integrator lease while active."""
        require_role(actor, Role.INTEGRATOR)
        if not 30 <= lease_seconds <= 3600:
            raise ValidationError("Lease duration must be between 30 and 3600 seconds")
        with self.store.connection(immediate=True) as connection:
            ticket, _project_id = self._authorized_ticket(connection, actor, ticket_id)
            job = self._merge_job(connection, merge_job_id)
            if job["ticket_id"] != ticket_id or job["lease_id"] != lease_id:
                raise AuthorizationError("Merge job does not belong to this Integrator lease")
            if job["requested_by"] != actor.agent_id:
                raise AuthorizationError("Merge job belongs to another Integrator")
            lease: dict[str, Any] | None = None
            if job["status"] in {"QUEUED", "RUNNING"}:
                if ticket["status"] != TicketStatus.MERGE_READY.value:
                    raise ConflictError("Ticket is not ready for integration")
                self._require_active_lease(connection, actor, ticket_id, lease_id)
                timestamp = _now()
                connection.execute(
                    "UPDATE leases SET heartbeat_at = ?, expires_at = ? WHERE lease_id = ?",
                    (_iso(timestamp), _iso(timestamp + timedelta(seconds=lease_seconds)), lease_id),
                )
                lease = self._lease(connection, lease_id)
            return {
                "ticket": self._ticket(connection, ticket_id),
                "lease": lease,
                "merge_job": self._merge_job(connection, merge_job_id),
            }

    def execute_queued_merge(self, merge_job_id: str) -> dict[str, Any]:
        """Serialize the Git/DB boundary with recovery using an OS-owned lock."""
        with self.store.connection() as connection:
            job = self._merge_job(connection, merge_job_id)
        if job['status'] != 'QUEUED':
            return job
        repository, _, _ = self._merge_git_inputs(merge_job_id)
        if repository is None:
            return self.fail_queued_merge(merge_job_id, 'Project repository is required')
        try:
            with self.git_worktrees.repository_lock(repository['repository_path']):
                return self._execute_queued_merge_locked(merge_job_id)
        except ConflictError:
            with self.store.connection() as connection:
                return self._merge_job(connection, merge_job_id)

    def _execute_queued_merge_locked(self, merge_job_id: str) -> dict[str, Any]:
        preflight_error: str | None = None
        with self.store.connection(immediate=True) as connection:
            job = self._merge_job(connection, merge_job_id)
            if job["status"] != "QUEUED":
                return job
            actor = Actor(job["requested_by"], Role.INTEGRATOR, frozenset({job["project_id"]}))
            ticket = self._ticket(connection, job["ticket_id"])
            try:
                if ticket["status"] != TicketStatus.MERGE_READY.value:
                    raise ConflictError(
                        "Ticket is no longer ready for integration before the merge worker started"
                    )
                self._expect_version(ticket, job["expected_version"])
                self._require_active_lease(connection, actor, job["ticket_id"], job["lease_id"])
            except DomainError as error:
                preflight_error = str(error)
            if preflight_error is None:
                repository = self._one(connection, 'SELECT * FROM project_repositories WHERE project_id = ?', (job['project_id'],))
                project = self._one(connection, 'SELECT * FROM projects WHERE project_id = ?', (job['project_id'],))
                candidate = self._one(connection, 'SELECT * FROM candidates WHERE candidate_id = ?', (job['candidate_id'],))
                if ticket['candidate_commit'] != candidate['commit_sha']:
                    raise ConflictError('Merge candidate no longer matches the ticket')
                connection.execute('''INSERT INTO merge_intents
                    (merge_job_id, repository_path, default_branch, base_commit, candidate_commit, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)''', (merge_job_id, repository['repository_path'],
                    project['default_branch'], candidate['base_commit'], candidate['commit_sha'], _iso()))
                connection.execute(
                    "UPDATE merge_jobs SET status = 'RUNNING', started_at = ? WHERE merge_job_id = ?",
                    (_iso(), merge_job_id),
                )
        if preflight_error is not None:
            return self.fail_queued_merge(merge_job_id, preflight_error)
        try:
            snapshot = self._release_policy_snapshot(actor, job["ticket_id"])
            self._require_release_evidence(snapshot)
            if snapshot["candidate"]["candidate_id"] != job["candidate_id"]:
                raise ConflictError("Queued candidate is no longer the current policy snapshot")
            profile = snapshot["profile"]
            command = profile["test_command"]
            self._test_command(command)
            with self.git_worktrees.integration_checkout(
                repository_path=repository["repository_path"], project_id=job["project_id"],
                ticket_id=job["ticket_id"], candidate_commit=candidate["commit_sha"],
            ) as integration_path:
                execution = self.project_tests.execute(command, integration_path, profile["timeout_seconds"])
            execution = replace(execution, environment_manifest={**execution.environment_manifest,
                "merge_job_id": merge_job_id,
                "frozen_profile": {"configured_at": profile["configured_at"],
                    "timeout_seconds": profile["timeout_seconds"],
                    "command_digest": hashlib.sha256("\0".join(command).encode()).hexdigest()}})
            with self.store.connection(immediate=True) as connection:
                gate_run = self._record_integration_gate(connection, actor, project_id=job["project_id"],
                    ticket=ticket, candidate_id=job["candidate_id"], candidate_commit=candidate["commit_sha"],
                    execution=execution, timestamp=_iso())
            if (execution.artifact_mismatch or execution.termination_reason in _INFRASTRUCTURE_OUTCOMES
                    or execution.capture_errors or execution.stdout_truncated or execution.stderr_truncated):
                return self.fail_queued_merge(merge_job_id,
                    f"Integration infrastructure failure: {execution.termination_reason}; preserve candidate and retry after remediation. No source rework.")
            if execution.exit_code != 0 or execution.timed_out:
                return self._complete_queued_merge(merge_job_id, False, candidate["base_commit"],
                    regression_failure=f"Integration regression failed; inspect {gate_run['storage_ref']} and submit corrected source for independent QA.")
            # A hold or revoked lease during a long gate must still prevent Git.
            current = self._release_policy_snapshot(actor, job["ticket_id"])
            self._require_release_evidence(current)
            if current["candidate"]["candidate_id"] != job["candidate_id"]:
                raise ConflictError("Candidate changed while the integration gate ran")
            with self.store.connection(immediate=True) as connection:
                current_ticket = self._ticket(connection, job["ticket_id"])
                self._expect_version(current_ticket, job["expected_version"])
                self._require_active_lease(connection, actor, job["ticket_id"], job["lease_id"])
                self._event(connection, actor, job["project_id"], "integration.gate_passed", "merge_job",
                    merge_job_id, job["expected_version"], {"candidate_id": job["candidate_id"],
                        "candidate_commit": candidate["commit_sha"], "test_run_id": gate_run["test_run_id"]})
        except DomainError as error:
            return self.fail_queued_merge(merge_job_id, str(error))
        try:
            merged, mainline_commit = self.git_worktrees.fast_forward_default_branch(
                repository_path=repository["repository_path"],
                default_branch=project["default_branch"],
                expected_base_commit=candidate["base_commit"],
                candidate_commit=candidate["commit_sha"],
            )
            return self._complete_queued_merge(merge_job_id, merged, mainline_commit)
        except DomainError as error:
            # Git may already have changed. Preserve the intent and reconcile
            # facts instead of turning an uncertain side effect into a retry.
            self._merge_needs_attention(merge_job_id, str(error))
            return self._reconcile_merge_locked(merge_job_id)

    def _queued_gate_is_recorded(self, connection: Connection, job: dict[str, Any]) -> bool:
        proof = _row(connection.execute(
            "SELECT * FROM audit_events WHERE event_type='integration.gate_passed' AND aggregate_id=? ORDER BY sequence DESC LIMIT 1",
            (job["merge_job_id"],)).fetchone())
        if proof is None or proof["payload"].get("candidate_id") != job["candidate_id"]:
            return False
        return connection.execute(
            "SELECT 1 FROM qa_test_runs WHERE test_run_id=? AND candidate_id=? AND exit_code=0 AND timed_out=0",
            (proof["payload"].get("test_run_id"), job["candidate_id"])).fetchone() is not None

    def reconcile_merge_job(self, actor: Actor, merge_job_id: str) -> dict[str, Any]:
        require_role(actor, Role.ADMIN)
        with self.store.connection() as connection:
            job = self._merge_job(connection, merge_job_id)
            self._authorized_ticket(connection, actor, job['ticket_id'])
            intent = _row(connection.execute('SELECT * FROM merge_intents WHERE merge_job_id = ?', (merge_job_id,)).fetchone())
        if job['status'] != 'RUNNING':
            return job
        if intent is None:
            return self._merge_needs_attention(merge_job_id, 'Missing durable merge intent; operator reconciliation required')
        try:
            with self.git_worktrees.repository_lock(intent['repository_path']):
                return self._reconcile_merge_locked(merge_job_id)
        except ConflictError:
            return job  # A live operation owns this repository. Never race it.
        except (DomainError, OSError) as error:
            return self._merge_needs_attention(merge_job_id, str(error))

    def _merge_needs_attention(self, merge_job_id: str, message: str) -> dict[str, Any]:
        with self.store.connection(immediate=True) as connection:
            connection.execute("UPDATE merge_jobs SET error_message = ? WHERE merge_job_id = ? AND status = 'RUNNING'",
                               ('Reconciliation required: ' + message[:3500], merge_job_id))
            return self._merge_job(connection, merge_job_id)

    def _reconcile_merge_locked(self, merge_job_id: str) -> dict[str, Any]:
        with self.store.connection() as connection:
            job = self._merge_job(connection, merge_job_id)
            intent = _row(connection.execute('SELECT * FROM merge_intents WHERE merge_job_id = ?', (merge_job_id,)).fetchone())
        if job['status'] != 'RUNNING' or intent is None:
            return job
        repository, project, candidate = self._merge_git_inputs(merge_job_id)
        if (repository is None or repository['repository_path'] != intent['repository_path']
                or project['default_branch'] != intent['default_branch']
                or candidate['commit_sha'] != intent['candidate_commit']
                or candidate['base_commit'] != intent['base_commit']):
            return self._merge_needs_attention(merge_job_id, 'Repository or candidate configuration changed')
        from pathlib import Path
        path = Path(intent['repository_path'])
        if self.git_worktrees._git(path, 'status', '--porcelain').stdout.strip():
            return self._merge_needs_attention(merge_job_id, 'Repository has uncommitted work; it was preserved')
        head = self.git_worktrees._resolve_commit(path, 'refs/heads/' + intent['default_branch'])
        if self.git_worktrees.commit_is_on_branch(repository_path=str(path), default_branch=intent['default_branch'], commit=intent['candidate_commit']):
            with self.store.connection() as connection:
                gate_recorded = self._queued_gate_is_recorded(connection, job)
            if not gate_recorded:
                return self._merge_needs_attention(merge_job_id, "No passed gate evidence is bound to this merge job; preserve external movement for operator review")
            try:
                return self._complete_queued_merge(merge_job_id, True, intent['candidate_commit'], recovered=True)
            except DomainError as error:
                return self._merge_needs_attention(merge_job_id, str(error))
        if head == intent['base_commit']:
            reason = job['error_message']
            if reason:
                reason = reason.removeprefix('Reconciliation required: ')
            return self.fail_queued_merge(merge_job_id, reason or 'Recovered interrupted merge: mainline is unchanged; a new authorized job is required')
        return self._merge_needs_attention(merge_job_id, 'Mainline diverged from both the authorized base and candidate')

    def _merge_git_inputs(
        self, merge_job_id: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
        with self.store.connection() as connection:
            job = self._merge_job(connection, merge_job_id)
            repository = _row(
                connection.execute(
                    "SELECT * FROM project_repositories WHERE project_id = ?", (job["project_id"],)
                ).fetchone()
            )
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (job["project_id"],)
            )
            candidate = self._one(
                connection, "SELECT * FROM candidates WHERE candidate_id = ?", (job["candidate_id"],)
            )
            return repository, project, candidate

    def _complete_queued_merge(
        self, merge_job_id: str, merged: bool, mainline_commit: str, *, recovered: bool = False,
        regression_failure: str | None = None,
    ) -> dict[str, Any]:
        with self.store.connection(immediate=True) as connection:
            job = self._merge_job(connection, merge_job_id)
            if job["status"] != "RUNNING":
                return job
            actor = Actor(job["requested_by"], Role.INTEGRATOR, frozenset({job["project_id"]}))
            ticket = self._ticket(connection, job["ticket_id"])
            if ticket["status"] != TicketStatus.MERGE_READY.value:
                raise ConflictError("Ticket changed while the merge worker ran")
            self._expect_version(ticket, job["expected_version"])
            if not recovered:
                self._require_active_lease(connection, actor, job["ticket_id"], job["lease_id"])
            elif not merged or connection.execute('SELECT 1 FROM merge_intents WHERE merge_job_id = ? AND candidate_commit = ?',
                                                  (merge_job_id, mainline_commit)).fetchone() is None:
                raise ConflictError('Recovery requires the original merge intent')
            if ticket['candidate_commit'] != mainline_commit and merged:
                raise ConflictError('Recovery cannot replace a newer candidate')
            timestamp = _iso()
            next_version = ticket["version"] + 1
            connection.execute("DELETE FROM leases WHERE lease_id = ?", (job["lease_id"],))
            if not merged:
                reason = regression_failure or (
                    "Mainline advanced after this candidate was based; rebase the candidate "
                    "and submit a new QA candidate."
                )
                connection.execute(
                    """
                    INSERT INTO context_entries
                        (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                         source_ref, artifact_commit, actor_id, actor_role, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _id("ctx"), job["project_id"], ticket["feature_id"], job["ticket_id"],
                        "integration_regression_failed" if regression_failure else "integration_base_drift",
                        reason, f"candidate:{job['candidate_id']}", mainline_commit,
                        actor.agent_id, actor.role.value, timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE tickets
                    SET status = ?, blocked_reason = ?, version = ?, updated_at = ?
                    WHERE ticket_id = ?
                    """,
                    (TicketStatus.REWORK.value, reason, next_version, timestamp, job["ticket_id"]),
                )
                self._event(
                    connection, actor, job["project_id"],
                    "integration.regression_failed" if regression_failure else "integration.base_drift", "ticket",
                    job["ticket_id"], next_version,
                    {"candidate_id": job["candidate_id"], "mainline_commit": mainline_commit},
                )
                rework_ticket = self._ticket(connection, job["ticket_id"])
                self._queue_ticket(connection, actor, job["project_id"], rework_ticket)
                # A drifted candidate is a planning problem as much as a rework
                # item: the Planner decides whether to rebase, re-scope or drop it.
                self._open_planner_job(
                    connection,
                    actor,
                    job["project_id"],
                    rework_ticket,
                    job_type=PlannerJobType.INTEGRATION_CONFLICT,
                    summary=reason,
                    evidence_refs=[
                        f"candidate:{job['candidate_id']}",
                        f"merge_job:{merge_job_id}",
                    ],
                    candidate_id=job["candidate_id"],
                    artifact_commit=mainline_commit,
                )
                connection.execute(
                    """UPDATE merge_jobs
                       SET status = ?, merge_status = ?, mainline_commit = ?,
                           error_message = ?, completed_at = ?
                       WHERE merge_job_id = ?""",
                    ("FAILED" if regression_failure else "COMPLETED",
                     None if regression_failure else "base_drift", mainline_commit,
                     regression_failure, timestamp, merge_job_id),
                )
                return self._merge_job(connection, merge_job_id)
            connection.execute(
                """
                UPDATE tickets
                SET status = ?, blocked_reason = NULL, version = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (TicketStatus.DONE.value, next_version, timestamp, job["ticket_id"]),
            )
            self._event(
                connection, actor, job["project_id"], "integration.completed", "ticket",
                job["ticket_id"], next_version,
                {"candidate_id": job["candidate_id"], "commit_sha": mainline_commit},
            )
            connection.execute(
                """UPDATE merge_jobs
                   SET status = 'COMPLETED', merge_status = 'merged', mainline_commit = ?,
                       error_message = NULL, completed_at = ?
                   WHERE merge_job_id = ?""",
                (mainline_commit, timestamp, merge_job_id),
            )
            return self._merge_job(connection, merge_job_id)

    def fail_queued_merge(self, merge_job_id: str, error_message: str) -> dict[str, Any]:
        """Terminally record a merge worker failure without blocking Integrator polling."""
        self._bounded_text(error_message, "merge_job.error_message", 4000)
        with self.store.connection(immediate=True) as connection:
            job = self._merge_job(connection, merge_job_id)
            if job["status"] in {"COMPLETED", "FAILED"}:
                return job
            connection.execute(
                """UPDATE merge_jobs SET status = 'FAILED', error_message = ?, completed_at = ?
                   WHERE merge_job_id = ?""",
                (error_message, _iso(), merge_job_id),
            )
            actor = Actor(job["requested_by"], Role.INTEGRATOR, frozenset({job["project_id"]}))
            ticket = self._ticket(connection, job["ticket_id"])
            self._event(
                connection, actor, job["project_id"], "integration.merge_failed", "ticket",
                job["ticket_id"], ticket["version"], {"merge_job_id": merge_job_id},
            )
            return self._merge_job(connection, merge_job_id)

    def _merge_job(self, connection: Connection, merge_job_id: str) -> dict[str, Any]:
        return self._one(
            connection, "SELECT * FROM merge_jobs WHERE merge_job_id = ?", (merge_job_id,)
        )
