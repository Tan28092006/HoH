"""Every Planner intervention path, exercised on an upgraded database.

The schema defect these cover is invisible on a freshly created file: only a
database carried forward from the pre-widening schema had its intervention and
lease foreign keys rewritten to a dropped temporary table. So each test here
runs the real service flow against a database that took that upgrade.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hoh_control_plane.domain import Actor
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store import SQLiteStore
from helpers.integration import (
    _candidate_ready_for_integration,
    _claimed_for_integration,
    _git,
)
from helpers.legacy_store import legacy_service, seed_project


@pytest.fixture
def upgraded(tmp_path: Path, actors: dict[str, Actor]) -> dict[str, object]:
    """A legacy control-plane file with real project state, upgraded in place."""
    path = tmp_path / "upgraded-control-plane.db"
    legacy = legacy_service(path)
    seeded = seed_project(legacy, actors["planner"])
    SQLiteStore(path).initialize()
    return {"service": ControlPlaneService(SQLiteStore(path)), "seeded": seeded, "path": path}


def _failing_finding(criterion: str) -> dict[str, object]:
    return {
        "criterion": criterion,
        "status": "failed",
        "severity": "high",
        "observation": "A duplicate order is still created.",
        "evidence": [
            {
                "type": "test_result",
                "observation": "duplicate-order regression reproduced",
                "storage_ref": "evidence://qa/duplicate-order.json",
            }
        ],
    }


def _qa_failed(service, actors, ticket):
    claim = service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)
    candidate = service.submit_candidate(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
        "a" * 40,
        "b" * 40,
        "agent/dev-01/DEV-142",
        ["Ready for QA."],
        context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
    )
    qa_claim = service.claim_qa(
        actors["qa"], ticket["ticket_id"], candidate["ticket"]["version"], 900
    )
    return service.submit_qa_verdict(
        actors["qa"],
        ticket["ticket_id"],
        qa_claim["lease"]["lease_id"],
        candidate["candidate"]["candidate_id"],
        qa_claim["ticket"]["version"],
        "failed",
        [_failing_finding(ticket["acceptance_criteria"][0])],
        context_snapshot_id=service.get_ticket_context(actors["qa"], ticket["ticket_id"])["context_snapshot_id"],
    )


def _rows(service, statement: str, parameters: tuple = ()) -> list[sqlite3.Row]:
    with service.store.connection() as connection:
        return connection.execute(statement, parameters).fetchall()


def test_a_qa_failure_commits_findings_context_rework_job_and_request_together(
    upgraded, actors
) -> None:
    """AC3: the whole verdict lands, or none of it would have."""
    service = upgraded["service"]
    ticket = upgraded["seeded"]["ticket"]

    failed = _qa_failed(service, actors, ticket)

    assert failed["ticket"]["status"] == "REWORK"
    assert failed["planner_job"]["job_type"] == "qa_failure"
    findings = _rows(
        service,
        "SELECT * FROM qa_findings WHERE candidate_id = ?",
        (failed["candidate"]["candidate_id"],),
    )
    contexts = _rows(
        service,
        "SELECT * FROM context_entries WHERE ticket_id = ? AND entry_type = 'known_failure'",
        (ticket["ticket_id"],),
    )
    interventions = service.list_planner_interventions(actors["planner"], "project-1")
    assert findings, "the failing criterion must be recorded"
    assert contexts, "the known failure must be written for the next Developer"
    assert len(interventions) == 1
    assert interventions[0]["event_class"] == "qa_failure"
    assert interventions[0]["planner_job_id"] == failed["planner_job"]["planner_job_id"]
    assert interventions[0]["candidate_id"] == failed["candidate"]["candidate_id"]


def test_a_qa_failure_leaves_no_planning_job_without_its_request(upgraded, actors) -> None:
    """The half-written state the defect produced: a job nobody is woken for."""
    service = upgraded["service"]

    _qa_failed(service, actors, upgraded["seeded"]["ticket"])

    jobs = _rows(service, "SELECT planner_job_id FROM planner_jobs")
    interventions = _rows(service, "SELECT planner_job_id FROM planner_interventions")
    assert {row["planner_job_id"] for row in jobs} == {
        row["planner_job_id"] for row in interventions
    }


def test_a_developer_blocker_still_raises_its_request(upgraded, actors) -> None:
    """AC4: the blocker path on the same upgraded database."""
    service = upgraded["service"]
    ticket = upgraded["seeded"]["ticket"]
    claim = service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)

    blocked = service.report_blocker(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
        "The upstream payment contract is undecided.",
    )

    interventions = service.list_planner_interventions(actors["planner"], "project-1")
    assert blocked["ticket"]["status"] == "BLOCKED"
    assert blocked["planner_job"]["job_type"] == "developer_blocker"
    assert len(interventions) == 1
    assert interventions[0]["event_class"] == "developer_blocker"
    assert interventions[0]["severity"] == "high"


def test_an_integration_conflict_still_raises_its_request(
    upgraded, actors, tmp_path: Path
) -> None:
    """AC4: base drift during a merge is a planning problem on an upgraded file too."""
    service = upgraded["service"]
    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, upgraded["seeded"], tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"],
        ticket["ticket_id"],
        integration["lease"]["lease_id"],
        integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(actors["integrator"], ticket["ticket_id"])["context_snapshot_id"],
    )
    (repository / "mainline.txt").write_text("new mainline work\n", encoding="utf-8")
    _git(repository, "add", "mainline.txt")
    _git(repository, "commit", "-m", "Advance mainline")

    completed = service.execute_queued_merge(queued["merge_job"]["merge_job_id"])

    assert completed["merge_status"] == "base_drift"
    interventions = service.list_planner_interventions(actors["planner"], "project-1")
    assert len(interventions) == 1
    assert interventions[0]["event_class"] == "integration_conflict"
    assert interventions[0]["candidate_id"] == candidate["candidate"]["candidate_id"]


def test_a_planner_can_claim_the_job_it_was_woken_for(upgraded, actors) -> None:
    """The rename damaged planner_job_leases too: waking a Planner is useless
    if the lease that grants the decision cannot be written."""
    service = upgraded["service"]
    failed = _qa_failed(service, actors, upgraded["seeded"]["ticket"])

    claimed = service.claim_planner_job(
        actors["planner"], failed["planner_job"]["planner_job_id"]
    )

    assert claimed["planner_job"]["status"] == "IN_PROGRESS"
    assert claimed["lease"]["agent_id"] == actors["planner"].agent_id


def test_acknowledging_a_request_on_an_upgraded_database_persists(upgraded, actors) -> None:
    service = upgraded["service"]
    _qa_failed(service, actors, upgraded["seeded"]["ticket"])
    pending = service.list_planner_interventions(actors["planner"], "project-1")[0]

    service.acknowledge_planner_intervention(actors["planner"], pending["intervention_id"])

    assert service.list_planner_interventions(actors["planner"], "project-1") == []
    acknowledged = service.list_planner_interventions(
        actors["planner"], "project-1", include_acknowledged=True
    )
    assert [row["status"] for row in acknowledged] == ["ACKNOWLEDGED"]
