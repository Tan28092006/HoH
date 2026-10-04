"""H1: the loop's own cycle time, read from events the workflow already writes."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hoh_control_plane.api import create_app
from hoh_control_plane.domain import ValidationError

_SEQ = [0]


def _event(connection, ticket_id, event_type, at, role="planner", payload=None,
           aggregate_type="ticket", aggregate_id=None) -> None:
    _SEQ[0] += 1
    connection.execute(
        """
        INSERT INTO audit_events
            (event_id, event_type, aggregate_type, aggregate_id, aggregate_version,
             project_id, actor_id, actor_role, payload, occurred_at)
        VALUES (?, ?, ?, ?, 1, 'project-1', 'seed', ?, ?, ?)
        """,
        (f"evt_seed_{_SEQ[0]}", event_type, aggregate_type, aggregate_id or ticket_id,
         role, json.dumps(payload or {}), at),
    )


def _deliver(connection, ticket_id, minutes: list[int], gated=True) -> None:
    ready, claim, submit, verdict, merge = (f"2026-09-01T10:{m:02d}:00Z" for m in minutes)
    _event(connection, ticket_id, "ticket.ready", ready)
    _event(connection, ticket_id, "ticket.claimed", claim, role="qa")  # ignored: not Developer
    _event(connection, ticket_id, "ticket.claimed", claim, role="developer")
    _event(connection, ticket_id, "candidate.frozen", submit, role="developer")
    if gated:
        _event(connection, ticket_id, "ticket.verified", verdict, role="qa")
        _event(connection, ticket_id, "integration.completed", merge, role="integrator")
    else:
        _event(connection, ticket_id, "integration.landed_ungated", merge, role="integrator")


@pytest.fixture
def fk_off_service(service, seeded):
    _seed_with_fk_off(service)
    return service


def _seed_with_fk_off(service) -> None:
    # Seeded rows reference candidates that do not exist; only the events matter.
    raw = sqlite3.connect(service.store.path)
    raw.execute("PRAGMA foreign_keys = OFF")
    try:
        _seed_body(raw)
        raw.commit()
    finally:
        raw.close()


def _seed_body(connection) -> None:
    _deliver(connection, "T-A", [0, 1, 3, 6, 10])
    _deliver(connection, "T-B", [0, 2, 6, 12, 20])
    _deliver(connection, "T-C", [0, 3, 9, 18, 30])
    _deliver(connection, "T-U", [0, 1, 2, 2, 4], gated=False)
    _event(connection, "T-OPEN", "ticket.ready", "2026-09-01T10:00:00Z")
    for run in range(2):
        connection.execute(
            """
            INSERT INTO qa_test_runs (test_run_id, candidate_id, ticket_id, command_digest,
                exit_code, duration_ms, storage_ref, report_path, executed_by, created_at)
            VALUES (?, 'cand-a', 'T-A', 'd', 0, 1, 's', 'r', 'qa', '2026-09-01T10:04:00Z')
            """,
            (f"run-{run}",),
        )
    for job, mode in (("mj-1", "executed"), ("mj-2", "reused_qa_run")):
        connection.execute(
            """
            INSERT INTO merge_jobs (merge_job_id, project_id, ticket_id, lease_id,
                expected_version, candidate_id, requested_by, status, created_at)
            VALUES (?, 'project-1', 'T-A', 'l', 1, 'cand-a', 'i', 'COMPLETED', '2026-09-01T10:07:00Z')
            """,
            (job,),
        )
        _event(connection, "T-A", "integration.gate_passed", "2026-09-01T10:08:00Z",
               role="integrator", payload={"gate_mode": mode},
               aggregate_type="merge_job", aggregate_id=job)


def test_cycle_metrics_reports_phases_and_distribution(fk_off_service, actors) -> None:
    report = fk_off_service.cycle_metrics(actors["planner"], "project-1", 10)

    assert report["delivered_tickets"] == 4
    by_id = {ticket["ticket_id"]: ticket for ticket in report["tickets"]}
    assert "T-OPEN" not in by_id
    a = by_id["T-A"]
    assert a["phases_seconds"] == {
        "ready_to_claim": 60.0, "claim_to_submit": 120.0, "submit_to_verdict": 180.0,
        "verdict_to_merge": 240.0, "ready_to_merge": 600.0,
    }
    assert a["qa_test_runs"] == 2
    assert a["gate_runs"] == {"executed": 1, "reused": 1}
    assert by_id["T-U"]["gated"] is False
    assert by_id["T-U"]["phases_seconds"]["submit_to_verdict"] is None

    summary = report["summary"]
    # ready_to_claim over A,B,C,U = 60,120,180,60 seconds.
    assert summary["ready_to_claim"] == {"count": 4, "median": 90.0, "p90": 162.0}
    # Verdict phases exist only for the gated three: 180, 360, 540.
    assert summary["submit_to_verdict"] == {"count": 3, "median": 360.0, "p90": 504.0}


def test_cycle_metrics_limit_keeps_the_most_recent(fk_off_service, actors) -> None:
    report = fk_off_service.cycle_metrics(actors["planner"], "project-1", 1)
    assert [ticket["ticket_id"] for ticket in report["tickets"]] == ["T-C"]
    with pytest.raises(ValidationError):
        fk_off_service.cycle_metrics(actors["planner"], "project-1", 0)


def test_cycle_metrics_is_served_over_rest(tmp_path: Path) -> None:
    app = create_app(database_path=tmp_path / "cm.db", local_mode=True, seed_demo=True)
    headers = {"X-HOH-Agent-Id": "ui-observer", "X-HOH-Role": "planner"}
    with TestClient(app) as client:
        response = client.get("/api/v1/projects/demo/cycle-metrics?limit=5", headers=headers)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["project_id"] == "demo"
    assert set(data["summary"]) >= {"ready_to_claim", "verdict_to_merge"}
