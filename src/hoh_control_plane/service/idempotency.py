from __future__ import annotations

from hoh_control_plane.service._common import (
    Actor,
    Any,
    Callable,
    DomainError,
    IDEMPOTENCY_RETENTION_SECONDS,
    IdempotencyConflictError,
    Role,
    _MAX_IDEMPOTENCY_KEY_CHARS,
    _id,
    _iso,
    _now,
    _row,
    _serialize,
    datetime,
    hashlib,
    json,
    re,
    require_role,
    timedelta,
)


class IdempotencyMixin:
    """Scoped idempotency reservation and replay."""

    def run_idempotent(
        self,
        actor: Actor,
        *,
        operation: str,
        idempotency_key: str | None,
        payload: dict[str, Any],
        run: Callable[[], Any],
        project_id: str | None = None,
    ) -> Any:
        """Run one consequential command so an identical retry replays its result.

        The reservation and the completion are separate short transactions because
        ``run`` opens its own write transaction; nothing here may hold one while the
        command executes. A command that fails releases its key rather than storing
        the failure, so a retry is re-executed and a failed transaction can never be
        replayed as a success.
        """
        if not idempotency_key:
            return run()
        self._bounded_text(idempotency_key, "idempotency_key", _MAX_IDEMPOTENCY_KEY_CHARS)
        self._bounded_text(operation, "operation", 120)
        scope_project = (
            project_id if project_id is not None else self._project_for_payload(actor, payload)
        )
        digest = hashlib.sha256(_serialize(payload).encode("utf-8")).hexdigest()
        scope = (actor.agent_id, scope_project, operation, idempotency_key)

        replay = self._reserve_idempotency_record(actor, scope, digest)
        if replay is not None:
            return replay
        try:
            result = run()
        except BaseException:
            self._release_idempotency_record(scope)
            raise
        self._complete_idempotency_record(scope, result)
        return result

    def _reserve_idempotency_record(
        self, actor: Actor, scope: tuple[str, str, str, str], digest: str
    ) -> Any | None:
        """Claim the key, or return the stored response when this is a true replay."""
        agent_id, project_id, operation, idempotency_key = scope
        with self.store.connection(immediate=True) as connection:
            existing = _row(
                connection.execute(
                    """
                    SELECT * FROM idempotency_records
                    WHERE actor_id = ? AND project_id = ? AND operation = ? AND idempotency_key = ?
                    """,
                    scope,
                ).fetchone()
            )
            timestamp = _now()
            if existing is not None:
                expired = (
                    datetime.fromisoformat(existing["expires_at"].replace("Z", "+00:00"))
                    <= timestamp
                )
                if not expired:
                    if existing["payload_digest"] != digest:
                        raise IdempotencyConflictError(
                            "Idempotency key was already used with a different request",
                            {"reason": "payload_mismatch", "operation": operation},
                        )
                    if existing["status"] == "IN_PROGRESS":
                        raise IdempotencyConflictError(
                            "A request with this idempotency key is still in progress",
                            {"reason": "in_progress", "operation": operation},
                        )
                    return json.loads(existing["response"])
                connection.execute(
                    "DELETE FROM idempotency_records WHERE record_id = ?",
                    (existing["record_id"],),
                )
            connection.execute(
                """
                INSERT INTO idempotency_records
                    (record_id, idempotency_key, actor_id, actor_role, project_id, operation,
                     payload_digest, status, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'IN_PROGRESS', ?, ?)
                """,
                (
                    _id(connection, "idem"),
                    idempotency_key,
                    agent_id,
                    actor.role.value,
                    project_id,
                    operation,
                    digest,
                    _iso(timestamp),
                    _iso(timestamp + timedelta(seconds=IDEMPOTENCY_RETENTION_SECONDS)),
                ),
            )
            return None

    def _complete_idempotency_record(self, scope: tuple[str, str, str, str], result: Any) -> None:
        with self.store.connection(immediate=True) as connection:
            connection.execute(
                """
                UPDATE idempotency_records
                SET status = 'COMPLETED', response = ?, completed_at = ?
                WHERE actor_id = ? AND project_id = ? AND operation = ? AND idempotency_key = ?
                """,
                (_serialize(result), _iso(), *scope),
            )

    def _release_idempotency_record(self, scope: tuple[str, str, str, str]) -> None:
        """Free a key whose command failed so the caller may retry a corrected request."""
        with self.store.connection(immediate=True) as connection:
            connection.execute(
                """
                DELETE FROM idempotency_records
                WHERE actor_id = ? AND project_id = ? AND operation = ? AND idempotency_key = ?
                  AND status = 'IN_PROGRESS'
                """,
                scope,
            )

    def _project_for_payload(self, actor: Actor, payload: dict[str, Any]) -> str:
        """Bind a record to its project when the payload identifies one; '' otherwise."""
        project_id = payload.get("project_id")
        if isinstance(project_id, str) and project_id:
            return project_id
        try:
            feature_id = payload.get("feature_id")
            if isinstance(feature_id, str) and feature_id:
                return self.project_id_for_feature(actor, feature_id)
            ticket_id = payload.get("ticket_id")
            if isinstance(ticket_id, str) and ticket_id:
                return self.project_id_for_ticket(actor, ticket_id)
        except DomainError:
            return ""
        return ""

    def purge_expired_idempotency_records(self, actor: Actor) -> dict[str, Any]:
        """Apply the retention policy; expired keys stop replaying and are removed."""
        require_role(actor, Role.ADMIN)
        with self.store.connection(immediate=True) as connection:
            cursor = connection.execute(
                "DELETE FROM idempotency_records WHERE expires_at <= ?", (_iso(),)
            )
            purged = cursor.rowcount
        return {"purged": max(purged, 0), "retention_seconds": IDEMPOTENCY_RETENTION_SECONDS}
