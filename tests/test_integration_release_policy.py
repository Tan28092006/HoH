from __future__ import annotations

from pathlib import Path

import pytest

from hoh_control_plane.domain import ReleasePolicyError
from test_integration_async import (
    REGRESSION_COMMAND,
    _candidate_ready_for_integration,
    _claimed_for_integration,
    _git,
    record_regression_run,
)


def _queue(service, actors, ticket, integration):
    return service.queue_merge_candidate(
        actors["integrator"],
        ticket["ticket_id"],
        integration["lease"]["lease_id"],
        integration["ticket"]["version"],
    )


def _mainline(repository: Path) -> str:
    return _git(repository, "rev-parse", "main")


def _merge_jobs(service, ticket_id: str) -> int:
    with service.store.connection() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM merge_jobs WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()[0]


def _add_context(service, ticket, entry_type: str, artifact_commit: str | None, created_at: str):
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO context_entries
                (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                 artifact_commit, actor_id, actor_role, created_at)
            VALUES (?, 'project-1', ?, ?, ?, 'Planner decision', ?, 'planner-01', 'planner', ?)
            """,
            (
                f"ctx_{entry_type}_{created_at}",
                ticket["feature_id"],
                ticket["ticket_id"],
                entry_type,
                artifact_commit,
                created_at,
            ),
        )


def test_a_fully_evidenced_candidate_queues_for_merge(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)

    queued = _queue(service, actors, ticket, integration)

    assert queued["merge_job"]["status"] == "QUEUED"
    assert queued["merge_job"]["candidate_id"] == candidate["candidate"]["candidate_id"]
    # Nothing has moved: queueing is a decision to merge, not the merge itself.
    assert _mainline(repository) != candidate["candidate"]["commit_sha"]


def test_base_drift_is_refused_before_anything_is_queued(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, _candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    (repository / "mainline.txt").write_text("new mainline work\n", encoding="utf-8")
    _git(repository, "add", "mainline.txt")
    _git(repository, "commit", "-m", "Advance mainline")
    advanced = _mainline(repository)
    integration = _claimed_for_integration(service, actors, ticket, verified)

    with pytest.raises(ReleasePolicyError) as excinfo:
        _queue(service, actors, ticket, integration)

    assert excinfo.value.details["reason"] == "base_drift"
    assert _mainline(repository) == advanced
    assert _merge_jobs(service, ticket["ticket_id"]) == 0


def test_a_missing_regression_run_is_refused(service, actors, seeded, tmp_path: Path) -> None:
    _repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "DELETE FROM qa_test_runs WHERE candidate_id = ?",
            (candidate["candidate"]["candidate_id"],),
        )
    integration = _claimed_for_integration(service, actors, ticket, verified)

    with pytest.raises(ReleasePolicyError) as excinfo:
        _queue(service, actors, ticket, integration)

    assert excinfo.value.details["reason"] == "regression_run_missing"
    assert _merge_jobs(service, ticket["ticket_id"]) == 0


def test_a_failed_regression_run_is_refused(service, actors, seeded, tmp_path: Path) -> None:
    _repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    candidate_id = candidate["candidate"]["candidate_id"]
    with service.store.connection(immediate=True) as connection:
        connection.execute("DELETE FROM qa_test_runs WHERE candidate_id = ?", (candidate_id,))
    record_regression_run(service, candidate_id, ticket["ticket_id"], exit_code=1)
    integration = _claimed_for_integration(service, actors, ticket, verified)

    with pytest.raises(ReleasePolicyError) as excinfo:
        _queue(service, actors, ticket, integration)

    assert excinfo.value.details["reason"] == "regression_run_failed"


def test_a_regression_run_for_another_command_does_not_count(
    service, actors, seeded, tmp_path: Path
) -> None:
    """Generic test activity is not the registered gate."""
    _repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE qa_test_runs SET command_digest = 'some-other-command' WHERE candidate_id = ?",
            (candidate["candidate"]["candidate_id"],),
        )
    integration = _claimed_for_integration(service, actors, ticket, verified)

    with pytest.raises(ReleasePolicyError) as excinfo:
        _queue(service, actors, ticket, integration)

    assert excinfo.value.details["reason"] == "regression_run_missing"


@pytest.mark.parametrize("status", ["failed", "flaky", "insufficient_evidence"])
def test_an_unverified_qa_finding_is_refused(
    service, actors, seeded, tmp_path: Path, status: str
) -> None:
    _repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE qa_findings SET status = ? WHERE candidate_id = ? AND criterion = ?",
            (status, candidate["candidate"]["candidate_id"], ticket["acceptance_criteria"][0]),
        )
    integration = _claimed_for_integration(service, actors, ticket, verified)

    with pytest.raises(ReleasePolicyError) as excinfo:
        _queue(service, actors, ticket, integration)

    assert excinfo.value.details["reason"] == "unverified_qa_finding"
    assert status in excinfo.value.details["statuses"]
    assert _merge_jobs(service, ticket["ticket_id"]) == 0


def test_a_criterion_without_candidate_bound_evidence_is_refused(
    service, actors, seeded, tmp_path: Path
) -> None:
    _repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    dropped = ticket["acceptance_criteria"][0]
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "DELETE FROM evidence WHERE candidate_id = ? AND criterion = ?",
            (candidate["candidate"]["candidate_id"], dropped),
        )
    integration = _claimed_for_integration(service, actors, ticket, verified)

    with pytest.raises(ReleasePolicyError) as excinfo:
        _queue(service, actors, ticket, integration)

    assert excinfo.value.details["reason"] == "missing_qa_evidence"
    assert excinfo.value.details["criteria"] == [dropped]


def test_a_candidate_specific_hold_blocks_the_merge(
    service, actors, seeded, tmp_path: Path
) -> None:
    _repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    _add_context(
        service, ticket, "integration_hold",
        candidate["candidate"]["commit_sha"], "2026-02-01T00:00:00Z",
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)

    with pytest.raises(ReleasePolicyError) as excinfo:
        _queue(service, actors, ticket, integration)

    assert excinfo.value.details["reason"] == "integration_hold"
    assert _merge_jobs(service, ticket["ticket_id"]) == 0


def test_an_authorization_for_another_sha_cannot_clear_the_hold(
    service, actors, seeded, tmp_path: Path
) -> None:
    _repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    _add_context(
        service, ticket, "integration_hold",
        candidate["candidate"]["commit_sha"], "2026-02-01T00:00:00Z",
    )
    _add_context(service, ticket, "integration_authorization", "f" * 40, "2026-02-02T00:00:00Z")
    integration = _claimed_for_integration(service, actors, ticket, verified)

    with pytest.raises(ReleasePolicyError) as excinfo:
        _queue(service, actors, ticket, integration)

    assert excinfo.value.details["reason"] == "integration_hold"


def test_an_authorization_naming_this_candidate_clears_the_hold(
    service, actors, seeded, tmp_path: Path
) -> None:
    _repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    commit = candidate["candidate"]["commit_sha"]
    _add_context(service, ticket, "integration_hold", commit, "2026-02-01T00:00:00Z")
    _add_context(service, ticket, "integration_authorization", commit, "2026-02-02T00:00:00Z")
    integration = _claimed_for_integration(service, actors, ticket, verified)

    queued = _queue(service, actors, ticket, integration)

    assert queued["merge_job"]["status"] == "QUEUED"


def test_an_authorization_issued_before_the_hold_does_not_clear_it(
    service, actors, seeded, tmp_path: Path
) -> None:
    _repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    commit = candidate["candidate"]["commit_sha"]
    _add_context(service, ticket, "integration_authorization", commit, "2026-02-01T00:00:00Z")
    _add_context(service, ticket, "integration_hold", commit, "2026-02-02T00:00:00Z")
    integration = _claimed_for_integration(service, actors, ticket, verified)

    with pytest.raises(ReleasePolicyError) as excinfo:
        _queue(service, actors, ticket, integration)

    assert excinfo.value.details["reason"] == "integration_hold"


def test_a_hold_for_another_candidate_does_not_block_this_one(
    service, actors, seeded, tmp_path: Path
) -> None:
    _repository, ticket, _candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    _add_context(service, ticket, "integration_hold", "e" * 40, "2026-02-01T00:00:00Z")
    integration = _claimed_for_integration(service, actors, ticket, verified)

    queued = _queue(service, actors, ticket, integration)

    assert queued["merge_job"]["status"] == "QUEUED"


def test_the_gate_never_holds_a_write_lock_while_git_runs(
    service, actors, seeded, tmp_path: Path
) -> None:
    """A concurrent writer must be able to commit while the gate inspects Git."""
    _repository, ticket, _candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    observed: list[str] = []
    real_descends = service.git_worktrees.commit_descends_from

    def descends_while_another_writer_commits(**kwargs):
        with service.store.connection(immediate=True) as connection:
            connection.execute(
                "UPDATE tickets SET updated_at = ? WHERE ticket_id = ?",
                ("2026-03-01T00:00:00Z", ticket["ticket_id"]),
            )
        observed.append("wrote")
        return real_descends(**kwargs)

    service.git_worktrees.commit_descends_from = descends_while_another_writer_commits
    try:
        queued = _queue(service, actors, ticket, integration)
    finally:
        service.git_worktrees.commit_descends_from = real_descends

    assert observed
    assert queued["merge_job"]["status"] == "QUEUED"


def test_the_refusal_reason_carries_no_evidence_content(
    service, actors, seeded, tmp_path: Path
) -> None:
    """A typed reason is auditable; it must not leak observations or lease ids."""
    _repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE qa_findings SET status = 'failed', observation = ? WHERE candidate_id = ?",
            ("secret-token-abc123 leaked observation", candidate["candidate"]["candidate_id"]),
        )
    integration = _claimed_for_integration(service, actors, ticket, verified)

    with pytest.raises(ReleasePolicyError) as excinfo:
        _queue(service, actors, ticket, integration)

    rendered = f"{excinfo.value} {excinfo.value.details}"
    assert "secret-token-abc123" not in rendered
    assert integration["lease"]["lease_id"] not in rendered
    assert excinfo.value.code == "release_policy_blocked"
