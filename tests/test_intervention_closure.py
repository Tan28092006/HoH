"""Review C4: a wake-up request closes when its Planner job resolves.

planner_interventions stayed PENDING or DELIVERED after their job was
resolved, so dispatch_overview and the adapter queue listed problems that no
longer existed, and operators learned to ignore them. Every resolution path
now closes the job's request.
"""
from __future__ import annotations

from helpers.capability import CAPABILITIES, _open_jobs, blocked  # noqa: F401
from helpers.planner import _blocked_ticket

from hoh_control_plane.domain import Actor, Role
from hoh_control_plane.service.planner_jobs import JOB_RESOLVED_ACKNOWLEDGER


def _interventions(service, planner_job_id):
    with service.store.connection() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM planner_interventions WHERE planner_job_id = ?", (planner_job_id,)
        )]


def _assert_closed(service, actors, planner_job_id):
    [intervention] = _interventions(service, planner_job_id)
    assert intervention["status"] == "ACKNOWLEDGED"
    assert intervention["acknowledged_by"] == JOB_RESOLVED_ACKNOWLEDGER
    assert planner_job_id not in {
        item["planner_job_id"]
        for item in service.list_planner_interventions(actors["planner"], "project-1")
    }
    assert planner_job_id not in {
        item["planner_job_id"] for item in service.pending_planner_interventions()
    }


def test_resolving_a_job_closes_its_wakeup(service, actors, seeded):
    job = _blocked_ticket(service, actors, seeded)["planner_job"]
    assert _interventions(service, job["planner_job_id"])[0]["status"] == "PENDING"
    lease = service.claim_planner_job(actors["planner"], job["planner_job_id"])["lease"]

    service.resolve_planner_job(actors["planner"], job["planner_job_id"], lease["lease_id"], "Decided.")

    _assert_closed(service, actors, job["planner_job_id"])


def test_resuming_after_a_failure_closes_its_wakeup(service, actors, seeded):
    reported = _blocked_ticket(service, actors, seeded)
    job = reported["planner_job"]
    service.record_intervention_delivery(
        _interventions(service, job["planner_job_id"])[0]["intervention_id"], delivered=True
    )
    lease = service.claim_planner_job(actors["planner"], job["planner_job_id"])["lease"]

    service.resume_ticket_after_failure(
        actors["planner"], job["planner_job_id"], lease["lease_id"],
        reported["ticket"]["version"], "developer", "Contract decided.",
    )

    _assert_closed(service, actors, job["planner_job_id"])


def test_an_open_job_keeps_its_wakeup(service, actors, seeded):
    job = _blocked_ticket(service, actors, seeded)["planner_job"]
    service.claim_planner_job(actors["planner"], job["planner_job_id"])

    assert _interventions(service, job["planner_job_id"])[0]["status"] == "PENDING"


def test_a_capability_approval_closes_the_review_wakeup(service, actors, blocked):
    blocked["prober"].equipped = {"build_execution"}
    service.run_capability_preflight(actors["qa"], "project-1")
    [review] = _open_jobs(service, blocked["ticket_id"])

    blocked["prober"].equipped = set(CAPABILITIES)
    service.run_capability_preflight(actors["qa"], "project-1")

    _assert_closed(service, actors, review["planner_job_id"])


def test_an_acknowledged_delivery_report_closes_its_wakeup(service, actors, seeded):
    with service.store.connection(immediate=True) as connection:
        report = service._report_delivery_completed(
            connection, actors["planner"], "project-1", seeded["ticket"],
            candidate_id=None, mainline_commit="c" * 40,
        )
        service._close_acknowledged_delivery_report(
            connection, actors["planner"], report["planner_job_id"]
        )

    _assert_closed(service, actors, report["planner_job_id"])


def test_a_finalized_retirement_closes_its_jobs_wakeups(service, actors, seeded):
    job = _blocked_ticket(service, actors, seeded)["planner_job"]
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """INSERT INTO repository_reconciliations
                   (reconciliation_id, project_id, observation_ref, observed_commits,
                    ticket_versions, status, created_at)
               VALUES ('recon_c4', 'project-1', 'git:observed:c4', '[]', '{}', 'RESOLVED',
                       '2026-10-06T00:00:00Z')"""
        )
        connection.execute(
            """INSERT INTO ticket_retirement_requests
                   (request_id, project_id, ticket_id, reconciliation_id, target_status,
                    reason, requested_by, status, requested_at)
               VALUES ('retire_c4', 'project-1', 'DEV-142', 'recon_c4', 'CANCELLED',
                       'Obsolete.', 'planner-01', 'PENDING', '2026-10-06T00:00:00Z')"""
        )

    service.finalize_pending_ticket_retirements(
        Actor("supervisor", Role.ADMIN, frozenset({"*"})), "retire_c4"
    )

    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT status FROM planner_jobs WHERE planner_job_id = ?", (job["planner_job_id"],)
        ).fetchone()["status"] == "RESOLVED"
    _assert_closed(service, actors, job["planner_job_id"])
