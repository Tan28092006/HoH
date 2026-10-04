"""Planner context entries."""
from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    CONTEXT_ENTRY_ALIASES,
    CONTEXT_ENTRY_TYPES,
    GENERAL_CONTEXT_ENTRY_TYPE,
    Role,
    ValidationError,
    _id,
    _iso,
    _row,
    require_project,
    require_role,
)


class PlannerContextMixin:
    """Planner context entries."""

    def append_planner_context(
        self,
        actor: Actor,
        *,
        entry_type: str,
        content: str,
        ticket_id: str | None = None,
        feature_id: str | None = None,
        source_ref: str | None = None,
    ) -> dict[str, Any]:
        """Record an auditable planning decision against exactly one context scope."""
        require_role(actor, Role.PLANNER, Role.ADMIN)
        if bool(ticket_id) == bool(feature_id):
            raise ValidationError("Provide exactly one of ticket_id or feature_id")
        self._bounded_text(entry_type, "entry_type", 64)
        self._bounded_text(content, "content", 8000)
        if source_ref is not None:
            self._bounded_text(source_ref, "source_ref", 1000)
        recorded_type, content = self._vocabulary_entry(entry_type, content)
        with self.store.connection(immediate=True) as connection:
            if ticket_id:
                ticket, project_id = self._authorized_ticket(connection, actor, ticket_id)
                resolved_feature_id = ticket["feature_id"]
                aggregate_type, aggregate_id, version = "ticket", ticket_id, ticket["version"]
            else:
                assert feature_id is not None
                project_id = self._project_for_feature(connection, feature_id)
                require_project(actor, project_id)
                feature = self._one(
                    connection, "SELECT * FROM features WHERE feature_id = ?", (feature_id,)
                )
                resolved_feature_id = feature_id
                aggregate_type, aggregate_id, version = "feature", feature_id, feature["version"]
            existing = _row(
                connection.execute(
                    """
                    SELECT * FROM context_entries
                    WHERE entry_type = ? AND content = ?
                      AND ((ticket_id IS NULL AND ? IS NULL) OR ticket_id = ?)
                      AND ((feature_id IS NULL AND ? IS NULL) OR feature_id = ?)
                      AND superseded_by IS NULL
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (
                        recorded_type, content,
                        ticket_id, ticket_id,
                        resolved_feature_id, resolved_feature_id,
                    ),
                ).fetchone()
            )
            if existing is not None:
                # Saying the same thing twice adds a row and no information. The
                # flag lets a caller tell "recorded" from "already said", which a
                # silent no-op would hide.
                return {**existing, "deduplicated": True}
            entry_id = _id(connection, "ctx")
            connection.execute(
                """
                INSERT INTO context_entries
                    (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                     source_ref, actor_id, actor_role, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id, project_id, resolved_feature_id, ticket_id, recorded_type, content,
                    source_ref, actor.agent_id, actor.role.value, _iso(),
                ),
            )
            self._event(
                connection, actor, project_id, "context.planning_appended", aggregate_type,
                aggregate_id, version, {"entry_id": entry_id, "entry_type": recorded_type},
            )
            entry = self._one(
                connection, "SELECT * FROM context_entries WHERE entry_id = ?", (entry_id,)
            )
            return {**entry, "deduplicated": False}

    @staticmethod
    def _vocabulary_entry(entry_type: str, content: str) -> tuple[str, str]:
        """Keep the type vocabulary closed, without losing what the caller meant.

        An unrecognised label is recorded under the general category rather than
        silently founding a new type, and is kept as a visible qualifier on the
        content so nothing about the caller's intent is thrown away.
        """
        canonical = CONTEXT_ENTRY_ALIASES.get(entry_type, entry_type)
        if canonical in CONTEXT_ENTRY_TYPES:
            return canonical, content
        return GENERAL_CONTEXT_ENTRY_TYPE, f"[{entry_type}] {content}"
