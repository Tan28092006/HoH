from __future__ import annotations

import pytest

from hoh_control_plane.domain import Actor, AuthorizationError, ConflictError, Role
from hoh_control_plane.mcp_server import create_mcp_server


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


def _blocked_ticket(service, actors, seeded):
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    return service.report_blocker(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
        "The upstream payment contract is undecided.",
    )


def _qa_failed_ticket(service, actors, seeded):
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    candidate = service.submit_candidate(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
        "a" * 40,
        "b" * 40,
        "agent/dev-01/DEV-142",
        ["Ready for QA."],
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
    )


def test_a_developer_blocker_creates_a_linked_planner_job(service, actors, seeded) -> None:
    blocked = _blocked_ticket(service, actors, seeded)

    job = blocked["planner_job"]

    assert job["job_type"] == "developer_blocker"
    assert job["status"] == "READY"
    assert job["ticket_id"] == seeded["ticket"]["ticket_id"]
    assert job["summary"] == "The upstream payment contract is undecided."
    assert job["context_entry_id"] == blocked["context_entry_id"]
    assert job["evidence_refs"] == [f"context:{blocked['context_entry_id']}"]
    assert blocked["ticket"]["status"] == "BLOCKED"


def test_a_qa_failure_creates_a_planner_job_linked_to_candidate_and_evidence(
    service, actors, seeded
) -> None:
    failed = _qa_failed_ticket(service, actors, seeded)

    job = failed["planner_job"]
    candidate_id = failed["candidate"]["candidate_id"]

    assert job["job_type"] == "qa_failure"
    assert job["status"] == "READY"
    assert job["candidate_id"] == candidate_id
    assert f"candidate:{candidate_id}" in job["evidence_refs"]
    assert any(ref.startswith("finding:") for ref in job["evidence_refs"])
    assert any(ref.startswith("context:") for ref in job["evidence_refs"])
    assert failed["ticket"]["status"] == "REWORK"


def test_a_passing_verdict_creates_no_planner_job(service, actors, seeded) -> None:
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    candidate = service.submit_candidate(
        actors["developer"], ticket["ticket_id"], claim["lease"]["lease_id"],
        claim["ticket"]["version"], "a" * 40, "b" * 40, "agent/dev-01/DEV-142", ["Ready."],
    )
    qa_claim = service.claim_qa(
        actors["qa"], ticket["ticket_id"], candidate["ticket"]["version"], 900
    )

    passed = service.submit_qa_verdict(
        actors["qa"],
        ticket["ticket_id"],
        qa_claim["lease"]["lease_id"],
        candidate["candidate"]["candidate_id"],
        qa_claim["ticket"]["version"],
        "passed",
        [
            {
                "criterion": criterion,
                "status": "verified",
                "severity": "none",
                "observation": "Verified against the frozen candidate.",
                "evidence": [
                    {
                        "type": "test_result",
                        "observation": "passed",
                        "storage_ref": f"evidence://qa/{index}.json",
                    }
                ],
            }
            for index, criterion in enumerate(ticket["acceptance_criteria"], start=1)
        ],
    )

    assert passed["planner_job"] is None
    assert service.discover_planner_jobs(actors["planner"], "project-1") == []


def test_only_one_planner_holds_the_lease_for_one_job(service, actors, seeded) -> None:
    job = _blocked_ticket(service, actors, seeded)["planner_job"]
    second_planner = Actor("planner-02", Role.PLANNER, frozenset({"project-1"}))

    claimed = service.claim_planner_job(actors["planner"], job["planner_job_id"])

    with pytest.raises(ConflictError, match="already leased"):
        service.claim_planner_job(second_planner, job["planner_job_id"])
    assert claimed["planner_job"]["status"] == "IN_PROGRESS"
    assert claimed["lease"]["agent_id"] == actors["planner"].agent_id


def test_an_expired_planner_lease_returns_the_job_to_the_ready_queue(
    service, actors, seeded
) -> None:
    job = _blocked_ticket(service, actors, seeded)["planner_job"]
    second_planner = Actor("planner-02", Role.PLANNER, frozenset({"project-1"}))
    claimed = service.claim_planner_job(actors["planner"], job["planner_job_id"])
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE planner_job_leases SET expires_at = ? WHERE lease_id = ?",
            ("2000-01-01T00:00:00Z", claimed["lease"]["lease_id"]),
        )

    retaken = service.claim_planner_job(second_planner, job["planner_job_id"])

    assert retaken["lease"]["agent_id"] == "planner-02"
    assert retaken["planner_job"]["status"] == "IN_PROGRESS"


def test_resolving_closes_the_job_and_releases_its_lease(service, actors, seeded) -> None:
    job = _blocked_ticket(service, actors, seeded)["planner_job"]
    claimed = service.claim_planner_job(actors["planner"], job["planner_job_id"])

    resolved = service.resolve_planner_job(
        actors["planner"],
        job["planner_job_id"],
        claimed["lease"]["lease_id"],
        "Contract decided; ticket re-scoped and made ready.",
    )

    assert resolved["planner_job"]["status"] == "RESOLVED"
    assert resolved["planner_job"]["lease"] is None
    assert service.discover_planner_jobs(actors["planner"], "project-1") == []
    assert (
        service.discover_planner_jobs(actors["planner"], "project-1", include_resolved=True)[0][
            "resolution"
        ]
        == "Contract decided; ticket re-scoped and made ready."
    )


def test_another_planners_lease_cannot_resolve_the_job(service, actors, seeded) -> None:
    job = _blocked_ticket(service, actors, seeded)["planner_job"]
    claimed = service.claim_planner_job(actors["planner"], job["planner_job_id"])
    impostor = Actor("planner-02", Role.PLANNER, frozenset({"project-1"}))

    with pytest.raises(AuthorizationError):
        service.resolve_planner_job(
            impostor, job["planner_job_id"], claimed["lease"]["lease_id"], "Not mine."
        )


def test_execution_roles_cannot_claim_planning_work(service, actors, seeded) -> None:
    job = _blocked_ticket(service, actors, seeded)["planner_job"]

    for role_key in ("developer", "qa", "integrator"):
        with pytest.raises(AuthorizationError):
            service.claim_planner_job(actors[role_key], job["planner_job_id"])


def test_new_evidence_updates_the_open_job_instead_of_queueing_a_second_planner(
    service, actors, seeded
) -> None:
    """One ticket carries one open planning problem."""
    blocked = _blocked_ticket(service, actors, seeded)
    ticket_id = seeded["ticket"]["ticket_id"]
    ready = service.set_ticket_ready(
        actors["planner"], ticket_id, blocked["ticket"]["version"]
    )
    second = service.report_blocker(
        actors["developer"],
        ticket_id,
        service.claim_ticket(
            actors["developer"], ticket_id, ready["ticket"]["version"], 900
        )["lease"]["lease_id"],
        ready["ticket"]["version"] + 1,
        "Still undecided after the follow-up review.",
    )

    jobs = service.discover_planner_jobs(actors["planner"], "project-1")

    assert len(jobs) == 1
    assert jobs[0]["planner_job_id"] == blocked["planner_job"]["planner_job_id"]
    assert jobs[0]["summary"] == "Still undecided after the follow-up review."
    assert len(jobs[0]["evidence_refs"]) == 2
    assert second["planner_job"]["planner_job_id"] == blocked["planner_job"]["planner_job_id"]


def test_planner_discovery_separates_planning_jobs_from_executable_tickets(
    service, actors, seeded
) -> None:
    _blocked_ticket(service, actors, seeded)

    planner_view = service.discover_work(actors["planner"], "project-1")
    developer_view = service.discover_work(actors["developer"], "project-1")

    planner_rollup = service.discover_work(actors["planner"], "project-1")
    assert len(planner_rollup["planning_jobs"]) == 1, (
        "the default Planner call must still surface the planning queue"
    )

    planner_view = service.discover_work(actors["planner"], "project-1", detail=True)
    planned_ticket_ids = {
        ticket["ticket_id"]
        for feature in planner_view["features"]
        for ticket in feature["tickets"]
    }

    assert len(planner_view["planning_jobs"]) == 1
    assert planner_view["planning_jobs"][0]["job_type"] == "developer_blocker"
    # The planning decision is its own queue entry, never an executable ticket row.
    assert "planner_job_id" not in planned_ticket_ids
    assert all("job_type" not in ticket for feature in planner_view["features"] for ticket in feature["tickets"])
    # A blocked ticket is nobody's executable work, and execution roles get no planning queue.
    assert developer_view["features"] == []
    assert "planning_jobs" not in developer_view


def test_planner_queue_does_not_disturb_the_developer_and_qa_queues(
    service, actors, seeded
) -> None:
    """Creating planning work must not change existing dispatch behavior."""
    failed = _qa_failed_ticket(service, actors, seeded)
    service.register_agent(actors["developer_2"], skills=["hoh-developer"], capacity=1)

    inbox = service.get_agent_inbox(actors["developer_2"], "project-1")

    assert failed["ticket"]["status"] == "REWORK"
    assert [offer["ticket"]["ticket_id"] for offer in inbox["offers"]] == [
        seeded["ticket"]["ticket_id"]
    ]
    assert inbox["offers"][0]["target_role"] == "developer"


def test_planning_work_grants_no_artifact_or_merge_permission(service, actors, seeded) -> None:
    job = _blocked_ticket(service, actors, seeded)["planner_job"]
    service.claim_planner_job(actors["planner"], job["planner_job_id"])

    capabilities = service.capabilities(actors["planner"])

    assert "candidate:submit" not in capabilities
    assert "candidate:integrate" not in capabilities
    assert "merge:record" not in capabilities
    assert "verdict:submit" not in capabilities


@pytest.mark.anyio
async def test_planner_mcp_claims_and_resolves_one_job(service, actors, seeded) -> None:
    job = _blocked_ticket(service, actors, seeded)["planner_job"]
    server = create_mcp_server(service, actors["planner"])

    names = {tool.name for tool in await server.list_tools()}
    _, claimed = await server.call_tool(
        "planner.claim_job", {"planner_job_id": job["planner_job_id"]}
    )
    _, resolved = await server.call_tool(
        "planner.resolve_job",
        {
            "planner_job_id": job["planner_job_id"],
            "lease_id": claimed["lease"]["lease_id"],
            "resolution": "Re-scoped and returned to the backlog.",
        },
    )

    assert {"planner.claim_job", "planner.resolve_job"}.issubset(names)
    assert claimed["planner_job"]["status"] == "IN_PROGRESS"
    assert resolved["planner_job"]["status"] == "RESOLVED"
    developer_names = {
        tool.name for tool in await create_mcp_server(service, actors["developer"]).list_tools()
    }
    assert "planner.claim_job" not in developer_names
