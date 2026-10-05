"""A merge that reuses a QA run does not wait behind a QA suite (wave 6E).

The host runs one project suite at a time. A merge whose gate reuses a passing
QA run of the same commit and command needs only seconds of Git, yet it used to
queue for the same slot as a QA suite that may run for 2,100 seconds. It now
takes its own merge lane; a merge that must execute the suite still takes the
test slot, so one heavy test process at a time still holds.
"""
from __future__ import annotations

import time
from concurrent.futures import Future
from pathlib import Path

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
    from test_integration_async import (
        _candidate_ready_for_integration, _claimed_for_integration, _mark_run_as_qa_job,
    )

    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    _mark_run_as_qa_job(service, candidate, ticket)
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(
            actors["integrator"], ticket["ticket_id"]
        )["context_snapshot_id"],
    )
    return repository, candidate, queued["merge_job"]["merge_job_id"]


def test_a_reuse_merge_completes_while_a_long_qa_job_holds_the_slot(
    service, actors, seeded, tmp_path: Path
) -> None:
    from test_integration_async import _git

    repository, candidate, merge_job_id = _queued_reuse_merge(service, actors, seeded, tmp_path)
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
    assert '"gate_mode": "reused_qa_run"' in proof["payload"]
    assert execution["outcome"] == "exit 0"


def test_a_merge_admitted_without_the_slot_never_runs_a_suite(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    from test_integration_async import (
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
