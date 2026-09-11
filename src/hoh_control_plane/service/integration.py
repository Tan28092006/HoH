from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    AuthorizationError,
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
    sqlite3,
    timedelta,
)


class IntegrationMixin:
    """Protected merge gating, release policy and merge jobs."""

    def claim_qa(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        lease_seconds: int,
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
        )

    def claim_integration(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        lease_seconds: int,
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
        )

    def merge_candidate(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        expected_version: int,
    ) -> dict[str, Any]:
        """Merge only a QA-verified candidate whose mainline base has not drifted."""
        require_role(actor, Role.INTEGRATOR)
        with self.store.connection(immediate=True) as connection:
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
            candidate = self._one(
                connection,
                "SELECT * FROM candidates WHERE ticket_id = ? AND commit_sha = ?",
                (ticket_id, ticket["candidate_commit"]),
            )
            if repository is None:
                raise ValidationError("Project repository is required before integrating a candidate")
            merged, mainline_commit = self.git_worktrees.fast_forward_default_branch(
                repository_path=repository["repository_path"],
                default_branch=project["default_branch"],
                expected_base_commit=candidate["base_commit"],
                candidate_commit=candidate["commit_sha"],
            )
            timestamp = _iso()
            next_version = ticket["version"] + 1
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
                        f"candidate:{candidate['candidate_id']}",
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
                    {"candidate_id": candidate["candidate_id"], "mainline_commit": mainline_commit},
                )
                rework_ticket = self._ticket(connection, ticket_id)
                self._queue_ticket(connection, actor, project_id, rework_ticket)
                return {"ticket": rework_ticket, "merge_status": "base_drift"}
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
                {"candidate_id": candidate["candidate_id"], "commit_sha": mainline_commit},
            )
            return {"ticket": self._ticket(connection, ticket_id), "merge_status": "merged"}

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
            candidate = _row(
                connection.execute(
                    "SELECT * FROM candidates WHERE ticket_id = ? AND commit_sha = ?",
                    (ticket_id, ticket["candidate_commit"]),
                ).fetchone()
            )
            if candidate is None:
                raise ReleasePolicyError(
                    "No frozen candidate matches the ticket's current artifact",
                    {"reason": "candidate_identity_mismatch"},
                )
            candidate_id = candidate["candidate_id"]
            findings = [
                _row(row)
                for row in connection.execute(
                    "SELECT * FROM qa_findings WHERE candidate_id = ?", (candidate_id,)
                ).fetchall()
            ]
            evidence = [
                _row(row)
                for row in connection.execute(
                    "SELECT * FROM evidence WHERE candidate_id = ?", (candidate_id,)
                ).fetchall()
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
            candidate = self._one(
                connection,
                "SELECT * FROM candidates WHERE ticket_id = ? AND commit_sha = ?",
                (ticket_id, ticket["candidate_commit"]),
            )
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
        """Worker-only Git fast-forward for a previously authorized merge job."""
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
                connection.execute(
                    "UPDATE merge_jobs SET status = 'RUNNING', started_at = ? WHERE merge_job_id = ?",
                    (_iso(), merge_job_id),
                )
        if preflight_error is not None:
            return self.fail_queued_merge(merge_job_id, preflight_error)
        try:
            repository, project, candidate = self._merge_git_inputs(merge_job_id)
            if repository is None:
                raise ValidationError("Project repository is required before integrating a candidate")
            merged, mainline_commit = self.git_worktrees.fast_forward_default_branch(
                repository_path=repository["repository_path"],
                default_branch=project["default_branch"],
                expected_base_commit=candidate["base_commit"],
                candidate_commit=candidate["commit_sha"],
            )
            return self._complete_queued_merge(merge_job_id, merged, mainline_commit)
        except DomainError as error:
            return self.fail_queued_merge(merge_job_id, str(error))

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
        self, merge_job_id: str, merged: bool, mainline_commit: str
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
            self._require_active_lease(connection, actor, job["ticket_id"], job["lease_id"])
            timestamp = _iso()
            next_version = ticket["version"] + 1
            connection.execute("DELETE FROM leases WHERE lease_id = ?", (job["lease_id"],))
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
                        _id("ctx"), job["project_id"], ticket["feature_id"], job["ticket_id"],
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
                    connection, actor, job["project_id"], "integration.base_drift", "ticket",
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
                       SET status = 'COMPLETED', merge_status = 'base_drift', mainline_commit = ?,
                           error_message = NULL, completed_at = ?
                       WHERE merge_job_id = ?""",
                    (mainline_commit, timestamp, merge_job_id),
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

    def _merge_job(self, connection: sqlite3.Connection, merge_job_id: str) -> dict[str, Any]:
        return self._one(
            connection, "SELECT * FROM merge_jobs WHERE merge_job_id = ?", (merge_job_id,)
        )
