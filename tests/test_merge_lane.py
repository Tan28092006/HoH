"""A merge that reuses a QA run does not wait behind a QA suite (wave 6E).

The host runs one project suite at a time. A merge whose gate reuses a passing
QA run of the same commit and command needs only seconds of Git, yet it used to
queue for the same slot as a QA suite that may run for 2,100 seconds. It now
takes its own merge lane; a merge that must execute the suite still takes the
test slot, so one heavy test process at a time still holds.
"""
from __future__ import annotations

import time
import json
from concurrent.futures import Future
from pathlib import Path

import pytest

from hoh_control_plane.supervisor import MERGE_LANE, TEST_LANE, Supervisor


def _hold_test_slot(supervisor, identifier: str = "q-long") -> Future:
    """A QA suite that is still running: it holds the test slot until resolved."""
    running = Future()
    supervisor.active[("qa", identifier)] = running
    supervisor.lanes[("qa", identifier)] = TEST_LANE
    return running


def test_a_reuse_merge_is_admitted_beside_a_running_qa_suite(service, monkeypatch) -> None:
    with Supervisor(service, max_workers=3) as supervisor:
        long_qa = _hold_test_slot(supervisor)
        monkeypatch.setattr(service, "merge_gate_reuses_qa_run", lambda job: job.startswith("m-reuse"))
        monkeypatch.setattr(supervisor, "_pending", lambda: [
            ("merge", "m-suite"), ("merge", "m-reuse-1"), ("merge", "m-reuse-2"), ("qa", "q2"),
        ])
        submitted = []

        def submit(kind, identifier):
            submitted.append((kind, identifier))
            return Future()

        monkeypatch.setattr(supervisor, "_submit", submit)
        supervisor.tick()

        # The suite merge and the second QA wait for the slot; the reuse merge
        # runs now; merges stay one at a time.
        assert submitted == [("merge", "m-reuse-1")]
        assert supervisor.lanes[("merge", "m-reuse-1")] == MERGE_LANE
        assert not long_qa.done()
        # Fake futures have no work to drain at fixture teardown.
        for future in supervisor.active.values():
            future.set_result(None)


def test_a_merge_that_runs_the_suite_still_holds_the_test_slot(service, monkeypatch) -> None:
    with Supervisor(service, max_workers=3) as supervisor:
        monkeypatch.setattr(service, "merge_gate_reuses_qa_run", lambda job: False)
        monkeypatch.setattr(supervisor, "_pending", lambda: [("merge", "m-suite"), ("qa", "q1")])
        submitted = []

        def submit(kind, identifier):
            submitted.append((kind, identifier))
            return Future()

        monkeypatch.setattr(supervisor, "_submit", submit)
        supervisor.tick()

        assert submitted == [("merge", "m-suite")]
        assert supervisor.lanes[("merge", "m-suite")] == TEST_LANE
        for future in supervisor.active.values():
            future.set_result(None)


def _queued_reuse_merge(service, actors, seeded, tmp_path: Path):
    from helpers.integration import (
        _candidate_ready_for_integration, _claimed_for_integration, _mark_run_as_qa_job,
    )

    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    run_id = _mark_run_as_qa_job(service, candidate, ticket)
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(
            actors["integrator"], ticket["ticket_id"]
        )["context_snapshot_id"],
    )
    return repository, candidate, queued["merge_job"]["merge_job_id"], run_id


def test_a_reuse_merge_completes_while_a_long_qa_job_holds_the_slot(
    service, actors, seeded, tmp_path: Path
) -> None:
    from helpers.integration import _git

    repository, candidate, merge_job_id, run_id = _queued_reuse_merge(service, actors, seeded, tmp_path)
    assert service.merge_gate_reuses_qa_run(merge_job_id) is True

    with Supervisor(service, max_workers=2) as supervisor:
        long_qa = _hold_test_slot(supervisor)
        deadline = time.monotonic() + 120
        try:
            while True:
                supervisor.tick()
                with service.store.connection() as connection:
                    job = service._merge_job(connection, merge_job_id)
                if job["status"] in {"COMPLETED", "FAILED"} or time.monotonic() > deadline:
                    break
                time.sleep(0.1)
            # Let the merge worker's thread finish before the pool shuts down.
            for key, future in list(supervisor.active.items()):
                if key[0] == "merge":
                    future.result(timeout=60)
            assert not long_qa.done(), "The test slot must remain occupied until the actual merge completes"
        finally:
            long_qa.set_result(None)

    assert job["status"] == "COMPLETED", job
    assert job["merge_status"] == "merged"
    assert _git(repository, "rev-parse", "main") == candidate["candidate"]["commit_sha"]
    with service.store.connection() as connection:
        proof = connection.execute(
            "SELECT payload FROM audit_events WHERE event_type = 'integration.gate_passed'"
        ).fetchone()
        execution = connection.execute(
            "SELECT outcome FROM worker_executions WHERE job_kind = 'merge' AND job_id = ?",
            (merge_job_id,),
        ).fetchone()
        assert connection.execute("SELECT COUNT(*) FROM qa_test_runs WHERE candidate_id=?", (candidate["candidate"]["candidate_id"],)).fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM qa_test_jobs WHERE candidate_id=?", (candidate["candidate"]["candidate_id"],)).fetchone()[0] == 1
    assert '"gate_mode": "reused_qa_run"' in proof["payload"]
    assert json.loads(proof["payload"])["test_run_id"] == run_id
    assert execution["outcome"] == "exit 0"


def test_a_merge_admitted_without_the_slot_never_runs_a_suite(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    from helpers.integration import (
        _candidate_ready_for_integration, _claimed_for_integration,
    )

    _repository, ticket, _candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    merge_job_id = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(
            actors["integrator"], ticket["ticket_id"]
        )["context_snapshot_id"],
    )["merge_job"]["merge_job_id"]
    monkeypatch.setattr(
        service.project_tests, "execute",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("suite ran outside the test slot")),
    )

    assert service.merge_gate_reuses_qa_run(merge_job_id) is False
    left = service.execute_queued_merge(merge_job_id, run_suite=False)

    # Untouched and still queued, for the supervisor to admit through the slot.
    assert left["status"] == "QUEUED"
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT 1 FROM merge_intents WHERE merge_job_id = ?", (merge_job_id,)
        ).fetchone() is None


@pytest.mark.parametrize("identity", [None, "f" * 64], ids=["unknown", "old-partial-or-mismatch"])
def test_unknown_or_mismatched_capture_cannot_use_the_merge_only_lane(service, actors, seeded, tmp_path, monkeypatch, identity):
    from helpers.integration import _git
    repository, candidate, job_id, run_id = _queued_reuse_merge(service, actors, seeded, tmp_path)
    before = _git(repository, "rev-parse", "main")
    with service.store.connection(immediate=True) as db:
        db.execute("UPDATE qa_test_jobs SET execution_environment_digest=? WHERE test_run_id=?", (identity, run_id))
    monkeypatch.setattr(service.project_tests, "execute", lambda *a, **k: pytest.fail("No-slot worker must not execute"))
    assert service.merge_gate_reuses_qa_run(job_id) is False
    assert service.execute_queued_merge(job_id, run_suite=False)["status"] == "QUEUED"
    assert _git(repository, "rev-parse", "main") == before
    assert before != candidate["candidate"]["commit_sha"]
    with service.store.connection() as db:
        assert db.execute("SELECT 1 FROM merge_intents WHERE merge_job_id=?", (job_id,)).fetchone() is None
        assert db.execute("SELECT execution_environment_digest FROM qa_test_jobs WHERE test_run_id=?", (run_id,)).fetchone()[0] == identity


def test_late_capture_miss_defers_without_the_slot_then_resumes(service, actors, seeded, tmp_path, monkeypatch):
    from helpers.integration import _git
    repository, candidate, job_id, _ = _queued_reuse_merge(service, actors, seeded, tmp_path)
    before = _git(repository, "rev-parse", "main")
    original = service.project_tests.execution_environment_digest
    key = original(["python", "-c", "pass"], 60)
    calls = []
    def changing(*args, **kwargs):
        calls.append(True)
        return key if len(calls) == 1 else None
    monkeypatch.setattr(service.project_tests, "execution_environment_digest", changing)
    execute = service._execute_frozen_profile
    monkeypatch.setattr(service, "_execute_frozen_profile", lambda *a, **k: pytest.fail("Late miss ran outside the test slot"))
    deferred = service.execute_queued_merge(job_id, run_suite=False)
    assert len(calls) >= 2
    assert deferred["status"] == "QUEUED" and deferred["started_at"] is None
    assert _git(repository, "rev-parse", "main") == before
    with service.store.connection() as db:
        assert db.execute("SELECT 1 FROM merge_intents WHERE merge_job_id=?", (job_id,)).fetchone() is None
        assert db.execute("SELECT COUNT(*) FROM leases WHERE lease_id=?", (deferred["lease_id"],)).fetchone()[0] == 1
    monkeypatch.setattr(service, "_execute_frozen_profile", execute)
    resumed = service.execute_queued_merge(job_id, run_suite=True)
    assert resumed["status"] == "COMPLETED" and resumed["merge_status"] == "merged"
    assert _git(repository, "rev-parse", "main") == candidate["candidate"]["commit_sha"]
