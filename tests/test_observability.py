"""Measuring the workflow without becoming part of it (HOH-227)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hoh_control_plane.api import create_app
from hoh_control_plane.domain import Actor, AuthorizationError, Role
from hoh_control_plane.observability import (
    WorkflowMetrics,
    correlation_scope,
    current_correlation_id,
    new_correlation_id,
)


# Anything a person wrote is content, and content must never reach a metric or a
# trace. These strings are planted in the workflow and then hunted for.
SECRET_OBJECTIVE = "an objective nobody should read from a metric surface"
SECRET_OBSERVATION = "an observation nobody should read from a trace"


def _metrics(service) -> WorkflowMetrics:
    return WorkflowMetrics(service.store)


def _events(service, project_id: str = "project-1") -> list[dict]:
    with service.store.connection() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM audit_events WHERE project_id = ? ORDER BY sequence",
                (project_id,),
            ).fetchall()
        ]


# --- correlation and causation ------------------------------------------------


def test_a_scope_marks_every_event_one_request_wrote(service, actors, seeded) -> None:
    ticket = seeded["ticket"]

    with correlation_scope() as correlation_id:
        service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)

    written = [e for e in _events(service) if e["correlation_id"] == correlation_id]
    assert written, "the claim's events carry the request's identifier"


def test_a_scope_does_not_leak_past_its_block(service, actors, seeded) -> None:
    with correlation_scope() as inner:
        assert current_correlation_id() == inner
    assert current_correlation_id() is None


def test_work_outside_any_scope_is_recorded_as_untraced_not_mislabelled(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]

    service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)

    claims = [e for e in _events(service) if e["event_type"] == "ticket.claimed"]
    assert claims and claims[-1]["correlation_id"] is None


def test_a_later_step_names_the_request_that_left_the_state_it_acted_on(
    service, actors, seeded
) -> None:
    """This is what links a chain across separate worker processes."""
    ticket = seeded["ticket"]

    with correlation_scope() as first:
        claim = service.claim_ticket(
            actors["developer"], ticket["ticket_id"], ticket["version"], 900
        )
    with correlation_scope() as second:
        service.append_progress(
            actors["developer"],
            ticket["ticket_id"],
            claim["lease"]["lease_id"],
            "A checkpoint written by a different request",
        )

    later = [e for e in _events(service) if e["correlation_id"] == second]
    assert later, "the second request wrote events"
    assert any(event["causation_id"] == first for event in later)


def test_sibling_events_from_one_request_are_not_called_cause_and_effect(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]

    with correlation_scope() as correlation_id:
        service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)

    written = [e for e in _events(service) if e["correlation_id"] == correlation_id]
    assert all(event["causation_id"] != correlation_id for event in written)


# --- retries must not move the numbers ---------------------------------------


def test_an_idempotent_replay_does_not_count_a_second_transition(
    service, actors, seeded
) -> None:
    """The reason metrics are derived from events rather than counted alongside them."""
    ticket = seeded["ticket"]
    key = "claim-retry-key-0001"

    first = service.run_idempotent(
        actors["developer"],
        operation="work.claim",
        idempotency_key=key,
        payload={"ticket_id": ticket["ticket_id"], "expected_version": ticket["version"]},
        run=lambda: service.claim_ticket(
            actors["developer"], ticket["ticket_id"], ticket["version"], 900
        ),
    )
    after_first = len([e for e in _events(service) if e["event_type"] == "ticket.claimed"])
    replay = service.run_idempotent(
        actors["developer"],
        operation="work.claim",
        idempotency_key=key,
        payload={"ticket_id": ticket["ticket_id"], "expected_version": ticket["version"]},
        run=lambda: service.claim_ticket(
            actors["developer"], ticket["ticket_id"], ticket["version"], 900
        ),
    )

    after_replay = len([e for e in _events(service) if e["event_type"] == "ticket.claimed"])
    assert replay["lease"]["lease_id"] == first["lease"]["lease_id"]
    assert after_replay == after_first, "a replay writes no new transition to count"


# --- the metric surface -------------------------------------------------------


def test_metrics_report_where_work_sits_and_how_it_fares(service, actors, seeded) -> None:
    ticket = seeded["ticket"]
    service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)

    report = _metrics(service).project_metrics(actors["planner"], "project-1")

    assert report["project_id"] == "project-1"
    assert report["throughput"]["tickets"] >= 1
    assert "by_status" in report["throughput"]
    assert "mean_wait_seconds" in report["waiting"]
    assert set(report) >= {
        "throughput",
        "waiting",
        "rework",
        "evidence",
        "leases",
        "context",
        "mainline",
    }
    assert report["leases"]["active"] == 1


def test_every_metric_value_is_an_aggregate_not_a_quotation(
    service, actors, seeded
) -> None:
    """A metric surface is read by tooling that has no confidentiality contract."""
    planner = actors["planner"]
    service.create_ticket(
        planner,
        feature_id="payment-retry",
        ticket_id="DEV-777",
        title="A ticket with a revealing objective",
        objective=SECRET_OBJECTIVE,
        required_role=Role.DEVELOPER,
        acceptance_criteria=["A criterion that must not be quoted"],
        preservation_constraints=["A constraint that must not be quoted"],
        file_scope=["src/secret/**"],
    )

    rendered = json.dumps(_metrics(service).project_metrics(planner, "project-1"))

    assert SECRET_OBJECTIVE not in rendered
    assert "must not be quoted" not in rendered
    assert "src/secret" not in rendered
    assert "DEV-777" not in rendered, "not even an identifier is needed to measure"


def test_metrics_refuse_a_project_outside_the_actor_scope(service, actors) -> None:
    outsider = Actor("outsider-01", Role.PLANNER, frozenset({"somewhere-else"}))

    with pytest.raises(AuthorizationError):
        _metrics(service).project_metrics(outsider, "project-1")


# --- traces -------------------------------------------------------------------


def test_a_trace_shows_what_moved_without_any_event_payload(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]
    with correlation_scope() as correlation_id:
        service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)

    trace = _metrics(service).trace(actors["planner"], "project-1", correlation_id)

    assert trace["events"], "the request's chain is visible"
    for event in trace["events"]:
        assert "payload" not in event
        assert set(event) <= {
            "sequence",
            "event_id",
            "event_type",
            "aggregate_type",
            "aggregate_id",
            "aggregate_version",
            "actor_role",
            "correlation_id",
            "causation_id",
            "occurred_at",
        }


def test_a_trace_carries_no_free_form_content(service, actors, seeded) -> None:
    ticket = seeded["ticket"]
    with correlation_scope() as correlation_id:
        claim = service.claim_ticket(
            actors["developer"], ticket["ticket_id"], ticket["version"], 900
        )
        service.report_blocker(
            actors["developer"],
            ticket["ticket_id"],
            claim["lease"]["lease_id"],
            claim["ticket"]["version"],
            SECRET_OBSERVATION,
        )

    rendered = json.dumps(_metrics(service).trace(actors["planner"], "project-1", correlation_id))

    assert SECRET_OBSERVATION not in rendered
    assert claim["lease"]["lease_id"] not in rendered, "a lease identifier is not trace material"


def test_traces_refuse_a_project_outside_the_actor_scope(service, actors, seeded) -> None:
    outsider = Actor("outsider-02", Role.PLANNER, frozenset({"somewhere-else"}))

    with pytest.raises(AuthorizationError):
        _metrics(service).trace(outsider, "project-1", new_correlation_id())


def test_an_unknown_correlation_traces_to_nothing_rather_than_erroring(
    service, actors, seeded
) -> None:
    trace = _metrics(service).trace(actors["planner"], "project-1", new_correlation_id())

    assert trace["events"] == []
    assert trace["elapsed_seconds"] is None


# --- the surfaced endpoints ---------------------------------------------------


def test_the_api_serves_metrics_and_returns_the_request_correlation(tmp_path: Path) -> None:
    app = create_app(database_path=tmp_path / "obs.db", local_mode=True, seed_demo=True)
    headers = {"X-HOH-Agent-Id": "ui-observer", "X-HOH-Role": "planner"}

    with TestClient(app) as client:
        response = client.get("/api/v1/projects/demo/metrics", headers=headers)

    assert response.status_code == 200
    assert response.json()["data"]["project_id"] == "demo"
    assert response.headers["x-hoh-correlation-id"].startswith("corr_")


def test_an_unauthorized_caller_cannot_read_detailed_traces(tmp_path: Path) -> None:
    app = create_app(
        database_path=tmp_path / "obs.db",
        local_mode=False,
        seed_demo=True,
        token_identities={},
    )

    with TestClient(app) as client:
        metrics = client.get("/api/v1/projects/demo/metrics")
        trace = client.get("/api/v1/projects/demo/traces/corr_whatever")

    assert metrics.status_code in (401, 403)
    assert trace.status_code in (401, 403)
