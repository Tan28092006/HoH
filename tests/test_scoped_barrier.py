"""A result that leaves main where it was freezes only the work it judged (wave 6D).

QA rejection and an integration gate regression used to close the whole project
until Planner had reconciled every open ticket. Neither moves main, so no other
ticket's plan can be wrong because of it: only the judged ticket and the tickets
that depend on it, transitively, wait for Planner. Every other ticket keeps its
offers and claims. Observed commits and merges keep the project-wide barrier.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from barrier_helpers import keep_open_tickets

from hoh_control_plane.domain import ConflictError, Role
from hoh_control_plane.service._common import ExecutionGate


def _ticket(service, ticket_id: str) -> dict:
    with service.store.connection() as connection:
        return service._ticket(connection, ticket_id)


def _open(service, actors, ticket_id: str, scope: list[str], depends_on=None) -> dict:
    return service.create_ticket(
        actors["planner"], feature_id="payment-retry", ticket_id=ticket_id,
        title=f"Work {ticket_id}", objective="Other work on the board",
        required_role=Role.DEVELOPER, acceptance_criteria=["It works"],
        preservation_constraints=["Checkout stays valid"], file_scope=scope,
        dependency_ids=depends_on or [], ready=True,
    )


def _board(service, actors) -> None:
    """DEV-B is unrelated to DEV-142; DEV-C depends on it, and DEV-D on DEV-C."""
    _open(service, actors, "DEV-B", ["src/reporting/**"])
    _open(service, actors, "DEV-C", ["src/receipts/**"], ["DEV-142"])
    _open(service, actors, "DEV-D", ["src/emails/**"], ["DEV-C"])


def _reject(service, actors, ticket_id: str = "DEV-142", attempt: int = 1,
            developer: str = "developer") -> dict:
    developer, qa = actors[developer], actors["qa"]
    claim = service.claim_ticket(developer, ticket_id, _ticket(service, ticket_id)["version"], 900)
    submitted = service.submit_candidate(
        developer, ticket_id, claim["lease"]["lease_id"], claim["ticket"]["version"],
        f"{attempt:x}" * 40, "b" * 40, f"agent/{developer.agent_id}/{ticket_id}", ["Ready."],
        context_snapshot_id=service.get_ticket_context(developer, ticket_id)["context_snapshot_id"],
    )
    qa_claim = service.claim_qa(qa, ticket_id, submitted["ticket"]["version"], 900)
    criterion = _ticket(service, ticket_id)["acceptance_criteria"][0]
    return service.submit_qa_verdict(
        qa, ticket_id, qa_claim["lease"]["lease_id"], submitted["candidate"]["candidate_id"],
        qa_claim["ticket"]["version"], "failed",
        [{
            "criterion": criterion, "status": "failed", "severity": "high",
            "observation": "A duplicate order is still created.",
            "evidence": [{"type": "test_result", "observation": "duplicate order",
                          "storage_ref": "evidence://qa/duplicate.json"}],
        }],
        context_snapshot_id=qa_claim["current_context_snapshot_id"],
    )


def _pending(service, actors) -> list[dict]:
    return service.list_repository_reconciliations(
        actors["planner"], "project-1"
    )["reconciliations"]


def _refused_for_reconciliation(call) -> ConflictError:
    with pytest.raises(ConflictError) as refused:
        call()
    assert refused.value.details["reason"] == "repository_reconciliation_required"
    return refused.value


def test_a_qa_rejection_freezes_only_the_ticket_and_its_dependents(
    service, actors, seeded
) -> None:
    _board(service, actors)

    verdict = _reject(service, actors)

    [reconciliation] = _pending(service, actors)
    assert verdict["reconciliation_id"] == reconciliation["reconciliation_id"]
    assert reconciliation["scope"] == "tickets"
    assert reconciliation["frozen_ticket_ids"] == ["DEV-142", "DEV-C", "DEV-D"]
    # Planner classifies only what the rejection can have affected.
    assert sorted(reconciliation["ticket_versions"]) == ["DEV-142", "DEV-C", "DEV-D"]
    # Planner still gets the failure job, so nothing is lost.
    assert verdict["planner_job"]["job_type"] == "qa_failure"

    # Unrelated work keeps flowing.
    unrelated = service.claim_ticket(
        actors["developer_2"], "DEV-B", _ticket(service, "DEV-B")["version"], 900
    )
    assert unrelated["ticket"]["status"] == "DEV_IN_PROGRESS"
    # The rejected ticket waits for Planner, although REWORK is claimable status.
    rework = _ticket(service, "DEV-142")
    assert rework["status"] == "REWORK"
    _refused_for_reconciliation(
        lambda: service.claim_ticket(actors["developer"], "DEV-142", rework["version"], 900)
    )
    # A dependent is frozen by the barrier itself, not only by its unmet dependency.
    _refused_for_reconciliation(
        lambda: service.claim_ticket(
            actors["developer"], "DEV-C", _ticket(service, "DEV-C")["version"], 900
        )
    )
    with service.store.connection() as connection:
        assert ExecutionGate.is_closed(connection, "project-1", "DEV-D")
        assert not ExecutionGate.is_closed(connection, "project-1", "DEV-B")
        assert not ExecutionGate.is_closed(connection, "project-1")
        project_state = ExecutionGate.state(connection, "project-1")
    assert project_state["repository_reconciliation_required"] is False
    assert project_state["frozen_ticket_ids"] == ["DEV-142", "DEV-C", "DEV-D"]

    # Each packet says what applies to its own ticket.
    frozen_packet = service.get_ticket_context(actors["planner"], "DEV-C")
    flowing_packet = service.get_ticket_context(actors["planner"], "DEV-B")
    assert frozen_packet["planning_barrier"]["repository_reconciliation_required"] is True
    assert flowing_packet["planning_barrier"] == {
        "repository_reconciliation_required": False, "pending": []
    }

    keep_open_tickets(service, actors["planner"])

    resumed = service.claim_ticket(
        actors["developer"], "DEV-142", _ticket(service, "DEV-142")["version"], 900
    )
    assert resumed["ticket"]["status"] == "DEV_IN_PROGRESS"


def test_a_rejection_elsewhere_does_not_stale_a_packet_in_flight(
    service, actors, seeded
) -> None:
    _open(service, actors, "DEV-B", ["src/reporting/**"])
    claim = service.claim_ticket(
        actors["developer_2"], "DEV-B", _ticket(service, "DEV-B")["version"], 900
    )
    snapshot = service.get_ticket_context(actors["developer_2"], "DEV-B")["context_snapshot_id"]

    _reject(service, actors)

    with service.store.connection() as connection:
        service.require_fresh_context(connection, "DEV-B", snapshot)
    submitted = service.submit_candidate(
        actors["developer_2"], "DEV-B", claim["lease"]["lease_id"], claim["ticket"]["version"],
        "c" * 40, "b" * 40, "agent/dev-02/DEV-B", ["Ready."], context_snapshot_id=snapshot,
    )
    assert submitted["ticket"]["status"] == "QA_READY"


def test_offers_skip_frozen_tickets_and_reach_the_rest(service, actors, seeded) -> None:
    _board(service, actors)
    _reject(service, actors)
    service.register_agent(actors["developer_2"], skills=["hoh-developer"], capacity=1)

    inbox = service.get_agent_inbox(actors["developer_2"], "project-1")

    assert {offer["ticket"]["ticket_id"] for offer in inbox["offers"]} == {"DEV-B"}
    assert inbox["planning_barrier"]["repository_reconciliation_required"] is False


def test_unrelated_rejections_are_decided_apart_and_never_shrink_a_freeze(
    service, actors, seeded
) -> None:
    _board(service, actors)
    first = _reject(service, actors)
    second = _reject(service, actors, "DEV-B", attempt=2, developer="developer_2")
    assert first["reconciliation_id"] != second["reconciliation_id"]

    # The newer one shares no ticket with the older, so it need not wait for it.
    later = next(item for item in _pending(service, actors)
                 if item["reconciliation_id"] == second["reconciliation_id"])
    service.resolve_repository_reconciliation(
        actors["planner"], "project-1", later["reconciliation_id"],
        [{"ticket_id": "DEV-B", "disposition": "keep", "reason": "Fix and resubmit."}],
        "DEV-B goes back to its Developer.",
    )

    with service.store.connection() as connection:
        assert not ExecutionGate.is_closed(connection, "project-1", "DEV-B")
        for frozen in ("DEV-142", "DEV-C", "DEV-D"):
            assert ExecutionGate.is_closed(connection, "project-1", frozen)


def test_a_project_wide_observation_waits_for_an_older_ticket_scoped_one(
    service, actors, seeded
) -> None:
    rejection = _reject(service, actors)
    with service.store.connection(immediate=True) as connection:
        newer = service._open_commit_reconciliation(
            connection, actors["planner"], "project-1", commit="e" * 40,
            subject="Teammate push", relation="incoming", observation_ref="test:push",
        )
        # A project-wide reconciliation closes everything, frozen or not.
        assert ExecutionGate.is_closed(connection, "project-1")
        newer_row = service._repository_reconciliation(connection.execute(
            "SELECT * FROM repository_reconciliations WHERE reconciliation_id = ?", (newer,)
        ).fetchone())

    with pytest.raises(ConflictError) as refused:
        service.resolve_repository_reconciliation(
            actors["planner"], "project-1", newer,
            [{"ticket_id": ticket_id, "disposition": "keep", "reason": "unaffected"}
             for ticket_id in newer_row["ticket_versions"]],
            "Push does not touch open work.",
        )
    assert refused.value.details == {
        "reason": "older_reconciliation_pending",
        "reconciliation_id": rejection["reconciliation_id"],
    }


def test_execution_roles_are_not_woken_by_a_ticket_scoped_barrier(
    service, actors, seeded
) -> None:
    from hoh_control_plane.event_wait import _pending as wait_candidates

    _open(service, actors, "DEV-B", ["src/reporting/**"])
    _reject(service, actors)
    service.register_agent(actors["developer_2"], skills=["hoh-developer"], capacity=1)

    developer_events = wait_candidates(service, actors["developer_2"], "project-1")
    planner_events = wait_candidates(service, actors["planner"], "project-1")

    assert [payload["kind"] for _key, payload in developer_events] == ["work_available"]
    assert developer_events[0][1]["ticket_ref"] == "ticket:DEV-B"
    [(_key, planner_event)] = planner_events
    assert planner_event["kind"] == "repository_changed"
    assert planner_event["scope"] == "tickets"
    assert planner_event["frozen_ticket_ids"] == ["DEV-142"]


def _gate_regression(service, actors, seeded, tmp_path: Path) -> dict:
    from test_integration_gate import _FAILING_REGRESSION, _gate_ready_candidate, _merge

    _repository, ticket, _candidate, integration = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_FAILING_REGRESSION
    )
    _board(service, actors)
    return _merge(service, actors["integrator"], ticket, integration)


def test_a_gate_regression_freezes_only_the_ticket_and_its_dependents(
    service, actors, seeded, tmp_path: Path
) -> None:
    gated = _gate_regression(service, actors, seeded, tmp_path)

    assert gated["ticket"]["status"] == "REWORK"
    [reconciliation] = _pending(service, actors)
    assert reconciliation["scope"] == "tickets"
    assert reconciliation["frozen_ticket_ids"] == ["DEV-142", "DEV-C", "DEV-D"]
    unrelated = service.claim_ticket(
        actors["developer_2"], "DEV-B", _ticket(service, "DEV-B")["version"], 900
    )
    assert unrelated["ticket"]["status"] == "DEV_IN_PROGRESS"
    _refused_for_reconciliation(
        lambda: service.claim_ticket(
            actors["developer"], "DEV-142", _ticket(service, "DEV-142")["version"], 900
        )
    )
    _refused_for_reconciliation(
        lambda: service.claim_ticket(
            actors["developer"], "DEV-C", _ticket(service, "DEV-C")["version"], 900
        )
    )
    with service.store.connection() as connection:
        planner_jobs = connection.execute(
            "SELECT job_type FROM planner_jobs WHERE ticket_id = 'DEV-142'"
        ).fetchall()
    assert [row["job_type"] for row in planner_jobs] == ["integration_conflict"]


def _queued_merge(service, actors, seeded, tmp_path: Path):
    from test_integration_async import (
        _candidate_ready_for_integration, _claimed_for_integration, _git,
    )

    repository, ticket, _candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(
            actors["integrator"], ticket["ticket_id"]
        )["context_snapshot_id"],
    )
    (repository / "teammate.txt").write_text("pushed meanwhile\n", encoding="utf-8")
    _git(repository, "add", "teammate.txt")
    _git(repository, "commit", "-m", "Teammate change")
    return repository, queued["merge_job"]["merge_job_id"], _git(repository, "rev-parse", "main")


def test_drift_onto_a_commit_hoh_never_observed_keeps_the_project_barrier(
    service, actors, seeded, tmp_path: Path
) -> None:
    _repository, merge_job_id, _main = _queued_merge(service, actors, seeded, tmp_path)

    assert service.execute_queued_merge(merge_job_id)["merge_status"] == "base_drift"

    [reconciliation] = _pending(service, actors)
    assert reconciliation["scope"] == "project"
    with service.store.connection() as connection:
        assert ExecutionGate.is_closed(connection, "project-1")


def test_drift_onto_an_observed_main_freezes_only_the_drifted_ticket(
    service, actors, seeded, tmp_path: Path
) -> None:
    _repository, merge_job_id, main = _queued_merge(service, actors, seeded, tmp_path)
    # HoH has already seen main at this commit (a merge or a sync recorded it),
    # and that observation opened, and Planner resolved, its own reconciliation.
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE project_repositories SET observed_mainline_commit = ? WHERE project_id = ?",
            (main, "project-1"),
        )
    _open(service, actors, "DEV-B", ["src/reporting/**"])

    assert service.execute_queued_merge(merge_job_id)["merge_status"] == "base_drift"

    [reconciliation] = _pending(service, actors)
    assert reconciliation["scope"] == "tickets"
    assert reconciliation["frozen_ticket_ids"] == ["DEV-142"]
    unrelated = service.claim_ticket(
        actors["developer_2"], "DEV-B", _ticket(service, "DEV-B")["version"], 900
    )
    assert unrelated["ticket"]["status"] == "DEV_IN_PROGRESS"
