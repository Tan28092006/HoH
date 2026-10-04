from __future__ import annotations

from dataclasses import replace

_INFRASTRUCTURE_OUTCOMES = frozenset({
    "resource_unready", "resource_exhausted", "native_termination", "timeout", "cancelled",
    "capture_failure", "output_truncated", "launch_failure", "incomplete_evidence",
    "artifact_mismatch", "test_configuration_error",
})

from hoh_control_plane.service._common import (
    require_snapshot_id,
    CHECKSUM_ARTIFACT,
    CHECKSUM_CLAIMED,
    Actor,
    Any,
    AuthorizationError,
    ConflictError,
    DomainError,
    InterventionSeverity,
    PlannerJobType,
    Path,
    Role,
    TestExecution,
    TicketStatus,
    ValidationError,
    _id,
    _iso,
    _now,
    _row,
    hashlib,
    json,
    re,
    require_role,
    Connection,
    timedelta,
)


class QaMixin:
    """QA test execution, evidence and criterion-level verdicts."""

    @staticmethod
    def _canonical_storage_ref(value: str) -> str:
        """Reduce a storage reference to one comparable form, or refuse it.

        Two references to the same artifact must not be able to disagree, so the
        form is fixed here rather than trusted from the caller.
        """
        candidate = value.strip().replace("\\", "/")
        while "//" in candidate.split("://", 1)[-1]:
            head, _, tail = candidate.partition("://")
            candidate = f"{head}://{tail.replace('//', '/')}"
        if not candidate.startswith("evidence://"):
            raise ValidationError(
                "Evidence storage_ref must use the evidence:// scheme",
                {"reason": "evidence_storage_ref_invalid", "storage_ref": value},
            )
        path = candidate[len("evidence://") :]
        if not path or ".." in path.split("/") or path.startswith("/"):
            raise ValidationError(
                "Evidence storage_ref is not a valid reference",
                {"reason": "evidence_storage_ref_invalid", "storage_ref": value},
            )
        return f"evidence://{path}"

    def _recorded_scenario_ref(self, supplied: object, criterion: str) -> str:
        """Name what the observation is about; no row may be left without one.

        A caller that names a finer scenario keeps it. Otherwise the criterion the
        finding addresses is the true, coarsest scenario reference, so it is
        recorded rather than inventing a more specific one.
        """
        if supplied is not None and str(supplied).strip():
            self._bounded_text(str(supplied), "evidence.scenario_ref", 1000)
            return str(supplied)
        return f"criterion:{criterion}"

    def _recorded_environment_digest(
        self, connection: Connection, supplied: object, candidate_id: str
    ) -> str:
        """Record the environment behind an observation, preferring the real run.

        When the candidate has an executed run, that run's environment is the one
        the evidence came from. Otherwise this records the environment the
        observation was filed in, which is a weaker but honest statement.
        """
        if supplied is not None and str(supplied).strip():
            self._bounded_text(str(supplied), "evidence.environment_digest", 128)
            return str(supplied)
        recorded = _row(
            connection.execute(
                """
                SELECT environment_digest FROM qa_test_runs
                WHERE candidate_id = ? AND environment_digest IS NOT NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (candidate_id,),
            ).fetchone()
        )
        if recorded is not None and recorded["environment_digest"]:
            return str(recorded["environment_digest"])
        profile = _row(
            connection.execute(
                "SELECT * FROM candidate_test_profiles WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
        )
        command = profile["test_command"] if profile is not None else []
        return self.project_tests.environment_digest(command, None)

    def _precomputed_evidence_checksums(
        self, candidate_id: str, findings: list[Any]
    ) -> dict[int, Any]:
        """Checksum every evidence item outside the write transaction.

        A failure is kept rather than raised, so the verdict reports errors in the
        same order it always did, after the ticket and lease checks.
        """
        results: dict[int, Any] = {}
        for finding_data in findings:
            if not isinstance(finding_data, dict):
                continue
            evidence_items = finding_data.get("evidence") or []
            if not isinstance(evidence_items, list):
                continue
            for item in evidence_items:
                if not isinstance(item, dict):
                    continue
                try:
                    storage_ref = self._canonical_storage_ref(
                        str(item.get("storage_ref", "evidence://inline"))
                    )
                    results[id(item)] = (storage_ref, *self._verified_storage_checksum(
                        storage_ref, candidate_id, item.get("storage_checksum"),
                        str(item.get("observation", "")),
                    ))
                except Exception as error:  # re-raised in order by the caller
                    results[id(item)] = error
        return results

    def _verified_storage_checksum(
        self, storage_ref: str, candidate_id: str, supplied: object, observation: str
    ) -> tuple[str, str]:
        """Bind an observation to the bytes and the candidate it claims to describe.

        A local artifact is checksummed from disk, so a supplied value that
        disagrees fails closed instead of being recorded as provenance. A local
        reference filed under another candidate is refused outright. When no
        artifact is held here, the digest covers the evidence content that is
        retained, so no row is left without a checksum and a later edit of the
        stored record is still detectable.

        Returns how the digest was obtained alongside it. Section 25 asks that no
        verdict be attached to an unidentified artifact, and the two cases are
        indistinguishable once written: a digest read from bytes this machine
        holds proves the artifact, while one covering the retained record is the
        agent's own claim about a file kept elsewhere. Both are legitimate; only
        one is evidence, and an operator whose shared storage is misconfigured
        needs to see the second quietly replacing the first.
        """
        if supplied is not None:
            self._bounded_text(str(supplied), "evidence.storage_checksum", 128)

        def recorded_content_digest() -> str:
            if supplied is not None and str(supplied).strip():
                return str(supplied)
            material = json.dumps(
                {"storage_ref": storage_ref, "observation": observation}, sort_keys=True
            )
            return hashlib.sha256(material.encode("utf-8")).hexdigest()

        local_prefix = "evidence://local/"
        if not storage_ref.startswith(local_prefix):
            return recorded_content_digest(), CHECKSUM_CLAIMED
        relative = storage_ref[len(local_prefix) :]
        owner = relative.split("/", 1)[0]
        if owner != candidate_id:
            raise ValidationError(
                "Evidence artifact belongs to another candidate",
                {
                    "reason": "evidence_candidate_mismatch",
                    "storage_ref": storage_ref,
                    "candidate_id": candidate_id,
                },
            )
        artifact = (self.project_tests.evidence_root / relative).resolve()
        if not artifact.is_relative_to(self.project_tests.evidence_root) or not artifact.is_file():
            # Nothing on disk to prove; a supplied digest is still recorded as the
            # QA agent's own claim about an artifact it holds elsewhere.
            return recorded_content_digest(), CHECKSUM_CLAIMED
        actual = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if supplied is not None and str(supplied) != actual:
            raise ValidationError(
                "Evidence checksum does not match the stored artifact",
                {"reason": "evidence_checksum_mismatch", "storage_ref": storage_ref},
            )
        return actual, CHECKSUM_ARTIFACT

    def record_test_run(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        summary: str,
        evidence_refs: list[str],
    ) -> dict[str, Any]:
        """Persist a QA test-run checkpoint before the final evidence-bearing verdict."""
        require_role(actor, Role.QA)
        self._bounded_text(summary, "summary", 8000)
        self._bounded_list(evidence_refs, "evidence_refs", 30, 1000)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            if ticket["status"] != TicketStatus.QA_IN_PROGRESS.value:
                raise ConflictError("Ticket is not in QA")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            entry_id = _id(connection, "ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     source_ref, actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'qa_test_run', ?, ?, ?, ?, ?)
                """,
                (entry_id, project_id, ticket["feature_id"], ticket_id, summary,
                 json.dumps(evidence_refs), actor.agent_id, actor.role.value, _iso()),
            )
            self._event(
                connection, actor, project_id, "qa.test_run_recorded", "ticket", ticket_id,
                ticket["version"], {"entry_id": entry_id, "evidence_count": len(evidence_refs)},
            )
            return self._one(
                connection, "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
            )

    def queue_project_tests(
        self, actor: Actor, ticket_id: str, lease_id: str, rerun: bool = False
    ) -> dict[str, Any]:
        """Persist a frozen QA test job and return before its command is executed.

        A candidate is immutable, so a completed passing run of the same frozen
        command and timeout is returned (``reused``) instead of spending another
        full suite. ``rerun=True`` forces a fresh run, e.g. to investigate flakiness.
        """
        require_role(actor, Role.QA)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            if ticket["status"] != TicketStatus.QA_IN_PROGRESS.value:
                raise ConflictError("Ticket is not in QA")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            candidate = self._one(
                connection,
                "SELECT * FROM candidates WHERE ticket_id = ? ORDER BY submitted_at DESC LIMIT 1",
                (ticket_id,),
            )
            active_job = connection.execute(
                """
                SELECT test_run_id FROM qa_test_jobs
                WHERE candidate_id = ? AND status IN ('QUEUED', 'RUNNING')
                """,
                (candidate["candidate_id"],),
            ).fetchone()
            if active_job is not None:
                raise ConflictError("Candidate already has an active project test run")
            test_profile = _row(
                connection.execute(
                    "SELECT * FROM candidate_test_profiles WHERE candidate_id = ?",
                    (candidate["candidate_id"],),
                ).fetchone()
            )
            if test_profile is None:
                raise ValidationError("Candidate has no Planner-frozen project test command")
            worktree = _row(
                connection.execute("SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)).fetchone()
            )
            repository = _row(connection.execute(
                "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
            ).fetchone())
            remote_workspace = repository is not None and repository.get("workspace_model") == "agent_workspace"
            if worktree is None and not remote_workspace:
                raise ValidationError("Candidate has no assigned Git worktree")
            command = test_profile["test_command"]
            self._test_command(command)
            timeout_seconds = test_profile["timeout_seconds"]
            if not isinstance(timeout_seconds, int) or not 5 <= timeout_seconds <= 1800:
                raise ValidationError("Candidate test command timeout is invalid")
            if not rerun:
                passing = connection.execute(
                    """
                    SELECT j.test_run_id FROM qa_test_jobs j
                    JOIN qa_test_runs r ON r.test_run_id = j.test_run_id
                    WHERE j.candidate_id = ? AND j.status = 'COMPLETED'
                      AND j.command_digest = ? AND j.timeout_seconds = ?
                      AND r.exit_code = 0 AND r.timed_out = 0
                    ORDER BY j.created_at DESC LIMIT 1
                    """,
                    (candidate["candidate_id"],
                     hashlib.sha256("\0".join(command).encode()).hexdigest(),
                     timeout_seconds),
                ).fetchone()
                if passing is not None:
                    return {
                        "ticket": self._ticket(connection, ticket_id),
                        "test_run": self._qa_test_job(connection, passing["test_run_id"]),
                        "reused": True,
                    }
            candidate_commit = candidate["commit_sha"]
            # Queueing must return promptly. The detached worker re-verifies this
            # immutable path and commit before it executes the test command.
            test_run_id = _id(connection, "testrun")
            worktree_path = str(self.git_worktrees.qa_path(test_run_id))
            timestamp = _iso()
            connection.execute(
                """
                INSERT INTO qa_test_jobs
                    (test_run_id, project_id, candidate_id, ticket_id, lease_id, test_command,
                     command_digest, timeout_seconds, worktree_path, candidate_commit,
                     requested_by, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?)
                """,
                (
                    test_run_id,
                    project_id,
                    candidate["candidate_id"],
                    ticket_id,
                    lease_id,
                    json.dumps(command),
                    hashlib.sha256("\0".join(command).encode()).hexdigest(),
                    timeout_seconds,
                    worktree_path,
                    candidate_commit,
                    actor.agent_id,
                    timestamp,
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                "qa.automated_test_queued",
                "ticket",
                ticket_id,
                ticket["version"],
                {"test_run_id": test_run_id, "candidate_id": candidate["candidate_id"]},
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "test_run": self._qa_test_job(connection, test_run_id),
            }

    def get_project_test_run(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        test_run_id: str,
        lease_seconds: int = 900,
    ) -> dict[str, Any]:
        """Poll a persisted QA test job and renew the caller's exclusive QA lease."""
        require_role(actor, Role.QA)
        if not 30 <= lease_seconds <= 3600:
            raise ValidationError("Lease duration must be between 30 and 3600 seconds")
        with self.store.connection(immediate=True) as connection:
            ticket, _project_id = self._authorized_ticket(connection, actor, ticket_id)
            job = self._qa_test_job(connection, test_run_id)
            if (job["ticket_id"] != ticket_id or job["lease_id"] != lease_id
                    or job["requested_by"] != actor.agent_id):
                raise AuthorizationError("Project test run does not belong to this QA lease")
            if job["status"] in {"COMPLETED", "FAILED", "STALE"} and ticket["status"] != TicketStatus.QA_IN_PROGRESS.value:
                return {"ticket": ticket, "lease": None, "test_run": job}
            if ticket["status"] != TicketStatus.QA_IN_PROGRESS.value:
                raise ConflictError("Ticket is not in QA")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            job = self._qa_test_job(connection, test_run_id)
            if job["ticket_id"] != ticket_id or job["lease_id"] != lease_id:
                raise AuthorizationError("Project test run does not belong to this QA lease")
            timestamp = _now()
            expires_at = _iso(timestamp + timedelta(seconds=lease_seconds))
            connection.execute(
                "UPDATE leases SET heartbeat_at = ?, expires_at = ? WHERE lease_id = ?",
                (_iso(timestamp), expires_at, lease_id),
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "lease": self._lease(connection, lease_id),
                "test_run": self._qa_test_job(connection, test_run_id),
            }

    def execute_queued_project_test(self, test_run_id: str) -> dict[str, Any]:
        """Worker-only execution for a previously authorized, immutable QA test job."""
        preflight_error: str | None = None
        with self.store.connection(immediate=True) as connection:
            job = self._qa_test_job(connection, test_run_id)
            if job["status"] != "QUEUED":
                return job
            actor = Actor(job["requested_by"], Role.QA, frozenset({job["project_id"]}))
            ticket = self._ticket(connection, job["ticket_id"])
            if ticket["status"] != TicketStatus.QA_IN_PROGRESS.value:
                preflight_error = "Ticket is no longer in QA before the test worker started."
            else:
                try:
                    self._require_active_lease(connection, actor, job["ticket_id"], job["lease_id"])
                    candidate = self._one(
                        connection,
                        "SELECT * FROM candidates WHERE candidate_id = ? AND ticket_id = ?",
                        (job["candidate_id"], job["ticket_id"]),
                    )
                    if candidate["commit_sha"] != job["candidate_commit"]:
                        preflight_error = "Candidate changed before the test worker started."
                except DomainError as error:
                    preflight_error = str(error)
                if preflight_error is None:
                    connection.execute(
                        "UPDATE qa_test_jobs SET status = 'RUNNING', started_at = ? WHERE test_run_id = ?",
                        (_iso(), test_run_id),
                    )

        if preflight_error is not None:
            return self.fail_queued_project_test(test_run_id, preflight_error)

        try:
            if Path(job["worktree_path"]).resolve() == self.git_worktrees.qa_path(test_run_id):
                with self.store.connection() as connection:
                    repository = self._one(connection,
                        "SELECT * FROM project_repositories WHERE project_id = ?", (job["project_id"],))
                with self.git_worktrees.qa_checkout(repository_path=repository["repository_path"],
                        test_run_id=test_run_id, candidate_commit=job["candidate_commit"]) as path:
                    execution = self.project_tests.execute(job["test_command"], path, job["timeout_seconds"])
                    if execution.artifact_mismatch and execution.termination_reason == "unclassified":
                        return self._fail_with_artifact_mismatch(test_run_id, job, execution)
                    return self._complete_queued_project_test(test_run_id, execution)
            worktree_path = self.git_worktrees.verify_frozen_candidate(
                job["worktree_path"], job["candidate_commit"]
            )
            execution = self.project_tests.execute(
                job["test_command"], worktree_path, job["timeout_seconds"]
            )
            if execution.artifact_mismatch and execution.termination_reason == "unclassified":
                return self._fail_with_artifact_mismatch(test_run_id, job, execution)
            return self._complete_queued_project_test(test_run_id, execution)
        except (DomainError, OSError) as error:
            return self.fail_queued_project_test(test_run_id, str(error))

    def _fail_with_artifact_mismatch(
        self, test_run_id: str, job: dict[str, Any], execution: TestExecution
    ) -> dict[str, Any]:
        """Record an infrastructure-only import mismatch without a candidate verdict.

        The frozen candidate's own git identity was already verified before this ran
        (verify_frozen_candidate); what failed is that the configured command imported
        hoh_control_plane from somewhere other than that verified worktree. No
        qa_test_runs row is written -- this must never read as a candidate regression --
        but the mismatch is still durably evidenced on disk and referenced from the
        job's error_message so QA and Planner can inspect expected-vs-observed.
        """
        storage_ref, _report_path, _checksum = self.project_tests.write_report(
            candidate_id=job["candidate_id"],
            test_run_id=test_run_id,
            execution=execution,
            candidate_commit=job["candidate_commit"],
        )
        message = (
            f"{execution.stderr} (verified candidate commit {job['candidate_commit']}; "
            f"durable evidence={storage_ref})"
        )
        return self.fail_queued_project_test(test_run_id, message)

    def run_project_tests(
        self, actor: Actor, ticket_id: str, lease_id: str
    ) -> dict[str, Any]:
        """Compatibility helper for direct callers; MCP callers must queue then poll."""
        queued = self.queue_project_tests(actor, ticket_id, lease_id)
        if queued.get("reused"):
            return self.get_project_test_run(
                actor, ticket_id, lease_id, queued["test_run"]["test_run_id"]
            )
        completed = self.execute_queued_project_test(queued["test_run"]["test_run_id"])
        if completed["status"] == "FAILED":
            raise ValidationError(completed["error_message"])
        return self.get_project_test_run(
            actor, ticket_id, lease_id, queued["test_run"]["test_run_id"]
        )

    def _require_project_test_current(
        self, connection: Connection, job: dict[str, Any]
    ) -> tuple[Actor, dict[str, Any]]:
        actor = Actor(job["requested_by"], Role.QA, frozenset({job["project_id"]}))
        ticket = self._ticket(connection, job["ticket_id"])
        if ticket["status"] != TicketStatus.QA_IN_PROGRESS.value:
            raise ConflictError("Ticket changed while project tests were running")
        self._require_active_lease(connection, actor, job["ticket_id"], job["lease_id"])
        candidate = self._one(
            connection,
            "SELECT * FROM candidates WHERE candidate_id = ? AND ticket_id = ?",
            (job["candidate_id"], job["ticket_id"]),
        )
        if candidate["commit_sha"] != job["candidate_commit"]:
            raise ConflictError("Candidate changed while project tests were running")
        return actor, ticket

    def _complete_queued_project_test(
        self, test_run_id: str, execution: TestExecution
    ) -> dict[str, Any]:
        try:
            # Read and check, write the report file with no transaction held, then
            # re-check under the write lock before recording the run.
            with self.store.connection() as connection:
                job = self._qa_test_job(connection, test_run_id)
                if job["status"] != "RUNNING":
                    return job
                self._require_project_test_current(connection, job)
                profile = self._one(connection, "SELECT * FROM candidate_test_profiles WHERE candidate_id = ?",
                                    (job["candidate_id"],))
            manifest = {**execution.environment_manifest,
                        "candidate_commit": job["candidate_commit"],
                        "frozen_profile": {"configured_at": profile["configured_at"],
                                           "command_digest": job["command_digest"],
                                           "timeout_seconds": job["timeout_seconds"]}}
            child = manifest.get("child", {})
            if execution.environment_manifest.get("instrumentation") and (
                child.get("candidate_commit") != job["candidate_commit"]
                or child.get("final_commit") != job["candidate_commit"]
            ) and execution.termination_reason == "passed":
                execution = replace(execution, artifact_mismatch=True, provenance_status="mismatch",
                                    termination_reason="artifact_mismatch")
            execution = replace(execution, environment_manifest=manifest,
                environment_digest=hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest())
            storage_ref, report_path, checksum = self.project_tests.write_report(
                candidate_id=job["candidate_id"],
                test_run_id=test_run_id,
                execution=execution,
                candidate_commit=job["candidate_commit"],
            )
            with self.store.connection(immediate=True) as connection:
                job = self._qa_test_job(connection, test_run_id)
                if job["status"] != "RUNNING":
                    return job
                actor, ticket = self._require_project_test_current(connection, job)
                timestamp = _iso()
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
                        job["candidate_id"],
                        job["ticket_id"],
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
                connection.execute(
                    """
                    UPDATE qa_test_jobs
                    SET status = 'COMPLETED', completed_at = ?, error_message = NULL
                    WHERE test_run_id = ?
                    """,
                    (timestamp, test_run_id),
                )
                entry_id = _id(connection, "ctx")
                outcome = "timed out" if execution.timed_out else f"exited {execution.exit_code}"
                connection.execute(
                    """
                    INSERT INTO context_entries
                        (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                         source_ref, artifact_commit, actor_id, actor_role, created_at)
                    VALUES (?, ?, ?, ?, 'qa_automated_test_run', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry_id,
                        job["project_id"],
                        ticket["feature_id"],
                        job["ticket_id"],
                        f"Planner-registered project test command {outcome} in {execution.duration_ms}ms.",
                        storage_ref,
                        job["candidate_commit"],
                        actor.agent_id,
                        actor.role.value,
                        timestamp,
                    ),
                )
                self._event(
                    connection,
                    actor,
                    job["project_id"],
                    "qa.automated_test_completed",
                    "ticket",
                    job["ticket_id"],
                    ticket["version"],
                    {
                        "test_run_id": test_run_id,
                        "exit_code": execution.exit_code,
                        "timed_out": execution.timed_out,
                    },
                )
                if execution.termination_reason in _INFRASTRUCTURE_OUTCOMES:
                    self._park_qa_infrastructure(connection, actor, ticket, job, execution.termination_reason, storage_ref)
                return self._qa_test_job(connection, test_run_id)
        except DomainError as error:
            return self.fail_queued_project_test(test_run_id, str(error))

    def _park_qa_infrastructure(self, connection, actor, ticket, job, reason, storage_ref):
        """Stop offers for source work and preserve the candidate for a guarded retry."""
        timestamp = _iso()
        next_version = ticket["version"] + 1
        connection.execute("UPDATE tickets SET status = ?, blocked_reason = ?, version = ?, updated_at = ? WHERE ticket_id = ?",
            (TicketStatus.BLOCKED.value, "qa.infrastructure/" + reason, next_version, timestamp, ticket["ticket_id"]))
        connection.execute("DELETE FROM leases WHERE lease_id = ?", (job["lease_id"],))
        self._close_ticket_dispatch(connection, ticket["ticket_id"], timestamp)
        entry_id = _id(connection, "ctx")
        connection.execute("""INSERT INTO context_entries
            (entry_id, project_id, feature_id, ticket_id, entry_type, content, source_ref,
             artifact_commit, actor_id, actor_role, created_at)
            VALUES (?, ?, ?, ?, 'risk', ?, ?, ?, ?, ?, ?)""",
            (entry_id, job["project_id"], ticket["feature_id"], ticket["ticket_id"],
             f"QA infrastructure blocked: {reason}. Preserve this candidate; Planner must record readiness before retry. No source rework is requested.",
             storage_ref, job["candidate_commit"], actor.agent_id, actor.role.value, timestamp))
        updated = self._ticket(connection, ticket["ticket_id"])
        self._event(connection, actor, job["project_id"], "qa.infrastructure_blocked", "ticket", ticket["ticket_id"],
                    next_version, {"test_run_id": job["test_run_id"], "reason": reason, "next_role": "planner"})
        self._open_planner_job(connection, actor, job["project_id"], updated,
            job_type=PlannerJobType.QA_FAILURE, summary="QA infrastructure needs remediation and a guarded same-candidate retry.",
            evidence_refs=[f"test_run:{job['test_run_id']}", storage_ref], candidate_id=job["candidate_id"],
            context_entry_id=entry_id, severity=InterventionSeverity.NORMAL, artifact_commit=job["candidate_commit"])

    def fail_queued_project_test(self, test_run_id: str, message: str) -> dict[str, Any]:
        """Terminally record infrastructure or stale-state failures without blocking QA polling."""
        self._bounded_text(message, "test worker error", 2000)
        with self.store.connection(immediate=True) as connection:
            job = self._qa_test_job(connection, test_run_id)
            if job["status"] in {"COMPLETED", "FAILED", "STALE"}:
                return job
            timestamp = _iso()
            connection.execute(
                """
                UPDATE qa_test_jobs
                SET status = 'FAILED', error_message = ?, completed_at = ?
                WHERE test_run_id = ?
                """,
                (message, timestamp, test_run_id),
            )
            ticket = self._ticket(connection, job["ticket_id"])
            actor = Actor(job["requested_by"], Role.QA, frozenset({job["project_id"]}))
            self._event(
                connection,
                actor,
                job["project_id"],
                "qa.automated_test_failed",
                "ticket",
                job["ticket_id"],
                ticket["version"],
                {"test_run_id": test_run_id},
            )
            owns_current_lease = connection.execute(
                "SELECT 1 FROM leases WHERE lease_id = ? AND ticket_id = ? AND agent_id = ?",
                (job["lease_id"], job["ticket_id"], job["requested_by"]),
            ).fetchone()
            if (owns_current_lease is not None and ticket["status"] == TicketStatus.QA_IN_PROGRESS.value
                    and ticket["candidate_commit"] == job["candidate_commit"]):
                self._park_qa_infrastructure(connection, actor, ticket, job, "worker_failure", f"test_run:{test_run_id}")
            return self._qa_test_job(connection, test_run_id)

    def _qa_test_job(self, connection: Connection, test_run_id: str) -> dict[str, Any]:
        job = self._one(
            connection, "SELECT * FROM qa_test_jobs WHERE test_run_id = ?", (test_run_id,)
        )
        test_run = _row(
            connection.execute(
                "SELECT * FROM qa_test_runs WHERE test_run_id = ?", (test_run_id,)
            ).fetchone()
        )
        job["test_run"] = test_run
        if test_run is not None:
            job.update({
                key: test_run[key]
                for key in ("exit_code", "timed_out", "duration_ms", "storage_ref", "report_path")
            })
        return job

    def submit_qa_verdict(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        candidate_id: str,
        expected_version: int,
        verdict: str,
        findings: list[dict[str, Any]],
        context_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        require_role(actor, Role.QA)
        context_snapshot_id = require_snapshot_id(context_snapshot_id, "qa.submit_verdict")
        if verdict not in {"passed", "failed"}:
            raise ValidationError("Verdict must be 'passed' or 'failed'")
        self._bounded_list(findings, "findings", 50, 1, required=True, strings_only=False)
        # Hashing evidence artifacts reads files of any size, so it runs before the
        # write lock is taken; the loop below consumes the results in order.
        checksums = self._precomputed_evidence_checksums(candidate_id, findings)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            self.require_fresh_context(connection, ticket_id, context_snapshot_id)
            if ticket["status"] != TicketStatus.QA_IN_PROGRESS.value:
                raise ConflictError("Ticket is not in QA")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            candidate = self._one(
                connection,
                "SELECT * FROM candidates WHERE candidate_id = ? AND ticket_id = ?",
                (candidate_id, ticket_id),
            )
            if candidate["commit_sha"] != ticket["candidate_commit"]:
                raise ConflictError("Candidate does not match the frozen ticket artifact")
            contract = {
                item["criterion"]: item for item in (ticket.get("validation_contract") or [])
            }
            if contract:
                for finding_data in findings:
                    criterion = str(finding_data.get("criterion", ""))
                    scenario = contract.get(criterion)
                    if scenario is None:
                        raise ValidationError(
                            "QA finding is not bound to the ticket validation contract",
                            {"criterion": criterion},
                        )
                    evidence_items = finding_data.get("evidence") or []
                    wrong_scenarios = [
                        item.get("scenario_ref")
                        for item in evidence_items
                        if item.get("scenario_ref") != scenario["scenario_ref"]
                    ]
                    if wrong_scenarios:
                        raise ValidationError(
                            "Evidence scenario_ref does not match the validation contract",
                            {"criterion": criterion, "expected": scenario["scenario_ref"]},
                        )
                    supplied_types = {str(item.get("type", "")) for item in evidence_items}
                    missing_types = sorted(
                        set(scenario["required_evidence_types"]) - supplied_types
                    )
                    if missing_types:
                        raise ValidationError(
                            "Evidence types do not satisfy the validation contract",
                            {"criterion": criterion, "missing_evidence_types": missing_types},
                        )
            verified_criteria: set[str] = set()
            evidence_records: list[dict[str, Any]] = []
            timestamp = _iso()
            for finding_data in findings:
                evidence_items = finding_data.get("evidence") or []
                self._bounded_text(str(finding_data.get("criterion", "")), "finding.criterion", 2000)
                self._bounded_text(str(finding_data.get("observation", "")), "finding.observation", 8000)
                self._bounded_text(str(finding_data.get("severity", "unknown")), "finding.severity", 30)
                self._bounded_list(evidence_items, "finding.evidence", 30, 1, required=True, strings_only=False)
                status = str(finding_data.get("status", ""))
                if status not in {"verified", "failed", "insufficient_evidence", "flaky"}:
                    raise ValidationError("QA finding has an invalid status")
                criterion = str(finding_data.get("criterion", ""))
                if status == "verified":
                    verified_criteria.add(criterion)
                evidence_ids = []
                for item in evidence_items:
                    self._bounded_text(str(item.get("type", "")), "evidence.type", 50)
                    self._bounded_text(str(item.get("observation", "")), "evidence.observation", 8000)
                    # Canonicalize first, so every rejected reference carries the same
                    # typed reason rather than whichever guard happened to fire.
                    storage_ref = self._canonical_storage_ref(
                        str(item.get("storage_ref", "evidence://inline"))
                    )
                    self._bounded_text(storage_ref, "evidence.storage_ref", 1000)
                    observation = str(item.get("observation", ""))
                    precomputed = checksums.get(id(item))
                    if isinstance(precomputed, Exception):
                        raise precomputed
                    if precomputed is None or precomputed[0] != storage_ref:
                        precomputed = (storage_ref, *self._verified_storage_checksum(
                            storage_ref, candidate_id, item.get("storage_checksum"), observation
                        ))
                    _, checksum, checksum_source = precomputed
                    scenario_ref = self._recorded_scenario_ref(
                        item.get("scenario_ref"), criterion
                    )
                    environment_digest = self._recorded_environment_digest(
                        connection, item.get("environment_digest"), candidate_id
                    )
                    evidence_id = _id(connection, "ev")
                    evidence_ids.append(evidence_id)
                    connection.execute(
                        """
                        INSERT INTO evidence
                            (evidence_id, candidate_id, criterion, evidence_type,
                             observation, storage_ref, scenario_ref, environment_digest,
                             storage_checksum, checksum_source, created_by, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            evidence_id,
                            candidate_id,
                            criterion,
                            item.get("type", "observation"),
                            item.get("observation", ""),
                            storage_ref,
                            scenario_ref,
                            environment_digest,
                            checksum,
                            checksum_source,
                            actor.agent_id,
                            timestamp,
                        ),
                    )
                finding_id = _id(connection, "finding")
                connection.execute(
                    """
                    INSERT INTO qa_findings
                        (finding_id, candidate_id, criterion, status, severity,
                         observation, evidence_refs, created_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        finding_id,
                        candidate_id,
                        criterion,
                        status,
                        finding_data.get("severity", "unknown"),
                        finding_data.get("observation", ""),
                        json.dumps(evidence_ids),
                        actor.agent_id,
                        timestamp,
                    ),
                )
                evidence_records.append({
                    "finding_id": finding_id,
                    "criterion": criterion,
                    "status": status,
                    "observation": finding_data.get("observation", ""),
                    "evidence_refs": evidence_ids,
                    "scenario_ref": (
                        contract[criterion]["scenario_ref"] if criterion in contract
                        else f"criterion:{criterion}"
                    ),
                })
            if verdict == "passed":
                missing = set(ticket["acceptance_criteria"]) - verified_criteria
                if missing:
                    raise ValidationError(
                        "Passed verdict requires verified evidence for every criterion",
                        {"missing_criteria": sorted(missing)},
                    )
                next_status = TicketStatus.VERIFIED
            else:
                evidence_gap = any(f.get("status") == "insufficient_evidence" for f in findings) and all(
                    f.get("status") in {"verified", "insufficient_evidence"} for f in findings)
                next_status = TicketStatus.BLOCKED if evidence_gap else TicketStatus.REWORK
                known_failure_entry_id = _id(connection, "ctx")
                connection.execute(
                    """
                    INSERT INTO context_entries
                        (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                         source_ref, artifact_commit, actor_id, actor_role, created_at)
                    VALUES (?, ?, ?, ?, 'known_failure', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        known_failure_entry_id,
                        project_id,
                        ticket["feature_id"],
                        ticket_id,
                        ("QA evidence is insufficient; infrastructure owner must record readiness before retry. No source rework is requested."
                         if evidence_gap else "QA rejected candidate; inspect linked findings and evidence"),
                        f"candidate:{candidate_id}",
                        candidate["commit_sha"],
                        actor.agent_id,
                        actor.role.value,
                        timestamp,
                    ),
                )
            next_version = ticket["version"] + 1
            connection.execute(
                "UPDATE tickets SET status = ?, version = ?, updated_at = ? WHERE ticket_id = ?",
                (next_status.value, next_version, timestamp, ticket_id),
            )
            connection.execute("DELETE FROM leases WHERE lease_id = ?", (lease_id,))
            if next_status == TicketStatus.BLOCKED:
                connection.execute("UPDATE tickets SET blocked_reason = ? WHERE ticket_id = ?",
                                   ("qa.infrastructure/insufficient_evidence", ticket_id))
                self._close_ticket_dispatch(connection, ticket_id, timestamp)
            self._event(
                connection,
                actor,
                project_id,
                ("ticket.verified" if verdict == "passed" else
                 "qa.infrastructure_blocked" if next_status == TicketStatus.BLOCKED else "ticket.rework_requested"),
                "ticket",
                ticket_id,
                next_version,
                {"candidate_id": candidate_id, "verdict": verdict},
            )
            verdict_ticket = self._ticket(connection, ticket_id)
            reconciliation_id = None
            if verdict == "failed":
                reconciliation_id = self._open_commit_reconciliation(
                    connection,
                    actor,
                    project_id,
                    commit=candidate["commit_sha"],
                    subject=f"QA rejected candidate {candidate_id}",
                    relation="qa_failed",
                    observation_ref=f"context:{known_failure_entry_id}",
                    branch=candidate.get("branch_ref"),
                )
            if verdict in {"passed", "failed"}:
                self._queue_ticket(connection, actor, project_id, verdict_ticket)
            planner_job = None
            if verdict == "failed":
                failed_findings = [
                    row["finding_id"]
                    for row in connection.execute(
                        """
                        SELECT finding_id FROM qa_findings
                        WHERE candidate_id = ? AND status != 'verified'
                        ORDER BY created_at ASC
                        """,
                        (candidate_id,),
                    ).fetchall()
                ]
                planner_job = self._open_planner_job(
                    connection,
                    actor,
                    project_id,
                    verdict_ticket,
                    job_type=PlannerJobType.QA_FAILURE,
                    summary="QA rejected the frozen candidate; inspect the linked findings.",
                    evidence_refs=[
                        f"candidate:{candidate_id}",
                        f"context:{known_failure_entry_id}",
                        *(f"finding:{finding_id}" for finding_id in failed_findings),
                    ],
                    candidate_id=candidate_id,
                    context_entry_id=known_failure_entry_id,
                    # A contradicted criterion is urgent; missing or inconsistent
                    # evidence is a slower question about how to test it.
                    severity=(
                        InterventionSeverity.HIGH
                        if any(
                            str(finding.get("status")) == "failed" for finding in findings
                        )
                        else InterventionSeverity.NORMAL
                    ),
                    artifact_commit=candidate["commit_sha"],
                )
            return {
                "ticket": verdict_ticket,
                "candidate": candidate,
                "verdict": verdict,
                "planner_job": planner_job,
                "reconciliation_id": reconciliation_id,
                "evidence_bundle": {
                    "schema_version": "1",
                    "candidate_id": candidate_id,
                    "candidate_commit": candidate["commit_sha"],
                    "verified_records": [
                        record for record in evidence_records if record["status"] == "verified"
                    ],
                    "gap_records": [
                        record for record in evidence_records if record["status"] != "verified"
                    ],
                    "planner_handoff": {
                        "preservation_constraints": [
                            record["criterion"] for record in evidence_records
                            if record["status"] == "verified"
                        ],
                        "update_targets": [
                            record["criterion"] for record in evidence_records
                            if record["status"] != "verified"
                        ],
                        "validation_requirements": [
                            contract[record["criterion"]]
                            for record in evidence_records
                            if record["status"] != "verified"
                            and record["criterion"] in contract
                        ],
                    },
                },
            }
