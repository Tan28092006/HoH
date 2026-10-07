"""Review C2: a capability approval never closes a failure job.

A ticket carries one open Planner job. A capability review was folded into an
open failure job (or a failure into an open review), and the approval that
closes reviews then closed the merged job: the ticket stayed BLOCKED with no
job and no wake-up. A review now never merges into a failure job, failure
evidence strips the review mark, and approval leaves a BLOCKED or REWORK
ticket's job open.
"""
from __future__ import annotations

from helpers.capability import CAPABILITIES, _open_jobs, blocked  # noqa: F401

from hoh_control_plane.domain import InterventionSeverity, PlannerJobType
from hoh_control_plane.service.planner_jobs import CAPABILITY_REVIEW_MARK


def _failure_job(service, actors, ticket_id, *, block=True):
    with service.store.connection(immediate=True) as connection:
        if block:
            connection.execute(
                "UPDATE tickets SET status = 'BLOCKED', blocked_reason = 'QA infra' WHERE ticket_id = ?",
                (ticket_id,),
            )
        ticket = service._ticket(connection, ticket_id)
        return service._open_planner_job(
            connection, actors["qa"], "project-1", ticket,
            job_type=PlannerJobType.QA_FAILURE, summary="QA infrastructure failed",
            evidence_refs=["test_run:testrun_0001", "failure:abc"],
            severity=InterventionSeverity.NORMAL,
        )


def _marks(job):
    import json

    refs = job["evidence_refs"]
    refs = json.loads(refs) if isinstance(refs, str) else refs
    return [ref for ref in refs if ref.startswith(CAPABILITY_REVIEW_MARK)]


def test_a_review_is_not_folded_into_an_open_failure_job(service, actors, blocked):
    failure = _failure_job(service, actors, blocked["ticket_id"], block=False)
    blocked["prober"].equipped = {"build_execution"}

    service.run_capability_preflight(actors["qa"], "project-1")

    [job] = _open_jobs(service, blocked["ticket_id"])
    assert job["planner_job_id"] == failure["planner_job_id"]
    assert _marks(job) == []
    assert "owner=infrastructure" not in job["summary"]


def test_failure_evidence_turns_a_review_into_a_failure_job(service, actors, blocked):
    blocked["prober"].equipped = {"build_execution"}
    service.run_capability_preflight(actors["qa"], "project-1")
    [review] = _open_jobs(service, blocked["ticket_id"])
    assert _marks(review) == [CAPABILITY_REVIEW_MARK + actors["qa"].agent_id]

    merged = _failure_job(service, actors, blocked["ticket_id"])
    blocked["prober"].equipped = set(CAPABILITIES)
    result = service.run_capability_preflight(actors["qa"], "project-1")

    assert merged["planner_job_id"] == review["planner_job_id"]
    assert _marks(merged) == []
    assert result["resolved_planner_jobs"] == []
    assert [job["planner_job_id"] for job in _open_jobs(service, blocked["ticket_id"])] == [
        review["planner_job_id"]
    ]


def test_approval_leaves_the_job_of_a_blocked_or_rework_ticket_open(service, actors, blocked):
    blocked["prober"].equipped = {"build_execution"}
    service.run_capability_preflight(actors["qa"], "project-1")
    [review] = _open_jobs(service, blocked["ticket_id"])
    for status in ("BLOCKED", "REWORK"):
        with service.store.connection(immediate=True) as connection:
            connection.execute("UPDATE tickets SET status = ? WHERE ticket_id = ?",
                               (status, blocked["ticket_id"]))
            resolved = service._resolve_capability_review_jobs(
                connection, actors["planner"], "project-1", actors["qa"].agent_id,
                set(CAPABILITIES), "approved",
            )
        assert resolved == []
        assert [job["planner_job_id"] for job in _open_jobs(service, blocked["ticket_id"])] == [
            review["planner_job_id"]
        ]


def test_a_pure_review_on_a_qa_ready_ticket_still_closes_on_approval(service, actors, blocked):
    blocked["prober"].equipped = {"build_execution"}
    service.run_capability_preflight(actors["qa"], "project-1")
    [review] = _open_jobs(service, blocked["ticket_id"])

    blocked["prober"].equipped = set(CAPABILITIES)
    result = service.run_capability_preflight(actors["qa"], "project-1")

    assert result["resolved_planner_jobs"] == [review["planner_job_id"]]
    assert _open_jobs(service, blocked["ticket_id"]) == []
