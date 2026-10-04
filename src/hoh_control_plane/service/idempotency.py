from __future__ import annotations

import logging
import time

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
from hoh_control_plane.store import ContentionError

_LOG = logging.getLogger(__name__)

# An IN_PROGRESS reservation older than this belongs to a request whose process
# died between running the command and recording completion (or release). No
# consequential command runs this long inside one request, so the age is a safe
# signal that nobody will ever finish the row.
IDEMPOTENCY_STALE_SECONDS = 600
_FINALIZE_ATTEMPTS = 5
_FINALIZE_BASE_DELAY_SECONDS = 0.05


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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

        Design (crash window). The reservation, the command and the completion are
        three transactions: ``run`` opens its own write transactions and the
        response is only known after they commit, so the completion cannot share
        the command's transaction without threading a connection through every
        service method. Instead the window between them is made safe:

        * Completion and release each retry ContentionError on their own, and a
          failure to record completion is logged, never raised: once ``run`` has
          returned, its effect is committed and the caller always gets the result.
          (Raising there would make the MCP contention retry report an error, or
          worse re-run, for a command that already succeeded.)
        * A reservation left IN_PROGRESS for longer than
          ``IDEMPOTENCY_STALE_SECONDS`` (a crash, or an unrecorded completion) is
          reclaimed by the next retry with ``idempotency_unknown``: the row is
          removed and the caller is told to re-read state, because whether the
          effect committed cannot be known here. A retry after that re-read runs
          the command again; the domain's own guards (versions, leases, context
          snapshots) reject a duplicate effect.

        A command that fails releases its key rather than storing the failure, so
        a retry is re-executed and a failed transaction can never be replayed as a
        success.
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
        reclaimed_stale = False
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
                expired = _parse(existing["expires_at"]) <= timestamp
                if not expired:
                    if existing["payload_digest"] != digest:
                        raise IdempotencyConflictError(
                            "Idempotency key was already used with a different request",
                            {"reason": "payload_mismatch", "operation": operation},
                        )
                    if existing["status"] != "IN_PROGRESS":
                        return json.loads(existing["response"])
                    age = timestamp - _parse(existing["created_at"])
                    if age < timedelta(seconds=IDEMPOTENCY_STALE_SECONDS):
                        raise IdempotencyConflictError(
                            "A request with this idempotency key is still in progress",
                            {"reason": "in_progress", "operation": operation},
                        )
                    reclaimed_stale = True
                connection.execute(
                    "DELETE FROM idempotency_records WHERE record_id = ?",
                    (existing["record_id"],),
                )
            if not reclaimed_stale:
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
        if reclaimed_stale:
            # Raised after the commit so the stale row stays removed.
            raise IdempotencyConflictError(
                "An earlier request with this idempotency key never recorded its outcome; "
                "its effect may or may not have been applied. Re-read the current state "
                "before retrying.",
                {"reason": "idempotency_unknown", "operation": operation},
            )
        return None

    def _finalize_idempotency_record(self, statement: str, parameters: tuple[Any, ...]) -> bool:
        """Run one completion/release write, retrying lock contention on its own.

        Returns False instead of raising when every attempt fails: the caller's
        command outcome must never be replaced by a bookkeeping error. The row then
        stays IN_PROGRESS and is reclaimed as stale by a later retry.
        """
        delay = _FINALIZE_BASE_DELAY_SECONDS
        for attempt in range(_FINALIZE_ATTEMPTS):
            try:
                with self.store.connection(immediate=True) as connection:
                    connection.execute(statement, parameters)
                return True
            except ContentionError:
                if attempt == _FINALIZE_ATTEMPTS - 1:
                    break
                time.sleep(delay)
                delay *= 2
            except Exception:  # noqa: BLE001 - see docstring
                _LOG.exception("Could not finalize an idempotency record")
                return False
        _LOG.warning("Idempotency record left IN_PROGRESS after repeated lock contention")
        return False

    def _complete_idempotency_record(self, scope: tuple[str, str, str, str], result: Any) -> None:
        self._finalize_idempotency_record(
            """
            UPDATE idempotency_records
            SET status = 'COMPLETED', response = ?, completed_at = ?
            WHERE actor_id = ? AND project_id = ? AND operation = ? AND idempotency_key = ?
            """,
            (_serialize(result), _iso(), *scope),
        )

    def _release_idempotency_record(self, scope: tuple[str, str, str, str]) -> None:
        """Free a key whose command failed so the caller may retry a corrected request."""
        self._finalize_idempotency_record(
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
