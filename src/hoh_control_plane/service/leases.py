from __future__ import annotations

from hoh_control_plane import scopes
from hoh_control_plane.service._common import (
    Actor,
    Any,
    AuthorizationError,
    ConflictError,
    DomainError,
    Role,
    SkillCompatibilityError,
    TicketStatus,
    ValidationError,
    _id,
    _iso,
    _now,
    _row,
    _required_execution_capabilities,
    _transition_ticket,
    datetime,
    json,
    re,
    require_project,
    require_role,
    Connection,
    timedelta,
)


class LeaseMixin:
    """Leases, claims, authorization and write-scope collision."""

    def heartbeat(self, actor: Actor, lease_id: str, lease_seconds: int = 900) -> dict[str, Any]:
        if not 30 <= lease_seconds <= 3600:
            raise ValidationError("Lease duration must be between 30 and 3600 seconds")
        with self.store.connection(immediate=True) as connection:
            lease = self._lease(connection, lease_id)
            if lease["agent_id"] != actor.agent_id or lease["role"] != actor.role.value:
                raise AuthorizationError("Lease belongs to another actor")
            timestamp = _now()
            if datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00")) <= timestamp:
                raise ConflictError("Lease has expired", {"reason": "lease_expired"})
            expires_at = _iso(timestamp + timedelta(seconds=lease_seconds))
            connection.execute(
                "UPDATE leases SET heartbeat_at = ?, expires_at = ? WHERE lease_id = ?",
                (_iso(timestamp), expires_at, lease_id),
            )
            renewed = self._lease(connection, lease_id)
            project_id = self._project_for_ticket(connection, lease["ticket_id"])
            renewed["planning_barrier"] = self._repository_reconciliation_state(
                connection, project_id
            )
            return renewed

    def sweep_expired_leases(self, actor: Actor) -> dict[str, Any]:
        """Recover expired Developer, QA and Planner job leases without waiting for a later claim."""
        require_role(actor, Role.ADMIN)
        timestamp = _now()
        requeued: list[dict[str, Any]] = []
        with self.store.connection(immediate=True) as connection:
            expired_leases = connection.execute(
                "SELECT * FROM leases WHERE expires_at <= ? ORDER BY expires_at ASC",
                (_iso(timestamp),),
            ).fetchall()
            for lease_row in expired_leases:
                lease = _row(lease_row)
                assert lease is not None
                if connection.execute("SELECT 1 FROM merge_jobs WHERE lease_id = ? AND status = 'RUNNING'", (lease['lease_id'],)).fetchone():
                    # Git may have landed. Recovery holds the repository lock
                    # and settles this intent before another owner is admitted.
                    continue
                ticket = self._ticket(connection, lease["ticket_id"])
                project_id = self._project_for_ticket(connection, ticket["ticket_id"])
                require_project(actor, project_id)
                if not self._lease_owns_ticket_state(lease, ticket):
                    self._retire_expired_jobs(connection, lease)
                    connection.execute("DELETE FROM leases WHERE lease_id = ?", (lease["lease_id"],))
                    self._event(connection, actor, project_id, "lease.orphan_retired", "ticket",
                                ticket["ticket_id"], ticket["version"], {"state_preserved": ticket["status"]})
                    continue
                fallback = (
                    TicketStatus.MERGE_READY
                    if lease["role"] == Role.INTEGRATOR.value
                    else TicketStatus.QA_READY
                    if lease["role"] == Role.QA.value
                    else TicketStatus.READY
                )
                self._retire_expired_jobs(connection, lease)
                connection.execute("DELETE FROM leases WHERE lease_id = ?", (lease["lease_id"],))
                next_version = _transition_ticket(
                    connection, ticket["ticket_id"], actor=actor, expected_version=ticket["version"],
                    from_statuses=(ticket["status"],),
                    changes={"status": fallback, "updated_at": _iso(timestamp)},
                )
                self._event(
                    connection,
                    actor,
                    project_id,
                    "lease.expired_requeued",
                    "ticket",
                    ticket["ticket_id"],
                    next_version,
                    {"requeued_to": fallback.value, "previous_role": lease["role"]},
                )
                requeued_ticket = self._ticket(connection, ticket["ticket_id"])
                self._queue_ticket(connection, actor, project_id, requeued_ticket)
                requeued.append(
                    {
                        "ticket_id": ticket["ticket_id"],
                        "project_id": project_id,
                        "status": fallback.value,
                        "version": next_version,
                    }
                )
            planner_jobs = self._expire_planner_job_leases(connection)
        return {
            "count": len(requeued),
            "requeued": requeued,
            "planner_jobs_requeued": planner_jobs,
        }

    @staticmethod
    def _lease_owns_ticket_state(lease: dict[str, Any], ticket: dict[str, Any]) -> bool:
        owned = {
            Role.DEVELOPER.value: {TicketStatus.DEV_IN_PROGRESS.value},
            Role.QA.value: {TicketStatus.QA_IN_PROGRESS.value},
            Role.INTEGRATOR.value: {TicketStatus.MERGE_READY.value},
        }
        return ticket["status"] in owned.get(lease["role"], set())

    @staticmethod
    def _retire_expired_jobs(connection: Connection, lease: dict[str, Any]) -> None:
        # These workers reauthorize before publishing. Fence their old DB jobs
        # as the lease is retired, otherwise the partial unique indexes prevent
        # every replacement from queueing work indefinitely after a crash.
        for table in ("worktree_provision_jobs", "candidate_submission_jobs", "qa_test_jobs"):
            connection.execute(
                f"UPDATE {table} SET status = 'FAILED', completed_at = ?, error_message = ? "
                "WHERE lease_id = ? AND status IN ('QUEUED', 'RUNNING')",
                (_iso(), "Owning lease expired; replacement work requires a new authorized job.", lease["lease_id"]),
            )
        # A running merge may already have changed Git. Keep that record for
        # reconciliation; blindly retrying it could duplicate an external effect.
        connection.execute(
            "UPDATE merge_jobs SET status = 'FAILED', completed_at = ?, error_message = ? "
            "WHERE lease_id = ? AND status = 'QUEUED'",
            (_iso(), "Owning lease expired before merge execution.", lease["lease_id"]),
        )

    def record_tool_call(
        self,
        actor: Actor,
        *,
        tool_name: str,
        input_summary: dict[str, Any],
        outcome: str,
        project_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """Store redacted transport-level MCP activity, separate from domain audit events."""
        if outcome not in {"succeeded", "rejected", "failed"}:
            raise ValidationError("Tool-call outcome is invalid")
        self._bounded_text(tool_name, "tool_name", 120)
        with self.store.connection(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO agent_tool_calls
                    (call_id, project_id, actor_id, actor_role, tool_name, input_summary,
                     outcome, error_code, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _id(connection, "toolcall"), project_id, actor.agent_id, actor.role.value, tool_name,
                    json.dumps(input_summary), outcome, error_code, _iso(),
                ),
            )

    @staticmethod
    def _require_skill_compatible(
        connection: Connection, actor: Actor, project_id: str
    ) -> None:
        """Refuse a claim from an agent whose declared skills fail this project's policy.

        Silent by default: an agent that never registered a manifest, or one
        whose project has no configured policy, is treated as compatible, so
        introducing policy enforcement never retroactively locks out an agent
        that predates it.
        """
        presence = _row(
            connection.execute(
                "SELECT skill_compatibility FROM agent_presence WHERE agent_id = ?",
                (actor.agent_id,),
            ).fetchone()
        )
        if presence is None:
            return
        verdict = presence["skill_compatibility"].get(project_id)
        if verdict is not None and not verdict["compatible"]:
            raise SkillCompatibilityError(
                "Agent skill/protocol version does not satisfy project policy",
                {"project_id": project_id, "reasons": verdict["reasons"]},
            )

    @staticmethod
    def _require_qa_execution_capabilities(
        connection: Connection, actor: Actor, project_id: str, ticket: dict[str, Any]
    ) -> None:
        """Require a strict-project QA to have the equipment promised at check-in."""
        if actor.role is not Role.QA:
            return
        project = _row(
            connection.execute(
                "SELECT verification_profile FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        )
        if project is None or (project.get("verification_profile") or {}).get(
            "quality_policy"
        ) != "claim-evidence-v1":
            return
        presence = _row(
            connection.execute(
                "SELECT execution_capabilities, approved_execution_capabilities, "
                "machine_id, capability_approved_machine_id "
                "FROM agent_presence WHERE agent_id = ?",
                (actor.agent_id,),
            ).fetchone()
        )
        if presence is None:
            raise ValidationError(
                "QA must register verified execution capabilities before claiming strict work",
                {"reason": "execution_capabilities_not_registered"},
            )
        required = _required_execution_capabilities(ticket)
        declared = set(presence.get("execution_capabilities") or [])
        approved = set(presence.get("approved_execution_capabilities") or [])
        if presence.get("machine_id") != presence.get("capability_approved_machine_id"):
            raise ValidationError(
                "QA session machine does not match its approved capability profile",
                {"reason": "execution_capability_machine_mismatch"},
            )
        unapproved = sorted(required - approved)
        if unapproved:
            raise ValidationError(
                "QA session lacks an approved capability profile for this ticket",
                {"reason": "execution_capabilities_unapproved", "capabilities": unapproved},
            )
        missing = sorted(required - declared)
        if missing:
            raise ValidationError(
                "QA session lacks execution capabilities required by this ticket",
                {"reason": "execution_capabilities_missing", "capabilities": missing},
            )

    def _claim(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        lease_seconds: int,
        allowed_statuses: set[TicketStatus],
        next_status: TicketStatus,
        event_type: str,
        context_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        if not 30 <= lease_seconds <= 3600:
            raise ValidationError("Lease duration must be between 30 and 3600 seconds")
        with self.store.connection(immediate=True) as connection:
            self._expire_lease(connection, ticket_id)
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._require_repository_reconciled(connection, project_id)
            self._expect_version(ticket, expected_version)
            self.require_fresh_context(connection, ticket_id, context_snapshot_id)
            self._require_skill_compatible(connection, actor, project_id)
            self._require_qa_execution_capabilities(connection, actor, project_id, ticket)
            existing = connection.execute(
                "SELECT * FROM leases WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if existing:
                raise ConflictError("Ticket is already leased")
            if TicketStatus(ticket["status"]) not in allowed_statuses:
                raise ConflictError(f"Ticket cannot be claimed from {ticket['status']}")
            if ticket["required_role"] != actor.role.value and actor.role not in {
                Role.QA,
                Role.INTEGRATOR,
            }:
                raise AuthorizationError("Ticket is assigned to another role")
            timestamp = _now()
            if actor.role is Role.DEVELOPER:
                unfinished_dependencies = self._unfinished_dependency_ids(connection, ticket_id)
                if unfinished_dependencies:
                    raise ConflictError(
                        "Ticket dependencies are not complete: "
                        + ", ".join(unfinished_dependencies)
                    )
                conflicting_ticket_id = self._active_write_scope_conflict(
                    connection, project_id, ticket, timestamp
                )
                if conflicting_ticket_id is not None:
                    raise ConflictError(
                        "Ticket write scope conflicts with active ticket " + conflicting_ticket_id
                    )
            lease_id = _id(connection, "lease")
            connection.execute(
                """
                INSERT INTO leases
                    (lease_id, ticket_id, agent_id, role, acquired_at, heartbeat_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    ticket_id,
                    actor.agent_id,
                    actor.role.value,
                    _iso(timestamp),
                    _iso(timestamp),
                    _iso(timestamp + timedelta(seconds=lease_seconds)),
                ),
            )
            next_version = _transition_ticket(
                connection, ticket_id, actor=actor, expected_version=ticket["version"],
                from_statuses=allowed_statuses,
                changes={"status": next_status, "updated_at": _iso(timestamp)},
            )
            self._event(
                connection,
                actor,
                project_id,
                event_type,
                "ticket",
                ticket_id,
                next_version,
                {"lease_id": lease_id},
            )
            self._mark_queue_claimed(connection, ticket_id, actor.agent_id, timestamp)
            result = {
                "ticket": self._ticket(connection, ticket_id),
                "lease": self._lease(connection, lease_id),
                # The snapshot the caller decided from, not a fresh identifier:
                # inventing one here would name a packet nobody ever read.
                "context_snapshot_id": context_snapshot_id,
            }
        # The packet ticket.get_context returns right now, compiled and recorded
        # after the claim committed: an agent can write from it without spending
        # a round trip and a full packet on re-reading.
        # The claim has committed. Nothing after this point may fail the call: a
        # raised error here would make the transport retry a claim the caller
        # already holds and report a conflict for work it owns.
        try:
            result["current_context_snapshot_id"] = self.get_ticket_context(
                actor, ticket_id
            )["context_snapshot_id"]
            result["worker_brief"] = self._worker_brief(
                actor, result["ticket"], result["lease"], result["current_context_snapshot_id"]
            )
        except DomainError:  # includes ContentionError
            result["current_context_snapshot_id"] = None
            result["worker_brief"] = None
            result["next_action"] = (
                "The claim succeeded; call ticket.get_context for this ticket and use "
                "its context_snapshot_id before the next write."
            )
        return result

    def _worker_brief(
        self,
        actor: Actor,
        ticket: dict[str, Any],
        lease: dict[str, Any],
        snapshot_id: str,
    ) -> dict[str, Any]:
        """A self-contained hand-off for a fresh subagent that works this one ticket.

        The waiting chat is a dispatcher: it passes this verbatim to a new agent
        and keeps only the structured result, so no ticket's transcript reaches
        the next ticket. Everything the worker needs to start is named here, and
        everything else it resolves from the packet itself.
        """
        ticket_id, lease_id = ticket["ticket_id"], lease["lease_id"]
        version = ticket["version"]
        with self.store.connection() as connection:
            worktree = _row(connection.execute(
                "SELECT worktree_path, branch_ref FROM worktrees WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone())
            candidate = _row(connection.execute(
                "SELECT candidate_id FROM candidates WHERE ticket_id = ? "
                "ORDER BY submitted_at DESC LIMIT 1",
                (ticket_id,),
            ).fetchone())
            repository = _row(connection.execute(
                "SELECT workspace_model FROM project_repositories WHERE project_id = ?",
                (self._project_for_ticket(connection, ticket_id),),
            ).fetchone())
        ids = f'ticket_id="{ticket_id}", lease_id="{lease_id}"'
        # The claimed status names the work, whoever holds the lease (an Admin may).
        role = {
            TicketStatus.DEV_IN_PROGRESS.value: Role.DEVELOPER,
            TicketStatus.QA_IN_PROGRESS.value: Role.QA,
        }.get(ticket["status"], Role.INTEGRATOR)
        calls: list[str] = []
        if role is Role.DEVELOPER:
            if (repository or {}).get("workspace_model", "colocated") == "colocated":
                calls += [
                    f"dev.prepare_worktree({ids}, expected_version={version})",
                    f"dev.get_worktree_provision({ids}, provision_id=<provision_id>) until terminal",
                ]
            calls += [
                f'ticket.get_context(ticket_ref="{ticket_id}") immediately before submit',
                f"dev.submit_candidate({ids}, expected_version=<packet version>, "
                "commit_sha=<sha>, base_commit=<base>, branch_ref=<branch>, "
                "developer_claims=[...], context_snapshot_id=<fresh packet>)",
                f"dev.get_candidate_submission({ids}, submission_id=<submission_id>) until terminal",
            ]
            returns = ["submission_id", "commit_sha", "status", "blockers"]
        elif role is Role.QA:
            candidate_id = candidate["candidate_id"] if candidate else "<candidate_id>"
            calls += [
                f"qa.run_project_tests({ids})",
                f"qa.get_project_test_run({ids}, test_run_id=<test_run_id>) until terminal",
                f'qa.submit_verdict({ids}, candidate_id="{candidate_id}", '
                f'expected_version={version}, verdict=<passed|failed>, findings=[...], '
                f'context_snapshot_id="{snapshot_id}")',
            ]
            returns = ["verdict", "evidence_bundle.candidate_id", "planner_job_id", "blockers"]
        else:
            calls += [
                f"integrator.merge_candidate({ids}, expected_version={version}, "
                f'context_snapshot_id="{snapshot_id}")',
                f"integrator.get_merge_candidate({ids}, merge_job_id=<merge_job_id>) until terminal",
            ]
            returns = ["merge_job_id", "status", "mainline_sha", "blockers"]
        brief: dict[str, Any] = {
            "ticket_id": ticket_id,
            "role": role.value,
            "lease_id": lease_id,
            "context_snapshot_id": snapshot_id,
            "skills": ["hoh-control-plane", f"hoh-{role.value}"],
            "next_calls": calls,
            "returns": returns,
        }
        if worktree is not None:
            brief["worktree_path"] = worktree["worktree_path"]
            brief["branch_ref"] = worktree["branch_ref"]
        return brief

    def _authorized_ticket(
        self, connection: Connection, actor: Actor, ticket_id: str
    ) -> tuple[dict[str, Any], str]:
        project_id = self._project_for_ticket(connection, ticket_id)
        require_project(actor, project_id)
        return self._ticket(connection, ticket_id), project_id

    def _expire_lease(self, connection: Connection, ticket_id: str) -> None:
        lease = connection.execute(
            "SELECT * FROM leases WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        if lease and datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00")) <= _now():
            if connection.execute("SELECT 1 FROM merge_jobs WHERE lease_id = ? AND status = 'RUNNING'", (lease['lease_id'],)).fetchone():
                raise ConflictError('Interrupted merge requires reconciliation before lease takeover')
            self._retire_expired_jobs(connection, dict(lease))
            connection.execute("DELETE FROM leases WHERE ticket_id = ?", (ticket_id,))
            current = self._ticket(connection, ticket_id)
            if not self._lease_owns_ticket_state(dict(lease), current):
                return
            fallback = (
                TicketStatus.MERGE_READY
                if lease["role"] == Role.INTEGRATOR.value
                else TicketStatus.QA_READY
                if lease["role"] == Role.QA.value
                else TicketStatus.READY
            )
            recovery = Actor("lease-recovery", Role.ADMIN, frozenset({"*"}))
            _transition_ticket(
                connection, ticket_id, actor=recovery, expected_version=current["version"],
                from_statuses=(current["status"],),
                changes={"status": fallback, "updated_at": _iso()},
            )
            project_id = self._project_for_ticket(connection, ticket_id)
            self._queue_ticket(
                connection,
                recovery,
                project_id,
                self._ticket(connection, ticket_id),
            )

    def _require_active_lease(
        self, connection: Connection, actor: Actor, ticket_id: str, lease_id: str
    ) -> dict[str, Any]:
        lease = self._lease(connection, lease_id)
        if lease["ticket_id"] != ticket_id:
            raise AuthorizationError("Lease does not cover this ticket")
        if lease["agent_id"] != actor.agent_id or lease["role"] != actor.role.value:
            raise AuthorizationError("Lease belongs to another actor")
        expires_at = datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00"))
        if expires_at <= _now():
            raise ConflictError("Lease has expired", {"reason": "lease_expired"})
        return lease

    def _lease(self, connection: Connection, lease_id: str) -> dict[str, Any]:
        return self._one(
            connection, "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
        )

    def _ticket(self, connection: Connection, ticket_id: str) -> dict[str, Any]:
        ticket = self._one(
            connection, "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
        )
        ticket["dependency_ids"] = [
            row["depends_on_ticket_id"]
            for row in connection.execute(
                "SELECT depends_on_ticket_id FROM ticket_dependencies WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchall()
        ]
        return ticket

    def _candidate_with_changed_paths(
        self, connection: Connection, candidate: dict[str, Any]
    ) -> dict[str, Any]:
        candidate["changed_paths"] = [
            row["path"]
            for row in connection.execute(
                "SELECT path FROM candidate_changed_paths WHERE candidate_id = ? ORDER BY path",
                (candidate["candidate_id"],),
            ).fetchall()
        ]
        return candidate

    @staticmethod
    def _dependency_would_cycle(
        connection: Connection, ticket_id: str, candidate_dependency_id: str
    ) -> bool:
        """True if candidate_dependency_id already transitively depends on ticket_id.

        Adding a ticket_id -> candidate_dependency_id edge on top of that existing
        chain would close a cycle, so the caller must refuse it before writing.
        """
        visited = {candidate_dependency_id}
        queue = [candidate_dependency_id]
        while queue:
            current = queue.pop()
            if current == ticket_id:
                return True
            for row in connection.execute(
                "SELECT depends_on_ticket_id FROM ticket_dependencies WHERE ticket_id = ?",
                (current,),
            ).fetchall():
                next_id = str(row["depends_on_ticket_id"])
                if next_id not in visited:
                    visited.add(next_id)
                    queue.append(next_id)
        return False

    @staticmethod
    def _unfinished_dependency_ids(connection: Connection, ticket_id: str) -> list[str]:
        return [
            row["ticket_id"]
            for row in connection.execute(
                """
                SELECT dependency.ticket_id
                FROM ticket_dependencies dependency_link
                JOIN tickets dependency ON dependency.ticket_id = dependency_link.depends_on_ticket_id
                WHERE dependency_link.ticket_id = ? AND dependency.status != ?
                ORDER BY dependency.ticket_id
                """,
                (ticket_id, TicketStatus.DONE.value),
            ).fetchall()
        ]

    def _active_write_scope_conflict(
        self,
        connection: Connection,
        project_id: str,
        ticket: dict[str, Any],
        now: datetime,
    ) -> str | None:
        active_tickets = connection.execute(
            """
            SELECT active_ticket.ticket_id, active_ticket.file_scope
            FROM leases lease
            JOIN tickets active_ticket ON active_ticket.ticket_id = lease.ticket_id
            JOIN features feature ON feature.feature_id = active_ticket.feature_id
            WHERE feature.project_id = ?
              AND lease.role = ?
              AND lease.expires_at > ?
              AND active_ticket.ticket_id != ?
            ORDER BY active_ticket.ticket_id
            """,
            (project_id, Role.DEVELOPER.value, _iso(now), ticket["ticket_id"]),
        ).fetchall()
        for active_ticket in active_tickets:
            active_scope = json.loads(active_ticket["file_scope"])
            if self._scopes_overlap(ticket["file_scope"], active_scope):
                return str(active_ticket["ticket_id"])
        return None

    @staticmethod
    def _scopes_overlap(left_scopes: list[str], right_scopes: list[str]) -> bool:
        if not left_scopes or not right_scopes:
            return True
        return any(
            LeaseMixin._scope_roots_overlap(left_scope, right_scope)
            for left_scope in left_scopes
            for right_scope in right_scopes
        )

    @staticmethod
    def _scope_roots_overlap(left_scope: str, right_scope: str) -> bool:
        return scopes.roots_overlap(
            scopes.scope_root(left_scope), scopes.scope_root(right_scope)
        )

    @staticmethod
    def _scope_root(scope: str) -> str:
        # Delegated rather than defined here: the store indexes what a note
        # attaches to using the same rule, and a second copy of it would let
        # lease collision and experience disagree about what overlaps.
        return scopes.scope_root(scope)

    @staticmethod
    def _path_is_in_any_scope(path: str, scopes: list[str]) -> bool:
        return any(LeaseMixin._path_matches_scope(path, scope) for scope in scopes)

    @staticmethod
    def _directory_scope(scope: str) -> str:
        return scopes.directory_scope(scope)

    @staticmethod
    def _repository_relative_scope(scope: str) -> str:
        raw_scope = scope.replace("\\", "/").strip()
        if not raw_scope or raw_scope.startswith("/") or re.match(r"^[A-Za-z]:", raw_scope):
            raise ValidationError("file_scope entries must be repository-relative paths")
        normalized = scopes.directory_scope(raw_scope).strip("/")
        if any(part in {".", ".."} for part in normalized.split("/")):
            raise ValidationError("file_scope entries must be repository-relative paths")
        if any(character in normalized for character in ("\x00", "\r", "\n")):
            raise ValidationError("file_scope entries contain invalid control characters")
        return normalized

    @staticmethod
    def _path_matches_scope(path: str, scope: str) -> bool:
        normalized_path = path.replace("\\", "/").lstrip("/")
        # A legacy row may still hold "dir/"; read it as "dir/**" (HOH-279).
        normalized_scope = scopes.directory_scope(scope).replace("\\", "/").strip("/")
        pattern = []
        index = 0
        while index < len(normalized_scope):
            character = normalized_scope[index]
            if (
                character == "*"
                and index + 1 < len(normalized_scope)
                and normalized_scope[index + 1] == "*"
            ):
                if index + 2 < len(normalized_scope) and normalized_scope[index + 2] == "/":
                    pattern.append("(?:.*/)?")
                    index += 3
                else:
                    pattern.append(".*")
                    index += 2
            elif character == "*":
                pattern.append("[^/]*")
                index += 1
            elif character == "?":
                pattern.append("[^/]")
                index += 1
            else:
                pattern.append(re.escape(character))
                index += 1
        return re.fullmatch("".join(pattern), normalized_path) is not None
