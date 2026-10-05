"""H1 and wave 6F: the loop's own cycle time, read from events the workflow already writes.

Each phase is either waiting for the next role or that role's service time, and
the project-level figures say how long barriers held work and how long jobs
waited for a worker. Everything is reported as p50/p90.
"""
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


def _at(minute: int, second: int = 0) -> str:
    return f"2026-09-01T10:{minute:02d}:{second:02d}Z"


def _deliver(connection, ticket_id, minutes: list[int]) -> None:
    """ready, Developer claim, freeze, QA claim, verdict, Integrator claim, merged."""
    ready, claim, submit, qa_claim, verdict, integrator_claim, merge = map(_at, minutes)
    _event(connection, ticket_id, "ticket.ready", ready)
    _event(connection, ticket_id, "ticket.claimed", claim, role="qa")  # ignored: not Developer
    _event(connection, ticket_id, "ticket.claimed", claim, role="developer")
    _event(connection, ticket_id, "candidate.frozen", submit, role="developer")
    _event(connection, ticket_id, "qa.started", qa_claim, role="qa")
    _event(connection, ticket_id, "ticket.verified", verdict, role="qa")
    _event(connection, ticket_id, "integration.started", integrator_claim, role="integrator")
    _event(connection, ticket_id, "integration.completed", merge, role="integrator")


def _land_ungated(connection, ticket_id, minutes: list[int]) -> None:
    ready, claim, submit, landed = map(_at, minutes)
    _event(connection, ticket_id, "ticket.ready", ready)
    _event(connection, ticket_id, "ticket.claimed", claim, role="developer")
    _event(connection, ticket_id, "candidate.frozen", submit, role="developer")
    _event(connection, ticket_id, "integration.landed_ungated", landed, role="integrator")


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


def _qa_job(connection, test_run_id, created, started, status="COMPLETED") -> None:
    connection.execute(
        """
        INSERT INTO qa_test_jobs
            (test_run_id, project_id, candidate_id, ticket_id, lease_id, test_command,
             command_digest, timeout_seconds, worktree_path, candidate_commit,
             requested_by, status, created_at, started_at)
        VALUES (?, 'project-1', 'cand-a', 'T-A', 'l', '[]', 'd', 60, 'w', 'c', 'qa',
                ?, ?, ?)
        """,
        (test_run_id, status, created, started),
    )


def _reconciliation(connection, identifier, scope, status, created, resolved=None,
                    resolved_by="planner-01") -> None:
    connection.execute(
        """
        INSERT INTO repository_reconciliations
            (reconciliation_id, project_id, observation_ref, observed_commits,
             ticket_versions, status, created_at, resolved_by, resolved_at, scope)
        VALUES (?, 'project-1', ?, '[]', '{}', ?, ?, ?, ?, ?)
        """,
        (identifier, f"seed:{identifier}", status, created,
         resolved_by if status == "RESOLVED" else None, resolved, scope),
    )


def _seed_body(connection) -> None:
    _deliver(connection, "T-A", [0, 1, 3, 4, 6, 7, 10])
    _deliver(connection, "T-B", [0, 2, 6, 8, 12, 15, 20])
    _deliver(connection, "T-C", [0, 3, 9, 12, 18, 22, 30])
    _land_ungated(connection, "T-U", [0, 1, 2, 4])
    _event(connection, "T-OPEN", "ticket.ready", _at(0))
    # Two QA suite runs and one gate run on T-A; the gate run has no QA job.
    for run in ("run-0", "run-1", "run-gate"):
        connection.execute(
            """
            INSERT INTO qa_test_runs (test_run_id, candidate_id, ticket_id, command_digest,
                exit_code, duration_ms, storage_ref, report_path, executed_by, created_at)
            VALUES (?, 'cand-a', 'T-A', 'd', 0, 1, 's', 'r', 'qa', ?)
            """,
            (run, _at(5)),
        )
    _qa_job(connection, "run-0", _at(3), _at(3, 30))
    _qa_job(connection, "run-1", _at(4), _at(5))
    _qa_job(connection, "run-waiting", _at(9), None, status="QUEUED")
    for job, mode, created, started in (
        ("mj-1", "executed", _at(7), _at(9)),
        ("mj-2", "reused_qa_run", _at(7), _at(7, 5)),
    ):
        connection.execute(
            """
            INSERT INTO merge_jobs (merge_job_id, project_id, ticket_id, lease_id,
                expected_version, candidate_id, requested_by, status, created_at, started_at)
            VALUES (?, 'project-1', 'T-A', 'l', 1, 'cand-a', 'i', 'COMPLETED', ?, ?)
            """,
            (job, created, started),
        )
        _event(connection, "T-A", "integration.gate_passed", _at(8),
               role="integrator", payload={"gate_mode": mode},
               aggregate_type="merge_job", aggregate_id=job)
    _reconciliation(connection, "recon-p", "project", "RESOLVED", _at(0), _at(10))
    _reconciliation(connection, "recon-t", "tickets", "RESOLVED", _at(0), _at(1))
    _reconciliation(connection, "recon-auto", "project", "RESOLVED", _at(2), _at(2),
                    resolved_by="system:scope-disjoint")
    _reconciliation(connection, "recon-open", "project", "PENDING", _at(30))


def test_cycle_metrics_split_waiting_from_service(fk_off_service, actors) -> None:
    report = fk_off_service.cycle_metrics(actors["planner"], "project-1", 10)

    assert report["delivered_tickets"] == 4
    by_id = {ticket["ticket_id"]: ticket for ticket in report["tickets"]}
    assert "T-OPEN" not in by_id
    a = by_id["T-A"]
    assert a["phases_seconds"] == {
        "ready_to_dev_claim": 60.0, "dev_claim_to_submit": 120.0,
        "submit_to_qa_claim": 60.0, "qa_claim_to_verdict": 120.0,
        "verdict_to_integrator_claim": 60.0, "integrator_claim_to_merged": 180.0,
    }
    assert (a["wait_seconds"], a["service_seconds"], a["ready_to_merged_seconds"]) == (
        180.0, 420.0, 600.0
    )
    assert a["suite_runs"] == {"qa": 2, "gate": 1, "total": 3}
    assert a["gate_runs"] == {"executed": 1, "reused": 1}
    ungated = by_id["T-U"]
    assert ungated["gated"] is False
    assert ungated["phases_seconds"]["qa_claim_to_verdict"] is None
    assert (ungated["wait_seconds"], ungated["service_seconds"]) == (60.0, 60.0)

    summary = report["summary"]
    # ready_to_dev_claim over A,B,C,U = 60,120,180,60 seconds.
    assert summary["ready_to_dev_claim"] == {"kind": "wait", "count": 4, "p50": 90.0, "p90": 162.0}
    # QA service exists only for the gated three: 120, 240, 360.
    assert summary["qa_claim_to_verdict"] == {
        "kind": "service", "count": 3, "p50": 240.0, "p90": 336.0,
    }
    assert summary["wait_seconds"]["count"] == 4
    # Suite runs per landed ticket: 3, 0, 0, 0.
    assert summary["suite_runs_per_landed_ticket"] == {
        "count": 4, "p50": 0.0, "p90": 2.1, "total": 3,
    }


def test_barrier_open_time_counts_only_what_planner_held(fk_off_service, actors) -> None:
    barriers = fk_off_service.cycle_metrics(actors["planner"], "project-1")["barrier_open_seconds"]

    # 600 s project-wide and 60 s ticket-scoped; the auto-kept merge held nothing.
    assert (barriers["count"], barriers["p50"], barriers["p90"]) == (2, 330.0, 546.0)
    assert barriers["by_scope"] == {
        "project": {"count": 1, "p50": 600.0, "p90": 600.0},
        "tickets": {"count": 1, "p50": 60.0, "p90": 60.0},
    }
    assert barriers["auto_resolved"] == 1
    assert barriers["pending"] == 1
    assert barriers["oldest_pending_seconds"] > 0


def test_job_queue_age_is_reported_per_kind_and_gate_mode(fk_off_service, actors) -> None:
    queues = fk_off_service.cycle_metrics(actors["planner"], "project-1")["job_queue_seconds"]

    assert queues["qa"] == {"count": 2, "p50": 45.0, "p90": 57.0, "queued_now": 1}
    merge = queues["merge"]
    assert (merge["count"], merge["p50"], merge["p90"], merge["queued_now"]) == (2, 62.5, 108.5, 0)
    assert merge["by_gate_mode"] == {
        "executed": {"count": 1, "p50": 120.0, "p90": 120.0},
        "reused_qa_run": {"count": 1, "p50": 5.0, "p90": 5.0},
    }


def test_a_rework_round_counts_as_developer_service_time(service) -> None:
    events = [
        {"event_type": kind, "actor_role": role, "occurred_at": _at(minute)}
        for kind, role, minute in (
            ("ticket.ready", "planner", 0),
            ("ticket.claimed", "developer", 1),
            ("candidate.frozen", "developer", 2),
            ("qa.started", "qa", 3),
            ("ticket.claimed", "developer", 5),
            ("candidate.frozen", "developer", 6),
            ("qa.started", "qa", 7),
            ("ticket.verified", "qa", 8),
            ("integration.started", "integrator", 9),
            ("integration.completed", "integrator", 10),
        )
    ]

    markers = service._cycle_markers(events)

    # The first round's QA claim belongs to a candidate that was replaced.
    assert markers == {
        "ready": _at(0), "dev_claim": _at(1), "submit": _at(6), "qa_claim": _at(7),
        "verdict": _at(8), "integrator_claim": _at(9), "delivered": _at(10),
    }


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
    assert set(data["summary"]) >= {"ready_to_dev_claim", "integrator_claim_to_merged"}
    assert {"barrier_open_seconds", "job_queue_seconds", "barrier_shadow"} <= set(data)
