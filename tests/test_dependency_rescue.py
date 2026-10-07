"""Review R2: a dependent of retired work always gets a way out.

Only DONE satisfies a dependency, so a ticket waiting on CANCELLED or
SUPERSEDED work could never be offered. Retirement now moves the edge to the
replacement, or opens a Planner job for the dependent when there is none.
"""
from __future__ import annotations

import json

from hoh_control_plane.domain import Role


def _ticket(service, actors, ticket_id, *, dependency_ids=(), ready=False, scope=None):
    return service.create_ticket(
        actors["planner"],
        feature_id="payment-retry",
        ticket_id=ticket_id,
        title=f"Work {ticket_id}",
        objective="Bounded work",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["It behaves as specified"],
        preservation_constraints=[],
        file_scope=[scope or f"src/{ticket_id.lower()}/**"],
        dependency_ids=list(dependency_ids),
        ready=ready,
    )


def _dependencies(service, ticket_id):
    with service.store.connection() as connection:
        return sorted(
            row["depends_on_ticket_id"]
            for row in connection.execute(
                "SELECT depends_on_ticket_id FROM ticket_dependencies WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchall()
        )


def _open_jobs(service, ticket_id):
    with service.store.connection() as connection:
        return [
            dict(row) for row in connection.execute(
                "SELECT * FROM planner_jobs WHERE ticket_id = ? AND status IN ('READY', 'IN_PROGRESS')",
                (ticket_id,),
            ).fetchall()
        ]


def test_supersede_moves_the_dependency_edge_to_the_replacement(service, actors, seeded):
    a = _ticket(service, actors, "DEV-A")
    a2 = _ticket(service, actors, "DEV-A2")
    _ticket(service, actors, "DEV-B", dependency_ids=["DEV-A"])

    result = service.supersede_ticket(
        actors["planner"], "DEV-A", a["version"], a2["ticket_id"], "A2 carries it now."
    )

    assert _dependencies(service, "DEV-B") == ["DEV-A2"]
    assert result["rewired_dependent_ticket_ids"] == ["DEV-B"]
    assert result["dependent_planner_job_ids"] == []
    assert _open_jobs(service, "DEV-B") == []
    with service.store.connection() as connection:
        event = connection.execute(
            "SELECT payload FROM audit_events WHERE event_type = 'ticket.dependency_rewired' "
            "AND aggregate_id = 'DEV-B'"
        ).fetchone()
    assert json.loads(event["payload"])["replacement_ticket_ids"] == ["DEV-A2"]


def test_cancel_without_replacement_opens_one_planner_job_per_open_dependent(
    service, actors, seeded
):
    a = _ticket(service, actors, "DEV-A")
    _ticket(service, actors, "DEV-B", dependency_ids=["DEV-A"])
    _ticket(service, actors, "DEV-C", dependency_ids=["DEV-A"])
    finished = _ticket(service, actors, "DEV-D", dependency_ids=["DEV-A"])
    service.cancel_ticket(actors["planner"], "DEV-D", finished["version"], "Dropped first.")

    result = service.cancel_ticket(actors["planner"], "DEV-A", a["version"], "Obsolete.")

    assert result["dependent_ticket_ids"] == ["DEV-B", "DEV-C", "DEV-D"]
    assert result["rewired_dependent_ticket_ids"] == []
    assert len(result["dependent_planner_job_ids"]) == 2, "a retired dependent needs no job"
    for dependent in ("DEV-B", "DEV-C"):
        jobs = _open_jobs(service, dependent)
        assert len(jobs) == 1
        assert jobs[0]["summary"].startswith("dependency_retired: ")
        assert "ticket:DEV-A" in json.loads(jobs[0]["evidence_refs"])
        assert _dependencies(service, dependent) == ["DEV-A"], "the Planner decides the edge"
    assert _open_jobs(service, "DEV-D") == []
    interventions = service.list_planner_interventions(actors["planner"], "project-1")
    assert {item["ticket_id"] for item in interventions} >= {"DEV-B", "DEV-C"}


def test_a_replacement_that_would_close_a_cycle_falls_back_to_a_planner_job(
    service, actors, seeded
):
    a = _ticket(service, actors, "DEV-A")
    _ticket(service, actors, "DEV-B", dependency_ids=["DEV-A"])
    _ticket(service, actors, "DEV-A2", dependency_ids=["DEV-B"])

    result = service.supersede_ticket(actors["planner"], "DEV-A", a["version"], "DEV-A2", "Cycle.")

    assert result["rewired_dependent_ticket_ids"] == []
    assert _dependencies(service, "DEV-B") == ["DEV-A"]
    jobs = _open_jobs(service, "DEV-B")
    assert len(jobs) == 1 and "cycle" in jobs[0]["summary"]


def test_a_retired_replacement_cannot_carry_the_edge(service, actors, seeded):
    a = _ticket(service, actors, "DEV-A")
    a2 = _ticket(service, actors, "DEV-A2")
    _ticket(service, actors, "DEV-B", dependency_ids=["DEV-A"])
    service.cancel_ticket(actors["planner"], "DEV-A2", a2["version"], "Replacement dropped.")

    result = service.supersede_ticket(actors["planner"], "DEV-A", a["version"], "DEV-A2", "Stale.")

    assert result["rewired_dependent_ticket_ids"] == []
    assert len(_open_jobs(service, "DEV-B")) == 1


def test_split_rewires_dependents_to_every_replacement(service, actors, seeded):
    a = _ticket(service, actors, "DEV-A")
    _ticket(service, actors, "DEV-A1")
    _ticket(service, actors, "DEV-A2")
    _ticket(service, actors, "DEV-B", dependency_ids=["DEV-A"])

    split = service.split_ticket(
        actors["planner"], "DEV-A", a["version"], ["DEV-A1", "DEV-A2"], "Two narrower tickets."
    )

    assert split["rewired_dependent_ticket_ids"] == ["DEV-B"]
    assert _dependencies(service, "DEV-B") == ["DEV-A1", "DEV-A2"]


def test_rewiring_does_not_duplicate_an_existing_edge(service, actors, seeded):
    a = _ticket(service, actors, "DEV-A")
    _ticket(service, actors, "DEV-A2")
    _ticket(service, actors, "DEV-B", dependency_ids=["DEV-A", "DEV-A2"])

    service.supersede_ticket(actors["planner"], "DEV-A", a["version"], "DEV-A2", "Replaced.")

    assert _dependencies(service, "DEV-B") == ["DEV-A2"]


def test_reconciliation_retirement_rescues_dependents_too(service, actors, seeded):
    a = _ticket(service, actors, "DEV-A")
    _ticket(service, actors, "DEV-B", dependency_ids=["DEV-A"])
    _ticket(service, actors, "DEV-A2")
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """INSERT INTO repository_reconciliations
               (reconciliation_id, project_id, observation_ref, observed_commits,
                ticket_versions, status, created_at, scope, frozen_ticket_ids)
               VALUES ('recon_r2', 'project-1', 'ctx:entry:x', '[]', '{}', 'PENDING',
                       '2026-10-06T00:00:00Z', 'project', NULL)"""
        )

    applied = service.retire_ticket_for_reconciliation(
        actors["planner"], "DEV-A", a["version"], "recon_r2", "Replaced by A2.",
        target_status="SUPERSEDED", replacement_ticket_id="DEV-A2",
    )

    assert applied["retirement"]["status"] == "APPLIED"
    assert _dependencies(service, "DEV-B") == ["DEV-A2"]


def test_reconciliation_cancel_without_replacement_opens_a_job(service, actors, seeded):
    a = _ticket(service, actors, "DEV-A")
    _ticket(service, actors, "DEV-B", dependency_ids=["DEV-A"])
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """INSERT INTO repository_reconciliations
               (reconciliation_id, project_id, observation_ref, observed_commits,
                ticket_versions, status, created_at, scope, frozen_ticket_ids)
               VALUES ('recon_r2', 'project-1', 'ctx:entry:x', '[]', '{}', 'PENDING',
                       '2026-10-06T00:00:00Z', 'project', NULL)"""
        )

    service.retire_ticket_for_reconciliation(
        actors["planner"], "DEV-A", a["version"], "recon_r2", "Obsolete."
    )

    jobs = _open_jobs(service, "DEV-B")
    assert len(jobs) == 1
    assert jobs[0]["created_by"] == actors["planner"].agent_id
