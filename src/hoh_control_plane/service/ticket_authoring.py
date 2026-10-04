"""Feature and ticket authoring: create, ready, scope, fields and split."""
from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    ConflictError,
    RETIRABLE_TICKET_STATUSES,
    Role,
    TicketStatus,
    ValidationError,
    _id,
    _iso,
    json,
    require_project,
    require_role,
    StoreIntegrityError,
)


class TicketAuthoringMixin:
    """Feature and ticket authoring: create, ready, scope, fields and split."""

    def create_feature(
        self,
        actor: Actor,
        project_id: str,
        feature_id: str,
        title: str,
        objective: str,
        parent_feature_id: str | None = None,
        priority: int = 50,
    ) -> dict[str, Any]:
        require_role(actor, Role.PLANNER, Role.ADMIN)
        require_project(actor, project_id)
        self._bounded_text(feature_id, "feature_id", 100)
        self._bounded_text(title, "title", 240)
        self._bounded_text(objective, "objective", 8000)
        if parent_feature_id is not None:
            self._bounded_text(parent_feature_id, "parent_feature_id", 100)
        self._priority(priority)
        timestamp = _iso()
        try:
            with self.store.connection(immediate=True) as connection:
                if parent_feature_id:
                    parent = self._one(
                        connection,
                        "SELECT project_id FROM features WHERE feature_id = ?",
                        (parent_feature_id,),
                    )
                    if parent["project_id"] != project_id:
                        raise ValidationError("Parent feature belongs to another project")
                connection.execute(
                    """
                    INSERT INTO features
                        (feature_id, project_id, parent_feature_id, title, objective,
                         priority, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        feature_id,
                        project_id,
                        parent_feature_id,
                        title,
                        objective,
                        priority,
                        timestamp,
                        timestamp,
                    ),
                )
                self._event(
                    connection,
                    actor,
                    project_id,
                    "feature.created",
                    "feature",
                    feature_id,
                    1,
                    {"title": title},
                )
                feature = self._one(
                    connection, "SELECT * FROM features WHERE feature_id = ?", (feature_id,)
                )
        except StoreIntegrityError as exc:
            raise ConflictError("Feature already exists or project is missing") from exc
        return feature

    def create_ticket(
        self,
        actor: Actor,
        feature_id: str,
        ticket_id: str,
        title: str,
        objective: str,
        required_role: Role,
        acceptance_criteria: list[str],
        preservation_constraints: list[str],
        file_scope: list[str],
        ready: bool = False,
        ticket_type: str = "feature_work",
        priority: int = 50,
        parent_ticket_id: str | None = None,
        dependency_ids: list[str] | None = None,
        validation_contract: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(ticket_id, "ticket_id", 100)
        self._bounded_text(title, "title", 240)
        self._bounded_text(objective, "objective", 8000)
        self._bounded_text(ticket_type, "ticket_type", 50)
        self._priority(priority)
        self._bounded_list(acceptance_criteria, "acceptance_criteria", 30, 2000, required=True)
        self._bounded_list(preservation_constraints, "preservation_constraints", 30, 2000)
        self._bounded_list(file_scope, "file_scope", 100, 1000)
        if file_scope:
            file_scope = [self._directory_scope(scope) for scope in file_scope]
        normalized_contract = (
            self._validated_validation_contract(acceptance_criteria, validation_contract)
            if validation_contract is not None else []
        )
        if required_role not in {Role.DEVELOPER, Role.QA, Role.INTEGRATOR}:
            raise ValidationError("Ticket must target an executable role")
        dependency_ids = dependency_ids or []
        self._bounded_list(dependency_ids, "dependency_ids", 100, 100)
        timestamp = _iso()
        try:
            with self.store.connection(immediate=True) as connection:
                project_id = self._project_for_feature(connection, feature_id)
                require_project(actor, project_id)
                status = TicketStatus.READY if ready else TicketStatus.BACKLOG
                project = self._one(
                    connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
                )
                planned_ticket = {
                    "ticket_id": ticket_id,
                    "validation_contract": normalized_contract,
                }
                if ready:
                    self._require_repository_reconciled(connection, project_id)
                    self._assert_quality_ready(project, planned_ticket)
                connection.execute(
                    """
                    INSERT INTO tickets
                        (ticket_id, feature_id, parent_ticket_id, ticket_type, title,
                         objective, status, priority, required_role, acceptance_criteria,
                         validation_contract,
                         preservation_constraints, file_scope, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ticket_id,
                        feature_id,
                        parent_ticket_id,
                        ticket_type,
                        title,
                        objective,
                        status.value,
                        priority,
                        required_role.value,
                        json.dumps(acceptance_criteria),
                        json.dumps(normalized_contract),
                        json.dumps(preservation_constraints),
                        json.dumps(file_scope),
                        timestamp,
                        timestamp,
                    ),
                )
                for dependency_id in dependency_ids:
                    if dependency_id == ticket_id:
                        raise ValidationError("Ticket cannot depend on itself")
                    connection.execute(
                        """
                        INSERT INTO ticket_dependencies (ticket_id, depends_on_ticket_id)
                        VALUES (?, ?)
                        """,
                        (ticket_id, dependency_id),
                    )
                self._event(
                    connection,
                    actor,
                    project_id,
                    "ticket.ready" if ready else "ticket.created",
                    "ticket",
                    ticket_id,
                    1,
                    {"title": title, "required_role": required_role.value},
                )
                ticket = self._ticket(connection, ticket_id)
                if ready:
                    self._queue_ticket(connection, actor, project_id, ticket)
        except StoreIntegrityError as exc:
            raise ConflictError("Ticket already exists or dependency is invalid") from exc
        return ticket

    def set_ticket_ready(
        self, actor: Actor, ticket_id: str, expected_version: int
    ) -> dict[str, Any]:
        """Move a planned ticket into the executable queue."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] not in {TicketStatus.BACKLOG.value, TicketStatus.BLOCKED.value}:
                raise ConflictError("Only backlog or blocked tickets can be made ready")
            project = self._one(
                connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            )
            self._assert_quality_ready(project, ticket)
            next_version = ticket["version"] + 1
            timestamp = _iso()
            connection.execute(
                """
                UPDATE tickets SET status = ?, blocked_reason = NULL, version = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (TicketStatus.READY.value, next_version, timestamp, ticket_id),
            )
            self._event(
                connection, actor, project_id, "ticket.ready", "ticket", ticket_id,
                next_version, {"previous_status": ticket["status"]},
            )
            ready_ticket = self._ticket(connection, ticket_id)
            self._queue_ticket(connection, actor, project_id, ready_ticket)
            return {"ticket": ready_ticket}

    def update_ticket_write_scope(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        file_scope: list[str],
        reason: str,
    ) -> dict[str, Any]:
        """Update planned or blocked ticket scope with optimistic concurrency and audit evidence."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_list(file_scope, "file_scope", 100, 1000, required=True)
        self._bounded_text(reason, "reason", 2000)
        normalized_scope = [self._repository_relative_scope(scope) for scope in file_scope]
        if len(set(normalized_scope)) != len(normalized_scope):
            raise ValidationError("file_scope cannot contain duplicate paths")

        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if ticket["status"] not in {TicketStatus.BACKLOG.value, TicketStatus.BLOCKED.value}:
                raise ConflictError("Write scope cannot change during active development or QA")
            active_lease = connection.execute(
                "SELECT 1 FROM leases WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if active_lease is not None:
                raise ConflictError("Write scope cannot change while a ticket has an active lease")

            previous_scope = ticket["file_scope"]
            if normalized_scope == previous_scope:
                raise ValidationError("file_scope is unchanged")
            timestamp = _iso()
            next_version = ticket["version"] + 1
            connection.execute(
                """
                UPDATE tickets SET file_scope = ?, version = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (json.dumps(normalized_scope), next_version, timestamp, ticket_id),
            )
            entry_id = _id(connection, "ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'write_scope_updated', ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    project_id,
                    ticket["feature_id"],
                    ticket_id,
                    f"Planner updated write scope: {reason}",
                    actor.agent_id,
                    actor.role.value,
                    timestamp,
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                "ticket.write_scope_updated",
                "ticket",
                ticket_id,
                next_version,
                {
                    "previous_file_scope": previous_scope,
                    "file_scope": normalized_scope,
                    "reason": reason,
                },
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "previous_file_scope": previous_scope,
                "context_entry": self._one(
                    connection, "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
                ),
            }

    def update_ticket_fields(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        reason: str,
        title: str | None = None,
        objective: str | None = None,
        priority: int | None = None,
        acceptance_criteria: list[str] | None = None,
        validation_contract: list[dict[str, Any]] | None = None,
        preservation_constraints: list[str] | None = None,
        dependency_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update bounded planning fields on an eligible ticket with optimistic concurrency."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(reason, "reason", 2000)
        if title is not None:
            self._bounded_text(title, "title", 240)
        if objective is not None:
            self._bounded_text(objective, "objective", 8000)
        if priority is not None:
            self._priority(priority)
        if acceptance_criteria is not None:
            self._bounded_list(acceptance_criteria, "acceptance_criteria", 30, 2000, required=True)
        if preservation_constraints is not None:
            self._bounded_list(preservation_constraints, "preservation_constraints", 30, 2000)
        normalized_dependency_ids: list[str] | None = None
        if dependency_ids is not None:
            self._bounded_list(dependency_ids, "dependency_ids", 100, 100)
            normalized_dependency_ids = list(dict.fromkeys(dependency_ids))
            if ticket_id in normalized_dependency_ids:
                raise ValidationError("Ticket cannot depend on itself")
        if all(
            value is None
            for value in (
                title, objective, priority, acceptance_criteria, validation_contract,
                preservation_constraints, dependency_ids,
            )
        ):
            raise ValidationError("At least one field must be provided to update")

        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if TicketStatus(ticket["status"]) not in {
                TicketStatus.BACKLOG, TicketStatus.READY, TicketStatus.BLOCKED,
            }:
                raise ConflictError(
                    "Only backlog, ready or blocked tickets can have their fields updated"
                )
            active_lease = connection.execute(
                "SELECT 1 FROM leases WHERE ticket_id = ? AND expires_at > ?",
                (ticket_id, _iso()),
            ).fetchone()
            if active_lease is not None:
                raise ConflictError(
                    "Ticket fields cannot change while a ticket has an active lease"
                )

            normalized_contract = None
            if validation_contract is not None:
                normalized_contract = self._validated_validation_contract(
                    acceptance_criteria or ticket["acceptance_criteria"], validation_contract
                )
            elif acceptance_criteria is not None and ticket.get("validation_contract"):
                raise ValidationError(
                    "Changing acceptance criteria requires a replacement validation contract"
                )
            if ticket["status"] == TicketStatus.READY.value and (
                acceptance_criteria is not None or normalized_contract is not None
            ):
                project = self._one(
                    connection, "SELECT * FROM projects WHERE project_id = ?", (project_id,)
                )
                self._assert_quality_ready(
                    project,
                    {
                        **ticket,
                        "acceptance_criteria": acceptance_criteria or ticket["acceptance_criteria"],
                        "validation_contract": (
                            normalized_contract
                            if normalized_contract is not None
                            else ticket.get("validation_contract") or []
                        ),
                    },
                )

            if normalized_dependency_ids is not None:
                unfinished_new_dependencies = []
                for dependency_id in normalized_dependency_ids:
                    dependency = self._one(
                        connection, "SELECT * FROM tickets WHERE ticket_id = ?", (dependency_id,)
                    )
                    if self._project_for_ticket(connection, dependency_id) != project_id:
                        raise ValidationError(
                            f"Dependency {dependency_id} must belong to the same project"
                        )
                    if self._dependency_would_cycle(connection, ticket_id, dependency_id):
                        raise ValidationError(
                            f"Dependency on {dependency_id} would create a dependency cycle"
                        )
                    if dependency["status"] != TicketStatus.DONE.value:
                        unfinished_new_dependencies.append(dependency_id)
                if (
                    TicketStatus(ticket["status"]) is TicketStatus.READY
                    and unfinished_new_dependencies
                ):
                    raise ConflictError(
                        "Cannot leave a ready ticket depending on unfinished work: "
                        + ", ".join(unfinished_new_dependencies)
                    )

            timestamp = _iso()
            next_version = ticket["version"] + 1
            set_clauses = ["version = ?", "updated_at = ?"]
            params: list[Any] = [next_version, timestamp]
            changed_fields = []
            if title is not None:
                set_clauses.append("title = ?")
                params.append(title)
                changed_fields.append("title")
            if objective is not None:
                set_clauses.append("objective = ?")
                params.append(objective)
                changed_fields.append("objective")
            if priority is not None:
                set_clauses.append("priority = ?")
                params.append(priority)
                changed_fields.append("priority")
            if acceptance_criteria is not None:
                set_clauses.append("acceptance_criteria = ?")
                params.append(json.dumps(acceptance_criteria))
                changed_fields.append("acceptance_criteria")
            if normalized_contract is not None:
                set_clauses.append("validation_contract = ?")
                params.append(json.dumps(normalized_contract))
                changed_fields.append("validation_contract")
            if preservation_constraints is not None:
                set_clauses.append("preservation_constraints = ?")
                params.append(json.dumps(preservation_constraints))
                changed_fields.append("preservation_constraints")
            params.append(ticket_id)
            connection.execute(
                f"UPDATE tickets SET {', '.join(set_clauses)} WHERE ticket_id = ?", params
            )
            if normalized_dependency_ids is not None:
                connection.execute(
                    "DELETE FROM ticket_dependencies WHERE ticket_id = ?", (ticket_id,)
                )
                for dependency_id in normalized_dependency_ids:
                    connection.execute(
                        """
                        INSERT INTO ticket_dependencies (ticket_id, depends_on_ticket_id)
                        VALUES (?, ?)
                        """,
                        (ticket_id, dependency_id),
                    )
                changed_fields.append("dependency_ids")

            entry_id = _id(connection, "ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'ticket_fields_updated', ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    project_id,
                    ticket["feature_id"],
                    ticket_id,
                    reason,
                    actor.agent_id,
                    actor.role.value,
                    timestamp,
                ),
            )
            self._event(
                connection,
                actor,
                project_id,
                "ticket.fields_updated",
                "ticket",
                ticket_id,
                next_version,
                {"changed_fields": changed_fields, "reason": reason},
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "changed_fields": changed_fields,
                "context_entry": self._one(
                    connection, "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
                ),
            }

    def split_ticket(
        self,
        actor: Actor,
        ticket_id: str,
        expected_version: int,
        replacement_ticket_ids: list[str],
        reason: str,
    ) -> dict[str, Any]:
        """Retire a ticket whose scope is now carried by two or more narrower tickets."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        self._bounded_text(reason, "reason", 2000)
        self._bounded_list(
            replacement_ticket_ids, "replacement_ticket_ids", 20, 100, required=True
        )
        if len(replacement_ticket_ids) < 2:
            raise ValidationError("Splitting a ticket requires at least two replacement tickets")
        if len(set(replacement_ticket_ids)) != len(replacement_ticket_ids):
            raise ValidationError("replacement_ticket_ids cannot contain duplicates")

        with self.store.connection(immediate=True) as connection:
            ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
            self._expect_version(ticket, expected_version)
            if TicketStatus(ticket["status"]) not in RETIRABLE_TICKET_STATUSES:
                raise ConflictError("Only backlog, ready or blocked tickets can be split")
            self._require_no_active_work(connection, ticket_id)
            replacements = [
                self._retirement_replacement(connection, ticket_id, project_id, replacement_id)
                for replacement_id in replacement_ticket_ids
            ]

            timestamp = _iso()
            next_version = ticket["version"] + 1
            closed_offers = self._close_ticket_dispatch(connection, ticket_id, timestamp)
            connection.execute(
                "UPDATE tickets SET status = ?, version = ?, updated_at = ? WHERE ticket_id = ?",
                (TicketStatus.SUPERSEDED.value, next_version, timestamp, ticket_id),
            )
            entry_id = _id(connection, "ctx")
            source_ref = ",".join(f"ticket:{rid}" for rid in replacement_ticket_ids)
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     source_ref, actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, 'ticket_split', ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    project_id,
                    ticket["feature_id"],
                    ticket_id,
                    reason,
                    source_ref,
                    actor.agent_id,
                    actor.role.value,
                    timestamp,
                ),
            )
            # Dependents of retired work stay blocked until a Planner rewires them;
            # naming them here keeps that consequence visible instead of silent.
            dependents = [
                str(row["ticket_id"])
                for row in connection.execute(
                    """
                    SELECT ticket_id FROM ticket_dependencies
                    WHERE depends_on_ticket_id = ? ORDER BY ticket_id
                    """,
                    (ticket_id,),
                ).fetchall()
            ]
            self._event(
                connection,
                actor,
                project_id,
                "ticket.split",
                "ticket",
                ticket_id,
                next_version,
                {
                    "previous_status": ticket["status"],
                    "reason": reason,
                    "replacement_ticket_ids": replacement_ticket_ids,
                    "closed_offers": closed_offers,
                    "dependent_ticket_ids": dependents,
                },
            )
            return {
                "ticket": self._ticket(connection, ticket_id),
                "replacements": replacements,
                "closed_offers": closed_offers,
                "dependent_ticket_ids": dependents,
                "context_entry": self._one(
                    connection, "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
                ),
            }
