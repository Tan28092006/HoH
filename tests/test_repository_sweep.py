"""The scheduler reconciles HoH against Git even when no Planner chat is open.

Before, Git was observed only while a Planner waited, a candidate fast-forwarded
by hand stayed an open ticket until somebody noticed, and a merge job that Git
had landed but whose ticket changed stayed RUNNING until an administrator
cleared it. Each case here uses a real temporary repository.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from barrier_helpers import keep_open_tickets

from hoh_control_plane import event_wait
from hoh_control_plane.domain import Actor, ConflictError, Role
from hoh_control_plane.mcp_server import create_mcp_server
from hoh_control_plane.scheduler import sweep_once
from test_integration_async import _candidate_ready_for_integration, _git, _repository

SCHEDULER = Actor("hoh-scheduler", Role.ADMIN, frozenset({"*"}))
LONG_AGO = "2000-01-01T00:00:00Z"


def _age_observation(service, project_id: str = "project-1") -> None:
    """Pretend the last observation is older than the throttle window."""
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE project_repositories SET observed_at = ? WHERE project_id = ?",
            (LONG_AGO, project_id),
        )


def _pending_reconciliations(service, planner) -> list[dict]:
    return [
        item for item in service.list_repository_reconciliations(planner, "project-1")["reconciliations"]
        if item["status"] == "PENDING"
    ]


def _ticket(service, ticket_id: str) -> dict:
    with service.store.connection() as connection:
        return service._ticket(connection, ticket_id)


def _external_commit(repository: Path, name: str) -> str:
    (repository / name).write_text("changed outside HoH\n", encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-m", f"Outside change {name}")
    return _git(repository, "rev-parse", "HEAD")


def _observed_baseline(service, actors, repository: Path | None, tmp_path) -> Path:
    """A project a Planner has observed once and reconciled, as a live one would be."""
    if repository is None:
        repository = _repository(tmp_path)
        service.configure_repository(actors["planner"], "project-1", str(repository))
    service.sync_repository(actors["planner"], "project-1")
    keep_open_tickets(service, actors["planner"])
    return repository


def test_the_sweep_opens_a_reconciliation_for_a_commit_no_planner_saw(
    service, store, actors, seeded, tmp_path
) -> None:
    repository = _observed_baseline(service, actors, None, tmp_path)
    commit = _external_commit(repository, "hotfix.txt")
    _age_observation(service)

    swept = sweep_once(store.path)

    observed = swept["repositories"]["observed"]
    assert [item["project_id"] for item in observed] == ["project-1"]
    assert observed[0]["changed"] is True
    pending = _pending_reconciliations(service, actors["planner"])
    assert len(pending) == 1
    assert pending[0]["reconciliation_id"] == observed[0]["reconciliation_id"]
    assert commit in [item["commit"] for item in pending[0]["observed_commits"]]
    # Execution is held until Planner reconciles, as it would be after a Planner sync.
    with pytest.raises(ConflictError):
        service.claim_ticket(
            actors["developer"], seeded["ticket"]["ticket_id"], seeded["ticket"]["version"], 900
        )

    # The observation is throttled for every observer: the sweep and a waiting Planner.
    assert service.repository_observation_due("project-1") is False
    assert sweep_once(store.path)["repositories"]["observed"] == []


def test_a_sweep_then_a_planner_sync_leave_one_pending_reconciliation(
    service, store, actors, seeded, tmp_path
) -> None:
    repository = _observed_baseline(service, actors, None, tmp_path)
    first = _external_commit(repository, "first.txt")
    _age_observation(service)
    swept = sweep_once(store.path)["repositories"]["observed"]
    second = _external_commit(repository, "second.txt")

    synced = service.sync_repository(actors["planner"], "project-1")

    pending = _pending_reconciliations(service, actors["planner"])
    assert len(pending) == 1
    assert synced["reconciliation_id"] == swept[0]["reconciliation_id"]
    assert synced["reconciliation_extended"] is True
    observed = [item["commit"] for item in pending[0]["observed_commits"]]
    assert first in observed and second in observed
    assert keep_open_tickets(service, actors["planner"]) == 1


def test_a_waiting_planner_shares_the_sweeps_throttle(
    service, actors, seeded, tmp_path, monkeypatch
) -> None:
    _observed_baseline(service, actors, None, tmp_path)
    _age_observation(service)
    service.sweep_repositories(SCHEDULER)
    monkeypatch.delitem(event_wait._REPOSITORY_OBSERVED_AT, "project-1", raising=False)

    def must_not_fetch(*_args, **_kwargs):
        raise AssertionError("the sweep observed this repository moments ago")

    monkeypatch.setattr(service, "sync_repository", must_not_fetch)

    events = asyncio.run(
        event_wait._repository_pending(service, actors["planner"], "project-1")
    )

    assert events == []


def test_a_project_no_planner_ever_observed_is_left_alone(
    service, actors, seeded, tmp_path
) -> None:
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))

    swept = service.sweep_repositories(SCHEDULER)

    assert swept["observed"] == [] and swept["failed"] == []
    assert _pending_reconciliations(service, actors["planner"]) == [], (
        "the baseline reconciliation belongs to the Planner that will resolve it"
    )


def test_one_projects_failure_does_not_stop_the_sweep(
    service, actors, seeded, tmp_path, monkeypatch
) -> None:
    _observed_baseline(service, actors, None, tmp_path)
    _age_observation(service)

    def unreachable(*_args, **_kwargs):
        raise ConnectionError("remote unreachable")

    monkeypatch.setattr(service, "sync_repository", unreachable)

    swept = service.sweep_repositories(SCHEDULER)

    assert swept["failed"] == [{
        "project_id": "project-1", "step": "observed",
        "error_type": "ConnectionError", "summary": "remote unreachable",
    }]
    assert swept["candidates_already_on_main"] == [] and swept["stuck_merge_jobs"] == []


def _landing_events(service) -> list[dict]:
    import json

    with service.store.connection() as connection:
        return [
            {"actor_id": row["actor_id"], **json.loads(row["payload"])}
            for row in connection.execute(
                "SELECT actor_id, payload FROM audit_events "
                "WHERE event_type = 'integration.landed_ungated'"
            ).fetchall()
        ]


def test_a_candidate_landed_by_hand_is_recorded_ungated_by_the_sweep(
    service, actors, seeded, tmp_path
) -> None:
    repository, ticket, candidate, _verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    _observed_baseline(service, actors, repository, tmp_path)
    commit = candidate["candidate"]["commit_sha"]
    _git(repository, "merge", "--ff-only", commit)
    _age_observation(service)

    swept = service.sweep_repositories(SCHEDULER)

    flagged = swept["candidates_already_on_main"]
    assert [(item["ticket_id"], item["landed_commit"], item["match"], item["recorded"])
            for item in flagged] == [(ticket["ticket_id"], commit, "ancestry", True)]
    assert _ticket(service, ticket["ticket_id"])["status"] == "DONE"
    [event] = _landing_events(service)
    assert event["actor_id"] == "system:sweep"
    assert event["gated"] is False and event["qa_verified"] is False
    # Idempotent: a DONE ticket is not swept again.
    _age_observation(service)
    assert service.sweep_repositories(SCHEDULER)["candidates_already_on_main"] == []


def test_a_cherry_picked_candidate_is_found_by_patch_id(
    service, actors, seeded, tmp_path
) -> None:
    repository, ticket, candidate, _verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    _observed_baseline(service, actors, repository, tmp_path)
    commit = candidate["candidate"]["commit_sha"]
    _external_commit(repository, "unrelated.txt")
    _git(repository, "cherry-pick", commit)
    picked = _git(repository, "rev-parse", "HEAD")
    assert picked != commit
    _age_observation(service)

    flagged = service.sweep_repositories(SCHEDULER)["candidates_already_on_main"]

    assert [(item["landed_commit"], item["match"], item["recorded"]) for item in flagged] == [
        (picked, "patch_id", True)
    ]
    assert _ticket(service, ticket["ticket_id"])["status"] == "DONE"
    [event] = _landing_events(service)
    assert event["landed_commit"] == picked and event["gated"] is False


def _stuck_merge(service, actors, seeded, tmp_path, monkeypatch) -> tuple[Path, dict, dict]:
    """Git landed the candidate, the worker died, and the ticket changed meanwhile."""
    from test_operational_recovery import queued_merge

    repository, candidate, job = queued_merge(service, actors, seeded, tmp_path)
    _observed_baseline(service, actors, repository, tmp_path)
    complete = service._complete_queued_merge

    def worker_died(*_args, **_kwargs):
        raise SystemExit("worker died")

    monkeypatch.setattr(service, "_complete_queued_merge", worker_died)
    with pytest.raises(SystemExit):
        service.execute_queued_merge(job["merge_job_id"])
    monkeypatch.setattr(service, "_complete_queued_merge", complete)
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE tickets SET version = version + 1 WHERE ticket_id = ?", (job["ticket_id"],)
        )
    recovered = service.reconcile_merge_job(SCHEDULER, job["merge_job_id"])
    assert recovered["status"] == "RUNNING"
    assert _git(repository, "rev-parse", "main") == candidate["commit_sha"]
    return repository, candidate, job


def _outlive_the_worker_timeout(service, merge_job_id: str) -> None:
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE merge_jobs SET started_at = ? WHERE merge_job_id = ?",
            (LONG_AGO, merge_job_id),
        )


@pytest.mark.anyio
async def test_a_stuck_merge_job_reaches_planner_who_closes_it_and_records_the_landing(
    service, actors, seeded, tmp_path, monkeypatch
) -> None:
    repository, candidate, job = _stuck_merge(service, actors, seeded, tmp_path, monkeypatch)
    merge_job_id, ticket_id = job["merge_job_id"], job["ticket_id"]
    planner = create_mcp_server(service, actors["planner"])

    # Today's dead end: even with the merge lease long expired, the landing is
    # refused while the job is RUNNING.
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE leases SET expires_at = ? WHERE lease_id = ?", (LONG_AGO, job["lease_id"])
        )
    with pytest.raises(ConflictError) as refused:
        service.record_external_landing(
            actors["integrator"], ticket_id, _ticket(service, ticket_id)["version"],
            candidate["commit_sha"], "Already on main.",
        )
    assert refused.value.details["reason"] == "landing_job_active"
    # Too young to be called stuck: recovery may still settle it.
    with pytest.raises(ConflictError) as young:
        service.close_stuck_merge_job(actors["planner"], merge_job_id, "Looks stuck.")
    assert young.value.details["reason"] == "merge_job_not_stuck"

    _outlive_the_worker_timeout(service, merge_job_id)
    _age_observation(service)
    swept = service.sweep_repositories(SCHEDULER)

    assert [item["merge_job_id"] for item in swept["stuck_merge_jobs"]] == [merge_job_id]
    assert swept["stuck_merge_jobs"][0]["candidate_on_default_branch"] is True
    assert swept["candidates_already_on_main"] == [], "the merge path owns a running job"
    jobs = service.discover_planner_jobs(actors["planner"], "project-1")
    assert len(jobs) == 1 and jobs[0]["summary"].startswith("merge_job_stuck:")
    assert merge_job_id in jobs[0]["summary"]
    assert f"merge_job:{merge_job_id}" in jobs[0]["evidence_refs"]
    interventions = service.list_planner_interventions(actors["planner"], "project-1")
    assert interventions[0]["severity"] == "high"
    _age_observation(service)
    assert service.sweep_repositories(SCHEDULER)["stuck_merge_jobs"] == [], "raised once"

    # A live operation on the repository is never taken over.
    with service.git_worktrees.repository_lock(str(repository)):
        with pytest.raises(ConflictError) as live:
            service.close_stuck_merge_job(actors["planner"], merge_job_id, "Stuck.")
    assert live.value.details["reason"] == "merge_job_live"

    _, closed = await planner.call_tool("planner.close_stuck_merge_job", {
        "merge_job_id": merge_job_id,
        "reason": "Git has the candidate; the ticket changed while the worker was down.",
    })

    assert closed["closed"] is True
    assert closed["merge_job"]["status"] == "FAILED"
    assert closed["git"]["candidate_on_default_branch"] is True
    assert "record_external_landing" in closed["next_action"]
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT 1 FROM merge_intents WHERE merge_job_id = ?", (merge_job_id,)
        ).fetchone() is not None, "the intent is evidence and stays"
        assert connection.execute(
            "SELECT 1 FROM leases WHERE lease_id = ?", (job["lease_id"],)
        ).fetchone() is None
    with pytest.raises(ConflictError) as again:
        service.close_stuck_merge_job(actors["planner"], merge_job_id, "Again.")
    assert again.value.details["reason"] == "merge_job_not_running"

    # The landing is now an ordinary explicit Planner record.
    assert _pending_reconciliations(service, actors["planner"])
    landed = service.record_external_landing(
        actors["planner"], ticket_id, _ticket(service, ticket_id)["version"],
        candidate["commit_sha"], "Landed by the stuck merge job; recorded ungated.",
    )
    assert landed["ticket"]["status"] == "DONE" and landed["gated"] is False


def test_closing_a_job_recovery_can_settle_closes_nothing(
    service, actors, seeded, tmp_path, monkeypatch
) -> None:
    """If the ticket changed back, recovery completes the merge itself."""
    _, _, job = _stuck_merge(service, actors, seeded, tmp_path, monkeypatch)
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE tickets SET version = version - 1 WHERE ticket_id = ?", (job["ticket_id"],)
        )
    _outlive_the_worker_timeout(service, job["merge_job_id"])

    result = service.close_stuck_merge_job(actors["planner"], job["merge_job_id"], "Stuck?")

    assert result["closed"] is False
    assert result["merge_job"]["status"] == "COMPLETED"
    assert _ticket(service, job["ticket_id"])["status"] == "DONE"
