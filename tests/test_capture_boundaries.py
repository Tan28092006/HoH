"""Consumption proof and two-phase capture fencing, with private state only."""
import csv
import os
from pathlib import Path
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from types import SimpleNamespace
import venv

import pytest

from helpers.qa import candidate_waiting_for_qa_run
from hoh_control_plane.domain import ConflictError, DomainError
from hoh_control_plane.qa_runner import ProjectTestRunner
from hoh_control_plane.supervisor import Supervisor


@pytest.fixture
def private_runtime(tmp_path):
    root = tmp_path / "runtime"
    venv.EnvBuilder(with_pip=False).create(root)
    python = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    site = root / ("Lib/site-packages" if os.name == "nt" else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
    return python, site


@pytest.mark.parametrize("case", ["external", "omitted", "startup-E", "editable-name"])
def test_module_consumption_is_not_proved_by_inventory(service, actors, seeded, tmp_path, private_runtime, case):
    python, site = private_runtime
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    metadata = site / "hoh_control_plane-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: hoh-control-plane\nVersion: 1.0\n", encoding="utf-8")
    dependency = site / "omitted.py"
    dependency.write_text("VALUE = 1\n", encoding="utf-8")
    entrypoint = candidate / "check_env.py"
    if case == "external":
        dependency = tmp_path / "external.py"
        dependency.write_text("VALUE = 1\n", encoding="utf-8")
        entrypoint.write_text(f"import sys\nsys.path.insert(0, {str(tmp_path)!r})\nfrom external import VALUE\nassert VALUE == 1\n", encoding="utf-8")
    else:
        entrypoint.write_text("from omitted import VALUE\nassert VALUE == 1\n", encoding="utf-8")
    if case == "editable-name":
        (metadata / "direct_url.json").write_text('{"dir_info":{"editable":true},"url":"file:///unproved"}', encoding="utf-8")
    with (metadata / "RECORD").open("w", newline="", encoding="utf-8") as stream:
        csv.writer(stream).writerows((p, "", "") for p in (
            "hoh_control_plane-1.0.dist-info/METADATA", "hoh_control_plane-1.0.dist-info/RECORD"))
    (site / "check_env.py").write_bytes(entrypoint.read_bytes())
    runner = ProjectTestRunner(tmp_path / "reports")
    flags = ["-E"] if case == "startup-E" else []
    command = [str(python), *flags, "-B", "-m", "check_env"]
    before = entrypoint.read_bytes(), (metadata / "RECORD").read_bytes()
    first = runner.execute(command, str(candidate), 30)
    if case in {"external", "omitted"}:
        t, submitted, owner = candidate_waiting_for_qa_run(service, actors, seeded, tmp_path, command)
        args = (actors["qa"], t["ticket_id"], owner["lease"]["lease_id"])
        queued = service.queue_project_tests(*args)["test_run"]
        completed = service.execute_queued_project_test(queued["test_run_id"])
        assert completed["exit_code"] == 0 and completed["execution_environment_digest"] is None
    dependency.write_text("VALUE = 2\n", encoding="utf-8")
    second = runner.execute(command, str(candidate), 30)
    assert (first.exit_code, second.exit_code) == (0, 1)
    assert first.environment_manifest.get("execution_environment_digest") is None
    assert second.environment_manifest.get("execution_environment_digest") is None
    assert before == (entrypoint.read_bytes(), (metadata / "RECORD").read_bytes())
    if case in {"external", "omitted"}:
        assert service._reusable_qa_run(submitted["candidate"]["candidate_id"], command, 120) is None
        again = service.queue_project_tests(*args)
        assert not again.get("reused") and again["test_run"]["test_run_id"] != queued["test_run_id"]


def test_literal_capture_and_execution_share_isolated_no_site_startup(tmp_path, private_runtime):
    python, site = private_runtime
    (site / "sitecustomize.py").write_text("raise SystemExit(99)\n", encoding="utf-8")
    runner = ProjectTestRunner(tmp_path / "reports")
    command = [str(python), "-c", "print('passed')"]
    key = runner.execution_environment_digest(command, 30)
    result = runner.execute(command, str(tmp_path), 30)
    assert key and result.environment_manifest["execution_environment_digest"] == key
    assert result.exit_code == 0 and result.stdout.strip() == "passed"
    (site / "sitecustomize.py").write_text("raise SystemExit(42)\n", encoding="utf-8")
    (site / "unpackaged.py").write_text("raise RuntimeError('not consumed')\n", encoding="utf-8")
    assert runner.execution_environment_digest(command, 30) == key
    assert runner.execute(command, str(tmp_path), 30).exit_code == 0


def test_unproved_runtime_preserves_ordinary_execution(tmp_path, private_runtime, monkeypatch):
    python, site = private_runtime
    (site / "sitecustomize.py").write_text("print('ordinary-startup')\n", encoding="utf-8")
    runner = ProjectTestRunner(tmp_path / "reports")
    monkeypatch.setattr(runner, "execution_environment_digest", lambda *a, **k: None)
    result = runner.execute([str(python), "-c", "print('passed')"], str(tmp_path), 30)
    assert result.exit_code == 0 and result.stdout.splitlines() == ["ordinary-startup", "passed"]
    assert result.environment_manifest["execution_environment_digest"] is None


@pytest.mark.parametrize("intervention", [None, "lease", "candidate", "profile", "retirement"])
def test_queue_capture_does_not_hold_writer_and_fences_changes(service, actors, seeded, tmp_path, monkeypatch, intervention):
    t, _, owner = candidate_waiting_for_qa_run(service, actors, seeded, tmp_path,
        [sys.executable, "-c", "print('passed')"])
    key = service.project_tests.execution_environment_digest([sys.executable, "-c", "print('passed')"], 120)
    entered, release = threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5), "Test must release the external capture"
        return key
    monkeypatch.setattr(service.project_tests, "execution_environment_digest", blocked)
    def concurrent_write():
        service.create_feature(actors["planner"], "project-1", "unrelated", "Unrelated", "Independent lifecycle write")
        if intervention:
            with service.store.connection(immediate=True) as db:
                if intervention == "lease":
                    db.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00Z' WHERE lease_id=?", (owner["lease"]["lease_id"],))
                elif intervention == "candidate":
                    db.execute("UPDATE tickets SET candidate_commit=? WHERE ticket_id=?", ("e" * 40, t["ticket_id"]))
                elif intervention == "profile":
                    db.execute("UPDATE candidate_test_profiles SET timeout_seconds=119")
                else:
                    reconciliation = service._open_commit_reconciliation(db, actors["planner"], "project-1",
                        commit="e" * 40, subject="Fixture retirement", relation="qa_failed",
                        observation_ref="evidence://fixture/retirement", frozen_ticket_ids=[t["ticket_id"]])
                    db.execute("INSERT INTO ticket_retirement_requests(request_id,project_id,ticket_id,reconciliation_id,target_status,reason,requested_by,status,requested_at) "
                        "VALUES ('fixture-retire','project-1',?,?,'CANCELLED','Fixture','planner','PENDING','2026-01-01T00:00:00Z')",
                        (t["ticket_id"], reconciliation))
    with ThreadPoolExecutor(max_workers=2) as pool:
        queued = pool.submit(service.queue_project_tests, actors["qa"], t["ticket_id"], owner["lease"]["lease_id"])
        assert entered.wait(3)
        writer = pool.submit(concurrent_write)
        try:
            try:
                writer.result(timeout=2)
                independent = True
            except FutureTimeout:
                independent = False
        finally:
            release.set()
        writer.result(timeout=5)
        if intervention:
            with pytest.raises(DomainError):
                queued.result(timeout=5)
        else:
            assert queued.result(timeout=5)["test_run"]["status"] == "QUEUED"
    assert independent, "Fingerprint work held the SQLite writer lock"
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM qa_test_jobs").fetchone()[0] == (0 if intervention else 1)


@pytest.mark.parametrize("phase,intervention", [(p, None) for p in ("poll", "worker", "complete", "supervisor")]
    + [(p, "lease") for p in ("worker", "complete", "supervisor")] + [("poll", "capture-failure")])
def test_authority_capture_releases_writer_and_rechecks(service, actors, seeded, tmp_path, monkeypatch, phase, intervention):
    command = [sys.executable, "-c", "print('passed')"]
    t, submitted, owner = candidate_waiting_for_qa_run(service, actors, seeded, tmp_path, command)
    args = (actors["qa"], t["ticket_id"], owner["lease"]["lease_id"])
    run = service.queue_project_tests(*args)["test_run"]
    run_id = run["test_run_id"]
    key = run["execution_environment_digest"]
    assert key
    execution = None
    if phase == "complete":
        execution = service.project_tests.execute(command, str(tmp_path), 120)
        with service.store.connection(immediate=True) as db:
            db.execute("UPDATE qa_test_jobs SET status='RUNNING' WHERE test_run_id=?", (run_id,))
    entered, release = threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return None if intervention == "capture-failure" else key
    monkeypatch.setattr(service.project_tests, "execution_environment_digest", blocked)
    decisions = []
    def managed(*args, **kwargs):
        decisions.append(kwargs["cancel"]())
        return SimpleNamespace(cancelled=decisions[-1], timed_out=False, returncode=0)
    monkeypatch.setattr("hoh_control_plane.supervisor.run_managed", managed)
    def consume():
        if phase == "poll":
            return service.get_project_test_run(*args, run_id)
        if phase == "worker":
            return service.execute_queued_project_test(run_id)
        if phase == "complete":
            return service._complete_queued_project_test(run_id, execution)
        return Supervisor(service)._execute("fixture-execution", "qa", run_id)
    def write():
        service.create_feature(actors["planner"], "project-1", "unrelated", "Unrelated", "Independent lifecycle")
        if intervention == "lease":
            with service.store.connection(immediate=True) as db:
                db.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00Z' WHERE lease_id=?", (owner["lease"]["lease_id"],))
    with ThreadPoolExecutor(max_workers=2) as pool:
        consuming = pool.submit(consume)
        assert entered.wait(3)
        writing = pool.submit(write)
        try:
            try:
                writing.result(timeout=2)
                independent = True
            except FutureTimeout:
                independent = False
        finally:
            release.set()
        writing.result(timeout=5)
        if intervention == "capture-failure":
            with pytest.raises(ConflictError):
                consuming.result(timeout=10)
        else:
            result = consuming.result(timeout=10)
            if phase in {"worker", "complete"}:
                assert result["status"] == ("FAILED" if intervention else "COMPLETED")
    assert independent, f"{phase} held a writer lock across external capture"
    if phase == "supervisor":
        assert decisions == [bool(intervention)]
    with service.store.connection() as db:
        expected = int(phase in {"worker", "complete"} and intervention is None)
        assert db.execute("SELECT COUNT(*) FROM qa_test_runs").fetchone()[0] == expected
        if intervention == "capture-failure":
            assert service._qa_test_job(db, run_id)["status"] == "QUEUED"
            assert db.execute("SELECT status FROM qa_reviews WHERE review_id=?", (owner["review"]["review_id"],)).fetchone()[0] == "IN_PROGRESS"


def test_merge_capture_rechecks_a_newer_failed_attempt(service, actors, seeded, tmp_path, monkeypatch):
    from helpers.integration import _candidate_ready_for_integration, _mark_run_as_qa_job, REGRESSION_COMMAND
    _, t, submitted, _ = _candidate_ready_for_integration(service, actors, seeded, tmp_path)
    run_id = _mark_run_as_qa_job(service, submitted, t)
    candidate_id = submitted["candidate"]["candidate_id"]
    key = service.project_tests.execution_environment_digest(REGRESSION_COMMAND, 60)
    entered, release = threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return key
    monkeypatch.setattr(service.project_tests, "execution_environment_digest", blocked)
    def newer():
        service.create_feature(actors["planner"], "project-1", "unrelated", "Unrelated", "Independent lifecycle")
        with service.store.connection(immediate=True) as db:
            db.execute("INSERT INTO qa_test_jobs(test_run_id,project_id,candidate_id,ticket_id,lease_id,test_command,command_digest,timeout_seconds,worktree_path,candidate_commit,requested_by,status,created_at,execution_environment_digest) "
                "SELECT test_run_id||'_newer',project_id,candidate_id,ticket_id,lease_id,test_command,command_digest,timeout_seconds,worktree_path,candidate_commit,requested_by,'FAILED','2099-01-01T00:00:00Z',execution_environment_digest FROM qa_test_jobs WHERE test_run_id=?", (run_id,))
    with ThreadPoolExecutor(max_workers=2) as pool:
        reusing = pool.submit(service._reusable_qa_run, candidate_id, REGRESSION_COMMAND, 60)
        assert entered.wait(3)
        writer = pool.submit(newer)
        try:
            writer.result(timeout=2)
        finally:
            release.set()
        assert reusing.result(timeout=5) is None
