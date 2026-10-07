"""Review C3: one broken QA machine no longer blocks every ticket it touched.

QA infrastructure failures were routed per ticket, so a machine that had lost
its browser or its memory blocked N tickets, each with its own Planner job.
Failures are now grouped by (machine, failure_class): the first opens one job
and revokes the machine's approvals; while that job is open the machine takes
no QA work, and a later failure from it joins the job and returns its ticket to
QA instead of blocking it.
"""
from __future__ import annotations

import json

import pytest

from hoh_control_plane.domain import Actor, ConflictError, Role
from hoh_control_plane.service.planner_jobs import QA_MACHINE_MARK

HOST = "qa-host-a"


def _qa_ready(service, actors, ticket_id, scope):
    if ticket_id != "DEV-142":
        service.create_ticket(
            actors["planner"], "payment-retry", ticket_id, "Subject", "Objective",
            Role.DEVELOPER, ["Done"], [], [scope], ready=True,
        )
    version = service.get_ticket_context(actors["planner"], ticket_id)["ticket"]["version"]
    claimed = service.claim_ticket(actors["developer"], ticket_id, version, 900)
    return service.submit_candidate(
        actors["developer"], ticket_id, claimed["lease"]["lease_id"],
        claimed["ticket"]["version"], "a" * 40, "b" * 40, f"agent/dev-01/{ticket_id}",
        ["Ready for independent QA."],
        context_snapshot_id=service.get_ticket_context(actors["developer"], ticket_id)["context_snapshot_id"],
    )["ticket"]


@pytest.fixture
def two_claimed(service, actors, seeded):
    """One QA on one machine holds two QA leases when the machine breaks."""
    first = _qa_ready(service, actors, "DEV-142", "src/checkout/**")
    second = _qa_ready(service, actors, "DEV-B", "src/reporting/**")
    service.register_agent(actors["qa"], skills=["hoh-qa"], capacity=2, machine_id=HOST)
    service.claim_qa(actors["qa"], "DEV-142", first["version"], 900)
    service.claim_qa(actors["qa"], "DEV-B", second["version"], 900)
    return ["DEV-142", "DEV-B"]


def _report(service, actors, ticket_id, summary="Browser driver crashed on this host"):
    return service.report_execution_failure(
        actors["qa"], "project-1", ticket_id, "infrastructure", summary,
    )


def _open_jobs(service):
    with service.store.connection() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM planner_jobs WHERE status IN ('READY', 'IN_PROGRESS')"
        )]


def _status(service, ticket_id):
    with service.store.connection() as connection:
        return service._ticket(connection, ticket_id)["status"]


def test_failures_on_one_machine_share_one_job_and_block_one_ticket(service, actors, two_claimed):
    first = _report(service, actors, "DEV-142")
    second = _report(service, actors, "DEV-B", summary="Same host, same crash")

    [job] = _open_jobs(service)
    refs = json.loads(job["evidence_refs"])
    assert job["planner_job_id"] == first["planner_job"]["planner_job_id"]
    assert second["grouped_by_machine"] is True
    assert second["planner_job"]["planner_job_id"] == job["planner_job_id"]
    assert any(ref.startswith(QA_MACHINE_MARK) and ref.endswith(":infrastructure") for ref in refs)
    assert "ticket:DEV-B" in refs
    assert HOST not in job["evidence_refs"], "the machine is named by fingerprint only"
    assert _status(service, "DEV-142") == "BLOCKED"
    assert _status(service, "DEV-B") == "QA_READY"
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM leases WHERE ticket_id = 'DEV-B'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT target_role FROM work_queue WHERE ticket_id = 'DEV-B' AND status IN ('pending','offered')"
        ).fetchone()["target_role"] == "qa"


def test_the_failed_machine_is_quarantined_but_another_machine_takes_the_work(
    service, actors, two_claimed
):
    _report(service, actors, "DEV-142")
    _report(service, actors, "DEV-B")
    same_machine = Actor("qa-02", Role.QA, frozenset({"project-1"}))
    other_machine = Actor("qa-03", Role.QA, frozenset({"project-1"}))
    service.register_agent(same_machine, skills=["hoh-qa"], machine_id=" QA-HOST-A ")
    service.register_agent(other_machine, skills=["hoh-qa"], machine_id="qa-host-b")
    version = service.get_ticket_context(actors["planner"], "DEV-B")["ticket"]["version"]

    assert service.get_agent_inbox(same_machine, "project-1")["offers"] == []
    with pytest.raises(ConflictError) as refused:
        service.claim_qa(same_machine, "DEV-B", version, 900)
    assert refused.value.details["reason"] == "qa_machine_quarantined"

    offers = service.get_agent_inbox(other_machine, "project-1")["offers"]
    assert [offer["ticket"]["ticket_id"] for offer in offers] == ["DEV-B"]
    assert service.claim_qa(other_machine, "DEV-B", version, 900)["ticket"]["status"] == "QA_IN_PROGRESS"


def test_the_machine_approvals_are_revoked(service, actors, two_claimed):
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE agent_presence SET approved_execution_capabilities = '[\"build_execution\"]', "
            "capability_approved_machine_id = ?, capability_approved_by = 'planner-01' "
            "WHERE agent_id = ?",
            (HOST, actors["qa"].agent_id),
        )

    _report(service, actors, "DEV-142")

    with service.store.connection() as connection:
        presence = dict(connection.execute(
            "SELECT * FROM agent_presence WHERE agent_id = ?", (actors["qa"].agent_id,)
        ).fetchone())
    assert json.loads(presence["approved_execution_capabilities"]) == []
    assert presence["capability_approved_machine_id"] is None
    assert presence["capability_approved_by"] == "system:approval-revoked"


def test_resolving_the_machine_job_lifts_the_quarantine(service, actors, two_claimed):
    job = _report(service, actors, "DEV-142")["planner_job"]
    _report(service, actors, "DEV-B")
    lease = service.claim_planner_job(actors["planner"], job["planner_job_id"])["lease"]
    blocked = service.get_ticket_context(actors["planner"], "DEV-142")["ticket"]

    service.resume_ticket_after_failure(
        actors["planner"], job["planner_job_id"], lease["lease_id"], blocked["version"], "qa",
        "QA host repaired; retry the same candidate.",
    )

    version = service.get_ticket_context(actors["planner"], "DEV-B")["ticket"]["version"]
    assert service.claim_qa(actors["qa"], "DEV-B", version, 900)["ticket"]["status"] == "QA_IN_PROGRESS"


def test_a_runner_infrastructure_park_joins_the_machine_job(service, actors, two_claimed):
    job = _report(service, actors, "DEV-142")["planner_job"]
    with service.store.connection(immediate=True) as connection:
        ticket = service._ticket(connection, "DEV-B")
        lease = connection.execute("SELECT lease_id FROM leases WHERE ticket_id = 'DEV-B'").fetchone()
        service._park_qa_infrastructure(
            connection, actors["qa"], ticket,
            {"project_id": "project-1", "test_run_id": "testrun_0042", "lease_id": lease["lease_id"],
             "candidate_id": None, "candidate_commit": "a" * 40},
            "resource_exhausted", "evidence://local/run.json",
        )

    [open_job] = _open_jobs(service)
    assert open_job["planner_job_id"] == job["planner_job_id"]
    assert "test_run:testrun_0042" in json.loads(open_job["evidence_refs"])
    assert _status(service, "DEV-B") == "QA_READY"


def test_a_qa_without_a_machine_id_keeps_per_ticket_routing(service, actors, seeded):
    first = _qa_ready(service, actors, "DEV-142", "src/checkout/**")
    second = _qa_ready(service, actors, "DEV-B", "src/reporting/**")
    service.register_agent(actors["qa"], skills=["hoh-qa"], capacity=2)
    service.claim_qa(actors["qa"], "DEV-142", first["version"], 900)
    service.claim_qa(actors["qa"], "DEV-B", second["version"], 900)

    _report(service, actors, "DEV-142")
    _report(service, actors, "DEV-B")

    assert len(_open_jobs(service)) == 2
    assert _status(service, "DEV-142") == _status(service, "DEV-B") == "BLOCKED"


def test_a_source_failure_is_never_grouped_by_machine(service, actors, two_claimed):
    service.report_execution_failure(
        actors["qa"], "project-1", "DEV-142", "source_or_contract", "Checkout crashes",
    )
    service.report_execution_failure(
        actors["qa"], "project-1", "DEV-B", "source_or_contract", "Report is empty",
    )

    jobs = _open_jobs(service)
    assert len(jobs) == 2
    assert not any(QA_MACHINE_MARK in job["evidence_refs"] for job in jobs)
