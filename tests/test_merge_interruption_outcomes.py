"""Review A7: each merge interruption has its own outcome; none is mislabelled.

A barrier or a lost lease after the gate, and a dirty shared checkout, all
stopped the merge before Git changed anything, yet each became an
"integration infrastructure failure" that blocked the ticket (the dirty
checkout went through uncertain-side-effect reconciliation first). A broken
pyproject.toml in the candidate escaped as an unclassified exception. Now the
preconditions fail cleanly and the candidate defect goes back as source rework.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_integration_gate import (
    _PASSING_REGRESSION,
    _gate_ready_candidate,
    _git,
    _merge,
)

from hoh_control_plane.qa_runner import INFRASTRUCTURE_REASONS, ProjectTestRunner, TestExecution


def _passing(**overrides):
    values = dict(command_digest="a" * 64, exit_code=0, timed_out=False, duration_ms=1,
                  stdout="", stderr="", termination_reason="passed")
    values.update(overrides)
    return TestExecution(**values)


def _open_barrier(service):
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO repository_reconciliations
                (reconciliation_id, project_id, observation_ref, observed_commits,
                 ticket_versions, status, created_at)
            VALUES ('recon_a7', 'project-1', 'git:observed:a7', ?, '{}', 'PENDING', ?)
            """,
            (json.dumps([{"commit": "c" * 40}]),
             datetime.now(UTC).isoformat().replace("+00:00", "Z")),
        )


def _expire_leases(service, ticket_id):
    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    with service.store.connection(immediate=True) as connection:
        connection.execute("UPDATE leases SET expires_at = ? WHERE ticket_id = ?", (past, ticket_id))


def _open_planner_jobs(service, ticket_id):
    with service.store.connection() as connection:
        return connection.execute(
            "SELECT COUNT(*) AS n FROM planner_jobs WHERE ticket_id = ? AND status != 'RESOLVED'",
            (ticket_id,),
        ).fetchone()["n"]


def _assert_clean_precondition(service, outcome, ticket, repository, before, expected):
    assert outcome["status"] == "FAILED"
    assert outcome["error_message"].startswith(f"precondition_failed:{expected}:")
    assert "Nothing changed in Git" in outcome["error_message"]
    assert "infrastructure" not in outcome["error_message"]
    assert outcome["ticket"]["status"] == "MERGE_READY"
    assert outcome["ticket"]["blocked_reason"] is None
    assert _git(repository, "rev-parse", "main") == before
    assert _open_planner_jobs(service, ticket["ticket_id"]) == 0


def test_a_barrier_opened_during_the_gate_fails_the_merge_cleanly(
    service, actors, seeded, tmp_path, monkeypatch
):
    repository, ticket, _candidate, claim = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_PASSING_REGRESSION)
    before = _git(repository, "rev-parse", "main")

    def gate(*_args):
        _open_barrier(service)
        return _passing()

    monkeypatch.setattr(service.project_tests, "execute", gate)
    outcome = _merge(service, actors["integrator"], ticket, claim)

    _assert_clean_precondition(service, outcome, ticket, repository, before, "barrier_pending")
    with service.store.connection() as connection:
        # The Integrator keeps its lease and queues the merge again later.
        assert connection.execute(
            "SELECT 1 FROM leases WHERE lease_id = ?", (claim["lease"]["lease_id"],)
        ).fetchone() is not None


def test_a_lease_lost_during_the_gate_returns_the_ticket_to_the_integrator_queue(
    service, actors, seeded, tmp_path, monkeypatch
):
    repository, ticket, _candidate, claim = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_PASSING_REGRESSION)
    before = _git(repository, "rev-parse", "main")

    def gate(*_args):
        _expire_leases(service, ticket["ticket_id"])
        return _passing()

    monkeypatch.setattr(service.project_tests, "execute", gate)
    outcome = _merge(service, actors["integrator"], ticket, claim)

    _assert_clean_precondition(service, outcome, ticket, repository, before, "lease_expired")
    with service.store.connection() as connection:
        queued = connection.execute(
            "SELECT target_role FROM work_queue WHERE ticket_id = ? AND status IN ('pending', 'offered')",
            (ticket["ticket_id"],),
        ).fetchone()
    assert queued["target_role"] == "integrator"


def test_a_barrier_before_the_worker_starts_is_a_precondition_too(
    service, actors, seeded, tmp_path, monkeypatch
):
    from merge_helpers import seed_release_regression_run

    repository, ticket, _candidate, claim = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_PASSING_REGRESSION)
    before = _git(repository, "rev-parse", "main")
    monkeypatch.setattr(service.project_tests, "execute", lambda *_a: _passing())
    seed_release_regression_run(service, ticket["ticket_id"])
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"], claim["lease"]["lease_id"],
        claim["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(
            actors["integrator"], ticket["ticket_id"])["context_snapshot_id"],
    )
    _open_barrier(service)

    job = service.execute_queued_merge(queued["merge_job"]["merge_job_id"])
    with service.store.connection() as connection:
        current = service._ticket(connection, ticket["ticket_id"])

    _assert_clean_precondition(
        service, {**job, "ticket": current}, ticket, repository, before, "barrier_pending")


def test_a_dirty_shared_checkout_is_a_certain_precondition_not_a_reconciliation(
    service, actors, seeded, tmp_path, monkeypatch
):
    repository, ticket, _candidate, claim = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_PASSING_REGRESSION)
    before = _git(repository, "rev-parse", "main")
    (Path(repository) / "stray.txt").write_text("left behind\n", encoding="utf-8")
    monkeypatch.setattr(service.project_tests, "execute", lambda *_a: _passing())
    reconciled = []
    monkeypatch.setattr(service, "_reconcile_merge_locked",
                        lambda job_id: reconciled.append(job_id))

    outcome = _merge(service, actors["integrator"], ticket, claim)

    assert reconciled == []
    assert outcome["status"] == "FAILED"
    assert outcome["error_message"].startswith("git_precondition_failed:")
    assert "must be clean" in outcome["error_message"]
    assert "Nothing changed in Git" in outcome["error_message"]
    assert _git(repository, "rev-parse", "main") == before


def test_an_unparseable_candidate_pyproject_goes_back_as_source_rework(
    service, actors, seeded, tmp_path, monkeypatch
):
    repository, ticket, _candidate, claim = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_PASSING_REGRESSION)
    before = _git(repository, "rev-parse", "main")
    monkeypatch.setattr(service.project_tests, "execute", lambda *_a: _passing(
        exit_code=None, termination_reason="candidate_config_invalid",
        stderr="candidate_config_invalid: pyproject.toml cannot be parsed: Expected '='"))

    outcome = _merge(service, actors["integrator"], ticket, claim)

    assert outcome["ticket"]["status"] == "REWORK"
    assert "Candidate configuration is invalid" in outcome["ticket"]["blocked_reason"]
    assert "pyproject.toml" in outcome["ticket"]["blocked_reason"]
    assert _git(repository, "rev-parse", "main") == before


def test_the_runner_classifies_a_broken_pyproject_as_a_candidate_failure(tmp_path):
    worktree = tmp_path / "candidate"
    worktree.mkdir()
    (worktree / "pyproject.toml").write_text("[tool.hoh\nbroken = \n", encoding="utf-8")
    runner = ProjectTestRunner(tmp_path / "evidence")

    execution = runner.execute([sys.executable, "-m", "pytest", "-q"], str(worktree), 30)

    assert execution.termination_reason == "candidate_config_invalid"
    assert execution.termination_reason not in INFRASTRUCTURE_REASONS
    assert execution.environment_manifest["classification"] == "candidate_failure"
    assert execution.exit_code is None
    assert "pyproject.toml cannot be parsed" in execution.stderr
