from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    AuthorizationError,
    CLOSED_TICKET_STATUSES,
    Role,
    TicketStatus,
    ValidationError,
    _id,
    _iso,
    _now,
    _row,
    _serialize,
    base64,
    datetime,
    json,
    require_project,
    require_role,
    Connection,
    timedelta,
)


class DiscoveryMixin:
    """Role-scoped work discovery, dispatch queue and agent presence."""

    # Which ticket states each role is the next actor for. Used by both the
    # implicit per-role discovery filter and the explicit awaiting_role filter.
    _ROLE_ACTIONABLE_STATUSES: dict[Role, tuple[TicketStatus, ...]] = {
        Role.DEVELOPER: (TicketStatus.READY, TicketStatus.REWORK),
        Role.QA: (TicketStatus.QA_READY,),
        Role.INTEGRATOR: (TicketStatus.VERIFIED, TicketStatus.MERGE_READY),
    }

    @classmethod
    def _discovery_statuses(
        cls, status: list[str] | None, awaiting_role: str | None
    ) -> list[str] | None:
        """Resolve the explicit status[]/awaiting_role filters into one status set."""
        selected: set[str] | None = None
        if status is not None:
            if not isinstance(status, list) or not status:
                raise ValidationError("status must be a non-empty list of ticket states")
            if len(status) > len(TicketStatus):
                raise ValidationError("status contains more entries than there are states")
            for value in status:
                try:
                    TicketStatus(value)
                except ValueError as error:
                    raise ValidationError(f"Unknown ticket status: {value}") from error
            selected = set(status)
        if awaiting_role is not None:
            try:
                role = Role(awaiting_role)
            except ValueError as error:
                raise ValidationError(f"Unknown awaiting_role: {awaiting_role}") from error
            actionable = cls._ROLE_ACTIONABLE_STATUSES.get(role)
            if actionable is None:
                raise ValidationError(f"Role {awaiting_role} is never the awaited actor")
            awaited = {state.value for state in actionable}
            selected = awaited if selected is None else selected & awaited
            if not selected:
                # An impossible combination must return nothing rather than
                # silently widening back to every status.
                return []
        return sorted(selected) if selected is not None else None

    @staticmethod
    def _encoded_cursor(keyset: tuple[Any, Any, Any]) -> str:
        raw = _serialize({"p": keyset[0], "c": keyset[1], "t": keyset[2]})
        return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")

    @staticmethod
    def _decoded_cursor(cursor: str | None) -> tuple[Any, Any, Any] | None:
        if cursor is None:
            return None
        if not isinstance(cursor, str) or len(cursor) > 500:
            raise ValidationError("cursor is not a discovery cursor")
        try:
            payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
            return (int(payload["p"]), str(payload["c"]), str(payload["t"]))
        except Exception as error:
            raise ValidationError("cursor is not a discovery cursor") from error

    def discover_work(
        self,
        actor: Actor,
        project_id: str,
        dependency_ready: bool = True,
        blocked_only: bool = False,
        limit: int = 50,
        status: list[str] | None = None,
        feature_id: str | None = None,
        priority_min: int | None = None,
        awaiting_role: str | None = None,
        unassigned_only: bool = False,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List role-appropriate work as bounded metadata plus typed references.

        Discovery answers "where should I look", not "what does this ticket say":
        it returns feature identity, counts and typed ticket references, never
        ticket bodies. The objective, acceptance criteria, preservation
        constraints, write scope and blocked reason live behind
        ticket.get_context, which the caller resolves for the one ticket it picks.
        """
        require_project(actor, project_id)
        limit = min(max(limit, 1), 100)
        selected_statuses = self._discovery_statuses(status, awaiting_role)
        if priority_min is not None and not isinstance(priority_min, bool):
            self._priority(priority_min)
        elif priority_min is not None:
            raise ValidationError("priority_min must be an integer")
        if feature_id is not None:
            self._bounded_text(feature_id, "feature_id", 100)
        keyset = self._decoded_cursor(cursor)
        with self.store.connection() as connection:
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            parameters: list[Any] = [project_id]
            where = ["f.project_id = ?"]
            # Retired work stays readable through ticket context, never through
            # a discovery surface that exists to hand out actionable work.
            closed = sorted(status.value for status in CLOSED_TICKET_STATUSES)
            where.append(f"t.status NOT IN ({', '.join('?' for _ in closed)})")
            parameters.extend(closed)
            if actor.role not in {Role.PLANNER, Role.ADMIN}:
                where.append("t.required_role = ?")
                parameters.append(actor.role.value)
            if blocked_only:
                where.append("t.status = ?")
                parameters.append(TicketStatus.BLOCKED.value)
            elif actor.role is Role.DEVELOPER:
                where.append("t.status IN (?, ?)")
                parameters.extend([TicketStatus.READY.value, TicketStatus.REWORK.value])
            elif actor.role is Role.QA:
                where.append("t.status = ?")
                parameters.append(TicketStatus.QA_READY.value)
            elif actor.role is Role.INTEGRATOR:
                where.append("t.status IN (?, ?)")
                parameters.extend(
                    [TicketStatus.VERIFIED.value, TicketStatus.MERGE_READY.value]
                )
            if selected_statuses is not None:
                where.append(f"t.status IN ({', '.join('?' for _ in selected_statuses)})")
                parameters.extend(selected_statuses)
            if feature_id is not None:
                where.append("t.feature_id = ?")
                parameters.append(feature_id)
            if priority_min is not None:
                where.append("t.priority >= ?")
                parameters.append(priority_min)
            if unassigned_only:
                where.append(
                    "NOT EXISTS (SELECT 1 FROM leases l WHERE l.ticket_id = t.ticket_id"
                    " AND l.expires_at > ?)"
                )
                parameters.append(_iso())
            if dependency_ready:
                where.append(
                    """NOT EXISTS (
                        SELECT 1 FROM ticket_dependencies d
                        JOIN tickets dependency ON dependency.ticket_id = d.depends_on_ticket_id
                        WHERE d.ticket_id = t.ticket_id AND dependency.status != 'DONE'
                    )"""
                )
            if keyset is not None:
                # Keyset rather than OFFSET: a ticket inserted between pages must
                # not shift the window and silently hide a row from the caller.
                where.append(
                    "(t.priority < ? OR (t.priority = ? AND (t.created_at > ?"
                    " OR (t.created_at = ? AND t.ticket_id > ?))))"
                )
                parameters.extend(
                    [keyset[0], keyset[0], keyset[1], keyset[1], keyset[2]]
                )
            parameters.append(limit + 1)
            rows = connection.execute(
                f"""
                SELECT t.*, f.project_id, f.title AS feature_title, f.status AS feature_status,
                       f.version AS feature_version, f.parent_feature_id
                FROM tickets t
                JOIN features f ON f.feature_id = t.feature_id
                WHERE {' AND '.join(where)}
                ORDER BY t.priority DESC, t.created_at ASC, t.ticket_id ASC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]

        grouped: dict[str, dict[str, Any]] = {}
        last: tuple[Any, Any, Any] | None = None
        for raw in rows:
            row = _row(raw)
            assert row is not None
            last = (row["priority"], row["created_at"], row["ticket_id"])
            group_id = row["feature_id"]
            feature = grouped.setdefault(
                group_id,
                {
                    "feature_id": group_id,
                    "title": row["feature_title"],
                    "status": row["feature_status"],
                    "parent_feature_id": row["parent_feature_id"],
                    "context_ref": f"ctx:feature:{group_id}:v{row['feature_version']}",
                    "pending_ticket_refs": [],
                    "blocked_ticket_count": 0,
                    "tickets": [],
                },
            )
            ticket_ref = f"ticket:{row['ticket_id']}"
            feature["pending_ticket_refs"].append(ticket_ref)
            if row["status"] == TicketStatus.BLOCKED.value:
                feature["blocked_ticket_count"] += 1
            calls = [
                {
                    "tool": "ticket.get_context",
                    "arguments": {"ticket_ref": ticket_ref},
                }
            ]
            if actor.role is Role.DEVELOPER:
                calls.append(
                    {
                        "tool": "work.claim",
                        "arguments": {
                            "ticket_id": row["ticket_id"],
                            "expected_version": row["version"],
                        },
                    }
                )
            elif actor.role is Role.QA:
                calls.append(
                    {
                        "tool": "qa.claim_candidate",
                        "arguments": {
                            "ticket_id": row["ticket_id"],
                            "expected_version": row["version"],
                        },
                    }
                )
            # Identity and routing fields only. Everything a worker must read
            # before acting -- objective, acceptance criteria, preservation
            # constraints, write scope, blocked reason, artifact identity --
            # stays behind the typed ticket.get_context reference above.
            feature["tickets"].append(
                {
                    "ticket_id": row["ticket_id"],
                    "ticket_ref": ticket_ref,
                    "feature_id": group_id,
                    "title": row["title"],
                    "ticket_type": row["ticket_type"],
                    "status": row["status"],
                    "priority": row["priority"],
                    "required_role": row["required_role"],
                    "version": row["version"],
                    "context_ref": f"ctx:ticket:{row['ticket_id']}:v{row['version']}",
                    "recommended_calls": calls,
                }
            )

        discovered = {
            "agent": {
                "agent_id": actor.agent_id,
                "role": actor.role.value,
                "capabilities": self.capabilities(actor),
            },
            "project": project,
            "features": list(grouped.values()),
            "next_cursor": self._encoded_cursor(last) if has_more and last else None,
        }
        if actor.role in {Role.PLANNER, Role.ADMIN}:
            # Planning decisions are their own queue: a Planner sees them beside,
            # never mixed into, the tickets an execution role can claim.
            discovered["planning_jobs"] = self.discover_planner_jobs(actor, project_id)
        return discovered

    def capabilities(self, actor: Actor) -> list[str]:
        common = [
            "project:read",
            "feature:read",
            "ticket:read",
            "context:resolve",
            "agent:presence",
            "work:inbox",
            "skill_compatibility:read",
        ]
        role_capabilities = {
            Role.PLANNER: [
                "feature:write",
                "ticket:plan",
                "ticket:update",
                "ticket:split",
                "ticket:retire",
                "candidate:requeue",
                "repository:configure",
                "test_command:configure",
                "skill_policy:configure",
            ],
            Role.DEVELOPER: [
                "ticket:claim:developer",
                "worktree:prepare",
                "progress:append",
                "candidate:submit",
            ],
            Role.QA: ["ticket:claim:qa", "tests:execute", "evidence:write", "verdict:submit"],
            Role.INTEGRATOR: ["candidate:integrate", "merge:record"],
            Role.ADMIN: ["*"],
        }
        return common + role_capabilities[actor.role]

    def register_agent(
        self,
        actor: Actor,
        *,
        skills: list[str],
        capacity: int = 1,
        machine_id: str | None = None,
        presence_seconds: int = 120,
        skill_manifest: list[dict[str, Any]] | None = None,
        protocol_version: str | None = None,
    ) -> dict[str, Any]:
        """Publish a short-lived, authenticated availability record for dispatch.

        skill_manifest/protocol_version are recorded for project skill-policy
        compatibility only; they never grant or widen role permission, which
        always comes from the authenticated actor's own role.
        """
        self._bounded_list(skills, "skills", 30, 100)
        self._capacity(capacity)
        self._presence_seconds(presence_seconds)
        if machine_id is not None:
            self._bounded_text(machine_id, "machine_id", 120)
        if protocol_version is not None:
            self._bounded_text(protocol_version, "protocol_version", 50)
        normalized_manifest = self._validate_skill_manifest(skill_manifest or [])
        timestamp = _now()
        with self.store.connection(immediate=True) as connection:
            existing = _row(
                connection.execute(
                    "SELECT * FROM agent_presence WHERE agent_id = ?", (actor.agent_id,)
                ).fetchone()
            )
            if existing is not None and existing["role"] != actor.role.value:
                raise AuthorizationError("Agent identity cannot change roles")

            compatibility: dict[str, Any] = {}
            for project_id in sorted(actor.project_ids):
                if project_id == "*":
                    continue
                policy = _row(
                    connection.execute(
                        "SELECT * FROM project_skill_policies WHERE project_id = ?",
                        (project_id,),
                    ).fetchone()
                )
                compatibility[project_id] = self._evaluate_skill_compatibility(
                    project_id, policy, protocol_version, normalized_manifest
                )

            connection.execute(
                """
                INSERT INTO agent_presence
                    (agent_id, role, project_ids, skills, capacity, machine_id,
                     last_seen_at, expires_at, created_at, updated_at,
                     skill_manifest, protocol_version, skill_compatibility)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    project_ids = excluded.project_ids,
                    skills = excluded.skills,
                    capacity = excluded.capacity,
                    machine_id = excluded.machine_id,
                    last_seen_at = excluded.last_seen_at,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at,
                    skill_manifest = excluded.skill_manifest,
                    protocol_version = excluded.protocol_version,
                    skill_compatibility = excluded.skill_compatibility
                """,
                (
                    actor.agent_id,
                    actor.role.value,
                    json.dumps(sorted(actor.project_ids)),
                    json.dumps(skills),
                    capacity,
                    machine_id,
                    _iso(timestamp),
                    _iso(timestamp + timedelta(seconds=presence_seconds)),
                    _iso(timestamp),
                    _iso(timestamp),
                    json.dumps(normalized_manifest),
                    protocol_version,
                    json.dumps(compatibility),
                ),
            )
            return self._presence(connection, actor.agent_id)

    @classmethod
    def _validate_skill_manifest(cls, skill_manifest: list[Any]) -> list[dict[str, str]]:
        """Bound and normalize a declared skill manifest before it is trusted as data.

        Every field here is informational provenance, never an authorization
        input: role and project scope come only from the authenticated Actor.
        """
        if not isinstance(skill_manifest, list):
            raise ValidationError("skill_manifest must be a list")
        if len(skill_manifest) > 30:
            raise ValidationError("skill_manifest supports at most 30 entries")
        normalized: list[dict[str, str]] = []
        for index, entry in enumerate(skill_manifest):
            if not isinstance(entry, dict):
                raise ValidationError(f"skill_manifest[{index}] must be an object")
            name = entry.get("name")
            version = entry.get("version")
            checksum = entry.get("checksum")
            loaded_at = entry.get("loaded_at")
            cls._bounded_text(name, f"skill_manifest[{index}].name", 100)
            cls._bounded_text(version, f"skill_manifest[{index}].version", 50)
            cls._bounded_text(checksum, f"skill_manifest[{index}].checksum", 128)
            cls._bounded_text(loaded_at, f"skill_manifest[{index}].loaded_at", 40)
            try:
                datetime.fromisoformat(loaded_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValidationError(
                    f"skill_manifest[{index}].loaded_at must be an ISO-8601 timestamp"
                ) from exc
            normalized.append(
                {"name": name, "version": version, "checksum": checksum, "loaded_at": loaded_at}
            )
        names = [entry["name"] for entry in normalized]
        if len(set(names)) != len(names):
            raise ValidationError("skill_manifest cannot declare the same skill name twice")
        return normalized

    @staticmethod
    def _evaluate_skill_compatibility(
        project_id: str,
        policy: dict[str, Any] | None,
        protocol_version: str | None,
        skill_manifest: list[dict[str, str]],
    ) -> dict[str, Any]:
        """Score one registration against one project's policy, if any is configured.

        A project with no configured policy accepts everything: enforcement is
        opt-in per project, so introducing this table cannot retroactively lock
        out an agent that has never declared a manifest at all.
        """
        if policy is None:
            return {"compatible": True, "reasons": []}
        reasons: list[str] = []
        accepted_protocol_versions = policy["accepted_protocol_versions"]
        if accepted_protocol_versions and protocol_version not in accepted_protocol_versions:
            reasons.append(
                f"protocol_version {protocol_version!r} is not accepted by {project_id}"
            )
        declared_versions = {entry["name"]: entry["version"] for entry in skill_manifest}
        for skill_name, accepted_versions in policy["accepted_skill_versions"].items():
            declared = declared_versions.get(skill_name)
            if declared is None or declared not in accepted_versions:
                reasons.append(
                    f"skill {skill_name!r} version {declared!r} is not accepted by {project_id}"
                )
        return {"compatible": not reasons, "reasons": reasons}

    def heartbeat_agent(
        self, actor: Actor, *, presence_seconds: int = 120
    ) -> dict[str, Any]:
        """Renew an agent's dispatch presence; separate from a ticket lease heartbeat."""
        self._presence_seconds(presence_seconds)
        with self.store.connection(immediate=True) as connection:
            return self._refresh_presence(connection, actor, presence_seconds)

    def get_agent_inbox(
        self,
        actor: Actor,
        project_id: str,
        *,
        limit: int = 20,
        offer_seconds: int = 90,
    ) -> dict[str, Any]:
        """Return durable offers for one role and make a newly-live agent dispatchable."""
        require_project(actor, project_id)
        if actor.role not in {Role.DEVELOPER, Role.QA, Role.INTEGRATOR}:
            raise AuthorizationError("Only execution roles have a work inbox")
        limit = min(max(limit, 1), 50)
        self._offer_seconds(offer_seconds)
        # The dedicated scheduler proactively reclaims expired leases.  An inbox
        # poll is also a safe recovery point: without this fallback, a stopped
        # scheduler leaves an otherwise-live worker waiting forever behind a
        # stale claim.  Recovery uses the internal admin identity rather than
        # granting execution workers permission to sweep leases themselves.
        self.sweep_expired_leases(
            Actor("lease-recovery", Role.ADMIN, frozenset({"*"}))
        )
        now = _now()
        with self.store.connection(immediate=True) as connection:
            presence = self._refresh_presence(connection, actor, 120, now=now)
            self._expire_offers(connection, now)
            pending = connection.execute(
                """
                SELECT * FROM work_queue
                WHERE project_id = ? AND target_role = ? AND status IN ('pending', 'offered')
                ORDER BY priority DESC, created_at ASC
                """,
                (project_id, actor.role.value),
            ).fetchall()
            created_offers: list[dict[str, Any]] = []
            for pending_row in pending:
                created_offers.extend(
                    self._offer_queue(
                        connection,
                        _row(pending_row),
                        now,
                        offer_seconds,
                        fanout=1,
                        only_agent_id=actor.agent_id,
                    )
                )
            offer_rows = connection.execute(
                """
                SELECT o.*, q.project_id, q.ticket_id, q.target_role, q.context_version,
                       q.priority, q.status AS queue_status
                FROM work_offers o
                JOIN work_queue q ON q.queue_id = o.queue_id
                WHERE o.agent_id = ? AND q.project_id = ? AND o.status = 'offered'
                ORDER BY q.priority DESC, o.offered_at ASC
                LIMIT ?
                """,
                (actor.agent_id, project_id, limit),
            ).fetchall()
            offers = [self._inbox_offer(connection, _row(row)) for row in offer_rows]
            lease_rows = connection.execute(
                """
                SELECT l.*
                FROM leases l
                JOIN tickets t ON t.ticket_id = l.ticket_id
                JOIN features f ON f.feature_id = t.feature_id
                WHERE l.agent_id = ? AND l.role = ? AND f.project_id = ?
                  AND l.expires_at > ?
                ORDER BY l.acquired_at ASC
                """,
                (actor.agent_id, actor.role.value, project_id, _iso(now)),
            ).fetchall()
            active_leases = []
            for lease_row in lease_rows:
                lease = _row(lease_row)
                assert lease is not None
                lease["ticket"] = self._ticket(connection, lease["ticket_id"])
                active_leases.append(lease)
        return {
            "agent": presence,
            "active_leases": active_leases,
            "offers": offers,
            "offered_now": len(created_offers),
            "next_poll_after_seconds": 20,
        }

    def dispatch_pending_work(
        self, actor: Actor, *, offer_seconds: int = 90, fanout: int = 3
    ) -> dict[str, Any]:
        """Fan pending hand-offs to live matching agents; safe to call repeatedly."""
        require_role(actor, Role.ADMIN)
        self._offer_seconds(offer_seconds)
        if not isinstance(fanout, int) or isinstance(fanout, bool) or not 1 <= fanout <= 20:
            raise ValidationError("fanout must be an integer from 1 to 20")
        now = _now()
        with self.store.connection(immediate=True) as connection:
            self._reconcile_work_queue(connection, actor)
            self._expire_offers(connection, now)
            queues = connection.execute(
                """
                SELECT * FROM work_queue WHERE status IN ('pending', 'offered')
                ORDER BY priority DESC, created_at ASC
                """
            ).fetchall()
            offers: list[dict[str, Any]] = []
            for queue_row in queues:
                offers.extend(
                    self._offer_queue(
                        connection, _row(queue_row), now, offer_seconds, fanout=fanout
                    )
                )
            pending_count = connection.execute(
                "SELECT COUNT(*) FROM work_queue WHERE status = 'pending'"
            ).fetchone()[0]
        return {
            "offered_count": len(offers),
            "pending_count": pending_count,
            "offers": offers,
        }

    def dispatch_overview(self, actor: Actor, project_id: str) -> dict[str, Any]:
        """Read-only project queue, offer and online-agent state for the dashboard."""
        require_project(actor, project_id)
        now = _iso()
        with self.store.connection() as connection:
            presence_rows = connection.execute(
                "SELECT * FROM agent_presence ORDER BY role, agent_id"
            ).fetchall()
            agents = []
            for row in presence_rows:
                presence = _row(row)
                assert presence is not None
                if "*" in presence["project_ids"] or project_id in presence["project_ids"]:
                    presence["online"] = presence["expires_at"] > now
                    agents.append(presence)
            queue_rows = connection.execute(
                """
                SELECT q.*, COUNT(o.offer_id) AS open_offer_count
                FROM work_queue q
                LEFT JOIN work_offers o ON o.queue_id = q.queue_id AND o.status = 'offered'
                WHERE q.project_id = ? AND q.status IN ('pending', 'offered', 'claimed')
                GROUP BY q.queue_id
                ORDER BY CASE q.status WHEN 'pending' THEN 0 WHEN 'offered' THEN 1 ELSE 2 END,
                         q.priority DESC, q.created_at ASC
                """,
                (project_id,),
            ).fetchall()
            queues = []
            for row in queue_rows:
                queue = _row(row)
                assert queue is not None
                queue["ticket"] = self._ticket(connection, queue["ticket_id"])
                queues.append(queue)
        # Bounded pointers only, so the existing read-only dashboard can show that
        # a Planner is being waited on without exposing the underlying evidence.
        interventions = [
            self.intervention_payload(intervention) | {"status": intervention["status"]}
            for intervention in self.list_planner_interventions(actor, project_id)
        ]
        return {"agents": agents, "queues": queues, "planner_interventions": interventions}

    def _queue_ticket(
        self,
        connection: Connection,
        actor: Actor,
        project_id: str,
        ticket: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Persist a hand-off before any notifier or polling loop sees it."""
        target_role = self._dispatch_target_role(ticket)
        if target_role is None:
            return None
        existing = _row(
            connection.execute(
                """
                SELECT * FROM work_queue
                WHERE ticket_id = ? AND status IN ('pending', 'offered')
                """,
                (ticket["ticket_id"],),
            ).fetchone()
        )
        if existing is not None:
            return existing
        timestamp = _iso()
        queue_id = _id("queue")
        connection.execute(
            """
            INSERT INTO work_queue
                (queue_id, ticket_id, project_id, target_role, context_version, priority,
                 status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                queue_id,
                ticket["ticket_id"],
                project_id,
                target_role.value,
                ticket["version"],
                ticket["priority"],
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO dispatch_outbox
                (outbox_id, queue_id, project_id, target_role, event_type, payload,
                 status, created_at)
            VALUES (?, ?, ?, ?, 'work.queued', ?, 'pending', ?)
            """,
            (
                _id("outbox"),
                queue_id,
                project_id,
                target_role.value,
                json.dumps(
                    {
                        "ticket_id": ticket["ticket_id"],
                        "context_ref": f"ctx:ticket:{ticket['ticket_id']}:v{ticket['version']}",
                    }
                ),
                timestamp,
            ),
        )
        self._event(
            connection,
            actor,
            project_id,
            "work.queued",
            "ticket",
            ticket["ticket_id"],
            ticket["version"],
            {"queue_id": queue_id, "target_role": target_role.value},
        )
        return self._one(connection, "SELECT * FROM work_queue WHERE queue_id = ?", (queue_id,))

    def _reconcile_work_queue(self, connection: Connection, actor: Actor) -> None:
        """Backfill durable queues for existing tickets created before the dispatcher."""
        rows = connection.execute(
            """
            SELECT t.ticket_id
            FROM tickets t
            JOIN features f ON f.feature_id = t.feature_id
            LEFT JOIN leases l ON l.ticket_id = t.ticket_id
            WHERE t.status IN ('READY', 'REWORK', 'QA_READY', 'VERIFIED', 'MERGE_READY')
              AND l.lease_id IS NULL
            ORDER BY t.priority DESC, t.created_at ASC
            """
        ).fetchall()
        for row in rows:
            ticket_id = str(row["ticket_id"])
            project_id = self._project_for_ticket(connection, ticket_id)
            self._queue_ticket(connection, actor, project_id, self._ticket(connection, ticket_id))

    @staticmethod
    def _dispatch_target_role(ticket: dict[str, Any]) -> Role | None:
        status = TicketStatus(ticket["status"])
        if status is TicketStatus.QA_READY:
            return Role.QA
        if status in {TicketStatus.VERIFIED, TicketStatus.MERGE_READY}:
            return Role.INTEGRATOR
        if status in {TicketStatus.READY, TicketStatus.REWORK}:
            role = Role(ticket["required_role"])
            return role if role in {Role.DEVELOPER, Role.INTEGRATOR} else None
        return None

    def _offer_queue(
        self,
        connection: Connection,
        queue: dict[str, Any] | None,
        now: datetime,
        offer_seconds: int,
        *,
        fanout: int,
        only_agent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if queue is None or queue["status"] not in {"pending", "offered"}:
            return []
        existing_offer_count = connection.execute(
            "SELECT COUNT(*) FROM work_offers WHERE queue_id = ? AND status = 'offered'",
            (queue["queue_id"],),
        ).fetchone()[0]
        available_slots = 1 if only_agent_id is not None else max(0, fanout - existing_offer_count)
        if available_slots == 0:
            return []
        candidate_rows = connection.execute(
            "SELECT * FROM agent_presence WHERE role = ? AND expires_at > ? ORDER BY last_seen_at DESC, agent_id ASC",
            (queue["target_role"], _iso(now)),
        ).fetchall()
        offers: list[dict[str, Any]] = []
        for candidate_row in candidate_rows:
            presence = _row(candidate_row)
            assert presence is not None
            if only_agent_id is not None and presence["agent_id"] != only_agent_id:
                continue
            if not self._presence_can_access(presence, queue["project_id"]):
                continue
            if len(offers) >= available_slots:
                break
            active_leases = connection.execute(
                "SELECT COUNT(*) FROM leases WHERE agent_id = ? AND expires_at > ?",
                (presence["agent_id"], _iso(now)),
            ).fetchone()[0]
            if active_leases >= presence["capacity"]:
                continue
            duplicate = connection.execute(
                """
                SELECT 1 FROM work_offers
                WHERE queue_id = ? AND agent_id = ? AND status = 'offered'
                """,
                (queue["queue_id"], presence["agent_id"]),
            ).fetchone()
            if duplicate is not None:
                continue
            timestamp = _iso(now)
            offer_id = _id("offer")
            connection.execute(
                """
                INSERT INTO work_offers
                    (offer_id, queue_id, agent_id, status, offered_at, expires_at)
                VALUES (?, ?, ?, 'offered', ?, ?)
                """,
                (
                    offer_id,
                    queue["queue_id"],
                    presence["agent_id"],
                    timestamp,
                    _iso(now + timedelta(seconds=offer_seconds)),
                ),
            )
            offers.append(self._one(connection, "SELECT * FROM work_offers WHERE offer_id = ?", (offer_id,)))
        if offers:
            timestamp = _iso(now)
            connection.execute(
                "UPDATE work_queue SET status = 'offered', updated_at = ? WHERE queue_id = ?",
                (timestamp, queue["queue_id"]),
            )
            connection.execute(
                """
                UPDATE dispatch_outbox SET status = 'dispatched', dispatched_at = ?
                WHERE queue_id = ? AND status = 'pending'
                """,
                (timestamp, queue["queue_id"]),
            )
        return offers

    def _expire_offers(self, connection: Connection, now: datetime) -> None:
        timestamp = _iso(now)
        connection.execute(
            """
            UPDATE work_offers SET status = 'expired', closed_at = ?
            WHERE status = 'offered' AND expires_at <= ?
            """,
            (timestamp, timestamp),
        )
        connection.execute(
            """
            UPDATE work_queue SET status = 'pending', updated_at = ?
            WHERE status = 'offered'
              AND NOT EXISTS (
                  SELECT 1 FROM work_offers o
                  WHERE o.queue_id = work_queue.queue_id AND o.status = 'offered'
              )
            """,
            (timestamp,),
        )

    def _mark_queue_claimed(
        self, connection: Connection, ticket_id: str, agent_id: str, now: datetime
    ) -> None:
        queue_rows = connection.execute(
            """
            SELECT queue_id FROM work_queue
            WHERE ticket_id = ? AND status IN ('pending', 'offered')
            """,
            (ticket_id,),
        ).fetchall()
        if not queue_rows:
            return
        timestamp = _iso(now)
        for row in queue_rows:
            queue_id = str(row["queue_id"])
            connection.execute(
                """
                UPDATE work_queue
                SET status = 'claimed', claimed_by = ?, claimed_at = ?, updated_at = ?
                WHERE queue_id = ?
                """,
                (agent_id, timestamp, timestamp, queue_id),
            )
            connection.execute(
                """
                UPDATE work_offers
                SET status = CASE WHEN agent_id = ? THEN 'claimed' ELSE 'cancelled' END,
                    closed_at = ?
                WHERE queue_id = ? AND status = 'offered'
                """,
                (agent_id, timestamp, queue_id),
            )

    def _refresh_presence(
        self,
        connection: Connection,
        actor: Actor,
        presence_seconds: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        presence = _row(
            connection.execute(
                "SELECT * FROM agent_presence WHERE agent_id = ?", (actor.agent_id,)
            ).fetchone()
        )
        if presence is None:
            raise ValidationError("Register agent presence before reading the work inbox")
        if presence["role"] != actor.role.value:
            raise AuthorizationError("Agent presence role does not match authenticated identity")
        timestamp = now or _now()
        connection.execute(
            """
            UPDATE agent_presence
            SET project_ids = ?, last_seen_at = ?, expires_at = ?, updated_at = ?
            WHERE agent_id = ?
            """,
            (
                json.dumps(sorted(actor.project_ids)),
                _iso(timestamp),
                _iso(timestamp + timedelta(seconds=presence_seconds)),
                _iso(timestamp),
                actor.agent_id,
            ),
        )
        return self._presence(connection, actor.agent_id)

    def _presence(self, connection: Connection, agent_id: str) -> dict[str, Any]:
        return self._one(
            connection, "SELECT * FROM agent_presence WHERE agent_id = ?", (agent_id,)
        )

    def _inbox_offer(
        self, connection: Connection, offer: dict[str, Any] | None
    ) -> dict[str, Any]:
        assert offer is not None
        ticket = self._ticket(connection, offer["ticket_id"])
        return {
            "offer_id": offer["offer_id"],
            "queue_id": offer["queue_id"],
            "target_role": offer["target_role"],
            "expires_at": offer["expires_at"],
            "context_ref": f"ctx:ticket:{ticket['ticket_id']}:v{ticket['version']}",
            "ticket": ticket,
        }

    @staticmethod
    def _presence_can_access(presence: dict[str, Any], project_id: str) -> bool:
        return "*" in presence["project_ids"] or project_id in presence["project_ids"]
