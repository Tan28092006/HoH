from __future__ import annotations

from hoh_control_plane.agent_wakeup import (
    MAX_DELIVERY_ATTEMPTS,
    EndpointNotAllowed,
    validate_endpoint,
)
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
    _required_execution_capabilities,
    _serialize,
    base64,
    datetime,
    json,
    require_project,
    require_role,
    Connection,
    timedelta,
)


# One ticket-level predicate, used as a filter by the row view and as a column
# by the rollup. Written once so the two views can never disagree about what
# "waiting on a dependency" means.
_DEPENDENCY_UNMET = """NOT EXISTS (
                        SELECT 1 FROM ticket_dependencies d
                        JOIN tickets dependency ON dependency.ticket_id = d.depends_on_ticket_id
                        WHERE d.ticket_id = t.ticket_id AND dependency.status != 'DONE'
                    )"""


class DiscoveryMixin:
    """Role-scoped work discovery, dispatch queue and agent presence."""

    # Which ticket states each role is the next actor for. Used by both the
    # implicit per-role discovery filter and the explicit awaiting_role filter.
    _ROLE_ACTIONABLE_STATUSES: dict[Role, tuple[TicketStatus, ...]] = {
        Role.DEVELOPER: (TicketStatus.READY, TicketStatus.REWORK),
        Role.QA: (TicketStatus.QA_READY,),
        Role.INTEGRATOR: (TicketStatus.VERIFIED, TicketStatus.MERGE_READY),
    }

    _AWAITED_BY_STATUS: dict[TicketStatus, Role | None] = {
        TicketStatus.BACKLOG: Role.PLANNER,
        TicketStatus.BLOCKED: Role.PLANNER,
        TicketStatus.READY: Role.DEVELOPER,
        TicketStatus.REWORK: Role.DEVELOPER,
        TicketStatus.DEV_IN_PROGRESS: Role.DEVELOPER,
        TicketStatus.QA_READY: Role.QA,
        TicketStatus.QA_IN_PROGRESS: Role.QA,
        TicketStatus.VERIFIED: Role.INTEGRATOR,
        TicketStatus.MERGE_READY: Role.INTEGRATOR,
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
        detail: bool | None = None,
    ) -> dict[str, Any]:
        """List role-appropriate work as bounded metadata plus typed references.

        Discovery answers "where should I look", not "what does this ticket say":
        it returns feature identity, counts and typed ticket references, never
        ticket bodies. The objective, acceptance criteria, preservation
        constraints, write scope and blocked reason live behind
        ticket.get_context, which the caller resolves for the one ticket it picks.

        A Planner asking the broad question gets one row per feature with
        complete status counts and no ticket rows, because "which feature needs
        me" is answered by counts and the ticket rows are what it pays for. Any
        narrowing argument, or detail=True, returns the ticket rows instead. An
        execution role always gets rows: its question is which ticket to claim.
        """
        require_project(actor, project_id)
        limit = min(max(limit, 1), 100)
        if detail is not None and not isinstance(detail, bool):
            raise ValidationError("detail must be a boolean")
        selected_statuses = self._discovery_statuses(status, awaiting_role)
        narrowed = any(
            value not in (None, False)
            for value in (
                feature_id, status, awaiting_role, priority_min, blocked_only,
                unassigned_only, cursor,
            )
        )
        # A narrowing argument is itself the request for rows: a Planner that
        # named a feature, a status or a role has already chosen where to look.
        rollup = (
            actor.role in {Role.PLANNER, Role.ADMIN}
            and (detail is False or (detail is None and not narrowed))
        )
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
            # Retired and delivered work stays readable through ticket context,
            # never through a discovery surface that exists to hand out
            # actionable work. DONE is excluded for the same reason CANCELLED is:
            # there is nothing left to look at. A caller that genuinely wants a
            # finished ticket asks for it by status, and then it is not hidden.
            finished = {status.value for status in CLOSED_TICKET_STATUSES}
            finished.add(TicketStatus.DONE.value)
            finished -= set(selected_statuses or ())
            if finished:
                excluded = sorted(finished)
                where.append(f"t.status NOT IN ({', '.join('?' for _ in excluded)})")
                parameters.extend(excluded)
            # required_role describes the ticket's originating execution owner.
            # QA and Integrator routing is lifecycle-driven: a Developer ticket
            # remains required_role=developer after its candidate becomes
            # QA_READY/VERIFIED. Filtering those phases by required_role made
            # discovery falsely report no candidate even while the durable QA
            # or Integrator queue contained it.
            if actor.role is Role.DEVELOPER:
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
            if dependency_ready and not rollup:
                where.append(_DEPENDENCY_UNMET)
            if rollup:
                # Aggregated in SQL rather than from the fetched page, so the
                # counts are the whole board. The row query is capped at `limit`
                # and paginates; a count that inherited that cap would quietly
                # under-report the very backlog Planner is deciding from.
                return self._feature_rollup(
                    connection, actor, project, where, parameters, limit
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
            qa_presence = (
                _row(connection.execute(
                    "SELECT * FROM agent_presence WHERE agent_id = ?",
                    (actor.agent_id,),
                ).fetchone())
                if actor.role is Role.QA else None
            )

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
                    "pending_ticket_count": 0,
                    "blocked_ticket_count": 0,
                    "tickets": [],
                },
            )
            ticket_ref = f"ticket:{row['ticket_id']}"
            feature["pending_ticket_count"] += 1
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
                capability_state = self._qa_ticket_capability_state(qa_presence, row)
                if capability_state["claimable"]:
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
            ticket_summary = {
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
            if actor.role is Role.QA:
                ticket_summary["capability_state"] = capability_state
            if actor.role in {Role.PLANNER, Role.ADMIN}:
                # Every Planner row would recommend the same ticket.get_context
                # call; the ticket_ref beside it is that call's only argument.
                del ticket_summary["recommended_calls"]
            feature["tickets"].append(ticket_summary)

        discovered = {
            "agent": {
                "agent_id": actor.agent_id,
                "role": actor.role.value,
                "capabilities": self.capabilities(actor),
            },
            "project": project,
            "view": "tickets",
            "features": list(grouped.values()),
            "next_cursor": self._encoded_cursor(last) if has_more and last else None,
        }
        if actor.role in {Role.PLANNER, Role.ADMIN}:
            # Planning decisions are their own queue: a Planner sees them beside,
            # never mixed into, the tickets an execution role can claim.
            discovered["planning_jobs"] = self.discover_planner_jobs(actor, project_id)
        return discovered

    _DEPENDENCY_UNMET_CASE = f"CASE WHEN {_DEPENDENCY_UNMET} THEN 0 ELSE 1 END"

    def _feature_rollup(
        self,
        connection: Connection,
        actor: Actor,
        project: dict[str, Any],
        where: list[str],
        parameters: list[Any],
        limit: int,
    ) -> dict[str, Any]:
        """One row per feature: complete counts, no ticket rows.

        This is the Planner's board view. It answers "which feature is waiting
        on which role" from counts, and hands back one typed reference per
        feature, so the Planner spends its budget on the feature it then opens
        rather than on every ticket it will not touch.

        Counts are complete: the row view is capped at `limit` and paginates,
        and a board total that inherited that cap would under-report the very
        backlog the Planner is deciding from. Work waiting on an unfinished
        dependency is counted in dependency_blocked_count rather than filtered
        out, so a feature never looks finished because its remainder is blocked.
        """
        rows = connection.execute(
            f"""
            SELECT t.feature_id, t.status,
                   {self._DEPENDENCY_UNMET_CASE} AS dependency_blocked,
                   f.title AS feature_title, f.status AS feature_status,
                   f.version AS feature_version, f.parent_feature_id,
                   COUNT(*) AS ticket_count,
                   MAX(t.priority) AS top_priority,
                   MIN(t.created_at) AS oldest_created_at,
                   MAX(t.updated_at) AS last_movement_at
            FROM tickets t
            JOIN features f ON f.feature_id = t.feature_id
            WHERE {' AND '.join(where)}
            GROUP BY t.feature_id, t.status, dependency_blocked
            """,
            parameters,
        ).fetchall()

        features: dict[str, dict[str, Any]] = {}
        for raw in rows:
            row = _row(raw)
            assert row is not None
            feature = features.setdefault(
                row["feature_id"],
                {
                    "feature_id": row["feature_id"],
                    "title": row["feature_title"],
                    "status": row["feature_status"],
                    "parent_feature_id": row["parent_feature_id"],
                    "context_ref": (
                        f"ctx:feature:{row['feature_id']}:v{row['feature_version']}"
                    ),
                    "open_ticket_count": 0,
                    "blocked_ticket_count": 0,
                    "dependency_blocked_count": 0,
                    "top_priority": 0,
                    "oldest_created_at": row["oldest_created_at"],
                    "last_movement_at": row["last_movement_at"],
                    "ticket_counts": {},
                    "awaiting": {},
                },
            )
            count = int(row["ticket_count"])
            waiting_on_dependency = bool(row["dependency_blocked"])
            feature["open_ticket_count"] += count
            counts = feature["ticket_counts"]
            counts[row["status"]] = counts.get(row["status"], 0) + count
            if row["status"] == TicketStatus.BLOCKED.value:
                feature["blocked_ticket_count"] += count
            feature["top_priority"] = max(feature["top_priority"], int(row["top_priority"]))
            feature["oldest_created_at"] = min(
                feature["oldest_created_at"], row["oldest_created_at"]
            )
            feature["last_movement_at"] = max(
                feature["last_movement_at"], row["last_movement_at"]
            )
            if waiting_on_dependency:
                # Counted, never hidden, but not attributed to a role: nobody can
                # claim it yet, so reading it as an idle Developer would be wrong.
                feature["dependency_blocked_count"] += count
                continue
            awaited = self._AWAITED_BY_STATUS.get(TicketStatus(row["status"]))
            if awaited is not None:
                key = awaited.value
                feature["awaiting"][key] = feature["awaiting"].get(key, 0) + count

        ordered = sorted(
            features.values(),
            key=lambda item: (-item["top_priority"], item["feature_id"]),
        )
        truncated = len(ordered) > limit
        return {
            "agent": {
                "agent_id": actor.agent_id,
                "role": actor.role.value,
                "capabilities": self.capabilities(actor),
            },
            "project": project,
            "view": "feature_rollup",
            "features": ordered[:limit],
            "feature_count": len(ordered),
            "open_ticket_count": sum(item["open_ticket_count"] for item in ordered),
            "dependency_blocked_count": sum(
                item["dependency_blocked_count"] for item in ordered
            ),
            "truncated": truncated,
            "next_cursor": None,
            # Planning decisions are the other half of the Planner's cycle, and
            # it reads them here. A board view that dropped them would make the
            # default call the one that hides the work Planner must do first.
            "planning_jobs": self.discover_planner_jobs(actor, project["project_id"]),
            "detail_call": {
                "tool": "work.discover",
                "arguments": {
                    "project_id": project["project_id"],
                    "feature_id": "<feature_id from this list>",
                },
                "note": (
                    "resolve one feature's ticket rows, or pass detail=true for "
                    "rows across the board"
                ),
            },
        }

    def capabilities(self, actor: Actor) -> list[str]:
        common = [
            "project:read",
            "feature:read",
            "ticket:read",
            "context:resolve",
            "agent:presence",
            "work:inbox",
            "work:wait_for_event",
            "skill_compatibility:read",
            "experience:record",
            "experience:get",
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
                "repository:sync",
                "repository:reconcile",
                "ticket:stop_for_reconciliation",
                "external_landing:record",
                "test_command:configure",
                "verification_profile:configure",
                "agent_capabilities:approve",
                "validation_contract:configure",
                "skill_policy:configure",
                "experience:list",
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
        execution_capabilities: list[str] | None = None,
        capacity: int = 1,
        machine_id: str | None = None,
        presence_seconds: int = 120,
        skill_manifest: list[dict[str, Any]] | None = None,
        protocol_version: str | None = None,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        """Publish a short-lived, authenticated availability record for dispatch.

        An endpoint is optional. Registering none is not a degraded mode: the
        agent polls, which remains a complete path. Registering one that is not
        allowed is refused outright, because an endpoint that looks deliverable
        and is not would silently swallow every wakeup sent to it.

        execution_capabilities records the observable equipment this session has
        actually checked it can use. Strict QA dispatch uses only its intersection
        with the separately approved profile; self-report never grants authority.

        skill_manifest/protocol_version are recorded for project skill-policy
        compatibility only; they never grant or widen role permission, which
        always comes from the authenticated actor's own role.
        """
        self._bounded_list(skills, "skills", 30, 100)
        self._bounded_list(
            execution_capabilities or [], "execution_capabilities", 50, 100
        )
        normalized_capabilities = sorted(set(execution_capabilities or []))
        if len(normalized_capabilities) != len(execution_capabilities or []):
            raise ValidationError("execution_capabilities cannot contain duplicates")
        for capability in normalized_capabilities:
            if not capability.replace("_", "").replace("-", "").isalnum():
                raise ValidationError("execution_capabilities must use portable names")
        self._capacity(capacity)
        self._presence_seconds(presence_seconds)
        if machine_id is not None:
            self._bounded_text(machine_id, "machine_id", 120)
        if protocol_version is not None:
            self._bounded_text(protocol_version, "protocol_version", 50)
        validated_endpoint: str | None = None
        if endpoint is not None and endpoint.strip():
            self._bounded_text(endpoint, "endpoint", 1000)
            try:
                validated_endpoint = validate_endpoint(endpoint)
            except EndpointNotAllowed as error:
                raise ValidationError(str(error), {"reason": "endpoint_not_allowed"}) from error
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
                    (agent_id, role, project_ids, skills, execution_capabilities,
                     capacity, machine_id,
                     last_seen_at, expires_at, created_at, updated_at,
                     skill_manifest, protocol_version, skill_compatibility, endpoint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    project_ids = excluded.project_ids,
                    skills = excluded.skills,
                    execution_capabilities = excluded.execution_capabilities,
                    capacity = excluded.capacity,
                    machine_id = excluded.machine_id,
                    last_seen_at = excluded.last_seen_at,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at,
                    skill_manifest = excluded.skill_manifest,
                    protocol_version = excluded.protocol_version,
                    skill_compatibility = excluded.skill_compatibility,
                    endpoint = excluded.endpoint
                """,
                (
                    actor.agent_id,
                    actor.role.value,
                    json.dumps(sorted(actor.project_ids)),
                    json.dumps(skills),
                    json.dumps(normalized_capabilities),
                    capacity,
                    machine_id,
                    _iso(timestamp),
                    _iso(timestamp + timedelta(seconds=presence_seconds)),
                    _iso(timestamp),
                    _iso(timestamp),
                    json.dumps(normalized_manifest),
                    protocol_version,
                    json.dumps(compatibility),
                    validated_endpoint,
                ),
            )
            registered = self._presence(connection, actor.agent_id)
            if actor.role == Role.QA and (
                existing is None
                or existing.get("execution_capabilities") != normalized_capabilities
                or existing.get("machine_id") != machine_id
            ):
                for project_id in sorted(actor.project_ids):
                    if project_id == "*":
                        continue
                    self._event(
                        connection,
                        actor,
                        project_id,
                        "agent.execution_capabilities_declared",
                        "agent_presence",
                        actor.agent_id,
                        1,
                        {
                            "capability_count": len(normalized_capabilities),
                            "machine_bound": bool(machine_id),
                        },
                    )
            return registered

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
            offered_count = connection.execute(
                "SELECT COUNT(*) FROM work_offers o JOIN work_queue q ON q.queue_id = o.queue_id "
                "WHERE o.agent_id = ? AND q.project_id = ? AND o.status = 'offered'",
                (actor.agent_id, project_id),
            ).fetchone()[0]
            active_count = connection.execute(
                "SELECT COUNT(*) FROM leases WHERE agent_id = ? AND expires_at > ?",
                (actor.agent_id, _iso(now)),
            ).fetchone()[0]
            offer_limit = max(0, limit - offered_count) if active_count < presence["capacity"] else 0
            pending = connection.execute(
                """
                SELECT * FROM work_queue
                WHERE project_id = ? AND target_role = ? AND status IN ('pending', 'offered')
                  AND NOT EXISTS (SELECT 1 FROM work_offers o WHERE o.queue_id = work_queue.queue_id
                                  AND o.agent_id = ? AND o.status = 'offered')
                ORDER BY priority DESC, created_at ASC
                LIMIT ?
                """,
                (project_id, actor.role.value, actor.agent_id, offer_limit),
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
            planning_barrier = self._with_barrier_age(
                self._repository_reconciliation_state(connection, project_id)
            )
            capability_blockers = (
                self._qa_queue_capability_blockers(
                    connection, project_id, presence
                ) if actor.role is Role.QA else []
            )
        # A block on approval alone is settled here, where QA observes it: with
        # a preflight already recorded the Planner job is opened now, so nobody
        # waits on anybody. No probe runs on this path.
        capability_preflight = (
            self._settle_capability_blocks(actor, project_id)
            if any(
                blocker["missing_approval"] and not blocker["missing_declaration"]
                for blocker in capability_blockers
            ) else None
        )
        return {
            "capability_preflight": capability_preflight,
            "agent": presence,
            "active_leases": active_leases,
            "offers": offers,
            "offered_now": len(created_offers),
            "planning_barrier": planning_barrier,
            "capability_blockers": capability_blockers,
            "next_poll_after_seconds": 20,
        }

    @staticmethod
    def _qa_ticket_capability_state(
        presence: dict[str, Any] | None, ticket: dict[str, Any]
    ) -> dict[str, Any]:
        required = _required_execution_capabilities(ticket)
        declared = set((presence or {}).get("execution_capabilities") or [])
        approved = set((presence or {}).get("approved_execution_capabilities") or [])
        machine_matches = bool(
            presence
            and presence.get("machine_id")
            and presence.get("machine_id") == presence.get("capability_approved_machine_id")
        )
        if not machine_matches:
            approved = set()
        missing_declaration = sorted(required - declared)
        # Approval can only be meaningfully missing for equipment the live QA
        # session has already declared. Undeclared equipment is a provisioning
        # or registration gap, never something Planner should approve blindly.
        missing_approval = sorted((required & declared) - approved)
        return {
            "claimable": not missing_declaration and not missing_approval,
            "required": sorted(required),
            "missing_declaration": missing_declaration,
            "missing_approval": missing_approval,
            "approval_machine_matches": machine_matches,
            "next_action": (
                "verify available equipment and re-register; Planner provisions remaining gaps"
                if missing_declaration
                else "run qa.run_capability_preflight once, then wait silently; HoH opens the "
                "Planner approval job from its result"
                if missing_approval
                else "claim candidate"
            ),
        }

    def _qa_queue_capability_blockers(
        self, connection: Connection, project_id: str, presence: dict[str, Any]
    ) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        other_qa = [
            _row(row)
            for row in connection.execute(
                """SELECT * FROM agent_presence
                   WHERE role = 'qa' AND agent_id != ? AND expires_at > ?""",
                (presence["agent_id"], _iso()),
            ).fetchall()
        ]
        rows = connection.execute(
            """
            SELECT DISTINCT t.* FROM tickets t
            JOIN work_queue q ON q.ticket_id = t.ticket_id
            WHERE q.project_id = ? AND q.target_role = 'qa'
              AND q.status IN ('pending', 'offered') AND t.status = 'QA_READY'
            ORDER BY t.priority DESC, t.ticket_id
            """,
            (project_id,),
        ).fetchall()
        for raw in rows:
            ticket = _row(raw)
            assert ticket is not None
            state = self._qa_ticket_capability_state(presence, ticket)
            if state["claimable"]:
                continue
            if any(
                candidate is not None
                and self._presence_can_access(candidate, project_id)
                and self._qa_ticket_capability_state(candidate, ticket)["claimable"]
                for candidate in other_qa
            ):
                # This ticket is already serviceable by another online QA. The
                # current agent simply has no matching offer; waking it or the
                # Planner would spend a turn without changing delivery.
                continue
            blockers.append(
                {
                    "ticket_id": ticket["ticket_id"],
                    "ticket_ref": f"ticket:{ticket['ticket_id']}",
                    "priority": ticket["priority"],
                    **state,
                }
            )
        return blockers

    def pending_wakeups(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Offers that are live, undelivered, and still worth knocking about."""
        limit = min(max(limit, 1), 200)
        with self.store.connection() as connection:
            rows = connection.execute(
                """
                SELECT o.offer_id, o.expires_at, o.delivery_attempts,
                       q.project_id, q.ticket_id, q.target_role, q.context_version,
                       p.endpoint
                FROM work_offers o
                JOIN work_queue q ON q.queue_id = o.queue_id
                LEFT JOIN agent_presence p ON p.agent_id = o.agent_id
                WHERE o.status = 'offered'
                  AND o.delivery_status IN ('pending', 'failed')
                  AND o.delivery_attempts < ?
                  AND o.expires_at > ?
                ORDER BY o.offered_at ASC
                LIMIT ?
                """,
                (MAX_DELIVERY_ATTEMPTS, _iso(), limit),
            ).fetchall()
        return [_row(row) for row in rows]

    def record_wakeup(self, offer_id: str, *, status: str) -> dict[str, Any]:
        """Record what happened to one knock, and nothing else.

        Never touches the offer's own status, the ticket, or any lease: a
        notification that failed has lost a notification, and the work it was
        about is exactly where it was.
        """
        if status not in {"delivered", "failed", "skipped"}:
            raise ValidationError("Unknown wakeup delivery status")
        with self.store.connection(immediate=True) as connection:
            connection.execute(
                """
                UPDATE work_offers
                SET delivery_status = ?,
                    delivery_attempts = delivery_attempts + 1,
                    delivered_at = CASE WHEN ? = 'delivered' THEN ? ELSE delivered_at END
                WHERE offer_id = ?
                """,
                (status, status, _iso(), offer_id),
            )
            return _row(
                connection.execute(
                    "SELECT * FROM work_offers WHERE offer_id = ?", (offer_id,)
                ).fetchone()
            )

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
            planning_barrier = self._with_barrier_age(
                self._repository_reconciliation_state(connection, project_id)
            )
        # Bounded pointers only, so the existing read-only dashboard can show that
        # a Planner is being waited on without exposing the underlying evidence.
        interventions = [
            self.intervention_payload(intervention) | {"status": intervention["status"]}
            for intervention in self.list_planner_interventions(actor, project_id)
        ]
        return {
            "agents": agents,
            "queues": queues,
            "planner_interventions": interventions,
            "planning_barrier": planning_barrier,
        }

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
        queue_id = _id(connection, "queue")
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
                _id(connection, "outbox"),
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
        if connection.execute(
            """
            SELECT 1 FROM repository_reconciliations
            WHERE project_id = ? AND status = 'PENDING' LIMIT 1
            """,
            (queue["project_id"],),
        ).fetchone() is not None:
            return []
        existing_offer_count = connection.execute(
            "SELECT COUNT(*) FROM work_offers WHERE queue_id = ? AND status = 'offered'",
            (queue["queue_id"],),
        ).fetchone()[0]
        available_slots = 1 if only_agent_id is not None else max(0, fanout - existing_offer_count)
        if available_slots == 0:
            return []
        if only_agent_id is not None:
            candidate_rows = connection.execute(
                "SELECT * FROM agent_presence WHERE agent_id = ? AND role = ? AND expires_at > ?",
                (only_agent_id, queue["target_role"], _iso(now)),
            ).fetchall()
        else:
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
            if queue["target_role"] == Role.QA.value:
                ticket = self._ticket(connection, queue["ticket_id"])
                required = _required_execution_capabilities(ticket)
                available = set(presence.get("execution_capabilities") or []) & set(
                    presence.get("approved_execution_capabilities") or []
                )
                if presence.get("machine_id") != presence.get("capability_approved_machine_id"):
                    available = set()
                if required - available:
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
            offer_id = _id(connection, "offer")
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
