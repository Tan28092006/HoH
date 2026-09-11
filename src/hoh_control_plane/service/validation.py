from __future__ import annotations

from hoh_control_plane.observability import current_correlation_id
from hoh_control_plane.service._common import (
    Actor,
    Any,
    COMMIT_RE,
    ConflictError,
    NotFoundError,
    ValidationError,
    _id,
    _iso,
    _row,
    json,
    sqlite3,
)


class ValidationMixin:
    """Shared row access, bounded input validation and audit events."""

    def _project_for_feature(self, connection: sqlite3.Connection, feature_id: str) -> str:
        row = connection.execute(
            "SELECT project_id FROM features WHERE feature_id = ?", (feature_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("Feature not found")
        return str(row["project_id"])

    def _project_for_ticket(self, connection: sqlite3.Connection, ticket_id: str) -> str:
        row = connection.execute(
            """
            SELECT f.project_id FROM tickets t
            JOIN features f ON f.feature_id = t.feature_id
            WHERE t.ticket_id = ?
            """,
            (ticket_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("Ticket not found")
        return str(row["project_id"])

    @staticmethod
    def _one(
        connection: sqlite3.Connection, query: str, parameters: tuple[Any, ...]
    ) -> dict[str, Any]:
        result = _row(connection.execute(query, parameters).fetchone())
        if result is None:
            raise NotFoundError("Resource not found")
        return result

    @staticmethod
    def _expect_version(resource: dict[str, Any], expected_version: int) -> None:
        if resource["version"] != expected_version:
            raise ConflictError(
                "Ticket version is stale",
                {"expected": expected_version, "current": resource["version"]},
            )

    @staticmethod
    def _not_blank(value: str, field: str) -> None:
        if not value or not value.strip():
            raise ValidationError(f"{field} cannot be blank")

    @classmethod
    def _bounded_text(
        cls, value: str, field: str, maximum: int, *, allow_blank: bool = False
    ) -> None:
        if not isinstance(value, str):
            raise ValidationError(f"{field} must be text")
        if not allow_blank:
            cls._not_blank(value, field)
        if len(value) > maximum:
            raise ValidationError(f"{field} must be at most {maximum} characters")

    @classmethod
    def _bounded_list(
        cls,
        values: list[Any],
        field: str,
        maximum_items: int,
        maximum_item_length: int,
        *,
        required: bool = False,
        strings_only: bool = True,
    ) -> None:
        if not isinstance(values, list):
            raise ValidationError(f"{field} must be a list")
        if required and not values:
            raise ValidationError(f"{field} cannot be empty")
        if len(values) > maximum_items:
            raise ValidationError(f"{field} supports at most {maximum_items} items")
        if strings_only:
            for index, value in enumerate(values):
                cls._bounded_text(value, f"{field}[{index}]", maximum_item_length)

    @classmethod
    def _test_command(cls, command: list[str]) -> None:
        cls._bounded_list(command, "test_command", 20, 500, required=True)
        for index, part in enumerate(command):
            if any(character in part for character in ("\x00", "\r", "\n")):
                raise ValidationError(f"test_command[{index}] contains an invalid control character")

    @staticmethod
    def _priority(value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
            raise ValidationError("priority must be an integer from 0 to 100")

    @staticmethod
    def _capacity(value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 20:
            raise ValidationError("capacity must be an integer from 1 to 20")

    @staticmethod
    def _presence_seconds(value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or not 30 <= value <= 3600:
            raise ValidationError("presence_seconds must be an integer from 30 to 3600")

    @staticmethod
    def _offer_seconds(value: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or not 30 <= value <= 3600:
            raise ValidationError("offer_seconds must be an integer from 30 to 3600")

    @staticmethod
    def _valid_commit(value: str, field: str) -> None:
        if not COMMIT_RE.fullmatch(value):
            raise ValidationError(f"{field} must be a 40-64 character hexadecimal digest")

    @staticmethod
    def _strip_ref(reference: str, kind: str) -> str:
        prefix = f"{kind}:"
        context_prefix = f"ctx:{kind}:"
        if reference.startswith(context_prefix):
            return reference[len(context_prefix) :].rsplit(":v", 1)[0]
        if reference.startswith(prefix):
            return reference[len(prefix) :]
        return reference

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        actor: Actor,
        project_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        payload: dict[str, Any],
    ) -> None:
        correlation_id = current_correlation_id()
        # The request that last moved this aggregate is what caused this one to
        # act, which is what links a chain across worker processes: each step
        # read the state the previous step left.
        previous = connection.execute(
            """
            SELECT correlation_id FROM audit_events
            WHERE aggregate_type = ? AND aggregate_id = ? AND correlation_id IS NOT NULL
            ORDER BY sequence DESC LIMIT 1
            """,
            (aggregate_type, aggregate_id),
        ).fetchone()
        causation_id = previous["correlation_id"] if previous else None
        if causation_id == correlation_id:
            # Several events from one request are siblings, not cause and effect.
            causation_id = None
        connection.execute(
            """
            INSERT INTO audit_events
                (event_id, event_type, aggregate_type, aggregate_id, aggregate_version,
                 project_id, actor_id, actor_role, payload, correlation_id, causation_id,
                 occurred_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _id("event"),
                event_type,
                aggregate_type,
                aggregate_id,
                aggregate_version,
                project_id,
                actor.agent_id,
                actor.role.value,
                json.dumps(payload),
                correlation_id,
                causation_id,
                _iso(),
            ),
        )
