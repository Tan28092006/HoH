from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    AuthorizationError,
    CLEAN_CHECKPOINT,
    ConflictError,
    DIRTY,
    DomainError,
    Path,
    Role,
    TicketStatus,
    ValidationError,
    WorkspaceModel,
    _bounded_join,
    _file_token,
    _id,
    _iso,
    _now,
    _row,
    _safe_path_segment,
    require_role,
    Connection,
    timedelta,
)


class WorktreeMixin:
    """Developer claim and ticket-bound Git worktree provisioning."""

    def claim_ticket(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        lease_seconds: int,
        context_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        require_role(actor, Role.DEVELOPER)
        return self._claim(
            actor,
            ticket_id,
            expected_version,
            lease_seconds,
            allowed_statuses={TicketStatus.READY, TicketStatus.REWORK},
            next_status=TicketStatus.DEV_IN_PROGRESS,
            event_type="ticket.claimed",
            context_snapshot_id=context_snapshot_id,
        )

    def prepare_worktree(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        expected_version: int,
    ) -> dict[str, Any]:
        """Create one ticket-and-agent-bound Git worktree for active Developer work.

        Inspecting a replaced Developer's worktree shells out to Git, so it runs
        between two short transactions rather than inside the write lock.
        """
        require_role(actor, Role.DEVELOPER)
        snapshot = self._worktree_preparation_snapshot(
            actor, ticket_id, lease_id, expected_version
        )
        if snapshot["reused"] is not None:
            return snapshot["reused"]
        takeover = self._plan_worktree_takeover(snapshot)
        return self._assign_worktree(
            actor, ticket_id, lease_id, expected_version, snapshot, takeover
        )

    @staticmethod
    def _require_colocated_workspace(repository: dict[str, Any]) -> None:
        """Refuse to provision a path the caller could not reach.

        Under the agent-workspace model the Developer holds its own checkout on
        its own machine, so a worktree created here would be a path that does not
        exist for it. Failing now is the honest answer; succeeding would hand back
        a location the agent cannot open and turn a configuration mistake into a
        confusing Git error later.
        """
        if repository.get("workspace_model", WorkspaceModel.COLOCATED.value) != (
            WorkspaceModel.COLOCATED.value
        ):
            raise ConflictError(
                "This project uses the agent-workspace model, so the service does not "
                "provision worktrees; the Developer works in its own checkout",
                {"reason": "workspace_model_not_colocated"},
            )

    def _worktree_preparation_snapshot(
        self, actor: Actor, ticket_id: str, lease_id: str, expected_version: int
    ) -> dict[str, Any]:
        """Authorize the request and read what the Git inspection needs."""
        with self.store.connection() as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._require_repository_reconciled(connection, project_id, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
                raise ConflictError("Worktrees can only be prepared for active development work")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            repository = _row(
                connection.execute(
                    "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            if repository is None:
                raise ValidationError(
                    "Planner must configure a project Git repository before preparing a worktree"
                )
            self._require_colocated_workspace(repository)
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            existing = _row(
                connection.execute(
                    "SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)
                ).fetchone()
            )
            reused = None
            ownership = connection.execute('SELECT * FROM worktree_ownership WHERE ticket_id = ?', (ticket_id,)).fetchone()
            if (existing is not None and existing["agent_id"] == actor.agent_id
                    and ownership is not None and ownership['lease_id'] == lease_id
                    and ownership['branch_ref'] == existing['branch_ref']):
                reused = {"ticket": ticket, "worktree": existing, "reused": True}
            return {
                "ticket": ticket,
                "project_id": project_id,
                "project": project,
                "repository": repository,
                "existing": existing,
                "reused": reused,
            }

    def _plan_worktree_takeover(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        """Decide what a replacement Developer may safely inherit from a predecessor."""
        existing = snapshot["existing"]
        if existing is None:
            return None
        ticket = snapshot["ticket"]
        repository_path = snapshot["repository"]["repository_path"]
        default_branch = snapshot["project"]["default_branch"]
        if ticket["candidate_commit"] is not None:
            candidate_is_current = self.git_worktrees.commit_descends_from(
                repository_path=repository_path,
                ancestor_commit=f"refs/heads/{default_branch}",
                descendant_commit=ticket["candidate_commit"],
            )
            return {
                "handoff_mode": "candidate_continuation" if candidate_is_current else "rebase_required",
                "start_commit": ticket["candidate_commit"] if candidate_is_current else None,
                "checkpoint": None,
                "artifact": None,
            }

        checkpoint = self.git_worktrees.inspect_checkpoint(
            repository_path=repository_path,
            worktree_path=existing["worktree_path"],
            expected_branch=existing["branch_ref"],
            expected_base_commit=existing["base_commit"],
            default_branch=default_branch,
        )
        if checkpoint.status == CLEAN_CHECKPOINT:
            return {
                "handoff_mode": "checkpoint_continuation",
                "start_commit": checkpoint.head_commit,
                "checkpoint": checkpoint,
                "artifact": None,
            }
        artifact = None
        if checkpoint.status == DIRTY:
            artifact = self.git_worktrees.capture_recovery_patch(
                worktree_path=existing["worktree_path"],
                destination=self._recovery_artifact_path(
                    snapshot["project_id"], ticket["ticket_id"], existing["agent_id"]
                ),
            )
        return {
            "handoff_mode": "recovery_required",
            "start_commit": None,
            "checkpoint": checkpoint,
            "artifact": artifact,
        }

    def _recovery_artifact_path(self, project_id: str, ticket_id: str, agent_id: str) -> Path:
        segments = [
            _safe_path_segment(project_id, "project_id"),
            _safe_path_segment(ticket_id, "ticket_id"),
            _safe_path_segment(agent_id, "agent_id"),
        ]
        return self.recovery_root.joinpath(*segments) / f"recovery_{_file_token()}.patch"

    def _assign_worktree(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        expected_version: int,
        snapshot: dict[str, Any],
        takeover: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Provision outside the database lock, then publish after reauthorization.

        A unique attempt path also lets an A -> B -> A takeover preserve A's old
        directory. If authorization expires during Git, the unpublished checkout
        is retained for operator recovery; it is never handed to an agent.
        """
        # The attempt number is allocated in its own short write before Git runs,
        # so the branch and directory names exist before the publish lock is
        # taken and never depend on how long provisioning takes.
        with self.store.connection(immediate=True) as connection:
            generation = _id(connection, "attempt")
        prepared = self.git_worktrees.prepare(
            repository_path=snapshot["repository"]["repository_path"],
            default_branch=snapshot["project"]["default_branch"],
            project_id=snapshot["project_id"],
            ticket_id=ticket_id,
            agent_id=actor.agent_id,
            start_commit=None if takeover is None else takeover["start_commit"],
            generation=generation,
        )
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._require_repository_reconciled(connection, project_id, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
                raise ConflictError("Worktrees can only be prepared for active development work")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            repository_now = _row(connection.execute(
                "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
            ).fetchone())
            if repository_now is None or any(
                repository_now[field] != snapshot["repository"][field]
                for field in ("repository_path", "workspace_model")
            ):
                raise ConflictError("Repository configuration changed while the worktree was being prepared")
            existing = _row(
                connection.execute(
                    "SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)
                ).fetchone()
            )
            ownership = connection.execute('SELECT * FROM worktree_ownership WHERE ticket_id = ?', (ticket_id,)).fetchone()
            if (existing is not None and existing["agent_id"] == actor.agent_id
                    and ownership is not None and ownership['lease_id'] == lease_id
                    and ownership['branch_ref'] == existing['branch_ref']):
                return {"ticket": ticket, "worktree": existing, "reused": True}
            inspected = None if snapshot["existing"] is None else snapshot["existing"]["branch_ref"]
            current = None if existing is None else existing["branch_ref"]
            if current != inspected:
                raise ConflictError(
                    "Ticket worktree assignment changed while it was being prepared"
                )

            timestamp = _iso()
            connection.execute('DELETE FROM worktree_ownership WHERE ticket_id = ?', (ticket_id,))
            connection.execute('INSERT INTO worktree_ownership (ticket_id, lease_id, branch_ref) VALUES (?, ?, ?)',
                               (ticket_id, lease_id, prepared.branch_ref))
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO worktrees
                        (ticket_id, project_id, agent_id, repository_path, worktree_path,
                         branch_ref, base_commit, created_at, last_verified_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ticket_id,
                        project_id,
                        actor.agent_id,
                        prepared.repository_path,
                        prepared.worktree_path,
                        prepared.branch_ref,
                        prepared.base_commit,
                        timestamp,
                        timestamp,
                    ),
                )
                event_type = "worktree.prepared"
                event_payload: dict[str, Any] = {"branch_ref": prepared.branch_ref}
            else:
                connection.execute(
                    """
                    UPDATE worktrees
                    SET agent_id = ?, repository_path = ?, worktree_path = ?, branch_ref = ?,
                        base_commit = ?, created_at = ?, last_verified_at = ?
                    WHERE ticket_id = ?
                    """,
                    (
                        actor.agent_id,
                        prepared.repository_path,
                        prepared.worktree_path,
                        prepared.branch_ref,
                        prepared.base_commit,
                        timestamp,
                        timestamp,
                        ticket_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE worktree_assignments SET released_at = ?
                    WHERE ticket_id = ? AND released_at IS NULL
                    """,
                    (timestamp, ticket_id),
                )
                assert takeover is not None
                event_type = "worktree.handed_off"
                event_payload = {
                    "previous_agent_id": existing["agent_id"],
                    "previous_branch_ref": existing["branch_ref"],
                    "branch_ref": prepared.branch_ref,
                    "rejected_candidate_commit": ticket["candidate_commit"],
                    "seed_commit": takeover["start_commit"] or prepared.base_commit,
                    "handoff_mode": takeover["handoff_mode"],
                }
                self._record_handoff_context(
                    connection,
                    actor,
                    project_id,
                    ticket,
                    existing,
                    prepared,
                    takeover,
                    snapshot["project"]["default_branch"],
                    timestamp,
                )
            connection.execute(
                """
                INSERT INTO worktree_assignments
                    (assignment_id, ticket_id, project_id, agent_id, worktree_path, branch_ref,
                     base_commit, seed_commit, handoff_mode, previous_agent_id, assigned_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _id(connection, "wtassign"),
                    ticket_id,
                    project_id,
                    actor.agent_id,
                    prepared.worktree_path,
                    prepared.branch_ref,
                    prepared.base_commit,
                    None if takeover is None else takeover["start_commit"],
                    None if takeover is None else takeover["handoff_mode"],
                    None if existing is None else existing["agent_id"],
                    timestamp,
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                event_type,
                "ticket",
                ticket_id,
                ticket["version"],
                event_payload,
            )
            worktree = self._one(
                connection, "SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)
            )
            return {"ticket": ticket, "worktree": worktree, "reused": False}

    def _record_handoff_context(
        self,
        connection: Connection,
        actor: Actor,
        project_id: str,
        ticket: dict[str, Any],
        previous: dict[str, Any],
        prepared: Any,
        takeover: dict[str, Any],
        default_branch: str,
        timestamp: str,
    ) -> None:
        """Leave the replacement a durable, redacted account of what was inherited."""
        mode = takeover["handoff_mode"]
        if mode == "candidate_continuation":
            return
        lineage = (
            f"Replaced Developer {previous['agent_id']} held branch {previous['branch_ref']} "
            f"at base {previous['base_commit']}."
        )
        if mode == "rebase_required":
            entry_type = "worktree_handoff"
            source_ref = f"candidate:{ticket['candidate_commit']}"
            artifact_commit = ticket["candidate_commit"]
            content = (
                "Rejected candidate diverges from the current default branch. "
                "The new Developer worktree starts at current main; inspect the "
                "frozen candidate and explicitly rebase, cherry-pick, or reimplement "
                "the required changes. No automatic merge was performed. " + lineage
            )
        elif mode == "checkpoint_continuation":
            entry_type = "worktree_handoff"
            source_ref = f"worktree:{previous['branch_ref']}"
            artifact_commit = takeover["start_commit"]
            content = (
                f"{lineage} That worktree was clean at verified checkpoint "
                f"{takeover['start_commit']}, so replacement branch {prepared.branch_ref} "
                "is seeded from that commit. The prior worktree stays read-only and its "
                "branch can no longer freeze a candidate."
            )
        else:
            entry_type = "worktree_recovery"
            checkpoint = takeover["checkpoint"]
            artifact = takeover["artifact"]
            source_ref = None if artifact is None else artifact.path
            artifact_commit = checkpoint.head_commit
            parts = [
                f"Failed closed on code transfer. {lineage} {checkpoint.detail} "
                f"Replacement branch {prepared.branch_ref} starts from current "
                f"{default_branch}; nothing from the prior worktree was trusted, "
                "staged, committed or merged."
            ]
            if artifact is not None:
                parts.append(
                    f"A private recovery patch was retained: sha256={artifact.sha256} "
                    f"bytes={artifact.byte_size}. Review and apply it explicitly, or discard it."
                )
            if checkpoint.untracked_paths:
                parts.append(
                    "Untracked paths left behind (names only): "
                    + _bounded_join(checkpoint.untracked_paths)
                )
            content = " ".join(parts)
        connection.execute(
            """
            INSERT INTO context_entries
                (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                 source_ref, artifact_commit, actor_id, actor_role, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _id(connection, "ctx"),
                project_id,
                ticket["feature_id"],
                ticket["ticket_id"],
                entry_type,
                content,
                source_ref,
                artifact_commit,
                actor.agent_id,
                actor.role.value,
                timestamp,
            ),
        )

    def queue_worktree_provision(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        expected_version: int,
    ) -> dict[str, Any]:
        """Queue potentially slow Git worktree setup for an isolated worker."""
        require_role(actor, Role.DEVELOPER)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
                raise ConflictError("Worktrees can only be prepared for active development work")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            repository = _row(
                connection.execute(
                    "SELECT * FROM project_repositories WHERE project_id = ?", (project_id,)
                ).fetchone()
            )
            if repository is not None:
                self._require_colocated_workspace(repository)
            existing = _row(
                connection.execute(
                    "SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)
                ).fetchone()
            )
            if existing is not None and existing["agent_id"] == actor.agent_id:
                return {"ticket": ticket, "worktree": existing, "provision": None, "reused": True}
            active = _row(
                connection.execute(
                    """
                    SELECT * FROM worktree_provision_jobs
                    WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (ticket_id,),
                ).fetchone()
            )
            if active is not None:
                if active["requested_by"] != actor.agent_id or active["lease_id"] != lease_id:
                    raise ConflictError("Ticket already has an active worktree provision")
                return {"ticket": ticket, "provision": active, "reused": False}

            provision_id = _id(connection, "worktree_job")
            timestamp = _iso()
            connection.execute(
                """
                INSERT INTO worktree_provision_jobs
                    (provision_id, project_id, ticket_id, lease_id, expected_version,
                     requested_by, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', ?)
                """,
                (
                    provision_id,
                    project_id,
                    ticket_id,
                    lease_id,
                    expected_version,
                    actor.agent_id,
                    timestamp,
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                "worktree.provision_queued",
                "ticket",
                ticket_id,
                ticket["version"],
                {"provision_id": provision_id},
            )
            return {
                "ticket": ticket,
                "provision": self._worktree_provision_job(connection, provision_id),
                "reused": False,
            }

    def get_worktree_provision(
        self,
        actor: Actor,
        ticket_id: str,
        lease_id: str,
        provision_id: str,
        lease_seconds: int = 900,
    ) -> dict[str, Any]:
        """Poll a worktree provision job while renewing its exclusive Developer lease."""
        require_role(actor, Role.DEVELOPER)
        if not 30 <= lease_seconds <= 3600:
            raise ValidationError("Lease duration must be between 30 and 3600 seconds")
        with self.store.connection(immediate=True) as connection:
            ticket, _project_id = self._authorized_ticket(connection, actor, ticket_id)
            if ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
                raise ConflictError("Ticket is not in active development")
            self._require_active_lease(connection, actor, ticket_id, lease_id)
            provision = self._worktree_provision_job(connection, provision_id)
            if provision["ticket_id"] != ticket_id or provision["lease_id"] != lease_id:
                raise AuthorizationError("Worktree provision does not belong to this Developer lease")
            if provision["requested_by"] != actor.agent_id:
                raise AuthorizationError("Worktree provision belongs to another Developer")
            timestamp = _now()
            connection.execute(
                "UPDATE leases SET heartbeat_at = ?, expires_at = ? WHERE lease_id = ?",
                (_iso(timestamp), _iso(timestamp + timedelta(seconds=lease_seconds)), lease_id),
            )
            worktree = _row(
                connection.execute("SELECT * FROM worktrees WHERE ticket_id = ?", (ticket_id,)).fetchone()
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "lease": self._lease(connection, lease_id),
                "provision": provision,
                "worktree": worktree,
            }

    def execute_queued_worktree_provision(self, provision_id: str) -> dict[str, Any]:
        """Worker-only Git setup for a previously authorized Developer job."""
        preflight_error: str | None = None
        with self.store.connection(immediate=True) as connection:
            provision = self._worktree_provision_job(connection, provision_id)
            if provision["status"] != "QUEUED":
                return provision
            actor = Actor(
                provision["requested_by"], Role.DEVELOPER, frozenset({provision["project_id"]})
            )
            ticket = self._ticket(connection, provision["ticket_id"])
            try:
                self._require_repository_reconciled(connection, provision["project_id"], provision["ticket_id"])
            except DomainError as error:
                preflight_error = str(error)
            if preflight_error is None and ticket["status"] != TicketStatus.DEV_IN_PROGRESS.value:
                preflight_error = "Ticket is no longer in active development before worktree setup."
            elif preflight_error is None:
                try:
                    self._require_active_lease(
                        connection, actor, provision["ticket_id"], provision["lease_id"]
                    )
                    self._expect_version(ticket, provision["expected_version"])
                except DomainError as error:
                    preflight_error = str(error)
            if preflight_error is None:
                connection.execute(
                    """
                    UPDATE worktree_provision_jobs
                    SET status = 'RUNNING', started_at = ? WHERE provision_id = ?
                    """,
                    (_iso(), provision_id),
                )

        if preflight_error is not None:
            return self.fail_queued_worktree_provision(provision_id, preflight_error)

        try:
            self.prepare_worktree(
                actor,
                provision["ticket_id"],
                provision["lease_id"],
                provision["expected_version"],
            )
        except DomainError as error:
            return self.fail_queued_worktree_provision(provision_id, str(error))

        with self.store.connection(immediate=True) as connection:
            completed = self._worktree_provision_job(connection, provision_id)
            if completed["status"] != "RUNNING":
                return completed
            timestamp = _iso()
            connection.execute(
                """
                UPDATE worktree_provision_jobs
                SET status = 'COMPLETED', error_message = NULL, completed_at = ?
                WHERE provision_id = ?
                """,
                (timestamp, provision_id),
            )
            ticket = self._ticket(connection, completed["ticket_id"])
            actor = Actor(
                completed["requested_by"], Role.DEVELOPER, frozenset({completed["project_id"]})
            )
            self._event(
                connection,
                actor,
                completed["project_id"],
                "worktree.provision_completed",
                "ticket",
                completed["ticket_id"],
                ticket["version"],
                {"provision_id": provision_id},
            )
            return self._worktree_provision_job(connection, provision_id)

    def fail_queued_worktree_provision(
        self, provision_id: str, error_message: str
    ) -> dict[str, Any]:
        """Persist a worker error so the owning Developer can decide how to proceed."""
        error_message = self._failure_text(error_message, 4000)
        with self.store.connection(immediate=True) as connection:
            provision = self._worktree_provision_job(connection, provision_id)
            if provision["status"] in {"COMPLETED", "FAILED"}:
                return provision
            timestamp = _iso()
            connection.execute(
                """
                UPDATE worktree_provision_jobs
                SET status = 'FAILED', error_message = ?, completed_at = ?
                WHERE provision_id = ?
                """,
                (error_message, timestamp, provision_id),
            )
            ticket = self._ticket(connection, provision["ticket_id"])
            actor = Actor(
                provision["requested_by"], Role.DEVELOPER, frozenset({provision["project_id"]})
            )
            self._event(
                connection,
                actor,
                provision["project_id"],
                "worktree.provision_failed",
                "ticket",
                provision["ticket_id"],
                ticket["version"],
                {"provision_id": provision_id},
            )
            return self._worktree_provision_job(connection, provision_id)

    def _worktree_provision_job(
        self, connection: Connection, provision_id: str
    ) -> dict[str, Any]:
        return self._one(
            connection,
            "SELECT * FROM worktree_provision_jobs WHERE provision_id = ?",
            (provision_id,),
        )
