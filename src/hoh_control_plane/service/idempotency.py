from __future__ import annotations

import logging
import time

from hoh_control_plane.observability import correlation_scope, current_correlation_id
from hoh_control_plane.service._common import (
    Actor,
    Any,
    Callable,
    Connection,
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
# How many committed effects a reconstructed outcome names. A command writes a
# handful of events; the bound only keeps a pathological one from flooding the
# caller.
_MAX_COMMITTED_EFFECTS = 20


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
        separate transactions: ``run`` opens its own write transactions and the
        response is only known after they commit, so the completion cannot share
        the command's transaction without threading a connection through every
        service method. The window between them is closed by the audit trail
        instead. Every state change writes an ``audit_events`` row in its own
        transaction, stamped with the request's correlation id, and the
        reservation records that id. So whether a request's effect committed is a
        question the database can answer: did this actor write an event under
        this correlation id?

        * A reservation left IN_PROGRESS for longer than
          ``IDEMPOTENCY_STALE_SECONDS`` (a crash, or an unrecorded completion) is
          settled by the next retry from that answer. If the effect committed,
          the record is completed with a reconstructed outcome
          (``status: committed`` and the committed events' ids) and the command
          does not run again. If nothing committed, the key is reserved afresh
          and the command runs. Only a legacy row with no correlation id still
          answers ``idempotency_unknown``.
        * A command that raises after one of its transactions committed (a
          multi-transaction command whose later step met lock contention) keeps
          its key bound to the committed outcome. A ``ContentionError`` there is
          answered with that outcome instead of an error, so the transport never
          retries an effect that already happened; any other error is raised, and
          a retry with the same key replays the committed outcome.
        * A command that fails before anything committed releases its key rather
          than storing the failure, so a retry is re-executed and a failed
          transaction can never be replayed as a success.
        * Completion and release name the reservation's ``record_id``, never the
          key alone, so a late completion of a reclaimed request cannot overwrite
          or delete a newer reservation of the same key. Both retry
          ContentionError on their own, and a failure to record completion is
          logged, never raised: once ``run`` has returned its effect is committed
          and the caller always gets the result.

        Housekeeping a command performs under an internal identity (lease
        recovery, retirement finalization) is written under the same correlation
        id but another actor, so it never counts as this request's effect.
        """
        if current_correlation_id() is None:
            # The correlation id is how a retry learns whether this request's
            # effect committed, so a request without one gets its own.
            with correlation_scope():
                return self._run_idempotent(
                    actor, operation, idempotency_key, payload, run, project_id
                )
        return self._run_idempotent(actor, operation, idempotency_key, payload, run, project_id)

    def _run_idempotent(
        self,
        actor: Actor,
        operation: str,
        idempotency_key: str | None,
        payload: dict[str, Any],
        run: Callable[[], Any],
        project_id: str | None,
    ) -> Any:
        correlation_id = current_correlation_id()
        assert correlation_id is not None
        if not idempotency_key:
            try:
                return run()
            except ContentionError as error:
                return self._settle_failed_command(actor, operation, correlation_id, error)
        self._bounded_text(idempotency_key, "idempotency_key", _MAX_IDEMPOTENCY_KEY_CHARS)
        self._bounded_text(operation, "operation", 120)
        scope_project = (
            project_id if project_id is not None else self._project_for_payload(actor, payload)
        )
        digest = hashlib.sha256(_serialize(payload).encode("utf-8")).hexdigest()
        scope = (actor.agent_id, scope_project, operation, idempotency_key)

        record_id, replay = self._reserve_idempotency_record(
            actor, scope, digest, correlation_id
        )
        if record_id is None:
            return replay
        try:
            result = run()
        except BaseException as error:
            return self._settle_failed_command(
                actor, operation, correlation_id, error, record_id=record_id
            )
        self._complete_idempotency_record(record_id, result)
        return result

    def _settle_failed_command(
        self,
        actor: Actor,
        operation: str,
        correlation_id: str,
        error: BaseException,
        *,
        record_id: str | None = None,
    ) -> Any:
        """Bind a failed command's key to what actually committed, then answer.

        Returns the committed outcome for a ContentionError raised after the
        request's effect committed; re-raises ``error`` in every other case.
        """
        try:
            effects = self._committed_effects(correlation_id, actor.agent_id)
        except Exception:  # noqa: BLE001 - the original error is the answer
            # Whether anything committed cannot be read now. The reservation stays
            # IN_PROGRESS and a retry settles it from the audit trail once stale.
            _LOG.warning("Could not read the outcome of a failed command; key kept")
            raise error
        if not effects:
            if record_id is not None:
                self._release_idempotency_record(record_id)
            raise error
        outcome = self._committed_outcome(operation, correlation_id, effects)
        if record_id is not None:
            self._complete_idempotency_record(record_id, outcome)
        if isinstance(error, ContentionError):
            return outcome
        raise error

    def request_has_committed(self, actor: Actor, correlation_id: str | None) -> bool:
        """True when this actor's request wrote a committed state change.

        Used by the transport before it retries a contended call: a retry is only
        safe while nothing the call wrote has committed. An unreadable answer
        counts as committed, so the caller fails closed instead of retrying.
        """
        if not correlation_id:
            return False
        try:
            return bool(self._committed_effects(correlation_id, actor.agent_id))
        except Exception:  # noqa: BLE001 - see docstring
            return True

    def _committed_effects(
        self,
        correlation_id: str,
        actor_id: str,
        connection: Connection | None = None,
    ) -> list[dict[str, Any]]:
        """The audit events one request's actor committed, oldest first."""
        statement = """
            SELECT event_type, aggregate_type, aggregate_id, aggregate_version, payload
            FROM audit_events
            WHERE correlation_id = ? AND actor_id = ?
            ORDER BY sequence ASC LIMIT ?
        """
        parameters = (correlation_id, actor_id, _MAX_COMMITTED_EFFECTS + 1)
        if connection is not None:
            rows = connection.execute(statement, parameters).fetchall()
        else:
            with self.store.connection() as read:
                rows = read.execute(statement, parameters).fetchall()
        return [_row(row) or {} for row in rows]

    @staticmethod
    def _committed_outcome(
        operation: str, correlation_id: str, effects: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """What a caller is told when its effect committed but its response was lost.

        The events name every aggregate the request changed and the version it
        left, which is what the caller needs to re-read and continue (a claim's
        event carries its lease id).
        """
        return {
            "status": "committed",
            "operation": operation,
            "correlation_id": correlation_id,
            "response_recorded": False,
            "effects": [
                {
                    "event_type": effect["event_type"],
                    "aggregate_type": effect["aggregate_type"],
                    "aggregate_id": effect["aggregate_id"],
                    "aggregate_version": effect["aggregate_version"],
                    "payload": effect["payload"],
                }
                for effect in effects[:_MAX_COMMITTED_EFFECTS]
            ],
            "effects_truncated": len(effects) > _MAX_COMMITTED_EFFECTS,
            "next_action": (
                "This request's effect is already applied; only its response was not "
                "recorded. Do not repeat it: re-read the changed resources "
                "(ticket.get_context or work.discover) and continue from their state."
            ),
        }

    def _reserve_idempotency_record(
        self,
        actor: Actor,
        scope: tuple[str, str, str, str],
        digest: str,
        correlation_id: str,
    ) -> tuple[str | None, Any]:
        """Claim the key, or return the response a true replay must see.

        Returns ``(record_id, None)`` for a new reservation and
        ``(None, response)`` for a replay.
        """
        agent_id, project_id, operation, idempotency_key = scope
        outcome_unknown = False
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
                        return None, json.loads(existing["response"])
                    age = timestamp - _parse(existing["created_at"])
                    if age < timedelta(seconds=IDEMPOTENCY_STALE_SECONDS):
                        raise IdempotencyConflictError(
                            "A request with this idempotency key is still in progress",
                            {"reason": "in_progress", "operation": operation},
                        )
                    # Stale: the request that reserved the key never recorded its
                    # outcome. Its correlation id says whether its effect committed.
                    earlier = existing.get("correlation_id")
                    if not earlier:
                        outcome_unknown = True
                    else:
                        effects = self._committed_effects(earlier, agent_id, connection)
                        if effects:
                            outcome = self._committed_outcome(operation, earlier, effects)
                            connection.execute(
                                """
                                UPDATE idempotency_records
                                SET status = 'COMPLETED', response = ?, completed_at = ?
                                WHERE record_id = ? AND status = 'IN_PROGRESS'
                                """,
                                (_serialize(outcome), _iso(timestamp), existing["record_id"]),
                            )
                            return None, outcome
                        # Nothing committed: the key is free and the command runs.
                connection.execute(
                    "DELETE FROM idempotency_records WHERE record_id = ?",
                    (existing["record_id"],),
                )
            record_id: str | None = None
            if not outcome_unknown:
                record_id = _id(connection, "idem")
                connection.execute(
                    """
                    INSERT INTO idempotency_records
                        (record_id, idempotency_key, actor_id, actor_role, project_id, operation,
                         payload_digest, status, created_at, expires_at, correlation_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'IN_PROGRESS', ?, ?, ?)
                    """,
                    (
                        record_id,
                        idempotency_key,
                        agent_id,
                        actor.role.value,
                        project_id,
                        operation,
                        digest,
                        _iso(timestamp),
                        _iso(timestamp + timedelta(seconds=IDEMPOTENCY_RETENTION_SECONDS)),
                        correlation_id,
                    ),
                )
        if outcome_unknown:
            # Raised after the commit so the stale row stays removed. Only a row
            # written before reservations recorded their correlation id gets here.
            raise IdempotencyConflictError(
                "An earlier request with this idempotency key never recorded its outcome; "
                "its effect may or may not have been applied. Re-read the current state "
                "before retrying.",
                {"reason": "idempotency_unknown", "operation": operation},
            )
        return record_id, None

    def _finalize_idempotency_record(self, statement: str, parameters: tuple[Any, ...]) -> bool:
        """Run one completion/release write, retrying lock contention on its own.

        Returns False instead of raising when every attempt fails: the caller's
        command outcome must never be replaced by a bookkeeping error. The row then
        stays IN_PROGRESS and a later retry settles it from the audit trail.
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

    def _complete_idempotency_record(self, record_id: str, result: Any) -> None:
        """Record the response on this reservation only, and only while it is open.

        By record id, so a request that finishes after its stale key was reclaimed
        cannot overwrite the newer reservation that now holds the key.
        """
        self._finalize_idempotency_record(
            """
            UPDATE idempotency_records
            SET status = 'COMPLETED', response = ?, completed_at = ?
            WHERE record_id = ? AND status = 'IN_PROGRESS'
            """,
            (_serialize(result), _iso(), record_id),
        )

    def _release_idempotency_record(self, record_id: str) -> None:
        """Free a key whose command failed so the caller may retry a corrected request."""
        self._finalize_idempotency_record(
            """
            DELETE FROM idempotency_records
            WHERE record_id = ? AND status = 'IN_PROGRESS'
            """,
            (record_id,),
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
