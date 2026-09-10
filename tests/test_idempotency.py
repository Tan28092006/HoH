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
