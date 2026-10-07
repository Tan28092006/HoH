"""``work.report_failure``: one durable execution failure reaches Planner once (review F2).

AGENTS.md makes reporting one unhandled failure before going idle a protocol
duty of every execution role, and nothing tested the tool. These cover who may
report, what proves the failure belongs to the reporter (a live lease or a
terminal worker job it owns), what the report does to the ticket, and that a
repeated report reuses the first one.
"""
from __future__ import annotations

import pytest

from hoh_control_plane.domain import (
    Actor,
    AuthorizationError,
    ConflictError,
    Role,
    ValidationError,
)
from hoh_control_plane.mcp_server import create_mcp_server


def _ticket(service, ticket_id: str = "DEV-142") -> dict:
    with service.store.connection() as connection:
        return service._ticket(connection, ticket_id)


def _claimed(service, actors, who: str = "developer") -> dict:
    return service.claim_ticket(actors[who], "DEV-142", _ticket(service)["version"], 900)


def _report(service, actor, **overrides):
    arguments = {
        "project_id": "project-1", "ticket_id": "DEV-142",
        "failure_class": "source_or_contract",
        "summary": "The payment contract rejects every retry with HTTP 409.",
    }
    arguments.update(overrides)
    return service.report_execution_failure(actor, **arguments)


def _failed_submission(service, actors, claim) -> str:
    queued = service.queue_candidate_submission(
        actors["developer"], "DEV-142", claim["lease"]["lease_id"], claim["ticket"]["version"],
        "a" * 40, "b" * 40, "agent/dev-01/DEV-142", ["Ready."],
        context_snapshot_id=service.get_ticket_context(
            actors["developer"], "DEV-142")["context_snapshot_id"],
    )
    submission_id = queued["submission"]["submission_id"]
    failed = service.fail_queued_candidate_submission(submission_id, "Git validation failed twice")
    assert failed["status"] == "FAILED"
    return submission_id


def _risk_entries(service) -> list[dict]:
    with service.store.connection() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM context_entries WHERE ticket_id = 'DEV-142' AND entry_type = 'risk'"
        ).fetchall()]


def test_a_developer_failure_under_a_live_lease_blocks_the_ticket_for_planner(
    service, actors, seeded
) -> None:
    _claimed(service, actors)

    reported = _report(service, actors["developer"], evidence_refs=["evidence://dev/log.txt"])

    ticket = reported["ticket"]
    assert ticket["status"] == "BLOCKED"
    assert ticket["blocked_reason"] == "The payment contract rejects every retry with HTTP 409."
    assert reported["deduplicated"] is False
    assert reported["next_action"] == "wait_for_planner_direction"
    job = reported["planner_job"]
    assert job["job_type"] == "developer_blocker"
    assert job["status"] == "READY"
    entry_id = reported["failure_context"]["entry_id"]
    assert job["evidence_refs"][0] == f"context:{entry_id}"
    assert "evidence://dev/log.txt" in job["evidence_refs"]
    assert reported["failure_context"]["content"].startswith("source_or_contract: ")
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM leases WHERE ticket_id = 'DEV-142'"
        ).fetchone()[0] == 0


def test_a_failed_worker_job_is_reported_once_however_often_it_is_sent(
    service, actors, seeded
) -> None:
    claim = _claimed(service, actors)
    submission_id = _failed_submission(service, actors, claim)

    first = _report(service, actors["developer"], failure_class="infrastructure",
                    job_kind="candidate_submission", job_id=submission_id)
    again = _report(service, actors["developer"], failure_class="infrastructure",
                    job_kind="candidate_submission", job_id=submission_id)

    assert first["ticket"]["status"] == "BLOCKED"
    assert f"candidate_submission_job:{submission_id}" in first["planner_job"]["evidence_refs"]
    assert again["deduplicated"] is True
    assert again["planner_job"]["planner_job_id"] == first["planner_job"]["planner_job_id"]
    assert len(_risk_entries(service)) == 1
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM planner_jobs WHERE ticket_id = 'DEV-142'"
        ).fetchone()[0] == 1


@pytest.mark.parametrize("who,job_type", [("qa", "qa_failure"), ("integrator", "integration_conflict")])
def test_each_execution_role_opens_its_own_kind_of_planner_job(
    service, actors, seeded, who, job_type
) -> None:
    # A ticket that role holds a lease on: QA claims a submitted candidate; an
    # integrator's lease is planted directly, since the merge path needs Git.
    claim = _claimed(service, actors)
    submitted = service.submit_candidate(
        actors["developer"], "DEV-142", claim["lease"]["lease_id"], claim["ticket"]["version"],
        "a" * 40, "b" * 40, "agent/dev-01/DEV-142", ["Ready."],
        context_snapshot_id=service.get_ticket_context(
            actors["developer"], "DEV-142")["context_snapshot_id"],
    )
    if who == "qa":
        service.claim_qa(actors["qa"], "DEV-142", submitted["ticket"]["version"], 900)
    else:
        with service.store.connection(immediate=True) as connection:
            connection.execute(
                "INSERT INTO leases (lease_id, ticket_id, agent_id, role, acquired_at, heartbeat_at, expires_at) "
                "VALUES ('lease_int', 'DEV-142', 'integrator-01', 'integrator', "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2999-01-01T00:00:00Z')"
            )

    reported = _report(service, actors[who], failure_class="infrastructure")

    assert reported["planner_job"]["job_type"] == job_type
    assert reported["ticket"]["status"] == "BLOCKED"


def test_planner_cannot_report_an_execution_failure(service, actors, seeded) -> None:
    with pytest.raises(AuthorizationError):
        _report(service, actors["planner"])


@pytest.mark.parametrize("overrides,message", [
    ({"failure_class": "flaky"}, "failure_class"),
    ({"summary": ""}, "summary"),
    ({"summary": "x" * 4001}, "summary"),
    ({"evidence_refs": ["ref"] * 51}, "evidence_refs"),
    ({"job_kind": "merge"}, "together"),
    ({"job_id": "merge_job_1"}, "together"),
    ({"job_kind": "deploy", "job_id": "x"}, "job_kind"),
])
def test_malformed_reports_are_refused_and_change_nothing(
    service, actors, seeded, overrides, message
) -> None:
    _claimed(service, actors)
    with pytest.raises(ValidationError, match=message):
        _report(service, actors["developer"], **overrides)
    assert _ticket(service)["status"] == "DEV_IN_PROGRESS"
    assert _risk_entries(service) == []


def test_a_report_needs_the_reporters_own_live_lease(service, actors, seeded) -> None:
    with pytest.raises(ConflictError, match="live ticket lease"):
        _report(service, actors["developer"])
    _claimed(service, actors, "developer")
    with pytest.raises(ConflictError, match="live ticket lease"):
        _report(service, actors["developer_2"])
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE leases SET expires_at = '2000-01-01T00:00:00Z' WHERE ticket_id = 'DEV-142'"
        )
    with pytest.raises(ConflictError, match="live ticket lease"):
        _report(service, actors["developer"])
    assert _risk_entries(service) == []


def test_an_active_worker_must_be_polled_to_its_result_first(service, actors, seeded) -> None:
    claim = _claimed(service, actors)
    service.queue_candidate_submission(
        actors["developer"], "DEV-142", claim["lease"]["lease_id"], claim["ticket"]["version"],
        "a" * 40, "b" * 40, "agent/dev-01/DEV-142", ["Ready."],
        context_snapshot_id=service.get_ticket_context(
            actors["developer"], "DEV-142")["context_snapshot_id"],
    )
    with pytest.raises(ConflictError, match="Poll the active worker"):
        _report(service, actors["developer"])


def test_a_job_proves_ownership_only_when_it_is_the_reporters_and_terminal(
    service, actors, seeded
) -> None:
    claim = _claimed(service, actors)
    queued = service.queue_candidate_submission(
        actors["developer"], "DEV-142", claim["lease"]["lease_id"], claim["ticket"]["version"],
        "a" * 40, "b" * 40, "agent/dev-01/DEV-142", ["Ready."],
        context_snapshot_id=service.get_ticket_context(
            actors["developer"], "DEV-142")["context_snapshot_id"],
    )
    submission_id = queued["submission"]["submission_id"]
    with pytest.raises(ConflictError, match="terminal failed"):
        _report(service, actors["developer"], job_kind="candidate_submission", job_id=submission_id)
    service.fail_queued_candidate_submission(submission_id, "Git validation failed")
    with pytest.raises(AuthorizationError):
        _report(service, actors["developer_2"], job_kind="candidate_submission", job_id=submission_id)
    with pytest.raises(AuthorizationError):
        _report(service, actors["developer"], job_kind="merge", job_id=submission_id)
    with pytest.raises(AuthorizationError):
        _report(service, actors["developer"], job_kind="candidate_submission", job_id="candidate_job_x")
    assert _report(service, actors["developer"], job_kind="candidate_submission",
                   job_id=submission_id)["ticket"]["status"] == "BLOCKED"


def test_a_planner_retirement_is_not_an_execution_failure(service, actors, seeded) -> None:
    claim = _claimed(service, actors)
    submission_id = _failed_submission(service, actors, claim)
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE candidate_submission_jobs SET error_message = ? WHERE submission_id = ?",
            ("Planner retired the ticket while validation ran", submission_id),
        )
    with pytest.raises(ConflictError, match="retirement"):
        _report(service, actors["developer"], job_kind="candidate_submission", job_id=submission_id)


def test_the_ticket_must_belong_to_the_named_project(service, actors, seeded) -> None:
    planner = Actor("planner-01", Role.PLANNER, frozenset({"project-1", "project-2"}))
    service.create_project(planner, project_id="project-2", name="Other", description="",
                           default_branch="main")
    developer = Actor("dev-01", Role.DEVELOPER, frozenset({"project-1", "project-2"}))
    _claimed(service, actors)
    with pytest.raises(ValidationError, match="does not belong"):
        _report(service, developer, project_id="project-2")
    with pytest.raises(AuthorizationError):
        _report(service, actors["developer"], project_id="project-2")


@pytest.mark.anyio
async def test_only_execution_roles_get_the_tool_and_it_runs_through_invoke(
    service, actors, seeded
) -> None:
    planner_tools = {tool.name for tool in await create_mcp_server(service, actors["planner"]).list_tools()}
    assert "work.report_failure" not in planner_tools
    for role in ("developer", "qa", "integrator"):
        tools = {tool.name for tool in await create_mcp_server(service, actors[role]).list_tools()}
        assert "work.report_failure" in tools

    _claimed(service, actors)
    server = create_mcp_server(service, actors["developer"])
    arguments = {
        "project_id": "project-1", "ticket_id": "DEV-142", "failure_class": "dependency",
        "summary": "The payments sandbox has been down for an hour.",
        "idempotency_key": "report-1",
    }
    _content, first = await server.call_tool("work.report_failure", arguments)
    _content, replay = await server.call_tool("work.report_failure", arguments)

    assert first["ticket"]["status"] == "BLOCKED"
    assert first["failure_context"]["content"].startswith("dependency: ")
    assert replay["planner_job"]["planner_job_id"] == first["planner_job"]["planner_job_id"]
    assert len(_risk_entries(service)) == 1
