from __future__ import annotations

import pytest

from hoh_control_plane.domain import (
    AuthorizationError,
    ConflictError,
    Role,
    TicketStatus,
)
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store import SQLiteStore


def test_sqlite_store_is_configured_for_local_multi_agent_writes(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "shared-control-plane.db")
    store.initialize()

    with store.connection() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

    assert journal_mode == "wal"
    assert busy_timeout >= 10_000


def test_developer_discovers_typed_context_refs_and_allowed_calls(
    service: ControlPlaneService,
    actors: dict[str, object],
    seeded: dict[str, object],
) -> None:
    result = service.discover_work(
        actors["developer"], project_id="project-1", dependency_ready=True
    )

    assert result["agent"]["role"] == "developer"
    assert result["features"][0]["context_ref"].startswith("ctx:feature:")
    ticket = result["features"][0]["tickets"][0]
    assert ticket["ticket_id"] == "DEV-142"
    assert ticket["context_ref"].startswith("ctx:ticket:DEV-142:")
    assert {call["tool"] for call in ticket["recommended_calls"]} == {
        "ticket.get_context",
        "work.claim",
    }


def test_only_one_developer_can_hold_a_ticket_lease(
    service: ControlPlaneService,
    actors: dict[str, object],
    seeded: dict[str, object],
) -> None:
    ticket = seeded["ticket"]
    claimed = service.claim_ticket(
        actors["developer"],
        ticket_id="DEV-142",
        expected_version=ticket["version"],
        lease_seconds=900,
    )

    assert claimed["ticket"]["status"] == TicketStatus.DEV_IN_PROGRESS.value
    assert claimed["lease"]["agent_id"] == "dev-01"
    with pytest.raises(ConflictError, match="already leased"):
        service.claim_ticket(
            actors["developer_2"],
            ticket_id="DEV-142",
            expected_version=claimed["ticket"]["version"],
            lease_seconds=900,
        )


def test_direct_claim_rejects_an_unfinished_dependency(
    service: ControlPlaneService,
    actors: dict[str, object],
    seeded: dict[str, object],
) -> None:
    prerequisite = service.create_ticket(
        actors["planner"],
        feature_id="payment-retry",
        ticket_id="DEV-141",
        title="Provide retry contract",
        objective="Define the retry contract before implementation work starts",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["The retry contract is documented and tested"],
        preservation_constraints=[],
        file_scope=["src/checkout/contracts/**"],
        ready=True,
    )
    dependent = service.create_ticket(
        actors["planner"],
        feature_id="payment-retry",
        ticket_id="DEV-143",
        title="Use the retry contract",
        objective="Implement a consumer of the retry contract",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["The consumer uses the retry contract"],
        preservation_constraints=[],
        file_scope=["src/checkout/consumer/**"],
        dependency_ids=[prerequisite["ticket_id"]],
        ready=True,
    )

    with pytest.raises(ConflictError, match="dependencies"):
        service.claim_ticket(
            actors["developer"],
            ticket_id=dependent["ticket_id"],
            expected_version=dependent["version"],
            lease_seconds=900,
        )


def test_developer_claim_rejects_an_overlapping_active_write_scope(
    service: ControlPlaneService,
    actors: dict[str, object],
    seeded: dict[str, object],
) -> None:
    overlapping = service.create_ticket(
        actors["planner"],
        feature_id="payment-retry",
        ticket_id="DEV-143",
        title="Update checkout retry serializer",
        objective="Make the serializer preserve retry metadata",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["Retry metadata survives serializer round trips"],
        preservation_constraints=[],
        file_scope=["src/checkout/serializers/**"],
        ready=True,
    )
    first_claim = service.claim_ticket(
        actors["developer"], "DEV-142", seeded["ticket"]["version"], 900
    )

    with pytest.raises(ConflictError, match="write scope"):
        service.claim_ticket(
            actors["developer_2"],
            ticket_id=overlapping["ticket_id"],
            expected_version=overlapping["version"],
            lease_seconds=900,
        )

    assert first_claim["ticket"]["status"] == TicketStatus.DEV_IN_PROGRESS.value


def test_developer_claim_allows_disjoint_active_write_scopes(
    service: ControlPlaneService,
    actors: dict[str, object],
    seeded: dict[str, object],
) -> None:
    disjoint = service.create_ticket(
        actors["planner"],
        feature_id="payment-retry",
        ticket_id="DEV-143",
        title="Update payment retry metrics",
        objective="Emit metrics for payment retries",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["Retry metrics are emitted"],
        preservation_constraints=[],
        file_scope=["src/payments/metrics/**"],
        ready=True,
    )
    service.claim_ticket(actors["developer"], "DEV-142", seeded["ticket"]["version"], 900)

    claim = service.claim_ticket(
        actors["developer_2"],
        ticket_id=disjoint["ticket_id"],
        expected_version=disjoint["version"],
        lease_seconds=900,
    )

    assert claim["ticket"]["status"] == TicketStatus.DEV_IN_PROGRESS.value


def test_stale_ticket_version_is_rejected(
    service: ControlPlaneService,
    actors: dict[str, object],
    seeded: dict[str, object],
) -> None:
    with pytest.raises(ConflictError, match="version"):
        service.claim_ticket(
            actors["developer"],
            ticket_id="DEV-142",
            expected_version=999,
            lease_seconds=900,
        )


def test_role_boundaries_are_enforced(
    service: ControlPlaneService,
    actors: dict[str, object],
    seeded: dict[str, object],
) -> None:
    with pytest.raises(AuthorizationError):
        service.claim_ticket(
            actors["qa"],
            ticket_id="DEV-142",
            expected_version=seeded["ticket"]["version"],
            lease_seconds=900,
        )
    with pytest.raises(AuthorizationError):
        service.create_feature(
            actors["developer"],
            project_id="project-1",
            feature_id="unauthorized",
            title="Not allowed",
            objective="Should fail",
        )


def test_failed_qa_becomes_rework_context_bound_to_candidate(
    service: ControlPlaneService,
    actors: dict[str, object],
    seeded: dict[str, object],
) -> None:
    claim = service.claim_ticket(
        actors["developer"],
        ticket_id="DEV-142",
        expected_version=seeded["ticket"]["version"],
        lease_seconds=900,
    )
    service.append_progress(
        actors["developer"],
        ticket_id="DEV-142",
        lease_id=claim["lease"]["lease_id"],
        message="Baseline reproduced; implementation completed",
    )
    submitted = service.submit_candidate(
        actors["developer"],
        ticket_id="DEV-142",
        lease_id=claim["lease"]["lease_id"],
        expected_version=claim["ticket"]["version"],
        commit_sha="a" * 40,
        base_commit="b" * 40,
        branch_ref="agent/dev-01/DEV-142",
        developer_claims=["Retry no longer duplicates an order"],
        context_snapshot_id=service.get_ticket_context(actors["developer"], "DEV-142")["context_snapshot_id"],
    )
    qa_claim = service.claim_qa(
        actors["qa"],
        ticket_id="DEV-142",
        expected_version=submitted["ticket"]["version"],
        lease_seconds=900,
    )
    verdict = service.submit_qa_verdict(
        actors["qa"],
        ticket_id="DEV-142",
        lease_id=qa_claim["lease"]["lease_id"],
        candidate_id=submitted["candidate"]["candidate_id"],
        expected_version=qa_claim["ticket"]["version"],
        verdict="failed",
        findings=[
            {
                "criterion": "A successful retry creates exactly one order",
                "status": "failed",
                "severity": "high",
                "observation": "Two orders were created on the second callback",
                "evidence": [
                    {
                        "type": "test_result",
                        "observation": "retry_duplicate failed 3/3 runs",
                        "storage_ref": "evidence://qa/retry-duplicate.json",
                    }
                ],
            }
        ],
        context_snapshot_id=service.get_ticket_context(actors["qa"], "DEV-142")["context_snapshot_id"],
    )

    assert verdict["ticket"]["status"] == TicketStatus.REWORK.value
    context = service.get_ticket_context(actors["developer_2"], "DEV-142")
    assert context["candidate"]["commit_sha"] == "a" * 40
    assert context["known_failures"][0]["evidence"][0]["storage_ref"].startswith(
        "evidence://"
    )
    assert context["context_snapshot_id"].startswith("ctxsnap_")


def test_successful_qa_verifies_ticket(
    service: ControlPlaneService,
    actors: dict[str, object],
    seeded: dict[str, object],
) -> None:
    claim = service.claim_ticket(
        actors["developer"], "DEV-142", seeded["ticket"]["version"], 900
    )
    submitted = service.submit_candidate(
        actors["developer"],
        "DEV-142",
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
        "c" * 40,
        "d" * 40,
        "agent/dev-01/DEV-142",
        ["Both acceptance criteria pass locally"],
        context_snapshot_id=service.get_ticket_context(actors["developer"], "DEV-142")["context_snapshot_id"],
    )
    qa_claim = service.claim_qa(
        actors["qa"], "DEV-142", submitted["ticket"]["version"], 900
    )
    verdict = service.submit_qa_verdict(
        actors["qa"],
        "DEV-142",
        qa_claim["lease"]["lease_id"],
        submitted["candidate"]["candidate_id"],
        qa_claim["ticket"]["version"],
        "passed",
        [
            {
                "criterion": criterion,
                "status": "verified",
                "severity": "none",
                "observation": "Observed in an isolated test run",
                "evidence": [
                    {
                        "type": "test_result",
                        "observation": "passed",
                        "storage_ref": f"evidence://qa/{index}.json",
                    }
                ],
            }
            for index, criterion in enumerate(
                seeded["ticket"]["acceptance_criteria"], start=1
            )
        ],
        context_snapshot_id=service.get_ticket_context(actors["qa"], "DEV-142")["context_snapshot_id"],
    )

    assert verdict["ticket"]["status"] == TicketStatus.VERIFIED.value


def test_heartbeat_feature_context_and_event_cursor(
    service: ControlPlaneService,
    actors: dict[str, object],
    seeded: dict[str, object],
) -> None:
    claim = service.claim_ticket(
        actors["developer"], "DEV-142", seeded["ticket"]["version"], 120
    )
    renewed = service.heartbeat(
        actors["developer"], claim["lease"]["lease_id"], lease_seconds=300
    )
    feature_context = service.get_feature_context(
        actors["developer"], "ctx:feature:payment-retry:v1"
    )
    events = service.list_events(actors["developer"], "project-1")
    no_new_events = service.list_events(
        actors["developer"], "project-1", after=events["next_cursor"]
    )

    assert renewed["expires_at"] > renewed["heartbeat_at"]
    assert feature_context["feature"]["feature_id"] == "payment-retry"
    assert feature_context["open_ticket_count"] == 1
    assert events["next_cursor"] > 0
    assert no_new_events["events"] == []


def test_board_routes_a_blocked_ticket_back_to_planner_without_an_owner(
    service: ControlPlaneService,
    actors: dict[str, object],
    seeded: dict[str, object],
) -> None:
    claim = service.claim_ticket(
        actors["developer"], "DEV-142", seeded["ticket"]["version"], 900
    )
    service.report_blocker(
        actors["developer"],
        "DEV-142",
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
        "The upstream API contract needs a Planner decision.",
    )

    ticket = service.board("project-1")["columns"][TicketStatus.BLOCKED.value][0]

    assert ticket["owner"] == {
        "state": "blocked",
        "agent_id": None,
        "role": None,
        "lease_expires_at": None,
        "awaiting_role": Role.PLANNER.value,
    }
