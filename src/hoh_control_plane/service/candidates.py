from __future__ import annotations

from hoh_control_plane import contracts
from hoh_control_plane.service._common import (
    require_snapshot_id,
    Actor,
    Any,
    AuthorizationError,
    ConflictError,
    DomainError,
    Role,
    TicketStatus,
    ValidationError,
    WorkspaceModel,
    _id,
    _iso,
    _now,
    _row,
    _transition_ticket,
    json,
    require_role,
    Connection,
    timedelta,
)


class CandidateMixin:
    """Asynchronous candidate submission, validation and freeze."""

    def queue_candidate_submission(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        expected_version: int,
        commit_sha: str,
        base_commit: str,
        branch_ref: str,
        developer_claims: list[str],
        context_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        """Durably acknowledge a candidate before detached Git validation begins."""
        require_role(actor, Role.DEVELOPER)
        context_snapshot_id = require_snapshot_id(context_snapshot_id, "dev.submit_candidate")
        commit_sha = self._full_commit(actor, ticket_id, commit_sha, "commit_sha")
        base_commit = self._full_commit(actor, ticket_id, base_commit, "base_commit")
        self._bounded_text(branch_ref, "branch_ref", 300)
        self._bounded_list(developer_claims, "developer_claims", contracts.MAX_DEVELOPER_CLAIMS, contracts.DEVELOPER_CLAIM_CHARS)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._require_repository_reconciled(connection, project_id)
            self._expect_version(ticket, expected_version)
            self.require_fresh_context(connection, ticket_id, context_snapshot_id)
            if ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
                raise ConflictError("Ticket is not in development")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            repository = _row(connection.execute(
                "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
            ).fetchone())
            if repository is not None and self._service_owns_the_workspace(repository):
                worktree = _row(connection.execute(
                    "SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)
                ).fetchone())
                if worktree is None or worktree["agent_id"] != actor.agent_id:
                    raise ValidationError("Developer must own an assigned Git worktree before submitting")
                if worktree["branch_ref"] != branch_ref:
                    raise ValidationError("Submitted branch does not match the assigned worktree branch")
            active = _row(connection.execute(
                """SELECT * FROM candidate_submission_jobs
                   WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING')
                   ORDER BY created_at DESC LIMIT 1""", (ticket_id,)
            ).fetchone())
            if active is not None:
                if (
                    active["requested_by"] == actor.agent_id
                    and active["lease_id"] == lease_id
                    and active["commit_sha"] == commit_sha
                    and active["base_commit"] == base_commit
                    and active["branch_ref"] == branch_ref
                ):
                    return {"ticket": ticket, "submission": self._candidate_submission_job(connection, active["submission_id"])}
                raise ConflictError("Ticket already has an active candidate submission")
            submission_id = _id(connection, "candidate_job")
            timestamp = _iso()
            connection.execute(
                """INSERT INTO candidate_submission_jobs
                   (submission_id, project_id, ticket_id, lease_id, expected_version, commit_sha,
                    base_commit, branch_ref, developer_claims, requested_by, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?)""",
                (submission_id, project_id, ticket_id, lease_id, expected_version, commit_sha,
                 base_commit, branch_ref, json.dumps(developer_claims), actor.agent_id, timestamp),
            )
            self._event(connection, actor, project_id, "candidate.submission_queued", "ticket",
                        ticket_id, ticket["version"], {"submission_id": submission_id})
            return {"ticket": self._ticket(connection, ticket_id),
                    "submission": self._candidate_submission_job(connection, submission_id)}

    def candidate_submission_requires_worker(self, actor: Actor, ticket_id: str) -> bool:
        """Compatibility path for repository-free in-memory workflow demonstrations."""
        require_role(actor, Role.DEVELOPER)
        with self.store.connection() as connection:
            _ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            return connection.execute(
                "SELECT 1 FROM project_repositories WHERE project_id = ?", (project_id,)
            ).fetchone() is not None

    def get_candidate_submission(
        self, actor: Actor, ticket_id: str, lease_id: str, submission_id: str,
        lease_seconds: int = 900,
    ) -> dict[str, Any]:
        """Poll one candidate submission and renew its Developer lease while active."""
        require_role(actor, Role.DEVELOPER)
        if not 30 <= lease_seconds <= 3600:
            raise ValidationError("Lease duration must be between 30 and 3600 seconds")
        with self.store.connection(immediate=True) as connection:
            ticket, _project_id = self._authorized_ticket(connection, actor, ticket_id)
            job = self._candidate_submission_job(connection, submission_id)
            if job["ticket_id"] != ticket_id or job["lease_id"] != lease_id:
                raise AuthorizationError("Candidate submission does not belong to this Developer lease")
            if job["requested_by"] != actor.agent_id:
                raise AuthorizationError("Candidate submission belongs to another Developer")
            lease: dict[str, Any] | None = None
            if job["status"] in {"QUEUED", "RUNNING"}:
                if ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
                    raise ConflictError("Ticket is not in active development")
                self._require_active_lease(connection, actor, ticket_id, lease_id)
                timestamp = _now()
                connection.execute("UPDATE leases SET heartbeat_at = ?, expires_at = ? WHERE lease_id = ?",
                    (_iso(timestamp), _iso(timestamp + timedelta(seconds=lease_seconds)), lease_id))
                lease = self._lease(connection, lease_id)
            return {"ticket": self._ticket(connection, ticket_id), "lease": lease,
                    "submission": self._candidate_submission_job(connection, submission_id)}

    def execute_queued_candidate_submission(self, submission_id: str) -> dict[str, Any]:
        """Worker-only Git validation and immutable candidate freeze."""
        preflight_error: str | None = None
        with self.store.connection(immediate=True) as connection:
            job = self._candidate_submission_job(connection, submission_id)
            if job["status"] != "QUEUED":
                return job
            actor = Actor(job["requested_by"], Role.DEVELOPER, frozenset({job["project_id"]}))
            ticket = self._ticket(connection, job["ticket_id"])
            try:
                self._require_repository_reconciled(connection, job["project_id"])
                if ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
                    raise ConflictError("Ticket is no longer in active development before candidate validation")
                self._expect_version(ticket, job["expected_version"])
                self._require_active_lease(connection, actor, job["ticket_id"], job["lease_id"])
            except DomainError as error:
                preflight_error = str(error)
            if preflight_error is None:
                connection.execute("UPDATE candidate_submission_jobs SET status = 'RUNNING', started_at = ? WHERE submission_id = ?",
                                   (_iso(), submission_id))
        if preflight_error is not None:
            return self.fail_queued_candidate_submission(submission_id, preflight_error)
        try:
            repository, worktree = self._candidate_submission_git_inputs(submission_id)
            commit_sha, base_commit, changed_paths = job["commit_sha"], job["base_commit"], []
            if repository is not None:
                if self._service_owns_the_workspace(repository):
                    assert worktree is not None
                    commit_sha, base_commit = self.git_worktrees.verify_candidate(
                        repository_path=repository["repository_path"], worktree_path=worktree["worktree_path"],
                        expected_branch=worktree["branch_ref"], expected_base_commit=worktree["base_commit"],
                        candidate_commit=job["commit_sha"], submitted_base_commit=job["base_commit"],
                        expected_iteration_parent=ticket.get("candidate_commit"),
                    )
                else:
                    # No service-side tree exists to inspect, so every fact comes
                    # from the repository the service itself can read.
                    commit_sha, base_commit = self.git_worktrees.verify_pushed_candidate(
                        repository_path=repository["repository_path"],
                        candidate_commit=job["commit_sha"],
                        submitted_base_commit=job["base_commit"],
                        expected_iteration_parent=ticket.get("candidate_commit"),
                    )
                changed_paths = self.git_worktrees.changed_paths(
                    repository_path=repository["repository_path"], base_commit=base_commit, candidate_commit=commit_sha
                )
            with self.store.connection() as connection:
                ticket = self._ticket(connection, job["ticket_id"])
                outside_scope = [path for path in changed_paths if not self._path_is_in_any_scope(path, ticket["file_scope"])]
            if outside_scope:
                raise ValidationError("Candidate changes paths outside ticket write scope: " + ", ".join(outside_scope))
            return self._complete_queued_candidate_submission(submission_id, commit_sha, base_commit, changed_paths)
        except DomainError as error:
            return self.fail_queued_candidate_submission(submission_id, str(error))

    @staticmethod
    def _service_owns_the_workspace(repository: dict[str, Any]) -> bool:
        """Whether the tree that produced a candidate is one the service holds."""
        return repository.get(
            "workspace_model", WorkspaceModel.COLOCATED.value
        ) == WorkspaceModel.COLOCATED.value

    def _candidate_submission_git_inputs(self, submission_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        with self.store.connection() as connection:
            job = self._candidate_submission_job(connection, submission_id)
            repository = _row(connection.execute("SELECT * FROM project_repositories WHERE project_id = ?", (job["project_id"],)).fetchone())
            worktree = _row(connection.execute("SELECT * FROM worktrees WHERE ticket_id = ?", (job["ticket_id"],)).fetchone())
            return repository, worktree

    def _complete_queued_candidate_submission(self, submission_id: str, commit_sha: str, base_commit: str,
                                              changed_paths: list[str]) -> dict[str, Any]:
        with self.store.connection(immediate=True) as connection:
            job = self._candidate_submission_job(connection, submission_id)
            if job["status"] != "RUNNING":
                return job
            actor = Actor(job["requested_by"], Role.DEVELOPER, frozenset({job["project_id"]}))
            ticket = self._ticket(connection, job["ticket_id"])
            self._require_repository_reconciled(connection, job["project_id"])
            if ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
                raise ConflictError("Ticket changed while candidate validation ran")
            self._expect_version(ticket, job["expected_version"])
            self._require_active_lease(connection, actor, job["ticket_id"], job["lease_id"])
            candidate_id = _id(connection, "cand")
            timestamp = _iso()
            connection.execute("""INSERT INTO candidates
                (candidate_id, ticket_id, base_commit, commit_sha, branch_ref, developer_claims, submitted_by, submitted_at, frozen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (candidate_id, job["ticket_id"], base_commit, commit_sha, job["branch_ref"], json.dumps(job["developer_claims"]), actor.agent_id, timestamp, timestamp))
            for path in changed_paths:
                connection.execute("INSERT INTO candidate_changed_paths (candidate_id, path) VALUES (?, ?)", (candidate_id, path))
            profile = _row(connection.execute("SELECT * FROM project_test_profiles WHERE project_id = ?", (job["project_id"],)).fetchone())
            if profile is not None:
                connection.execute("""INSERT INTO candidate_test_profiles (candidate_id, test_command, timeout_seconds, configured_at)
                    VALUES (?, ?, ?, ?)""", (candidate_id, json.dumps(profile["test_command"]), profile["timeout_seconds"], timestamp))
            next_version = _transition_ticket(
                connection, job["ticket_id"], actor=actor, expected_version=ticket["version"],
                from_statuses=(TicketStatus.DEV_IN_PROGRESS,),
                changes={"status": TicketStatus.QA_READY, "base_commit": base_commit,
                         "candidate_commit": commit_sha, "updated_at": timestamp},
            )
            connection.execute("DELETE FROM leases WHERE lease_id = ?", (job["lease_id"],))
            connection.execute("UPDATE worktrees SET last_verified_at = ? WHERE ticket_id = ?", (timestamp, job["ticket_id"]))
            connection.execute("""UPDATE candidate_submission_jobs SET status = 'COMPLETED', candidate_id = ?, error_message = NULL, completed_at = ?
                WHERE submission_id = ?""", (candidate_id, timestamp, submission_id))
            self._event(connection, actor, job["project_id"], "candidate.frozen", "ticket", job["ticket_id"], next_version,
                        {"candidate_id": candidate_id, "commit_sha": commit_sha, "submission_id": submission_id})
            qa_ready = self._ticket(connection, job["ticket_id"])
            self._queue_ticket(connection, actor, job["project_id"], qa_ready)
            return self._candidate_submission_job(connection, submission_id)

    def fail_queued_candidate_submission(self, submission_id: str, error_message: str) -> dict[str, Any]:
        self._bounded_text(error_message, "candidate_submission.error_message", 4000)
        with self.store.connection(immediate=True) as connection:
            job = self._candidate_submission_job(connection, submission_id)
            if job["status"] in {"COMPLETED", "FAILED"}:
                return job
            connection.execute("UPDATE candidate_submission_jobs SET status = 'FAILED', error_message = ?, completed_at = ? WHERE submission_id = ?",
                               (error_message, _iso(), submission_id))
            actor = Actor(job["requested_by"], Role.DEVELOPER, frozenset({job["project_id"]}))
            ticket = self._ticket(connection, job["ticket_id"])
            self._event(connection, actor, job["project_id"], "candidate.submission_failed", "ticket", job["ticket_id"], ticket["version"],
                        {"submission_id": submission_id})
            return self._candidate_submission_job(connection, submission_id)

    def _candidate_submission_job(self, connection: Connection, submission_id: str) -> dict[str, Any]:
        job = self._one(connection, "SELECT * FROM candidate_submission_jobs WHERE submission_id = ?", (submission_id,))
        if isinstance(job["developer_claims"], str):
            job["developer_claims"] = json.loads(job["developer_claims"])
        if job["candidate_id"] is not None:
            job["candidate"] = self._candidate_with_changed_paths(connection, self._one(
                connection, "SELECT * FROM candidates WHERE candidate_id = ?", (job["candidate_id"],)))
        else:
            job["candidate"] = None
        return job

    def _check_candidate_submittable(
        self,
        connection: Connection,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        expected_version: int,
        context_snapshot_id: str,
    ) -> None:
        ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
        self._require_repository_reconciled(connection, project_id)
        self._expect_version(ticket, expected_version)
        self.require_fresh_context(connection, ticket_id, context_snapshot_id)
        if ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
            raise ConflictError("Ticket is not in development")
        self._require_active_lease(connection, actor, ticket_id, lease_id)

    def submit_candidate(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        expected_version: int,
        commit_sha: str,
        base_commit: str,
        branch_ref: str,
        developer_claims: list[str],
        context_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        require_role(actor, Role.DEVELOPER)
        context_snapshot_id = require_snapshot_id(context_snapshot_id, "dev.submit_candidate")
        commit_sha = self._full_commit(actor, ticket_id, commit_sha, "commit_sha")
        base_commit = self._full_commit(actor, ticket_id, base_commit, "base_commit")
        self._bounded_text(branch_ref, "branch_ref", 300)
        self._bounded_list(developer_claims, "developer_claims", contracts.MAX_DEVELOPER_CLAIMS, contracts.DEVELOPER_CLAIM_CHARS)
        # Read -> Git with no transaction held -> re-validate and write. Git
        # verification can take seconds, and the immediate connection is the one
        # global write lock, so it must never wrap a subprocess.
        with self.store.connection() as connection:
            self._check_candidate_submittable(
                connection, actor, ticket_id, lease_id, expected_version, context_snapshot_id
            )
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            repository = _row(
                connection.execute(
                    "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            worktree: dict[str, Any] | None = None
            if repository is not None and self._service_owns_the_workspace(repository):
                worktree = _row(
                    connection.execute(
                        "SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)
                    ).fetchone()
                )
                if worktree is None:
                    raise ValidationError(
                        "Developer must prepare the assigned Git worktree before submitting a candidate"
                    )
                if worktree["agent_id"] != actor.agent_id:
                    raise AuthorizationError("Ticket worktree belongs to another Developer")
                if branch_ref != worktree["branch_ref"]:
                    raise ValidationError("Submitted branch does not match the assigned worktree branch")
        changed_paths: list[str] = []
        if repository is not None:
            if worktree is None:
                # The Developer holds its own checkout, so there is no service-side
                # tree to inspect and every fact comes from what the service fetched.
                commit_sha, base_commit = self.git_worktrees.verify_pushed_candidate(
                    repository_path=repository["repository_path"],
                    candidate_commit=commit_sha,
                    submitted_base_commit=base_commit,
                    expected_iteration_parent=ticket.get("candidate_commit"),
                )
            else:
                commit_sha, base_commit = self.git_worktrees.verify_candidate(
                    repository_path=repository["repository_path"],
                    worktree_path=worktree["worktree_path"],
                    expected_branch=worktree["branch_ref"],
                    expected_base_commit=worktree["base_commit"],
                    candidate_commit=commit_sha,
                    submitted_base_commit=base_commit,
                    expected_iteration_parent=ticket.get("candidate_commit"),
                )
            changed_paths = self.git_worktrees.changed_paths(
                repository_path=repository["repository_path"],
                base_commit=base_commit,
                candidate_commit=commit_sha,
            )
            paths_outside_scope = [
                path
                for path in changed_paths
                if not self._path_is_in_any_scope(path, ticket["file_scope"])
            ]
            if paths_outside_scope:
                raise ValidationError(
                    "Candidate changes paths outside ticket write scope: "
                    + ", ".join(paths_outside_scope)
                )
        with self.store.connection(immediate=True) as connection:
            # Everything the Git result was judged against is re-checked: the
            # version also pins file_scope and the prior candidate commit.
            self._check_candidate_submittable(
                connection, actor, ticket_id, lease_id, expected_version, context_snapshot_id
            )
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            if worktree is not None:
                current = _row(
                    connection.execute(
                        "SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)
                    ).fetchone()
                )
                if current is None or any(
                    current[key] != worktree[key]
                    for key in ("agent_id", "worktree_path", "branch_ref", "base_commit")
                ):
                    raise ConflictError("Ticket worktree changed while candidate validation ran")
                connection.execute(
                    "UPDATE worktrees SET last_verified_at = ? WHERE ticket_id = ?",
                    (_iso(), ticket_id),
                )
            candidate_id = _id(connection, "cand")
            timestamp = _iso()
            connection.execute(
                """
                INSERT INTO candidates
                    (candidate_id, ticket_id, base_commit, commit_sha, branch_ref,
                     developer_claims, submitted_by, submitted_at, frozen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    ticket_id,
                    base_commit,
                    commit_sha,
                    branch_ref,
                    json.dumps(developer_claims),
                    actor.agent_id,
                    timestamp,
                    timestamp,
                ),
            )
            for path in changed_paths:
                connection.execute(
                    "INSERT INTO candidate_changed_paths (candidate_id, path) VALUES (?, ?)",
                    (candidate_id, path),
                )
            test_profile = _row(
                connection.execute(
                    "SELECT * FROM project_test_profiles WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            if test_profile is not None:
                connection.execute(
                    """
                    INSERT INTO candidate_test_profiles
                        (candidate_id, test_command, timeout_seconds, configured_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        json.dumps(test_profile["test_command"]),
                        test_profile["timeout_seconds"],
                        timestamp,
                    ),
                )
            next_version = _transition_ticket(
                connection, ticket_id, actor=actor, expected_version=ticket["version"],
                from_statuses=(TicketStatus.DEV_IN_PROGRESS,),
                changes={"status": TicketStatus.QA_READY, "base_commit": base_commit,
                         "candidate_commit": commit_sha, "updated_at": timestamp},
            )
            connection.execute("DELETE FROM leases WHERE lease_id = ?", (lease_id,))
            self._event(
                connection,
                actor,
                project_id,
                "candidate.frozen",
                "ticket",
                ticket_id,
                next_version,
                {"candidate_id": candidate_id, "commit_sha": commit_sha},
            )
            qa_ready_ticket = self._ticket(connection, ticket_id)
            self._queue_ticket(connection, actor, project_id, qa_ready_ticket)
            return {
                "ticket": qa_ready_ticket,
                "candidate": self._candidate_with_changed_paths(connection, self._one(
                    connection,
                    "SELECT * FROM candidates WHERE candidate_id = ?",
                    (candidate_id,),
                )),
            }
