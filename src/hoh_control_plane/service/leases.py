from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    AuthorizationError,
    ConflictError,
    Role,
    SkillCompatibilityError,
    TicketStatus,
    ValidationError,
    _id,
    _iso,
    _now,
    _row,
    datetime,
    json,
    re,
    require_project,
    require_role,
    sqlite3,
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
                raise ConflictError("Lease has expired")
            expires_at = _iso(timestamp + timedelta(seconds=lease_seconds))
            connection.execute(
                "UPDATE leases SET heartbeat_at = ?, expires_at = ? WHERE lease_id = ?",
                (_iso(timestamp), expires_at, lease_id),
            )
            return self._lease(connection, lease_id)

    def sweep_expired_leases(self, actor: Actor) -> dict[str, Any]:
        """Recover expired Developer and QA leases without waiting for a later claim."""
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
                ticket = self._ticket(connection, lease["ticket_id"])
                project_id = self._project_for_ticket(connection, ticket["ticket_id"])
                require_project(actor, project_id)
                fallback = (
                    TicketStatus.MERGE_READY
                    if lease["role"] == Role.INTEGRATOR.value
                    else TicketStatus.QA_READY
                    if lease["role"] == Role.QA.value
                    else TicketStatus.READY
                )
                next_version = ticket["version"] + 1
                connection.execute("DELETE FROM leases WHERE lease_id = ?", (lease["lease_id"],))
                connection.execute(
                    """
                    UPDATE tickets
                    SET status = ?, version = ?, updated_at = ?
                    WHERE ticket_id = ?
                    """,
                    (fallback.value, next_version, _iso(timestamp), ticket["ticket_id"]),
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
        return {"count": len(requeued), "requeued": requeued}

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
                    _id("toolcall"), project_id, actor.agent_id, actor.role.value, tool_name,
                    json.dumps(input_summary), outcome, error_code, _iso(),
                ),
            )

    @staticmethod
    def _require_skill_compatible(
        connection: sqlite3.Connection, actor: Actor, project_id: str
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
            self._expect_version(ticket, expected_version)
            self.require_fresh_context(connection, ticket_id, context_snapshot_id)
            self._require_skill_compatible(connection, actor, project_id)
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
            lease_id = _id("lease")
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
            next_version = ticket["version"] + 1
            connection.execute(
                "UPDATE tickets SET status = ?, version = ?, updated_at = ? WHERE ticket_id = ?",
                (next_status.value, next_version, _iso(timestamp), ticket_id),
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
            return {
                "ticket": self._ticket(connection, ticket_id),
                "lease": self._lease(connection, lease_id),
                # The snapshot the caller decided from, not a fresh identifier:
                # inventing one here would name a packet nobody ever read.
                "context_snapshot_id": context_snapshot_id,
            }

    def _authorized_ticket(
        self, connection: sqlite3.Connection, actor: Actor, ticket_id: str
    ) -> tuple[dict[str, Any], str]:
        project_id = self._project_for_ticket(connection, ticket_id)
        require_project(actor, project_id)
        return self._ticket(connection, ticket_id), project_id

    def _expire_lease(self, connection: sqlite3.Connection, ticket_id: str) -> None:
        lease = connection.execute(
            "SELECT * FROM leases WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
        if lease and datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00")) <= _now():
            connection.execute("DELETE FROM leases WHERE ticket_id = ?", (ticket_id,))
            current = self._ticket(connection, ticket_id)
            fallback = (
                TicketStatus.MERGE_READY
                if lease["role"] == Role.INTEGRATOR.value
                else TicketStatus.QA_READY
                if lease["role"] == Role.QA.value
                else TicketStatus.READY
            )
            connection.execute(
                "UPDATE tickets SET status = ?, version = version + 1, updated_at = ? WHERE ticket_id = ?",
                (fallback.value, _iso(), ticket_id),
            )
            project_id = self._project_for_ticket(connection, ticket_id)
            self._queue_ticket(
                connection,
                Actor("lease-recovery", Role.ADMIN, frozenset({"*"})),
                project_id,
                self._ticket(connection, ticket_id),
            )

    def _require_active_lease(
        self, connection: sqlite3.Connection, actor: Actor, ticket_id: str, lease_id: str
    ) -> dict[str, Any]:
        lease = self._lease(connection, lease_id)
        if lease["ticket_id"] != ticket_id:
            raise AuthorizationError("Lease does not cover this ticket")
        if lease["agent_id"] != actor.agent_id or lease["role"] != actor.role.value:
            raise AuthorizationError("Lease belongs to another actor")
        expires_at = datetime.fromisoformat(lease["expires_at"].replace("Z", "+00:00"))
        if expires_at <= _now():
            raise ConflictError("Lease has expired")
        return lease

    def _lease(self, connection: sqlite3.Connection, lease_id: str) -> dict[str, Any]:
        return self._one(
            connection, "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
        )

    def _ticket(self, connection: sqlite3.Connection, ticket_id: str) -> dict[str, Any]:
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
        self, connection: sqlite3.Connection, candidate: dict[str, Any]
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
        connection: sqlite3.Connection, ticket_id: str, candidate_dependency_id: str
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
    def _unfinished_dependency_ids(connection: sqlite3.Connection, ticket_id: str) -> list[str]:
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
        connection: sqlite3.Connection,
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
        left_root = LeaseMixin._scope_root(left_scope)
        right_root = LeaseMixin._scope_root(right_scope)
        if not left_root or not right_root:
            return True
        return (
            left_root == right_root
            or left_root.startswith(f"{right_root}/")
            or right_root.startswith(f"{left_root}/")
        )

    @staticmethod
    def _scope_root(scope: str) -> str:
        normalized = scope.replace("\\", "/").strip("/")
        static_parts = []
        for part in normalized.split("/"):
            if any(character in part for character in "*?["):
                break
            if part:
                static_parts.append(part)
        return "/".join(static_parts)

    @staticmethod
    def _path_is_in_any_scope(path: str, scopes: list[str]) -> bool:
        return any(LeaseMixin._path_matches_scope(path, scope) for scope in scopes)

    @staticmethod
    def _repository_relative_scope(scope: str) -> str:
        raw_scope = scope.replace("\\", "/").strip()
        if not raw_scope or raw_scope.startswith("/") or re.match(r"^[A-Za-z]:", raw_scope):
            raise ValidationError("file_scope entries must be repository-relative paths")
        normalized = raw_scope.strip("/")
        if any(part in {".", ".."} for part in normalized.split("/")):
            raise ValidationError("file_scope entries must be repository-relative paths")
        if any(character in normalized for character in ("\x00", "\r", "\n")):
            raise ValidationError("file_scope entries contain invalid control characters")
        return normalized

    @staticmethod
    def _path_matches_scope(path: str, scope: str) -> bool:
        normalized_path = path.replace("\\", "/").lstrip("/")
        normalized_scope = scope.replace("\\", "/").strip("/")
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
