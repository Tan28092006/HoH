from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hoh_control_plane.api import create_app
from hoh_control_plane.domain import (
    Actor,
    AuthorizationError,
    ConflictError,
    IdempotencyConflictError,
    Role,
    ValidationError,
)
from hoh_control_plane.mcp_server import create_mcp_server


ADMIN = Actor("admin-01", Role.ADMIN, frozenset({"*"}))


def _count(service, table: str) -> int:
    with service.store.connection() as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _claim_payload(ticket, lease_seconds: int = 900) -> dict[str, object]:
    return {
        "ticket_id": ticket["ticket_id"],
        "expected_version": ticket["version"],
        "lease_seconds": lease_seconds,
    }


def test_replayed_claim_returns_the_original_lease_after_a_lost_acknowledgment(
    service, actors, seeded
) -> None:
    """The first claim succeeded but its acknowledgment was lost; the retry must not
    fail on the now-stale expected_version, and must not create a second lease."""
    ticket = seeded["ticket"]
    payload = _claim_payload(ticket)

    first = service.run_idempotent(
        actors["developer"],
        operation="work.claim",
        idempotency_key="claim-key-1",
        payload=payload,
        run=lambda: service.claim_ticket(actors["developer"], **payload),
    )
    replay = service.run_idempotent(
        actors["developer"],
        operation="work.claim",
        idempotency_key="claim-key-1",
        payload=payload,
        run=lambda: service.claim_ticket(actors["developer"], **payload),
    )

    assert replay["lease"]["lease_id"] == first["lease"]["lease_id"]
    assert replay["ticket"]["version"] == first["ticket"]["version"]
    assert _count(service, "leases") == 1


def test_replay_does_not_duplicate_domain_events(service, actors, seeded) -> None:
    ticket = seeded["ticket"]
    payload = _claim_payload(ticket)

    def claim():
        return service.run_idempotent(
            actors["developer"],
            operation="work.claim",
            idempotency_key="claim-key-events",
            payload=payload,
            run=lambda: service.claim_ticket(actors["developer"], **payload),
        )

    claim()
    events_after_first = service.list_events(actors["planner"], "project-1")["events"]
    claim()
    events_after_replay = service.list_events(actors["planner"], "project-1")["events"]

    claimed = [event for event in events_after_replay if event["event_type"] == "ticket.claimed"]
    assert len(events_after_replay) == len(events_after_first)
    assert len(claimed) == 1


def test_key_reuse_with_a_different_payload_is_rejected(service, actors, seeded) -> None:
    ticket = seeded["ticket"]
    payload = _claim_payload(ticket)
    service.run_idempotent(
        actors["developer"],
        operation="work.claim",
        idempotency_key="claim-key-2",
        payload=payload,
        run=lambda: service.claim_ticket(actors["developer"], **payload),
    )
    different = _claim_payload(ticket, lease_seconds=1800)

    with pytest.raises(IdempotencyConflictError):
        service.run_idempotent(
            actors["developer"],
            operation="work.claim",
            idempotency_key="claim-key-2",
            payload=different,
            run=lambda: pytest.fail("A reused key with a different payload must not execute"),
        )

    assert _count(service, "leases") == 1


def test_the_same_key_is_scoped_to_one_actor_and_operation(service, actors, seeded) -> None:
    """A key belonging to another agent or another operation must never collide."""
    ticket = seeded["ticket"]
    payload = _claim_payload(ticket)
    service.run_idempotent(
        actors["developer"],
        operation="work.claim",
        idempotency_key="shared-key",
        payload=payload,
        run=lambda: service.claim_ticket(actors["developer"], **payload),
    )

    other_actor_ran = service.run_idempotent(
        actors["developer_2"],
        operation="work.claim",
        idempotency_key="shared-key",
        payload={"probe": "other-actor"},
        run=lambda: {"executed": "developer_2"},
    )
    # Same actor, same project binding, same key: only the operation differs.
    other_operation_ran = service.run_idempotent(
        actors["developer"],
        operation="dev.append_progress",
        idempotency_key="shared-key",
        payload=payload,
        run=lambda: {"executed": "other-operation"},
    )

    assert other_actor_ran == {"executed": "developer_2"}
    assert other_operation_ran == {"executed": "other-operation"}


def test_a_failed_command_releases_its_key_for_a_corrected_retry(
    service, actors, seeded
) -> None:
    """A failed transaction is never replayed as a success, and the caller may retry
    the same key once the stale input is corrected."""
    ticket = seeded["ticket"]
    stale = _claim_payload(ticket)
    stale["expected_version"] = ticket["version"] + 5

    with pytest.raises(ConflictError):
        service.run_idempotent(
            actors["developer"],
            operation="work.claim",
            idempotency_key="claim-key-3",
            payload=stale,
            run=lambda: service.claim_ticket(actors["developer"], **stale),
        )

    corrected = _claim_payload(ticket)
    result = service.run_idempotent(
        actors["developer"],
        operation="work.claim",
        idempotency_key="claim-key-3",
        payload=corrected,
        run=lambda: service.claim_ticket(actors["developer"], **corrected),
    )

    assert result["ticket"]["status"] == "DEV_IN_PROGRESS"
    assert _count(service, "leases") == 1


def test_a_duplicate_start_while_the_first_is_in_flight_is_rejected(
    service, actors, seeded
) -> None:
    """Two workers starting the same keyed command must not both execute it."""
    ticket = seeded["ticket"]
    payload = _claim_payload(ticket)
    duplicate_outcome: list[str] = []

    def first_attempt():
        try:
            service.run_idempotent(
                actors["developer"],
                operation="work.claim",
                idempotency_key="claim-key-4",
                payload=payload,
                run=lambda: pytest.fail("The duplicate start must not execute the command"),
            )
        except IdempotencyConflictError:
            duplicate_outcome.append("rejected")
        return service.claim_ticket(actors["developer"], **payload)

    result = service.run_idempotent(
        actors["developer"],
        operation="work.claim",
        idempotency_key="claim-key-4",
        payload=payload,
        run=first_attempt,
    )

    assert duplicate_outcome == ["rejected"]
    assert result["ticket"]["status"] == "DEV_IN_PROGRESS"
    assert _count(service, "leases") == 1


def test_a_command_without_a_key_records_nothing_and_still_runs(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]
    payload = _claim_payload(ticket)

    result = service.run_idempotent(
        actors["developer"],
        operation="work.claim",
        idempotency_key=None,
        payload=payload,
        run=lambda: service.claim_ticket(actors["developer"], **payload),
    )

    assert result["ticket"]["status"] == "DEV_IN_PROGRESS"
    assert _count(service, "idempotency_records") == 0


def test_a_blank_or_oversized_key_is_rejected(service, actors, seeded) -> None:
    payload = _claim_payload(seeded["ticket"])

    for bad_key in ("   ", "k" * 201):
        with pytest.raises(ValidationError):
            service.run_idempotent(
                actors["developer"],
                operation="work.claim",
                idempotency_key=bad_key,
                payload=payload,
                run=lambda: pytest.fail("An invalid key must not execute the command"),
            )


def test_records_bind_actor_project_operation_and_payload_digest(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]
    payload = _claim_payload(ticket)
    service.run_idempotent(
        actors["developer"],
        operation="work.claim",
        idempotency_key="claim-key-5",
        payload=payload,
        run=lambda: service.claim_ticket(actors["developer"], **payload),
    )

    with service.store.connection() as connection:
        record = dict(
            connection.execute(
                "SELECT * FROM idempotency_records WHERE idempotency_key = ?", ("claim-key-5",)
            ).fetchone()
        )

    assert record["actor_id"] == actors["developer"].agent_id
    assert record["project_id"] == "project-1"
    assert record["operation"] == "work.claim"
    assert record["status"] == "COMPLETED"
    assert len(record["payload_digest"]) == 64
    assert record["expires_at"] > record["created_at"]
    # The binding is a digest: no raw lease id, branch ref or claim text is retained.
    assert ticket["ticket_id"] not in record["payload_digest"]


def test_expired_records_are_purged_and_stop_replaying(service, actors, seeded) -> None:
    ticket = seeded["ticket"]
    payload = _claim_payload(ticket)
    first = service.run_idempotent(
        actors["developer"],
        operation="work.claim",
        idempotency_key="claim-key-6",
        payload=payload,
        run=lambda: service.claim_ticket(actors["developer"], **payload),
    )
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE idempotency_records SET expires_at = ? WHERE idempotency_key = ?",
            ("2000-01-01T00:00:00Z", "claim-key-6"),
        )

    purged = service.purge_expired_idempotency_records(ADMIN)

    assert first["lease"]["lease_id"]
    assert purged["purged"] == 1
    assert _count(service, "idempotency_records") == 0


def test_purging_requires_an_administrator(service, actors) -> None:
    with pytest.raises(AuthorizationError):
        service.purge_expired_idempotency_records(actors["developer"])


@pytest.mark.anyio
async def test_mcp_claim_replays_one_lease_for_a_repeated_key(
    service, actors, seeded
) -> None:
    server = create_mcp_server(service, actors["developer"])
    arguments = {
        "ticket_id": seeded["ticket"]["ticket_id"],
        "expected_version": seeded["ticket"]["version"],
        "idempotency_key": "mcp-claim-1",
    }

    _, first = await server.call_tool("work.claim", arguments)
    _, replay = await server.call_tool("work.claim", arguments)

    assert replay["lease"]["lease_id"] == first["lease"]["lease_id"]
    assert _count(service, "leases") == 1


def test_rest_replays_one_project_for_a_repeated_idempotency_key(tmp_path: Path) -> None:
    app = create_app(database_path=tmp_path / "api.db", local_mode=True)
    headers = {
        "X-HOH-Agent-Id": "planner-01",
        "X-HOH-Role": "planner",
        "Idempotency-Key": "rest-project-1",
    }
    payload = {
        "project_id": "p1",
        "name": "Project One",
        "description": "Test",
        "default_branch": "main",
    }

    with TestClient(app) as client:
        first = client.post("/api/v1/projects", json=payload, headers=headers)
        replay = client.post("/api/v1/projects", json=payload, headers=headers)
        conflicting = client.post(
            "/api/v1/projects",
            json={**payload, "name": "Renamed"},
            headers=headers,
        )
        without_key = client.post(
            "/api/v1/projects",
            json=payload,
            headers={key: value for key, value in headers.items() if key != "Idempotency-Key"},
        )

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["data"] == first.json()["data"]
    assert conflicting.status_code == 409
    assert conflicting.json()["error"]["code"] == "idempotency_conflict"
    # Without a key the duplicate reaches the domain and is rejected as a real conflict.
    assert without_key.status_code == 409
    assert without_key.json()["error"]["code"] == "state_conflict"


class _SimulatedCrash(BaseException):
    """Stands in for the process dying between the command and its completion."""


def _crash(*_args, **_kwargs):
    raise _SimulatedCrash()


def _age_reservation(service, key: str) -> None:
    """Make a reservation older than the stale age, as a dead request's would be."""
    from hoh_control_plane.service import idempotency

    assert idempotency.IDEMPOTENCY_STALE_SECONDS < 10**9
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE idempotency_records SET created_at = ? WHERE idempotency_key = ?",
            ("2000-01-01T00:00:00Z", key),
        )


def _record(service, key: str) -> dict:
    with service.store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM idempotency_records WHERE idempotency_key = ?", (key,)
        ).fetchone()
    return dict(row) if row is not None else {}


def _must_not_run():
    pytest.fail("A retry must not re-run a command whose effect already committed")


def test_a_crash_after_the_commit_is_answered_as_committed_without_a_second_effect(
    service, actors, seeded, monkeypatch
) -> None:
    """The claim committed, then the process died before recording completion."""
    ticket = seeded["ticket"]
    payload = _claim_payload(ticket)
    developer = actors["developer"]

    monkeypatch.setattr(type(service), "_complete_idempotency_record", _crash)
    with pytest.raises(_SimulatedCrash):
        service.run_idempotent(
            developer,
            operation="work.claim",
            idempotency_key="crash-key",
            payload=payload,
            run=lambda: service.claim_ticket(developer, **payload),
        )
    monkeypatch.undo()
    assert _count(service, "leases") == 1
    reservation = _record(service, "crash-key")
    assert reservation["status"] == "IN_PROGRESS"
    assert reservation["correlation_id"], "the reservation names its request"

    # A fresh reservation is still treated as in flight.
    with pytest.raises(IdempotencyConflictError) as fresh:
        service.run_idempotent(
            developer, operation="work.claim", idempotency_key="crash-key",
            payload=payload, run=_must_not_run,
        )
    assert fresh.value.details["reason"] == "in_progress"

    _age_reservation(service, "crash-key")
    outcome = service.run_idempotent(
        developer, operation="work.claim", idempotency_key="crash-key",
        payload=payload, run=_must_not_run,
    )

    with service.store.connection() as connection:
        lease_id = connection.execute("SELECT lease_id FROM leases").fetchone()[0]
    assert outcome["status"] == "committed"
    assert outcome["correlation_id"] == reservation["correlation_id"]
    claimed = [effect for effect in outcome["effects"] if effect["event_type"] == "ticket.claimed"]
    assert claimed and claimed[0]["aggregate_id"] == ticket["ticket_id"]
    assert claimed[0]["payload"]["lease_id"] == lease_id, "enough to continue with the lease"
    assert _count(service, "leases") == 1
    settled = _record(service, "crash-key")
    assert settled["record_id"] == reservation["record_id"]
    assert settled["status"] == "COMPLETED"
    # From now on the key replays the same answer.
    assert service.run_idempotent(
        developer, operation="work.claim", idempotency_key="crash-key",
        payload=payload, run=_must_not_run,
    ) == outcome


def test_a_crash_before_the_commit_runs_the_command_again_after_the_stale_age(
    service, actors, seeded, monkeypatch
) -> None:
    """The process died before the command wrote anything, and before releasing."""
    ticket = seeded["ticket"]
    payload = _claim_payload(ticket)
    developer = actors["developer"]

    monkeypatch.setattr(type(service), "_release_idempotency_record", _crash)
    with pytest.raises(_SimulatedCrash):
        service.run_idempotent(
            developer, operation="work.claim", idempotency_key="early-crash",
            payload=payload, run=_crash,
        )
    monkeypatch.undo()
    stale = _record(service, "early-crash")
    assert stale["status"] == "IN_PROGRESS"
    assert _count(service, "leases") == 0

    _age_reservation(service, "early-crash")
    result = service.run_idempotent(
        developer, operation="work.claim", idempotency_key="early-crash",
        payload=payload, run=lambda: service.claim_ticket(developer, **payload),
    )

    assert result["ticket"]["status"] == "DEV_IN_PROGRESS"
    assert _count(service, "leases") == 1
    settled = _record(service, "early-crash")
    assert settled["record_id"] != stale["record_id"], "the stale reservation was replaced"
    assert settled["status"] == "COMPLETED"


def test_a_late_completion_never_overwrites_a_newer_reservation(
    service, actors, seeded, monkeypatch
) -> None:
    """ABA: the first request was only slow, and finishes while a retry holds the key."""
    ticket = seeded["ticket"]
    payload = _claim_payload(ticket)
    developer = actors["developer"]

    monkeypatch.setattr(type(service), "_release_idempotency_record", _crash)
    with pytest.raises(_SimulatedCrash):
        service.run_idempotent(
            developer, operation="work.claim", idempotency_key="aba-key",
            payload=payload, run=_crash,
        )
    monkeypatch.undo()
    first = _record(service, "aba-key")["record_id"]
    _age_reservation(service, "aba-key")

    def retry_while_the_first_finishes_late():
        service._complete_idempotency_record(first, {"late": True})
        service._release_idempotency_record(first)
        newer = _record(service, "aba-key")
        assert newer["record_id"] != first
        assert newer["status"] == "IN_PROGRESS", "the late finish left the newer key alone"
        return service.claim_ticket(developer, **payload)

    result = service.run_idempotent(
        developer, operation="work.claim", idempotency_key="aba-key",
        payload=payload, run=retry_while_the_first_finishes_late,
    )
    replay = service.run_idempotent(
        developer, operation="work.claim", idempotency_key="aba-key",
        payload=payload, run=_must_not_run,
    )

    assert replay == result
    assert "late" not in replay
    assert _count(service, "leases") == 1


def test_a_stale_reservation_without_a_correlation_id_is_still_unknown(
    service, actors, seeded, monkeypatch
) -> None:
    """A row written before reservations recorded their request cannot be settled."""
    ticket = seeded["ticket"]
    payload = _claim_payload(ticket)
    developer = actors["developer"]

    monkeypatch.setattr(type(service), "_complete_idempotency_record", _crash)
    with pytest.raises(_SimulatedCrash):
        service.run_idempotent(
            developer, operation="work.claim", idempotency_key="legacy-key",
            payload=payload, run=lambda: service.claim_ticket(developer, **payload),
        )
    monkeypatch.undo()
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE idempotency_records SET correlation_id = NULL WHERE idempotency_key = ?",
            ("legacy-key",),
        )
    _age_reservation(service, "legacy-key")

    with pytest.raises(IdempotencyConflictError) as unknown:
        service.run_idempotent(
            developer, operation="work.claim", idempotency_key="legacy-key",
            payload=payload, run=_must_not_run,
        )
    assert unknown.value.details["reason"] == "idempotency_unknown"
    assert _count(service, "idempotency_records") == 0
    assert _count(service, "leases") == 1


def test_housekeeping_under_another_identity_is_not_the_requests_effect(
    service, actors, seeded, monkeypatch
) -> None:
    """Lease recovery writes under the caller's correlation id but its own actor."""
    from hoh_control_plane.store import ContentionError

    developer = actors["developer"]
    recovery = Actor("lease-recovery", Role.ADMIN, frozenset({"*"}))

    def sweep_then_contend():
        with service.store.connection(immediate=True) as connection:
            service._event(
                connection, recovery, "project-1", "lease.expired_requeued", "ticket",
                "DEV-142", 1, {},
            )
        raise ContentionError("busy")

    with pytest.raises(ContentionError):
        service.run_idempotent(
            developer, operation="work.inbox", idempotency_key="inbox-key",
            payload={"project_id": "project-1"}, run=sweep_then_contend,
        )
    assert _count(service, "idempotency_records") == 0, "released: nothing of its own committed"


@pytest.mark.anyio
@pytest.mark.parametrize("idempotency_key", [None, "update-key"])
async def test_contention_after_a_committed_write_is_not_retried_by_the_transport(
    service, actors, seeded, monkeypatch, idempotency_key
) -> None:
    """A command whose effect committed and whose later transaction met contention.

    Before, the MCP layer retried the whole call because ContentionError was
    assumed to mean nothing had happened; the retry then ran the effect again or
    reported a conflict for work the caller had already done.
    """
    from hoh_control_plane.mcp import invoke as invoke_module
    from hoh_control_plane.store import ContentionError

    monkeypatch.setattr(invoke_module, "_CONTENTION_RETRY_BASE_SECONDS", 0)
    ticket = seeded["ticket"]
    real_update = service.update_ticket_fields
    calls: list[int] = []

    def update_then_contend(*args, **kwargs):
        calls.append(1)
        real_update(*args, **kwargs)
        raise ContentionError("busy")  # a later transaction of the same command

    monkeypatch.setattr(service, "update_ticket_fields", update_then_contend)
    server = create_mcp_server(service, actors["planner"])
    arguments = {
        "ticket_id": ticket["ticket_id"],
        "expected_version": ticket["version"],
        "reason": "Sharpen the title",
        "title": "Repair checkout retry behaviour",
    }
    if idempotency_key:
        arguments["idempotency_key"] = idempotency_key

    _, outcome = await server.call_tool("planner.update_ticket", arguments)

    assert outcome["status"] == "committed"
    assert any(effect["event_type"] == "ticket.fields_updated" for effect in outcome["effects"])
    assert len(calls) == 1, "the committed effect was not run a second time"
    with service.store.connection() as connection:
        current = service._ticket(connection, ticket["ticket_id"])
    assert current["version"] == ticket["version"] + 1
    assert current["title"] == "Repair checkout retry behaviour"
    if idempotency_key:
        _, replay = await server.call_tool("planner.update_ticket", arguments)
        assert replay == outcome
        assert len(calls) == 1


@pytest.mark.anyio
async def test_contention_before_anything_committed_is_still_retried(
    service, actors, seeded, monkeypatch
) -> None:
    from hoh_control_plane.mcp import invoke as invoke_module
    from hoh_control_plane.store import ContentionError

    monkeypatch.setattr(invoke_module, "_CONTENTION_RETRY_BASE_SECONDS", 0)
    ticket = seeded["ticket"]
    real_update = service.update_ticket_fields
    calls: list[int] = []

    def contend_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ContentionError("busy")
        return real_update(*args, **kwargs)

    monkeypatch.setattr(service, "update_ticket_fields", contend_once)
    server = create_mcp_server(service, actors["planner"])

    _, result = await server.call_tool("planner.update_ticket", {
        "ticket_id": ticket["ticket_id"],
        "expected_version": ticket["version"],
        "reason": "Sharpen the title",
        "title": "Repair checkout retry behaviour",
        "idempotency_key": "retried-key",
    })

    assert len(calls) == 2
    assert result["ticket"]["version"] == ticket["version"] + 1


def test_contention_on_completion_never_hides_a_committed_effect(
    service, actors, seeded, monkeypatch
) -> None:
    from hoh_control_plane.service import idempotency
    from hoh_control_plane.store import ContentionError

    monkeypatch.setattr(idempotency, "_FINALIZE_BASE_DELAY_SECONDS", 0)
    ticket = seeded["ticket"]
    payload = _claim_payload(ticket)
    developer = actors["developer"]
    real_connection = service.store.connection
    calls = {"reserve": 0, "finalize": 0}

    def flaky_connection(*args, **kwargs):
        calls["reserve"] += 1
        if calls["reserve"] in (3, 4):  # reserve=1, claim=2, then completion contends twice
            calls["finalize"] += 1
            raise ContentionError("busy")
        return real_connection(*args, **kwargs)

    commands = []

    def command():
        result = service.claim_ticket(developer, **payload)
        commands.append(result)
        monkeypatch.setattr(service.store, "connection", flaky_connection)
        calls["reserve"] = 2
        return result

    result = service.run_idempotent(
        developer, operation="work.claim", idempotency_key="contended-key",
        payload=payload, run=command,
    )
    monkeypatch.setattr(service.store, "connection", real_connection)
    assert calls["finalize"] == 2
    assert result["ticket"]["status"] == "DEV_IN_PROGRESS"
    replay = service.run_idempotent(
        developer, operation="work.claim", idempotency_key="contended-key",
        payload=payload, run=lambda: pytest.fail("The completed claim must replay"),
    )
    assert replay == result
    assert len(commands) == 1

    # Contention that never clears still returns the committed result.
    monkeypatch.setattr(
        service.store, "connection",
        lambda *a, **k: (_ for _ in ()).throw(ContentionError("busy")),
    )
    assert service._complete_idempotency_record("idem_9999", {"ok": True}) is None
