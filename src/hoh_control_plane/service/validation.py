from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    COMMIT_RE,
    ConflictError,
    NotFoundError,
    ValidationError,
    _row,
    _write_audit_event,
    Connection,
    Path,
    re,
    require_project,
)

_ABBREVIATED_COMMIT_RE = re.compile(r"[0-9a-fA-F]{7,39}")


class ValidationMixin:
    """Shared row access, bounded input validation and audit events."""

    def _project_for_feature(self, connection: Connection, feature_id: str) -> str:
        row = connection.execute(
            "SELECT project_id FROM features WHERE feature_id = ?", (feature_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("Feature not found")
        return str(row["project_id"])

    def _project_for_ticket(self, connection: Connection, ticket_id: str) -> str:
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
        connection: Connection, query: str, parameters: tuple[Any, ...]
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

    @staticmethod
    def _failure_text(value: Any, maximum: int) -> str:
        """Fit a worker's error into its column instead of refusing it.

        A failure recorder runs inside a worker's ``except``. Validating the text
        there like user input raised a second error and left the job RUNNING
        whenever the first error was long or empty, so the text is bounded here.
        """
        text = str(value or "").strip() or "Worker failed without an error message"
        if len(text) <= maximum:
            return text
        marker = " ... [truncated]"
        return text[: maximum - len(marker)] + marker

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

    def _full_commit(self, actor: Actor, ticket_id: str, value: Any, field: str) -> str:
        """Accept a full or abbreviated (7+ hex) commit SHA; return the full one.

        Agents read commits as 12-character abbreviations, so a tool that takes
        a commit must accept one. The prefix is resolved in the project
        repository before anything compares it with a stored full SHA. Git runs
        outside any write transaction.
        """
        if isinstance(value, str) and COMMIT_RE.fullmatch(value):
            return value
        if not isinstance(value, str) or not _ABBREVIATED_COMMIT_RE.fullmatch(value):
            raise ValidationError(
                f"{field} must be a commit SHA of at least 7 hexadecimal characters",
                {"reason": "commit_invalid", "field": field},
            )
        with self.store.connection() as connection:
            project_id = self._project_for_ticket(connection, ticket_id)
            require_project(actor, project_id)
            repository = _row(connection.execute(
                "SELECT repository_path FROM project_repositories WHERE project_id = ?",
                (project_id,),
            ).fetchone())
        if repository is None:
            raise ValidationError(
                f"{field} is abbreviated and this project has no repository to resolve it in; "
                "pass the full SHA",
                {"reason": "commit_unresolvable", "field": field},
            )
        return self.git_worktrees.expand_commit(
            Path(repository["repository_path"]), value, field
        )

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
        connection: Connection,
        actor: Actor,
        project_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        payload: dict[str, Any],
    ) -> None:
        _write_audit_event(
            connection, actor, project_id, event_type, aggregate_type, aggregate_id,
            aggregate_version, payload,
        )
