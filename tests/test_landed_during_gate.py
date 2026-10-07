"""R7: a candidate landed by hand while its gate runs is delivered work, not drift.

The fast-forward finds main moved and reports ``(False, main)``. Before that is
classified as base drift (REWORK), the merge asks Git whether the candidate is
already on the default branch.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from test_integration_async import (
    _candidate_ready_for_integration,
    _claimed_for_integration,
    _git,
)


def _queued(service, actors, seeded, tmp_path):
    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path)
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(
            actors["integrator"], ticket["ticket_id"])["context_snapshot_id"])
    return repository, ticket, candidate, queued["merge_job"]["merge_job_id"]


def _during_the_gate(service, monkeypatch, action):
    real_execute = service.project_tests.execute

    def execute(*arguments, **keywords):
        action()
        return real_execute(*arguments, **keywords)

    monkeypatch.setattr(service.project_tests, "execute", execute)


def _status(service, ticket_id):
    with service.store.connection() as connection:
        return connection.execute(
            "SELECT status FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()["status"]


@pytest.mark.parametrize("commit_on_top", [False, True])
def test_a_candidate_landed_by_hand_during_the_gate_is_not_sent_to_rework(
    service, actors, seeded, tmp_path: Path, monkeypatch, commit_on_top
):
    repository, ticket, candidate, merge_job_id = _queued(service, actors, seeded, tmp_path)
    commit = candidate["candidate"]["commit_sha"]

    def land_by_hand():
        _git(repository, "merge", "--ff-only", commit)
        if commit_on_top:
            (repository / "later.txt").write_text("after the landing\n", encoding="utf-8")
            _git(repository, "add", "later.txt")
            _git(repository, "commit", "-m", "Later work on top")

    _during_the_gate(service, monkeypatch, land_by_hand)

    job = service.execute_queued_merge(merge_job_id)

    assert job["status"] == "FAILED"
    assert job["error_message"].startswith("candidate_already_on_main")
    assert _status(service, ticket["ticket_id"]) != "REWORK"
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT 1 FROM audit_events WHERE event_type = 'integration.base_drift'"
        ).fetchone() is None
    # The hand landing is then recorded as ungated, not as this gate's approval.
    version = service.get_ticket_context(
        actors["integrator"], ticket["ticket_id"])["ticket"]["version"]
    recorded = service.record_external_landing(
        actors["integrator"], ticket["ticket_id"], version, commit,
        "Landed by hand while the merge gate ran.")
    assert _status(service, ticket["ticket_id"]) == "DONE"
    assert recorded


def test_unrelated_work_landing_during_the_gate_is_still_drift(
    service, actors, seeded, tmp_path: Path, monkeypatch
):
    repository, ticket, _candidate, merge_job_id = _queued(service, actors, seeded, tmp_path)

    def outside_push():
        (repository / "outside.txt").write_text("someone else's work\n", encoding="utf-8")
        _git(repository, "add", "outside.txt")
        _git(repository, "commit", "-m", "Outside work")

    _during_the_gate(service, monkeypatch, outside_push)

    job = service.execute_queued_merge(merge_job_id)

    assert (job["status"], job["merge_status"]) == ("COMPLETED", "base_drift")
    assert _status(service, ticket["ticket_id"]) == "REWORK"
