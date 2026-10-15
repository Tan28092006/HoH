"""Mainline outcomes use runner classifications and exact current-main proof."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from hoh_control_plane.qa_runner import INFRASTRUCTURE_REASONS, TestExecution as Execution

OLD = "a" * 40
CURRENT = "b" * 40


def _setup(service, actors, seeded, *, commit=OLD, status="QUEUED"):
    with service.store.connection(immediate=True) as db:
        db.execute(
            "INSERT INTO project_repositories (project_id, repository_path, configured_by, configured_at, "
            "observed_mainline_commit) VALUES (?, ?, ?, ?, ?)",
            ("project-1", "/disposable", "planner-01", "2026-10-14T00:00:00Z", commit),
        )
        db.execute(
            "INSERT INTO mainline_checks (check_id, project_id, mainline_commit, trigger_ref, "
            "trigger_ticket_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("old", "project-1", commit, "fixture:landing", seeded["ticket"]["ticket_id"],
             status, "2026-10-14T00:00:00Z"),
        )

    service.configure_test_command(actors["planner"], "project-1", ["python", "-m", "pytest"], 60)


def _finish(service, outcome="red"):
    return service._finish_mainline_check(
        "old", outcome=outcome, exit_code=0 if outcome == "green" else 1,
        termination_reason="passed" if outcome == "green" else "test_failure",
        output_tail="retained diagnostic",
    )


def _new_main(service, *, proof_commit=CURRENT, outcome="covered_by_gate"):
    with service.store.connection(immediate=True) as db:
        db.execute("UPDATE project_repositories SET observed_mainline_commit = ?", (CURRENT,))
        db.execute(
            "INSERT INTO mainline_checks (check_id, project_id, mainline_commit, trigger_ref, "
            "status, outcome, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("new", "project-1", proof_commit, "fixture:new", "COMPLETED", outcome,
             "2026-10-14T00:01:00Z", "2026-10-14T00:02:00Z"),
        )


def _reverts(service):
    with service.store.connection() as db:
        return db.execute("SELECT COUNT(*) FROM tickets WHERE ticket_type = 'revert'").fetchone()[0]


@pytest.mark.parametrize("reason", sorted(INFRASTRUCTURE_REASONS | {
    "pytest_interrupted", "pytest_internal_error", "pytest_usage_error", "foreign_import",
}))
def test_every_runner_infrastructure_reason_is_not_red(service, actors, seeded, monkeypatch, reason):
    _setup(service, actors, seeded)
    execution = Execution("digest", 1, reason == "timeout", 1, "host error", "",
                          termination_reason=reason)
    monkeypatch.setattr(service.git_worktrees, "integration_checkout", lambda **kw: nullcontext("/disposable"))
    monkeypatch.setattr(service, "project_tests", SimpleNamespace(execute=lambda *args, **kw: execution))
    result = service.execute_queued_mainline_check("old")
    assert result["outcome"] == "infrastructure"
    assert result["status"] == "FAILED"
    assert result["termination_reason"] == reason
    assert result["revert_ticket_id"] is None
    assert _reverts(service) == 0
    guard = service.get_run_guardrails(actors["planner"], "project-1")
    assert guard["consecutive_failures"] == 1
    assert not guard["merge_queue_paused"]


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING"])
@pytest.mark.parametrize("proof", ["green", "covered_by_gate"])
@pytest.mark.parametrize("outcome", ["red", "infrastructure", "green"])
def test_historical_result_does_not_change_newer_exact_green_main(
    service, actors, seeded, status, proof, outcome,
):
    _setup(service, actors, seeded, status=status)
    _new_main(service, outcome=proof)
    with service.store.connection(immediate=True) as db:
        service._ensure_guardrail_row(db, "project-1")
        db.execute("UPDATE project_run_guardrails SET consecutive_failures = 2")
    result = _finish(service, outcome)
    assert result["outcome"] == outcome
    assert result["output_tail"] == "retained diagnostic"
    assert result["revert_ticket_id"] is None
    assert _reverts(service) == 0
    guard = service.get_run_guardrails(actors["planner"], "project-1")
    assert not guard["merge_queue_paused"]
    assert guard["consecutive_failures"] == 2


@pytest.mark.parametrize("proof_commit,proof_outcome", [(OLD, "covered_by_gate"), (CURRENT, "red")])
def test_unproven_current_main_does_not_suppress_red(service, actors, seeded, proof_commit, proof_outcome):
    _setup(service, actors, seeded)
    _new_main(service, proof_commit=proof_commit, outcome=proof_outcome)
    result = _finish(service)
    assert result["revert_ticket_id"] is not None
    assert service.get_run_guardrails(actors["planner"], "project-1")["merge_queue_paused"]


def test_current_red_pauses_and_opens_only_one_revert(service, actors, seeded):
    _setup(service, actors, seeded, commit=CURRENT)
    result = _finish(service)
    assert result["revert_ticket_id"]
    assert _finish(service) == result
    assert _reverts(service) == 1
    guard = service.get_run_guardrails(actors["planner"], "project-1")
    assert guard["pause_reason"] == "mainline_check_failed"
    assert guard["consecutive_failures"] == 1


def test_new_proof_does_not_clear_existing_pause(service, actors, seeded):
    _setup(service, actors, seeded)
    with service.store.connection(immediate=True) as db:
        service._ensure_guardrail_row(db, "project-1")
        db.execute("UPDATE project_run_guardrails SET merge_queue_paused = 1, pause_reason = 'existing'")
    _new_main(service)
    _finish(service)
    guard = service.get_run_guardrails(actors["planner"], "project-1")
    assert guard["merge_queue_paused"]
    assert guard["pause_reason"] == "existing"
