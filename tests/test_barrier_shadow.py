"""Shadow mode for a scope-partitioned merge barrier (wave 6D, part 2).

A merge Planner has to review still closes the whole project. Beside it HoH
records the tickets a scope rule would have frozen, and when Planner resolves
the reconciliation, whether every ticket Planner changed fell inside that set.
The prediction is never used to admit work; it is evidence for deciding later
whether partitioning merges is safe.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hoh_control_plane.domain import ConflictError, Role
from hoh_control_plane.service._common import ExecutionGate


def _open(service, actors, ticket_id: str, scope: list[str], depends_on=None) -> dict:
    return service.create_ticket(
        actors["planner"], feature_id="payment-retry", ticket_id=ticket_id,
        title=f"Work {ticket_id}", objective="Other work on the board",
        required_role=Role.DEVELOPER, acceptance_criteria=["It works"],
        preservation_constraints=["Checkout stays valid"], file_scope=scope,
        dependency_ids=depends_on or [], ready=True,
    )


def _partition(service, open_ids: list[str], paths: list[str], merged: str = "DEV-142") -> dict:
    with service.store.connection() as connection:
        return service._shadow_merge_partition(connection, open_ids, merged, paths)


def test_the_partition_freezes_overlap_dependents_and_unscoped_work(
    service, actors, seeded
) -> None:
    _open(service, actors, "DEV-OVER", ["src/checkout/**"])
    _open(service, actors, "DEV-FREE", ["src/reporting/**"])
    _open(service, actors, "DEV-NEXT", ["src/emails/**"], ["DEV-142"])
    _open(service, actors, "DEV-LATER", ["src/sms/**"], ["DEV-NEXT"])
    _open(service, actors, "DEV-NONE", ["placeholder/**"])
    with service.store.connection(immediate=True) as connection:
        connection.execute("UPDATE tickets SET file_scope = '[]' WHERE ticket_id = 'DEV-NONE'")

    partition = _partition(
        service, ["DEV-OVER", "DEV-FREE", "DEV-NEXT", "DEV-LATER", "DEV-NONE"],
        ["src/checkout/retry.py"],
    )

    assert partition["project_wide"] is False
    assert partition["frozen_ticket_ids"] == ["DEV-LATER", "DEV-NEXT", "DEV-NONE", "DEV-OVER"]
    assert partition["flowing_ticket_ids"] == ["DEV-FREE"]
    assert partition["rule"] == service.SHADOW_RULE


@pytest.mark.parametrize("paths,reason", [
    (["docs/checkout.md", "src/checkout/retry.py"], "shared_paths_changed"),
    (["pyproject.toml"], "shared_paths_changed"),
    ([], "changed_paths_unknown"),
])
def test_shared_or_unknown_paths_freeze_the_whole_project(
    service, actors, seeded, paths, reason
) -> None:
    _open(service, actors, "DEV-FREE", ["src/reporting/**"])

    partition = _partition(service, ["DEV-FREE"], paths)

    assert partition["project_wide"] is True
    assert partition["reason"] == reason
    assert partition["frozen_ticket_ids"] == ["DEV-FREE"]
    assert partition["flowing_ticket_ids"] == []


def test_only_a_changed_ticket_outside_the_prediction_is_a_disagreement(service) -> None:
    partition = {"frozen_ticket_ids": ["DEV-A"]}
    keep = {"ticket_id": "x", "disposition": "keep"}

    agreed = service._shadow_outcome(partition, {
        "DEV-A": {**keep, "disposition": "revise"}, "DEV-B": keep,
    })
    missed = service._shadow_outcome(partition, {
        "DEV-A": keep, "DEV-B": {**keep, "disposition": "rework"},
    })

    assert agreed == {"agreed": True, "changed_ticket_ids": ["DEV-A"], "missed_ticket_ids": []}
    assert missed == {"agreed": False, "changed_ticket_ids": ["DEV-B"], "missed_ticket_ids": ["DEV-B"]}


def _merge_needing_review(service, actors, seeded, tmp_path: Path) -> dict:
    from test_integration_async import _merge

    _open(service, actors, "DEV-OVER", ["src/checkout/**"])
    _open(service, actors, "DEV-FREE", ["src/reporting/**"])
    _open(service, actors, "DEV-NEXT", ["src/emails/**"], ["DEV-142"])
    assert _merge(service, actors, seeded, tmp_path)["merge_status"] == "merged"
    [reconciliation] = service.list_repository_reconciliations(
        actors["planner"], "project-1"
    )["reconciliations"]
    return reconciliation


def _resolve(service, actors, reconciliation, revised: str) -> dict:
    planner = actors["planner"]
    with service.store.connection() as connection:
        version = service._ticket(connection, revised)["version"]
    service.update_ticket_fields(
        planner, revised, version, "The merge changed what this ticket builds on.",
        objective="Build on the merged retry behaviour",
    )
    return service.resolve_repository_reconciliation(
        planner, "project-1", reconciliation["reconciliation_id"],
        [
            {"ticket_id": ticket_id,
             "disposition": "revise" if ticket_id == revised else "keep",
             "reason": "Reviewed against the merged commit."}
            for ticket_id in reconciliation["ticket_versions"]
        ],
        "Merged retry reviewed.",
    )["reconciliation"]


def test_a_reviewed_merge_records_its_partition_but_still_closes_the_project(
    service, actors, seeded, tmp_path: Path
) -> None:
    reconciliation = _merge_needing_review(service, actors, seeded, tmp_path)

    assert reconciliation["scope"] == "project"
    shadow = reconciliation["shadow_partition"]
    assert shadow["project_wide"] is False
    assert shadow["frozen_ticket_ids"] == ["DEV-NEXT", "DEV-OVER"]
    assert shadow["flowing_ticket_ids"] == ["DEV-FREE"]
    assert reconciliation["shadow_agreed"] is None
    # Not acted on: a ticket the partition would let flow is still refused.
    with service.store.connection() as connection:
        assert ExecutionGate.is_closed(connection, "project-1", "DEV-FREE")
        version = service._ticket(connection, "DEV-FREE")["version"]
    with pytest.raises(ConflictError) as refused:
        service.claim_ticket(actors["developer"], "DEV-FREE", version, 900)
    assert refused.value.details["reason"] == "repository_reconciliation_required"


def test_agreement_is_scored_when_planner_resolves_and_summarized_in_metrics(
    service, actors, seeded, tmp_path: Path
) -> None:
    reconciliation = _merge_needing_review(service, actors, seeded, tmp_path)

    resolved = _resolve(service, actors, reconciliation, revised="DEV-OVER")

    assert resolved["shadow_agreed"] == 1
    assert resolved["shadow_partition"]["outcome"] == {
        "agreed": True, "changed_ticket_ids": ["DEV-OVER"], "missed_ticket_ids": [],
    }
    # The prediction itself is kept exactly as it was made.
    assert resolved["shadow_partition"]["frozen_ticket_ids"] == ["DEV-NEXT", "DEV-OVER"]
    summary = service.cycle_metrics(actors["planner"], "project-1")["barrier_shadow"]
    assert summary["resolved"] == 1 and summary["agreed"] == 1
    assert summary["agreement_rate"] == 1.0
    assert summary["partitioned"] == 1
    assert summary["tickets_that_would_have_flowed"] == 1


def test_a_change_outside_the_prediction_is_reported_as_a_disagreement(
    service, actors, seeded, tmp_path: Path
) -> None:
    reconciliation = _merge_needing_review(service, actors, seeded, tmp_path)

    resolved = _resolve(service, actors, reconciliation, revised="DEV-FREE")

    assert resolved["shadow_agreed"] == 0
    summary = service.cycle_metrics(actors["planner"], "project-1")["barrier_shadow"]
    assert summary["disagreed"] == 1 and summary["agreement_rate"] == 0.0
    assert summary["disagreements"] == [{
        "reconciliation_id": reconciliation["reconciliation_id"],
        "missed_ticket_ids": ["DEV-FREE"],
    }]
    with service.store.connection() as connection:
        event = connection.execute(
            "SELECT payload FROM audit_events WHERE event_type = 'project.repository_reconciled'"
        ).fetchone()
    assert '"shadow_agreed": false' in event["payload"]


def test_an_auto_kept_merge_carries_no_prediction(
    service, actors, seeded, tmp_path: Path
) -> None:
    from test_integration_async import _merge

    _open(service, actors, "DEV-FREE", ["src/reporting/**"])
    _merge(service, actors, seeded, tmp_path)

    with service.store.connection() as connection:
        rows = connection.execute(
            "SELECT status, shadow_partition FROM repository_reconciliations"
        ).fetchall()
    assert [(row["status"], row["shadow_partition"]) for row in rows][-1] == ("RESOLVED", None)
    assert service.cycle_metrics(actors["planner"], "project-1")["barrier_shadow"]["resolved"] == 0
