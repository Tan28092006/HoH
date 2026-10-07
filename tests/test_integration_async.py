from __future__ import annotations

import asyncio
import json
import time
import hashlib
import subprocess
from pathlib import Path

import pytest
from helpers.barrier import keep_open_tickets
from helpers.integration import (
    REGRESSION_COMMAND,
    _candidate_ready_for_integration,
    _claimed_for_integration,
    _git,
    _mark_run_as_qa_job,
    _merge,
    _repository,
    record_regression_run,
)

from hoh_control_plane import mcp_server
from hoh_control_plane.mcp import workers as mcp_workers
from hoh_control_plane.domain import AuthorizationError, ConflictError, ValidationError
from hoh_control_plane.mcp_server import create_mcp_server


def test_merge_candidate_queues_without_running_git(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    _repository, ticket, _candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    monkeypatch.setattr(
        service.git_worktrees,
        "fast_forward_default_branch",
        lambda *_args, **_kwargs: pytest.fail("Git must run in the detached merge worker"),
    )

    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(actors["integrator"], ticket["ticket_id"])["context_snapshot_id"],
    )

    assert queued["merge_job"]["status"] == "QUEUED"
    assert queued["ticket"]["status"] == "MERGE_READY"


def test_merge_worker_fast_forwards_then_poll_reports_completed(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(actors["integrator"], ticket["ticket_id"])["context_snapshot_id"],
    )

    completed = service.execute_queued_merge(queued["merge_job"]["merge_job_id"])

    assert completed["status"] == "COMPLETED"
    assert completed["merge_status"] == "merged"
    assert _git(repository, "rev-parse", "main") == candidate["candidate"]["commit_sha"]
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM leases WHERE lease_id = ?", (integration["lease"]["lease_id"],)
        ).fetchone()[0] == 0
        final_ticket = connection.execute(
            "SELECT status FROM tickets WHERE ticket_id = ?", (ticket["ticket_id"],)
        ).fetchone()
        assert final_ticket["status"] == "DONE"


def test_successful_merge_reports_once_and_wakes_the_planner(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    """A delivery is not complete until the planning loop receives its report."""
    _repository_path, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(actors["integrator"], ticket["ticket_id"])["context_snapshot_id"],
    )

    service.register_agent(actors["planner"], skills=["planning"], capacity=1)
    event_wait = __import__(
        "hoh_control_plane.event_wait", fromlist=["wait_for_event"]
    )
    wait_for_event = event_wait.wait_for_event
    # Planner synchronises repository truth at bootstrap, before it waits; the
    # first observation of this repository is its own barrier to resolve.
    service.sync_repository(actors["planner"], "project-1")
    keep_open_tickets(service, actors["planner"])
    monkeypatch.setitem(event_wait._REPOSITORY_OBSERVED_AT, "project-1", time.monotonic())

    async def merge_while_planner_waits():
        waiting = asyncio.create_task(
            wait_for_event(service, actors["planner"], "project-1", timeout_seconds=30)
        )
        await asyncio.sleep(0.04)
        completed_job = await asyncio.to_thread(
            service.execute_queued_merge, queued["merge_job"]["merge_job_id"]
        )
        return completed_job, await asyncio.wait_for(waiting, 1)

    completed, report = asyncio.run(merge_while_planner_waits())
    replay = service.execute_queued_merge(queued["merge_job"]["merge_job_id"])

    assert completed["merge_status"] == replay["merge_status"] == "merged"
    assert report["status"] == "event"
    assert report["event"]["kind"] == "planner_work"
    planning_jobs = service.discover_planner_jobs(actors["planner"], "project-1")
    assert len(planning_jobs) == 1
    assert planning_jobs[0]["job_type"] == "delivery_completed"
    assert planning_jobs[0]["candidate_id"] == candidate["candidate"]["candidate_id"]
    assert planning_jobs[0]["evidence_refs"] == [
        f"candidate:{candidate['candidate']['candidate_id']}",
        f"merge_job:{queued['merge_job']['merge_job_id']}",
        f"mainline:{candidate['candidate']['commit_sha']}",
    ]
    interventions = service.list_planner_interventions(actors["planner"], "project-1")
    assert len(interventions) == 1
    assert interventions[0]["event_class"] == "delivery_completed"
    recovered = service.reconcile_delivery_report(
        actors["planner"], queued["merge_job"]["merge_job_id"]
    )
    assert recovered["planner_job_id"] == planning_jobs[0]["planner_job_id"]
    assert len(service.discover_planner_jobs(actors["planner"], "project-1")) == 1


def test_delivery_report_recovery_refuses_unfinished_or_unauthorized_merges(
    service, actors, seeded, tmp_path: Path
) -> None:
    _repository_path, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(actors["integrator"], ticket["ticket_id"])["context_snapshot_id"],
    )["merge_job"]

    with pytest.raises(AuthorizationError):
        service.reconcile_delivery_report(actors["developer"], queued["merge_job_id"])
    with pytest.raises(ConflictError, match="successfully completed"):
        service.reconcile_delivery_report(actors["planner"], queued["merge_job_id"])

    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """UPDATE merge_jobs SET status='COMPLETED', merge_status='merged',
               mainline_commit=? WHERE merge_job_id=?""",
            (candidate["candidate"]["commit_sha"], queued["merge_job_id"]),
        )
    with pytest.raises(ConflictError, match="ticket is not in its completed state"):
        service.reconcile_delivery_report(actors["planner"], queued["merge_job_id"])


def test_merge_worker_requeues_rework_when_mainline_drifts_after_queueing(
    service, actors, seeded, tmp_path: Path
) -> None:
    """Drift that appears between queueing and execution is the worker's race to lose.

    Drift present before queueing is refused by the release policy instead, so
    this exercises the window the gate cannot close.
    """
    repository, ticket, _candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(actors["integrator"], ticket["ticket_id"])["context_snapshot_id"],
    )
    (repository / "mainline.txt").write_text("new mainline work\n", encoding="utf-8")
    _git(repository, "add", "mainline.txt")
    _git(repository, "commit", "-m", "Advance mainline")

    completed = service.execute_queued_merge(queued["merge_job"]["merge_job_id"])

    assert completed["status"] == "COMPLETED"
    assert completed["merge_status"] == "base_drift"
    with service.store.connection() as connection:
        final_ticket = connection.execute(
            "SELECT status FROM tickets WHERE ticket_id = ?", (ticket["ticket_id"],)
        ).fetchone()
        assert final_ticket["status"] == "REWORK"


def test_duplicate_merge_request_reuses_one_job_and_merges_once(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    arguments = (
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        service.get_ticket_context(actors["integrator"], ticket["ticket_id"])["context_snapshot_id"],
    )

    first = service.queue_merge_candidate(*arguments)
    retry = service.queue_merge_candidate(*arguments)
    completed = service.execute_queued_merge(first["merge_job"]["merge_job_id"])
    replay = service.execute_queued_merge(first["merge_job"]["merge_job_id"])

    assert retry["merge_job"]["merge_job_id"] == first["merge_job"]["merge_job_id"]
    assert replay["status"] == completed["status"] == "COMPLETED"
    assert _git(repository, "rev-parse", "main") == candidate["candidate"]["commit_sha"]
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM merge_jobs WHERE ticket_id = ?", (ticket["ticket_id"],)
        ).fetchone()[0] == 1
        final_ticket = connection.execute(
            "SELECT status, version FROM tickets WHERE ticket_id = ?", (ticket["ticket_id"],)
        ).fetchone()
        assert final_ticket["status"] == "DONE"
        assert final_ticket["version"] == integration["ticket"]["version"] + 1


def test_merge_worker_persists_git_failure_for_polling(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    _repository, ticket, _candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(actors["integrator"], ticket["ticket_id"])["context_snapshot_id"],
    )
    monkeypatch.setattr(
        service.git_worktrees,
        "fast_forward_default_branch",
        lambda **_kwargs: (_ for _ in ()).throw(ValidationError("Git operation timed out")),
    )

    failed = service.execute_queued_merge(queued["merge_job"]["merge_job_id"])
    polled = service.get_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], queued["merge_job"]["merge_job_id"],
    )

    assert failed["status"] == "FAILED"
    assert polled["merge_job"]["error_message"] == "Git operation timed out"
    # An infrastructure failure is a durable Planner job, not a silent retry:
    # the ticket is BLOCKED until Planner resumes it (failure feedback loop).
    assert polled["ticket"]["status"] == "BLOCKED"
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM planner_jobs WHERE ticket_id = ? AND status = 'READY'",
            (ticket["ticket_id"],),
        ).fetchone()[0] == 1


def test_poll_before_worker_runs_renews_the_integrator_lease(
    service, actors, seeded, tmp_path: Path
) -> None:
    _repository, ticket, _candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(actors["integrator"], ticket["ticket_id"])["context_snapshot_id"],
    )

    polled = service.get_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], queued["merge_job"]["merge_job_id"],
        lease_seconds=1200,
    )

    assert polled["merge_job"]["status"] == "QUEUED"
    assert polled["lease"] is not None
    assert polled["lease"]["expires_at"] > integration["lease"]["expires_at"]


@pytest.mark.anyio
async def test_integrator_mcp_acknowledges_merge_before_worker_runs(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    _repository, ticket, _candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    launched: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        mcp_workers,
        "_start_merge_worker",
        lambda database_path, merge_job_id: launched.append((database_path, merge_job_id)),
    )

    _, result = await create_mcp_server(service, actors["integrator"]).call_tool(
        "integrator.merge_candidate",
        {
            "ticket_id": ticket["ticket_id"],
            "lease_id": integration["lease"]["lease_id"],
            "expected_version": integration["ticket"]["version"],
            "context_snapshot_id": service.get_ticket_context(
                actors["integrator"], ticket["ticket_id"]
            )["context_snapshot_id"],
        },
    )

    assert result["merge_job"]["status"] == "QUEUED"
    assert launched == [(service.store.path, result["merge_job"]["merge_job_id"])]


def test_queue_merge_candidate_reports_whether_the_job_is_new(
    service, actors, seeded, tmp_path: Path
) -> None:
    """The caller must be able to tell a fresh job from an idempotent replay."""
    _repository, ticket, _candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    arguments = (
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        service.get_ticket_context(actors["integrator"], ticket["ticket_id"])["context_snapshot_id"],
    )

    first = service.queue_merge_candidate(*arguments)
    retry = service.queue_merge_candidate(*arguments)

    assert first["reused"] is False
    assert retry["reused"] is True
    assert retry["merge_job"]["merge_job_id"] == first["merge_job"]["merge_job_id"]


@pytest.mark.anyio
async def test_retried_mcp_merge_request_spawns_at_most_one_worker(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    """A client retry with no idempotency_key must not double-spawn the worker.

    This is the real shape of 'retry under contention': the caller does not know
    whether its first request was durably queued, so it calls again with the same
    ticket/lease/version. queue_merge_candidate already dedupes the job row; the
    MCP tool must not launch a second OS process for that same job.
    """
    _repository, ticket, _candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    launched: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        mcp_workers,
        "_start_merge_worker",
        lambda database_path, merge_job_id: launched.append((database_path, merge_job_id)),
    )
    server = create_mcp_server(service, actors["integrator"])
    arguments = {
        "ticket_id": ticket["ticket_id"],
        "lease_id": integration["lease"]["lease_id"],
        "expected_version": integration["ticket"]["version"],
        "context_snapshot_id": service.get_ticket_context(
            actors["integrator"], ticket["ticket_id"]
        )["context_snapshot_id"],
    }

    _, first_result = await server.call_tool("integrator.merge_candidate", arguments)
    _, retry_result = await server.call_tool("integrator.merge_candidate", arguments)

    assert first_result["merge_job"]["merge_job_id"] == retry_result["merge_job"]["merge_job_id"]
    assert launched == [(service.store.path, first_result["merge_job"]["merge_job_id"])]
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM merge_jobs WHERE ticket_id = ?", (ticket["ticket_id"],)
        ).fetchone()[0] == 1


@pytest.mark.anyio
async def test_a_refused_merge_request_never_spawns_a_worker(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    """A rolled-back or refused enqueue must leave no orphan worker behind."""
    _repository, ticket, _candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    launched: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        mcp_workers,
        "_start_merge_worker",
        lambda database_path, merge_job_id: launched.append((database_path, merge_job_id)),
    )

    with pytest.raises(mcp_server.ToolError):
        await create_mcp_server(service, actors["integrator"]).call_tool(
            "integrator.merge_candidate",
            {
                "ticket_id": ticket["ticket_id"],
                "lease_id": integration["lease"]["lease_id"],
                "expected_version": integration["ticket"]["version"] + 1,
                "context_snapshot_id": service.get_ticket_context(
                    actors["integrator"], ticket["ticket_id"]
                )["context_snapshot_id"],
            },
        )

    assert launched == []
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM merge_jobs WHERE ticket_id = ?", (ticket["ticket_id"],)
        ).fetchone()[0] == 0


def test_a_second_worker_racing_the_same_job_never_runs_git_twice(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    """Two OS processes claiming the same merge_job_id must still fast-forward once.

    This is the atomic-claim guarantee (AC4) exercised without real threads: from
    inside the Git call of the first execution, a second worker for the very same
    job id is invoked reentrantly. Since the first execution's QUEUED->RUNNING
    transition already committed by the time Git runs, the second call must see
    a non-QUEUED job and return immediately without invoking Git again.
    """
    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(actors["integrator"], ticket["ticket_id"])["context_snapshot_id"],
    )
    merge_job_id = queued["merge_job"]["merge_job_id"]
    git_calls: list[None] = []
    real_fast_forward = service.git_worktrees.fast_forward_default_branch

    def racing_fast_forward(**kwargs):
        git_calls.append(None)
        second = service.execute_queued_merge(merge_job_id)
        assert second["status"] == "RUNNING"
        return real_fast_forward(**kwargs)

    monkeypatch.setattr(
        service.git_worktrees, "fast_forward_default_branch", racing_fast_forward
    )

    completed = service.execute_queued_merge(merge_job_id)

    assert len(git_calls) == 1
    assert completed["status"] == "COMPLETED"
    assert _git(repository, "rev-parse", "main") == candidate["candidate"]["commit_sha"]


def test_a_barrier_opened_during_the_merge_does_not_strand_a_landed_candidate(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    # A teammate push observed while the gate runs opens a reconciliation.
    # Git has already fast-forwarded by completion time, so the job must record
    # the landing rather than stay RUNNING with the ticket at MERGE_READY.
    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(actors["integrator"], ticket["ticket_id"])["context_snapshot_id"],
    )
    real_fast_forward = service.git_worktrees.fast_forward_default_branch

    def fast_forward_after_a_push(**kwargs):
        result = real_fast_forward(**kwargs)
        with service.store.connection(immediate=True) as connection:
            service._open_commit_reconciliation(
                connection, actors["planner"], "project-1", commit="b" * 40,
                subject="teammate push", relation="fast_forward", observation_ref="test:push",
            )
        return result

    monkeypatch.setattr(
        service.git_worktrees, "fast_forward_default_branch", fast_forward_after_a_push
    )

    completed = service.execute_queued_merge(queued["merge_job"]["merge_job_id"])

    assert completed["status"] == "COMPLETED"
    assert _git(repository, "rev-parse", "main") == candidate["candidate"]["commit_sha"]
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT status FROM tickets WHERE ticket_id = ?", (ticket["ticket_id"],)
        ).fetchone()["status"] == "DONE"


def test_gate_reuses_a_passing_qa_run_of_the_same_commit_and_command(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    # Fast-forward lands exactly the commit QA ran, so rerunning the identical
    # frozen command only repeats a full suite.
    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    _mark_run_as_qa_job(service, candidate, ticket)
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(actors["integrator"], ticket["ticket_id"])["context_snapshot_id"],
    )
    executions: list[object] = []
    monkeypatch.setattr(service.project_tests, "execute", lambda *a, **k: executions.append(a))

    completed = service.execute_queued_merge(queued["merge_job"]["merge_job_id"])

    assert completed["status"] == "COMPLETED"
    assert executions == []
    assert _git(repository, "rev-parse", "main") == candidate["candidate"]["commit_sha"]
    with service.store.connection() as connection:
        proof = connection.execute(
            "SELECT payload FROM audit_events WHERE event_type = 'integration.gate_passed'"
        ).fetchone()
    assert '"gate_mode": "reused_qa_run"' in proof["payload"]


def test_gate_still_runs_when_the_qa_run_used_another_timeout(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    _mark_run_as_qa_job(service, candidate, ticket, timeout_seconds=5)
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(actors["integrator"], ticket["ticket_id"])["context_snapshot_id"],
    )

    completed = service.execute_queued_merge(queued["merge_job"]["merge_job_id"])

    assert completed["status"] == "COMPLETED"
    with service.store.connection() as connection:
        proof = connection.execute(
            "SELECT payload FROM audit_events WHERE event_type = 'integration.gate_passed'"
        ).fetchone()
    assert '"gate_mode": "executed"' in proof["payload"]


def test_drift_before_the_gate_skips_the_suite(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    # Fast-forward cannot land on a moved main; finding that out after an
    # 18-minute gate wasted the whole run.
    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(actors["integrator"], ticket["ticket_id"])["context_snapshot_id"],
    )
    (repository / "teammate.txt").write_text("pushed meanwhile\n", encoding="utf-8")
    _git(repository, "add", "teammate.txt")
    _git(repository, "commit", "-m", "Teammate change")
    executions: list[object] = []
    monkeypatch.setattr(service.project_tests, "execute", lambda *a, **k: executions.append(a))

    service.execute_queued_merge(queued["merge_job"]["merge_job_id"])

    assert executions == []
    assert _git(repository, "rev-parse", "main") != candidate["candidate"]["commit_sha"]
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT 1 FROM audit_events WHERE event_type = 'integration.base_drift'"
        ).fetchone() is not None


def _open_ticket(service, actors, ticket_id: str, file_scope: list[str], depends_on=None):
    from hoh_control_plane.domain import Role
    return service.create_ticket(
        actors["planner"],
        feature_id="payment-retry",
        ticket_id=ticket_id,
        title=f"Other work {ticket_id}",
        objective="Unrelated work on the board",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["It works"],
        preservation_constraints=["Existing checkout remains valid"],
        file_scope=file_scope,
        dependency_ids=depends_on or [],
    )


def _latest_reconciliation(service):
    with service.store.connection() as connection:
        return connection.execute(
            "SELECT status, resolved_by FROM repository_reconciliations ORDER BY created_at DESC LIMIT 1"
        ).fetchone()


def test_a_merge_outside_every_open_scope_resolves_its_own_reconciliation(
    service, actors, seeded, tmp_path: Path
) -> None:
    # The merge touched only src/checkout; the other open ticket works in
    # src/reporting and does not depend on it, so nothing it planned can change.
    _open_ticket(service, actors, "DEV-200", ["src/reporting/**"])

    completed = _merge(service, actors, seeded, tmp_path)

    assert completed["status"] == "COMPLETED"
    latest = _latest_reconciliation(service)
    assert latest["status"] == "RESOLVED"
    assert latest["resolved_by"] == "system:scope-disjoint"


@pytest.mark.parametrize("scope,depends", [
    (["src/checkout/"], False),      # legacy trailing slash still overlaps
    (["src/**"], False),
    (["src/reporting/**"], True),    # depends on the merged ticket
    ([], False),                     # unknown scope
])
def test_a_merge_that_may_affect_open_work_flags_it_for_an_intent_check(
    service, actors, seeded, tmp_path: Path, scope, depends
) -> None:
    """Wave 7C: HoH's own merge never waits for Planner; reachable work is flagged."""
    _open_ticket(service, actors, "DEV-201", scope or ["placeholder/**"],
                 depends_on=["DEV-142"] if depends else None)
    if not scope:
        with service.store.connection(immediate=True) as connection:
            connection.execute("UPDATE tickets SET file_scope = '[]' WHERE ticket_id = 'DEV-201'")

    _merge(service, actors, seeded, tmp_path)

    latest = _latest_reconciliation(service)
    assert (latest["status"], latest["resolved_by"]) == ("RESOLVED", "system:merge-advisory")
    with service.store.connection() as connection:
        flag = connection.execute(
            "SELECT planning_base_stale FROM tickets WHERE ticket_id = 'DEV-201'"
        ).fetchone()["planning_base_stale"]
    assert json.loads(flag)["intent_check"] == "required"


def _snapshot(service, actor, ticket):
    return service.get_ticket_context(actor, ticket["ticket_id"])["context_snapshot_id"]


def test_a_candidate_already_on_main_is_not_queued_as_drift(
    service, actors, seeded, tmp_path: Path
) -> None:
    # Someone fast-forwarded main to the candidate by hand. That is delivered
    # work: the merge must refuse with a landing hint, not send it to rework.
    from hoh_control_plane.domain import ReleasePolicyError
    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    _git(repository, "merge", "--ff-only", candidate["candidate"]["commit_sha"])

    with pytest.raises(ReleasePolicyError) as refused:
        service.queue_merge_candidate(
            actors["integrator"], ticket["ticket_id"],
            integration["lease"]["lease_id"], integration["ticket"]["version"],
            context_snapshot_id=_snapshot(service, actors["integrator"], ticket),
        )
    assert refused.value.details["reason"] == "candidate_already_on_main"


def test_a_candidate_landed_after_queueing_closes_the_job_without_rework(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        context_snapshot_id=_snapshot(service, actors["integrator"], ticket),
    )
    _git(repository, "merge", "--ff-only", candidate["candidate"]["commit_sha"])

    job = service.execute_queued_merge(queued["merge_job"]["merge_job_id"])

    assert job["status"] == "FAILED"
    assert "candidate_already_on_main" in job["error_message"]
    with service.store.connection() as connection:
        status = connection.execute(
            "SELECT status FROM tickets WHERE ticket_id = ?", (ticket["ticket_id"],)
        ).fetchone()["status"]
    assert status != "REWORK"


def test_a_committed_claim_survives_contention_while_building_its_brief(
    service, actors, seeded, monkeypatch
) -> None:
    # The lease is committed before the packet is compiled. Contention there
    # must not surface as an error, or the transport retries a claim the
    # caller already holds.
    from hoh_control_plane.store import ContentionError
    ticket = seeded["ticket"]

    def contended(*args, **kwargs):
        raise ContentionError("database is busy")

    monkeypatch.setattr(service, "get_ticket_context", contended)
    claimed = service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)

    assert claimed["lease"]["lease_id"]
    assert claimed["current_context_snapshot_id"] is None
    assert claimed["worker_brief"] is None
    assert "ticket.get_context" in claimed["next_action"]
