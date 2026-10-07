from __future__ import annotations

import json
from dataclasses import replace

from hoh_control_plane.domain import GitPreconditionError
from hoh_control_plane.service.qa import _INFRASTRUCTURE_OUTCOMES
from hoh_control_plane.service.run_guardrails import MergeQueueGuard
from hoh_control_plane.service._common import (
    _audit_event_has_field,
    require_snapshot_id,
    Actor,
    Any,
    AuthorizationError,
    CLOSED_TICKET_STATUSES,
    ConflictError,
    DomainError,
    MAX_AUTO_REBASES,
    MERGE_JOB_STUCK_SECONDS,
    PlannerJobType,
    ReleasePolicyError,
    Role,
    TicketStatus,
    ValidationError,
    _id,
    _iso,
    _now,
    _row,
    _transition_ticket,
    Path,
    datetime,
    hashlib,
    re,
    require_project,
    require_role,
    Connection,
    timedelta,
)


def candidate_commit_of(payload: Any) -> str | None:
    """The candidate commit an ``integration.gate_passed`` payload names."""
    import json

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    return payload.get("candidate_commit") if isinstance(payload, dict) else None


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

    def record_external_landing(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        landed_commit: str,
        reason: str,
        *,
        detection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record that a ticket's work is already on the default branch.

        This exists because work does land outside the gate — a hotfix, an
        emergency merge, a revert applied by hand — and a board that cannot say so
        reports finished work as outstanding forever. It records history and
        certifies nothing: no candidate becomes verified, no criterion becomes
        satisfied, and every surface it touches says the work did not pass the
        integration gate.
        """
        require_role(actor, Role.PLANNER, Role.INTEGRATOR, Role.ADMIN)
        landed_commit = self._full_commit(actor, ticket_id, landed_commit, "landed_commit")
        self._bounded_text(reason, "reason", 2000)
        with self.store.connection() as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            if actor.role is Role.PLANNER:
                pending = self._repository_reconciliation_state(connection, project_id)
                if not pending["repository_reconciliation_required"]:
                    raise ConflictError(
                        "Planner may record an external landing only while reconciling observed Git movement",
                        {"reason": "repository_reconciliation_not_pending"},
                    )
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
            if actor.role is Role.PLANNER:
                pending = self._repository_reconciliation_state(connection, project_id)
                if not pending["repository_reconciliation_required"]:
                    raise ConflictError(
                        "Repository reconciliation finished while the landing was being verified"
                    )
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
                    _id(connection, "ctx"), project_id, ticket["feature_id"], ticket_id, note,
                    landed_commit, actor.agent_id, actor.role.value, timestamp,
                ),
            )
            _transition_ticket(
                connection, ticket_id, actor=actor, expected_version=ticket["version"],
                from_statuses=(ticket["status"],),
                changes={"status": TicketStatus.DONE, "blocked_reason": None,
                         "updated_at": timestamp},
            )
            self._event(
                connection, actor, project_id,
                # A distinct event type, so no consumer can count this as a gated
                # merge by reading integration.merged.
                "integration.landed_ungated", "ticket", ticket_id, next_version,
                {"landed_commit": landed_commit, "gated": False, "qa_verified": False,
                 **({"detection": detection} if detection else {})},
            )
            self._close_ticket_dispatch(connection, ticket_id, timestamp)
            # An expired lease is no longer an owner. Leaving it behind lets an
            # older recovery path put already-landed work back on the queue.
            connection.execute("DELETE FROM leases WHERE ticket_id = ?", (ticket_id,))
            completed_ticket = self._ticket(connection, ticket_id)
            self._report_delivery_completed(
                connection,
                actor,
                project_id,
                completed_ticket,
                candidate_id=None,
                mainline_commit=landed_commit,
                gated=False,
            )
            # Main moved without the gate's suite: check it like any landing.
            mainline_check_id = self._queue_mainline_check(
                connection, project_id, mainline_commit=landed_commit,
                trigger_ref=f"landing:{ticket_id}", trigger_ticket_id=ticket_id,
            )
            return {
                "mainline_check_id": mainline_check_id,
                "ticket": completed_ticket,
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
        test_run_id = _id(connection, "testrun")
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
            # A derived candidate whose drift was disjoint carries the QA verdict
            # of the candidate it was rebased from; regression runs stay its own.
            evidence_candidate_id = candidate.get("qa_evidence_from") or candidate_id
            findings = [
                _row(row)
                for row in connection.execute(
                    "SELECT * FROM qa_findings WHERE candidate_id = ?", (evidence_candidate_id,)
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
                    "SELECT * FROM evidence WHERE candidate_id = ?", (evidence_candidate_id,)
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
            qa_verified = _audit_event_has_field(
                connection, ticket_id, "ticket.verified", "candidate_id", evidence_candidate_id
            )
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
            "qa_verified": qa_verified,
        }

    def _require_release_evidence(
        self, snapshot: dict[str, Any], *, require_regression_run: bool = True
    ) -> None:
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
        # A rebased candidate has no run of its own until its gate executes the
        # suite; the check after the gate (``_finish_gate_and_merge``) requires it.
        digest = hashlib.sha256("\0".join(profile["test_command"]).encode()).hexdigest()
        matching = [run for run in snapshot["test_runs"] if run["command_digest"] == digest]
        if not matching and require_regression_run:
            raise ReleasePolicyError(
                "No registered regression run was recorded against this candidate",
                {"reason": "regression_run_missing"},
            )
        if matching and not any(run["exit_code"] == 0 and not run["timed_out"] for run in matching):
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
        # A candidate someone already put on main is delivered work, not drift:
        # sending it to rework would ask for a change that has landed.
        if self.git_worktrees.commit_descends_from(
            repository_path=repository_path,
            ancestor_commit=candidate["commit_sha"],
            descendant_commit=mainline,
        ):
            raise ReleasePolicyError(
                "The candidate is already on the default branch; record the landing "
                "with record_external_landing instead of merging",
                {"reason": "candidate_already_on_main"},
            )
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
        context_snapshot_id = require_snapshot_id(context_snapshot_id, "integrator.merge_candidate")
        # The standing release policy is judged before anything is queued, and the
        # Git half runs without a write lock held.
        snapshot = self._release_policy_snapshot(actor, ticket_id)
        if snapshot["ticket"]["status"] == TicketStatus.MERGE_READY.value:
            self._require_release_evidence(snapshot)
            self._require_mergeable_identity(snapshot)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._require_repository_reconciled(connection, project_id, ticket_id)
            MergeQueueGuard.require_open(connection, project_id)
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
            # Risk tier gate (wave 7C3): medium needs adversarial review evidence,
            # high also needs policy.approve_high_risk on this candidate.
            self._require_risk_tier_cleared(connection, project_id, ticket, candidate)
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
            merge_job_id = _id(connection, "merge_job")
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

    def execute_queued_merge(self, merge_job_id: str, *, run_suite: bool = True) -> dict[str, Any]:
        """Serialize the Git/DB boundary with recovery using an OS-owned lock.

        ``run_suite=False`` is how the supervisor runs a merge it admitted outside
        the host's single test slot because its gate reuses a passing QA run. If
        that does not hold, the job is left QUEUED and untouched, so the next
        supervisor tick admits it through the test slot instead.
        """
        with self.store.connection() as connection:
            job = self._merge_job(connection, merge_job_id)
        if job['status'] != 'QUEUED':
            return job
        # A paused merge queue (red main, failure or hourly budget) holds the job
        # QUEUED and untouched; it starts once the queue is open again.
        if not self.merge_queue_admits(merge_job_id):
            return job
        if not run_suite and not self.merge_gate_reuses_qa_run(merge_job_id):
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
        outcome = "barrier_pending"
        with self.store.connection(immediate=True) as connection:
            job = self._merge_job(connection, merge_job_id)
            if job["status"] != "QUEUED":
                return job
            actor = Actor(job["requested_by"], Role.INTEGRATOR, frozenset({job["project_id"]}))
            ticket = self._ticket(connection, job["ticket_id"])
            try:
                self._require_repository_reconciled(connection, job["project_id"], job["ticket_id"])
                outcome = "ticket_changed"
                if ticket["status"] != TicketStatus.MERGE_READY.value:
                    raise ConflictError(
                        "Ticket is no longer ready for integration before the merge worker started"
                    )
                self._expect_version(ticket, job["expected_version"])
                outcome = "lease_expired"
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
            return self._fail_merge_precondition(merge_job_id, outcome, preflight_error)
        try:
            snapshot = self._release_policy_snapshot(actor, job["ticket_id"])
            # A rebased candidate carrying QA over gets its regression run from
            # this job's gate; ``_finish_gate_and_merge`` then requires it.
            self._require_release_evidence(
                snapshot, require_regression_run=not candidate.get("qa_evidence_from"))
            if snapshot["candidate"]["candidate_id"] != job["candidate_id"]:
                raise ConflictError("Queued candidate is no longer the current policy snapshot")
            profile = snapshot["profile"]
            command = profile["test_command"]
            self._test_command(command)
            # Fast-forward needs main at the frozen base. If main already moved,
            # the gate would run for nothing and the merge would drift anyway.
            current_main = self.git_worktrees._resolve_commit(
                Path(repository["repository_path"]), f"refs/heads/{project['default_branch']}")
            if current_main != candidate["base_commit"]:
                if self.git_worktrees.commit_descends_from(
                    repository_path=repository["repository_path"],
                    ancestor_commit=candidate["commit_sha"],
                    descendant_commit=current_main,
                ):
                    # Landed outside this job. Close the job without rework so the
                    # landing can be recorded instead of refused as landing_job_active.
                    return self.fail_queued_merge(
                        merge_job_id,
                        "candidate_already_on_main: the candidate is already on the default "
                        "branch; record the landing with record_external_landing.",
                    )
                return self._auto_rebase_drifted(
                    merge_job_id, job, actor, ticket, candidate, repository, project, current_main)
            reused = self._reusable_qa_run(job["candidate_id"], command, profile["timeout_seconds"])
        except DomainError as error:
            return self.fail_queued_merge(merge_job_id, str(error))
        if reused is not None:
            return self._finish_gate_and_merge(
                merge_job_id, job, actor, candidate, repository, project, reused, gate_mode="reused_qa_run")
        try:
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
                    regression_failure=(
                        f"Candidate configuration is invalid ({execution.stderr[:300]}); fix it and "
                        "submit corrected source for independent QA."
                        if execution.termination_reason == "candidate_config_invalid" else
                        f"Integration regression failed; inspect {gate_run['storage_ref']} and submit corrected source for independent QA."))
        except DomainError as error:
            return self.fail_queued_merge(merge_job_id, str(error))
        return self._finish_gate_and_merge(
            merge_job_id, job, actor, candidate, repository, project, gate_run["test_run_id"],
            gate_mode="executed_rebased" if candidate.get("derived_from") else "executed")

    def _auto_rebase_drifted(self, merge_job_id, job, actor, ticket, candidate, repository, project,
                             current_main: str) -> dict[str, Any]:
        """Main moved under a queued candidate: rebase it server-side instead of rework.

        The candidate's one commit is cherry-picked onto current main in a
        private integration checkout. A clean result becomes a new derived
        candidate (``derived_from``; parent = current main; same patch). When
        the paths main changed since the old base are disjoint, by scope root,
        from the candidate's paths, the QA verdict carries over
        (``qa_evidence_from``) and this same merge job is re-queued, so the
        normal gate executes the frozen suite on the rebased tree and the
        landing is a compare-and-set ref update. Otherwise the derived
        candidate goes to QA_READY. A conflict keeps today's rework path; the
        ``MAX_AUTO_REBASES``th + 1 drift blocks the ticket for Planner.
        Only drift onto a main HoH already observed is rebased: unobserved
        outside work still closes the project through the drift path.
        """
        repository_path = repository["repository_path"]
        with self.store.connection() as connection:
            rebases = connection.execute(
                "SELECT COUNT(*) AS n FROM candidates WHERE ticket_id = ? AND derived_from IS NOT NULL",
                (job["ticket_id"],),
            ).fetchone()["n"]
            observed = connection.execute(
                "SELECT observed_mainline_commit FROM project_repositories WHERE project_id = ?",
                (job["project_id"],),
            ).fetchone()
            candidate_paths = [row["path"] for row in connection.execute(
                "SELECT path FROM candidate_changed_paths WHERE candidate_id = ?",
                (candidate["candidate_id"],),
            ).fetchall()]
        if observed is None or observed["observed_mainline_commit"] != current_main:
            return self._complete_queued_merge(merge_job_id, False, current_main)
        if rebases >= MAX_AUTO_REBASES:
            return self._complete_queued_merge(
                merge_job_id, False, current_main, drift_budget_exhausted=True)
        derived_id_hint = f"{candidate['candidate_id']}-r{rebases + 1}"
        try:
            rebased = self.git_worktrees.rebase_candidate(
                repository_path=repository_path, project_id=job["project_id"],
                ticket_id=job["ticket_id"], candidate_commit=candidate["commit_sha"],
                onto_commit=current_main,
                keep_ref=f"refs/hoh/rebased/{job['ticket_id']}/{derived_id_hint}",
            )
            main_paths = self.git_worktrees.changed_paths(
                repository_path=repository_path, base_commit=candidate["base_commit"],
                candidate_commit=current_main)
            rebased_paths = None if rebased is None else self.git_worktrees.changed_paths(
                repository_path=repository_path, base_commit=current_main, candidate_commit=rebased)
        except DomainError as error:
            return self.fail_queued_merge(merge_job_id, str(error))
        if rebased is None:
            return self._complete_queued_merge(
                merge_job_id, False, current_main,
                drift_reason=("Mainline advanced after this candidate was based and its change "
                              "does not apply cleanly to current main; rebase it and submit a "
                              "new QA candidate."))
        shared = [path for path in main_paths
                  if "/" not in path or path.lower().startswith(self._SHARED_PATH_PREFIXES)]
        carry_qa = not shared and not any(
            self._scope_roots_overlap(left, right) for left in main_paths for right in candidate_paths)
        try:
            return self._record_rebased_candidate(
                merge_job_id, job, actor, candidate, current_main, rebased, rebased_paths,
                main_paths, carry_qa, rebases, derived_id_hint)
        except DomainError as error:
            return self.fail_queued_merge(merge_job_id, str(error))

    def _record_rebased_candidate(self, merge_job_id, job, actor, candidate, current_main, rebased,
                                  rebased_paths, main_paths, carry_qa, rebases,
                                  derived_id_hint) -> dict[str, Any]:
        with self.store.connection(immediate=True) as connection:
            current_job = self._merge_job(connection, merge_job_id)
            current_ticket = self._ticket(connection, job["ticket_id"])
            if (current_job["status"] != "RUNNING" or current_ticket["status"] != TicketStatus.MERGE_READY.value
                    or current_ticket["version"] != job["expected_version"]
                    or current_ticket["candidate_commit"] != candidate["commit_sha"]):
                raise ConflictError("Ticket or merge job changed while the candidate was rebased")
            self._require_active_lease(connection, actor, job["ticket_id"], job["lease_id"])
            timestamp = _iso()
            derived_id = _id(connection, "cand")
            connection.execute(
                """INSERT INTO candidates
                   (candidate_id, ticket_id, base_commit, commit_sha, branch_ref, developer_claims,
                    submitted_by, submitted_at, frozen_at, derived_from, qa_evidence_from)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (derived_id, job["ticket_id"], current_main, rebased,
                 f"refs/hoh/rebased/{job['ticket_id']}/{derived_id_hint}",
                 candidate["developer_claims"] if isinstance(candidate["developer_claims"], str)
                 else json.dumps(candidate["developer_claims"]),
                 actor.agent_id, timestamp, timestamp, candidate["candidate_id"],
                 (candidate.get("qa_evidence_from") or candidate["candidate_id"]) if carry_qa else None),
            )
            # The patch is still the Developer's: the derived candidate keeps the
            # original maker's family, never the Integrator's (wave 7C3).
            if candidate.get("maker_model_family"):
                connection.execute(
                    "UPDATE candidates SET maker_model_family = ?, maker_model = ? "
                    "WHERE candidate_id = ?",
                    (candidate["maker_model_family"], candidate.get("maker_model"), derived_id),
                )
            else:
                self._stamp_candidate_maker(connection, derived_id, candidate["submitted_by"])
            for path in rebased_paths:
                connection.execute(
                    "INSERT INTO candidate_changed_paths (candidate_id, path) VALUES (?, ?)",
                    (derived_id, path))
            connection.execute(
                """INSERT INTO candidate_test_profiles (candidate_id, test_command, timeout_seconds, configured_at)
                   SELECT ?, test_command, timeout_seconds, configured_at
                   FROM candidate_test_profiles WHERE candidate_id = ?""",
                (derived_id, candidate["candidate_id"]))
            payload = {"candidate_id": derived_id, "derived_from": candidate["candidate_id"],
                       "commit_sha": rebased, "base_commit": current_main,
                       "qa_reused": carry_qa, "main_changed_paths": main_paths[:50],
                       "auto_rebase": rebases + 1, "merge_job_id": merge_job_id}
            if carry_qa:
                next_version = _transition_ticket(
                    connection, job["ticket_id"], actor=actor, expected_version=current_ticket["version"],
                    from_statuses=(TicketStatus.MERGE_READY,),
                    changes={"candidate_commit": rebased, "base_commit": current_main,
                             "updated_at": timestamp})
                self._event(connection, actor, job["project_id"], "candidate.rebased", "ticket",
                            job["ticket_id"], next_version, payload)
                # Same job, same lease: the Integrator keeps polling it. The
                # supervisor re-admits it through the test slot, since the
                # derived candidate has no QA run of its own to reuse.
                connection.execute("DELETE FROM merge_intents WHERE merge_job_id = ?", (merge_job_id,))
                connection.execute(
                    """UPDATE merge_jobs SET status = 'QUEUED', started_at = NULL, candidate_id = ?,
                       expected_version = ? WHERE merge_job_id = ?""",
                    (derived_id, next_version, merge_job_id))
                return self._merge_job(connection, merge_job_id)
            next_version = _transition_ticket(
                connection, job["ticket_id"], actor=actor, expected_version=current_ticket["version"],
                from_statuses=(TicketStatus.MERGE_READY,),
                changes={"status": TicketStatus.QA_READY, "candidate_commit": rebased,
                         "base_commit": current_main, "blocked_reason": None, "updated_at": timestamp})
            self._event(connection, actor, job["project_id"], "candidate.rebased", "ticket",
                        job["ticket_id"], next_version, payload)
            connection.execute("DELETE FROM leases WHERE lease_id = ?", (job["lease_id"],))
            connection.execute(
                """UPDATE merge_jobs SET status = 'COMPLETED', merge_status = 'rebased_for_qa',
                   mainline_commit = ?, completed_at = ? WHERE merge_job_id = ?""",
                (current_main, timestamp, merge_job_id))
            self._queue_ticket(connection, actor, job["project_id"], self._ticket(connection, job["ticket_id"]))
            return self._merge_job(connection, merge_job_id)

    def merge_gate_reuses_qa_run(self, merge_job_id: str) -> bool:
        """Whether this merge's gate will reuse a QA run rather than run the suite.

        Read-only; the supervisor asks before admitting the job, so a merge that
        needs only Git does not wait behind a QA suite for the host's single test
        slot. The answer can only turn from False to True: a candidate's test
        profile is written once at submission and a completed passing QA run is
        never rewritten. An unknown job answers False, the conservative lane.
        """
        with self.store.connection() as connection:
            job = _row(connection.execute(
                "SELECT candidate_id FROM merge_jobs WHERE merge_job_id = ?", (merge_job_id,)
            ).fetchone())
            profile = None if job is None else _row(connection.execute(
                "SELECT test_command, timeout_seconds FROM candidate_test_profiles "
                "WHERE candidate_id = ?",
                (job["candidate_id"],),
            ).fetchone())
        if profile is None:
            return False
        return self._reusable_qa_run(
            job["candidate_id"], profile["test_command"], profile["timeout_seconds"]
        ) is not None

    def _reusable_qa_run(self, candidate_id: str, command: list[str], timeout_seconds: int) -> str | None:
        """A passing QA run of the frozen command on this exact candidate commit.

        Fast-forward only lands when main equals the candidate's base, so the tree
        that reaches main is the candidate commit QA already ran. Re-running the
        same command on the same commit costs a full suite and proves nothing new.
        Only a QA job (not an earlier gate) with the same digest and timeout counts.
        """
        digest = hashlib.sha256("\0".join(command).encode()).hexdigest()
        with self.store.connection() as connection:
            row = connection.execute(
                """
                SELECT r.test_run_id FROM qa_test_runs r
                JOIN qa_test_jobs j ON j.test_run_id = r.test_run_id
                WHERE r.candidate_id = ? AND r.command_digest = ? AND j.timeout_seconds = ?
                  AND r.exit_code = 0 AND r.timed_out = 0 AND j.status = 'COMPLETED'
                ORDER BY r.created_at DESC LIMIT 1
                """,
                (candidate_id, digest, timeout_seconds),
            ).fetchone()
        return None if row is None else row["test_run_id"]

    def _finish_gate_and_merge(self, merge_job_id, job, actor, candidate, repository, project,
                               test_run_id: str, *, gate_mode: str) -> dict[str, Any]:
        # A hold, barrier or lost lease during a long gate must still prevent
        # Git. Each is a precondition that changed before Git was touched, so
        # the job fails cleanly under its own outcome (review A7).
        outcome = "release_policy_changed"
        try:
            current = self._release_policy_snapshot(actor, job["ticket_id"])
            self._require_release_evidence(current)
            if current["candidate"]["candidate_id"] != job["candidate_id"]:
                raise ConflictError("Candidate changed while the integration gate ran")
            with self.store.connection(immediate=True) as connection:
                current_ticket = self._ticket(connection, job["ticket_id"])
                outcome = "barrier_pending"
                self._require_repository_reconciled(connection, job["project_id"], job["ticket_id"])
                outcome = "ticket_changed"
                self._expect_version(current_ticket, job["expected_version"])
                outcome = "lease_expired"
                self._require_active_lease(connection, actor, job["ticket_id"], job["lease_id"])
                self._event(connection, actor, job["project_id"], "integration.gate_passed", "merge_job",
                    merge_job_id, job["expected_version"], {"candidate_id": job["candidate_id"],
                        "candidate_commit": candidate["commit_sha"], "test_run_id": test_run_id,
                        "gate_mode": gate_mode})
        except DomainError as error:
            return self._fail_merge_precondition(merge_job_id, outcome, str(error))
        try:
            if candidate.get("derived_from"):
                # A server-rebased tree lands only by compare-and-set: Git
                # refuses the ref write if main moved since the gate's base.
                merged, mainline_commit = self.git_worktrees.cas_fast_forward(
                    repository_path=repository["repository_path"],
                    default_branch=project["default_branch"],
                    expected_old_commit=candidate["base_commit"],
                    new_commit=candidate["commit_sha"],
                )
            else:
                merged, mainline_commit = self.git_worktrees.fast_forward_default_branch(
                    repository_path=repository["repository_path"],
                    default_branch=project["default_branch"],
                    expected_base_commit=candidate["base_commit"],
                    candidate_commit=candidate["commit_sha"],
                )
            return self._complete_queued_merge(merge_job_id, merged, mainline_commit)
        except GitPreconditionError as error:
            # Refused before any write: nothing to reconcile. A dirty or
            # wrongly checked-out shared checkout is the host's to repair.
            return self.fail_queued_merge(
                merge_job_id,
                f"git_precondition_failed: {error}. Nothing changed in Git; repair the shared "
                "checkout, then resume the ticket for the Integrator. No source rework.",
            )
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
            retirement = _row(connection.execute(
                """SELECT * FROM ticket_retirement_requests
                   WHERE ticket_id = ? AND status = 'PENDING'""",
                (job["ticket_id"],),
            ).fetchone())
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
        if retirement is None and self.git_worktrees._git(path, 'status', '--porcelain').stdout.strip():
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
        if retirement is not None:
            return self.fail_queued_merge(
                merge_job_id,
                "Planner retired the ticket before its candidate reached the default branch; "
                f"observed mainline remains {head}.",
            )
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
        regression_failure: str | None = None, drift_reason: str | None = None,
        drift_budget_exhausted: bool = False,
    ) -> dict[str, Any]:
        with self.store.connection(immediate=True) as connection:
            job = self._merge_job(connection, merge_job_id)
            if job["status"] != "RUNNING":
                return job
            actor = Actor(job["requested_by"], Role.INTEGRATOR, frozenset({job["project_id"]}))
            ticket = self._ticket(connection, job["ticket_id"])
            retirement_pending = connection.execute(
                """SELECT 1 FROM ticket_retirement_requests
                   WHERE ticket_id = ? AND status = 'PENDING'""",
                (job["ticket_id"],),
            ).fetchone() is not None
            # Once main has moved, Git is the fact to record. A barrier that
            # opened during the gate must not strand the job RUNNING with the
            # candidate already on main; completion opens the next
            # reconciliation, so Planner still assesses this commit.
            if not merged and not (recovered and retirement_pending):
                self._require_repository_reconciled(connection, job["project_id"], job["ticket_id"])
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
                if drift_budget_exhausted:
                    drift_reason = (
                        f"drift_budget_exhausted: mainline moved again after {MAX_AUTO_REBASES} "
                        "automatic rebases; Planner decides whether to rebase, re-scope or drop it."
                    )
                reason = regression_failure or drift_reason or (
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
                        _id(connection, "ctx"), job["project_id"], ticket["feature_id"], job["ticket_id"],
                        "integration_regression_failed" if regression_failure else "integration_base_drift",
                        reason, f"candidate:{job['candidate_id']}", mainline_commit,
                        actor.agent_id, actor.role.value, timestamp,
                    ),
                )
                _transition_ticket(
                    connection, job["ticket_id"], actor=actor, expected_version=ticket["version"],
                    from_statuses=(TicketStatus.MERGE_READY,),
                    changes={"status": (TicketStatus.BLOCKED if drift_budget_exhausted
                                        else TicketStatus.REWORK),
                             "blocked_reason": reason, "updated_at": timestamp},
                )
                self._event(
                    connection, actor, job["project_id"],
                    "integration.regression_failed" if regression_failure else "integration.base_drift", "ticket",
                    job["ticket_id"], next_version,
                    {"candidate_id": job["candidate_id"], "mainline_commit": mainline_commit},
                )
                rework_ticket = self._ticket(connection, job["ticket_id"])
                # A gate regression leaves main untouched. Drift means main moved,
                # but when HoH had already observed main at this commit (a merge
                # or a sync recorded it) that movement opened its own
                # reconciliation; this ticket only needs a rebase. Either way only
                # the ticket and its dependents freeze. Drift onto a commit HoH
                # never observed is outside work arriving, and closes the project.
                observed_mainline = connection.execute(
                    "SELECT observed_mainline_commit FROM project_repositories WHERE project_id = ?",
                    (job["project_id"],),
                ).fetchone()
                ticket_scoped = bool(regression_failure) or (
                    observed_mainline is not None
                    and observed_mainline["observed_mainline_commit"] == mainline_commit
                )
                self._open_commit_reconciliation(
                    connection,
                    actor,
                    job["project_id"],
                    commit=(ticket["candidate_commit"] if regression_failure else mainline_commit),
                    subject=(
                        f"Integration gate rejected candidate {job['candidate_id']}"
                        if regression_failure else
                        f"Mainline moved before candidate {job['candidate_id']} could land"
                    ),
                    relation=("integration_gate_failed" if regression_failure else "integration_base_drift"),
                    observation_ref=f"merge_job:{merge_job_id}",
                    advance_mainline=not bool(regression_failure),
                    frozen_ticket_ids=(
                        self._frozen_ticket_closure(connection, job["ticket_id"])
                        if ticket_scoped else None
                    ),
                    rework_ticket_id=job["ticket_id"] if regression_failure else None,
                    resolved_by="system:gate-regression",
                )
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
                if regression_failure:
                    self._record_run_outcome(
                        connection, job["project_id"], succeeded=False, ticket=rework_ticket,
                        evidence_ref=f"merge_job:{merge_job_id}",
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
            _transition_ticket(
                connection, job["ticket_id"], actor=actor, expected_version=ticket["version"],
                from_statuses=(TicketStatus.MERGE_READY,),
                changes={"status": TicketStatus.DONE, "blocked_reason": None,
                         "updated_at": timestamp},
            )
            self._event(
                connection, actor, job["project_id"], "integration.completed", "ticket",
                job["ticket_id"], next_version,
                {"candidate_id": job["candidate_id"], "commit_sha": mainline_commit},
            )
            completed_ticket = self._ticket(connection, job["ticket_id"])
            self._report_delivery_completed(
                connection,
                actor,
                job["project_id"],
                completed_ticket,
                candidate_id=job["candidate_id"],
                mainline_commit=mainline_commit,
                merge_job_id=merge_job_id,
            )
            self._open_commit_reconciliation(
                connection,
                actor,
                job["project_id"],
                commit=mainline_commit,
                subject=f"Integrated candidate {job['candidate_id']}",
                relation="integrated",
                observation_ref=f"merge_job:{merge_job_id}",
                advance_mainline=True,
                merged_ticket_id=job["ticket_id"],
                changed_paths=[
                    row["path"] for row in connection.execute(
                        "SELECT path FROM candidate_changed_paths WHERE candidate_id = ?",
                        (job["candidate_id"],),
                    ).fetchall()
                ],
            )
            connection.execute(
                """UPDATE merge_jobs
                   SET status = 'COMPLETED', merge_status = 'merged', mainline_commit = ?,
                       error_message = NULL, completed_at = ?
                   WHERE merge_job_id = ?""",
                (mainline_commit, timestamp, merge_job_id),
            )
            self._record_run_outcome(connection, job["project_id"], succeeded=True)
            # A fast-forward makes main exactly the tree the gate just tested with
            # the frozen command, so that run is the check of main: recorded as
            # covered, not run twice in the one test slot. Anything else is queued.
            gate_covers_main = connection.execute(
                "SELECT payload FROM audit_events WHERE event_type = 'integration.gate_passed' "
                "AND aggregate_id = ? ORDER BY sequence DESC LIMIT 1",
                (merge_job_id,),
            ).fetchone()
            self._queue_mainline_check(
                connection, job["project_id"], mainline_commit=mainline_commit,
                trigger_ref=f"merge_job:{merge_job_id}", trigger_ticket_id=job["ticket_id"],
                covered_by_gate=bool(gate_covers_main) and mainline_commit == candidate_commit_of(
                    gate_covers_main["payload"]),
            )
            return self._merge_job(connection, merge_job_id)

    # A merge that stopped on one of these never reached Git. None is an
    # infrastructure fault or a source defect, so the ticket is not blocked
    # and no Planner job opens: the candidate and its QA verdict stand.
    _MERGE_PRECONDITION_NEXT = {
        "barrier_pending": "queue the merge again once Planner resolves the reconciliation.",
        "lease_expired": "the ticket returns to the Integrator queue; claim it and queue the merge again.",
        "ticket_changed": "read the ticket again; it moved on while the merge waited.",
        "release_policy_changed": "the release evidence changed during the gate; restore it, then queue the merge again.",
    }

    def _fail_merge_precondition(
        self, merge_job_id: str, outcome: str, detail: str
    ) -> dict[str, Any]:
        """Fail a merge cleanly on a precondition that changed before Git was touched."""
        message = (
            f"precondition_failed:{outcome}: {detail[:1000]}. Nothing changed in Git; the "
            f"candidate and its QA verdict stand; {self._MERGE_PRECONDITION_NEXT[outcome]}"
        )
        with self.store.connection(immediate=True) as connection:
            job = self._merge_job(connection, merge_job_id)
            if job["status"] in {"COMPLETED", "FAILED"}:
                return job
            timestamp = _iso()
            connection.execute(
                """UPDATE merge_jobs SET status = 'FAILED', error_message = ?, completed_at = ?
                   WHERE merge_job_id = ?""",
                (message, timestamp, merge_job_id),
            )
            actor = Actor(job["requested_by"], Role.INTEGRATOR, frozenset({job["project_id"]}))
            ticket = self._ticket(connection, job["ticket_id"])
            self._event(
                connection, actor, job["project_id"], "integration.merge_precondition_failed",
                "ticket", job["ticket_id"], ticket["version"],
                {"merge_job_id": merge_job_id, "outcome": outcome},
            )
            if connection.execute(
                "SELECT 1 FROM leases WHERE ticket_id = ? AND expires_at > ?",
                (job["ticket_id"], timestamp),
            ).fetchone() is None:
                # Nobody holds the ticket any more; offer it again rather than
                # leave verified work waiting for someone to notice.
                self._queue_ticket(connection, actor, job["project_id"], ticket)
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
            retirement_pending = connection.execute(
                "SELECT 1 FROM ticket_retirement_requests "
                "WHERE ticket_id = ? AND status = 'PENDING'",
                (job["ticket_id"],),
            ).fetchone() is not None
            self._event(
                connection, actor, job["project_id"], "integration.merge_failed", "ticket",
                job["ticket_id"], ticket["version"], {"merge_job_id": merge_job_id},
            )
            # A merge that settles as "already on main" is not a failure of the run.
            if not error_message.startswith("candidate_already_on_main"):
                self._record_run_outcome(
                    connection, job["project_id"], succeeded=False, ticket=ticket,
                    evidence_ref=f"merge_job:{merge_job_id}",
                )
            if not retirement_pending and ticket["status"] not in {
                TicketStatus.DONE.value,
                TicketStatus.CANCELLED.value,
                TicketStatus.SUPERSEDED.value,
            }:
                timestamp = _iso()
                entry_id = _id(connection, "ctx")
                connection.execute(
                    """
                    INSERT INTO context_entries
                        (entry_id, project_id, feature_id, ticket_id, entry_type,
                         content, source_ref, artifact_commit, actor_id, actor_role, created_at)
                    VALUES (?, ?, ?, ?, 'risk', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry_id,
                        job["project_id"],
                        ticket["feature_id"],
                        ticket["ticket_id"],
                        f"integration infrastructure failure: {error_message}",
                        f"merge_job:{merge_job_id}",
                        ticket.get("candidate_commit") or ticket.get("base_commit"),
                        actor.agent_id,
                        actor.role.value,
                        timestamp,
                    ),
                )
                self._close_ticket_dispatch(connection, ticket["ticket_id"], timestamp)
                connection.execute(
                    "DELETE FROM worktree_ownership WHERE ticket_id = ?", (ticket["ticket_id"],)
                )
                connection.execute(
                    "DELETE FROM leases WHERE ticket_id = ?", (ticket["ticket_id"],)
                )
                next_version = _transition_ticket(
                    connection, ticket["ticket_id"], actor=actor, expected_version=ticket["version"],
                    from_statuses=(ticket["status"],),
                    changes={"status": TicketStatus.BLOCKED, "blocked_reason": error_message,
                             "updated_at": timestamp},
                )
                blocked = self._ticket(connection, ticket["ticket_id"])
                self._event(
                    connection,
                    actor,
                    job["project_id"],
                    "ticket.execution_failure_reported",
                    "ticket",
                    ticket["ticket_id"],
                    next_version,
                    {
                        "context_ref": f"ctx:entry:{entry_id}",
                        "failure_class": "infrastructure",
                        "merge_job_id": merge_job_id,
                    },
                )
                self._open_planner_job(
                    connection,
                    actor,
                    job["project_id"],
                    blocked,
                    job_type=PlannerJobType.INTEGRATION_CONFLICT,
                    summary=error_message,
                    evidence_refs=[
                        f"context:{entry_id}",
                        f"merge_job:{merge_job_id}",
                    ],
                    candidate_id=job.get("candidate_id"),
                    context_entry_id=entry_id,
                    artifact_commit=ticket.get("candidate_commit") or ticket.get("base_commit"),
                )
            return self._merge_job(connection, merge_job_id)

    @staticmethod
    def _merge_job_is_stuck(job: dict[str, Any]) -> bool:
        """RUNNING for longer than any live worker could still be running it."""
        if job["status"] != "RUNNING":
            return False
        started = job.get("started_at") or job["created_at"]
        try:
            began = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        except ValueError:
            return False
        return _now() - began >= timedelta(seconds=MERGE_JOB_STUCK_SECONDS)

    def _stuck_merge_git_facts(self, job: dict[str, Any]) -> dict[str, Any]:
        """What Git says about a merge job's candidate, asked outside any transaction.

        Read from the durable merge intent when there is one, which names the
        repository, branch, base and candidate the job was authorized for.
        """
        with self.store.connection() as connection:
            intent = _row(connection.execute(
                "SELECT * FROM merge_intents WHERE merge_job_id = ?", (job["merge_job_id"],)
            ).fetchone())
            repository = _row(connection.execute(
                "SELECT repository_path FROM project_repositories WHERE project_id = ?",
                (job["project_id"],),
            ).fetchone())
            project = _row(connection.execute(
                "SELECT default_branch FROM projects WHERE project_id = ?", (job["project_id"],)
            ).fetchone())
            candidate = _row(connection.execute(
                "SELECT commit_sha, base_commit FROM candidates WHERE candidate_id = ?",
                (job["candidate_id"],),
            ).fetchone())
        facts: dict[str, Any] = {
            "merge_intent_recorded": intent is not None,
            "default_branch": (intent or project or {}).get("default_branch"),
            "candidate_commit": (intent or {}).get("candidate_commit")
            or (candidate or {}).get("commit_sha"),
            "base_commit": (intent or {}).get("base_commit") or (candidate or {}).get("base_commit"),
            "default_branch_head": None,
            "candidate_on_default_branch": None,
            "recovery_message": job.get("error_message"),
        }
        repository_path = (intent or repository or {}).get("repository_path")
        if not repository_path or not facts["default_branch"] or not facts["candidate_commit"]:
            facts["git_error"] = "The repository, branch or candidate is not recorded"
            return facts
        try:
            facts["default_branch_head"] = self.git_worktrees._resolve_commit(
                Path(repository_path), f"refs/heads/{facts['default_branch']}"
            )
            facts["candidate_on_default_branch"] = self.git_worktrees.commit_is_on_branch(
                repository_path=repository_path,
                default_branch=facts["default_branch"],
                commit=facts["candidate_commit"],
            )
        except (DomainError, OSError) as error:
            facts["git_error"] = str(error)[:300]
        return facts

    def close_stuck_merge_job(
        self, actor: Actor, merge_job_id: str, reason: str
    ) -> dict[str, Any]:
        """End a merge job that recovery cannot settle, so its ticket can be decided.

        Without this, a job whose candidate reached Git while its ticket changed
        stays RUNNING for good, ``record_external_landing`` refuses with
        ``landing_job_active`` and only an administrator can clear it. Planner
        may close the job once it has outlived the worker timeout and no live
        operation holds the repository. Recovery runs once more first; if it
        settles the job, nothing is closed. Otherwise the job ends FAILED with
        Planner's reason and the Git facts, its merge intent stays, and its
        lease is released. Nothing is recorded about the ticket: an ungated
        landing, a retirement or a requeue is a separate, explicit call.
        """
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(reason, "reason", 2000)
        with self.store.connection() as connection:
            job = self._merge_job(connection, merge_job_id)
            require_project(actor, job["project_id"])
            intent = _row(connection.execute(
                "SELECT repository_path FROM merge_intents WHERE merge_job_id = ?", (merge_job_id,)
            ).fetchone())
            repository = _row(connection.execute(
                "SELECT repository_path FROM project_repositories WHERE project_id = ?",
                (job["project_id"],),
            ).fetchone())
        if job["status"] != "RUNNING":
            raise ConflictError(
                "Only a RUNNING merge job can be closed as stuck",
                {"reason": "merge_job_not_running", "status": job["status"]},
            )
        if not self._merge_job_is_stuck(job):
            raise ConflictError(
                "The merge job has not outlived the worker timeout; recovery may still settle it",
                {"reason": "merge_job_not_stuck", "stuck_after_seconds": MERGE_JOB_STUCK_SECONDS},
            )
        repository_path = (intent or repository or {}).get("repository_path")
        if not repository_path:
            raise ValidationError("Project repository is required to inspect the merge job")
        lock = self.git_worktrees.repository_lock(repository_path)
        try:
            lock.__enter__()
        except ConflictError as error:
            raise ConflictError(
                "A live operation holds the repository; the merge job is not abandoned",
                {"reason": "merge_job_live"},
            ) from error
        try:
            settled = self._reconcile_merge_locked(merge_job_id)
            if settled["status"] != "RUNNING":
                return {
                    "merge_job": settled,
                    "closed": False,
                    "next_action": "Recovery settled the job; read the ticket again.",
                }
            facts = self._stuck_merge_git_facts(settled)
            with self.store.connection(immediate=True) as connection:
                job = self._merge_job(connection, merge_job_id)
                if job["status"] != "RUNNING":
                    return {"merge_job": job, "closed": False,
                            "next_action": "The job settled meanwhile; read the ticket again."}
                timestamp = _iso()
                connection.execute(
                    """UPDATE merge_jobs SET status = 'FAILED', error_message = ?, completed_at = ?
                       WHERE merge_job_id = ? AND status = 'RUNNING'""",
                    (
                        f"Closed by Planner {actor.agent_id} after recovery could not decide: "
                        f"{reason}"[:4000],
                        timestamp,
                        merge_job_id,
                    ),
                )
                # The lease existed for this merge only; keeping it would refuse
                # the landing Planner is about to record (landing_lease_active).
                connection.execute("DELETE FROM leases WHERE lease_id = ?", (job["lease_id"],))
                self._event(
                    connection, actor, job["project_id"], "integration.merge_job_closed",
                    "merge_job", merge_job_id, job["expected_version"],
                    {"ticket_id": job["ticket_id"], "reason": reason[:500], **facts},
                )
                closed = self._merge_job(connection, merge_job_id)
        finally:
            lock.__exit__(None, None, None)
        landed = facts.get("candidate_on_default_branch")
        return {
            "merge_job": closed,
            "closed": True,
            "git": facts,
            "next_action": (
                "The candidate is on the default branch: record it with "
                "planner.record_external_landing (ungated) during the pending reconciliation."
                if landed else
                "The candidate is not on the default branch: retire, stop or requeue the "
                "ticket through its own tool."
                if landed is False else
                "Git could not be read; inspect the repository before deciding the ticket."
            ),
        }

    def reconcile_delivery_report(self, actor: Actor, merge_job_id: str) -> dict[str, Any]:
        """Idempotently recover the Planner report for an already completed merge."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        with self.store.connection(immediate=True) as connection:
            job = self._merge_job(connection, merge_job_id)
            require_project(actor, job["project_id"])
            if (
                job["status"] != "COMPLETED"
                or job["merge_status"] != "merged"
                or not job["mainline_commit"]
            ):
                raise ConflictError("Only a successfully completed merge has a delivery report")
            ticket = self._ticket(connection, job["ticket_id"])
            if ticket["status"] != TicketStatus.DONE.value:
                raise ConflictError("The merged ticket is not in its completed state")
            return self._report_delivery_completed(
                connection,
                actor,
                job["project_id"],
                ticket,
                candidate_id=job["candidate_id"],
                mainline_commit=job["mainline_commit"],
                merge_job_id=merge_job_id,
            )

    def _merge_job(self, connection: Connection, merge_job_id: str) -> dict[str, Any]:
        return self._one(
            connection, "SELECT * FROM merge_jobs WHERE merge_job_id = ?", (merge_job_id,)
        )
