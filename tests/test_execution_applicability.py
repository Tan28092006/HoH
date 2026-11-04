"""Trusted effective controls and disposable installed-software applicability."""
import csv
import json
import os
from pathlib import Path
import sys
import venv

import pytest

from helpers.qa import candidate_waiting_for_qa_run
from hoh_control_plane.qa_runner import ProjectTestRunner
from hoh_control_plane.domain import ConflictError


def test_optimization_change_cannot_change_result_under_equal_applicability(tmp_path, monkeypatch):
    runner = ProjectTestRunner(tmp_path / "reports")
    worktree = tmp_path / "candidate"
    worktree.mkdir()
    command = [sys.executable, "-B", "-c", "assert False"]
    monkeypatch.setenv("PYTHONOPTIMIZE", "1")
    first_key = runner.execution_environment_digest(command, 30)
    first = runner.execute(command, str(worktree), 30)
    monkeypatch.delenv("PYTHONOPTIMIZE")
    second_key = runner.execution_environment_digest(command, 30)
    second = runner.execute(command, str(worktree), 30)
    assert first.exit_code == second.exit_code or first_key != second_key
    assert first.exit_code == second.exit_code == 1
    assert first_key and first_key == second_key
    assert second.exit_code == 1 and "AssertionError" in second.stderr


@pytest.fixture
def private_software(tmp_path):
    # No pip or shared installs. This is a private interpreter and hand-written
    # distribution fixture with a real file manifest and executable module.
    root = tmp_path / "private-python"
    venv.EnvBuilder(with_pip=False).create(root)
    python = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    packages = root / ("Lib/site-packages" if os.name == "nt" else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
    metadata = packages / "audit_addon-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: audit-addon\nVersion: 1.0\n", encoding="utf-8")
    (packages / "audit_addon.py").write_text("VALUE = 1\n", encoding="utf-8")
    (packages / "audit_command.py").write_text(
        "from importlib.metadata import version\nassert version('audit-addon') == '1.0'\n"
        "from audit_addon import VALUE\nassert VALUE == 1\n", encoding="utf-8")
    with (metadata / "RECORD").open("w", newline="", encoding="utf-8") as stream:
        csv.writer(stream).writerows((name, "", "") for name in (
            "audit_addon.py", "audit_command.py", "audit_addon-1.0.dist-info/METADATA", "audit_addon-1.0.dist-info/RECORD"))
    return python, packages, metadata


@pytest.mark.git
def test_dependency_change_prevents_queue_and_merge_reuse(service, actors, seeded, tmp_path, private_software):
    python, packages, metadata = private_software
    command = [str(python), "-B", "-m", "audit_command"]
    t, submitted, owner = candidate_waiting_for_qa_run(service, actors, seeded, tmp_path, command)
    args = (actors["qa"], t["ticket_id"], owner["lease"]["lease_id"])
    first = service.queue_project_tests(*args)["test_run"]
    result = service.execute_queued_project_test(first["test_run_id"])
    assert result["exit_code"] == 0 and result["termination_reason"] == "passed"
    candidate_id = submitted["candidate"]["candidate_id"]
    assert result["execution_environment_digest"] is None
    assert service._reusable_qa_run(candidate_id, command, first["timeout_seconds"]) is None
    with service.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM qa_test_jobs").fetchone()[0] == 1
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: audit-addon\nVersion: 2.0\n", encoding="utf-8")
    changed = service.project_tests.execute(command, str(tmp_path), first["timeout_seconds"])
    assert changed.exit_code == 1 and "AssertionError" in changed.stderr
    assert service._reusable_qa_run(candidate_id, command, first["timeout_seconds"]) is None
    second = service.queue_project_tests(*args)
    assert not second.get("reused") and second["test_run"]["test_run_id"] != first["test_run_id"]
    assert second["test_run"]["execution_environment_digest"] is first["execution_environment_digest"] is None
    (packages / "audit_addon.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(ConflictError) as denied:
        service.queue_project_tests(*args)
    assert denied.value.details["reason"] == "execution_environment_changed"
    with service.store.connection() as db:
        # Unknown is not reusable certification, but does not revoke an
        # authorized observer/continuation of the one current execution.
        assert service._qa_test_authority(db, service._qa_test_job(db, second["test_run"]["test_run_id"]))["ticket"]["ticket_id"] == t["ticket_id"]
        assert db.execute("SELECT COUNT(*) FROM qa_test_jobs WHERE status IN ('QUEUED','RUNNING')").fetchone()[0] == 1
        review = db.execute("SELECT review_id,status FROM qa_reviews WHERE lease_id=?", (owner["lease"]["lease_id"],)).fetchone()
        assert tuple(review) == (owner["review"]["review_id"], "IN_PROGRESS")


def test_dependency_bytes_not_only_version_are_identity(tmp_path, private_software):
    python, packages, _ = private_software
    runner = ProjectTestRunner(tmp_path / "reports")
    command = [str(python), "-B", "-m", "audit_command"]
    first = runner.execution_environment_digest(command, 30)
    assert runner.execute(command, str(tmp_path), 30).exit_code == 0
    (packages / "audit_addon.py").write_text("VALUE = 2\n", encoding="utf-8")
    second = runner.execution_environment_digest(command, 30)
    assert runner.execute(command, str(tmp_path), 30).exit_code == 1
    assert first is second is None  # Module consumption is not proved by an inventory.


def test_missing_dependency_manifest_stays_unproved(tmp_path, private_software):
    python, _, metadata = private_software
    runner = ProjectTestRunner(tmp_path / "reports")
    command = [str(python), "-B", "-m", "audit_command"]
    assert runner.execution_environment_digest(command, 30) is None
    (metadata / "RECORD").unlink()
    assert runner.execution_environment_digest(command, 30) is None
    result = runner.execute(command, str(tmp_path), 30)
    assert result.exit_code == 0
    assert result.environment_manifest["execution_environment_digest"] is None


def test_unproved_interpreter_hook_is_not_attested(tmp_path):
    runner = ProjectTestRunner(tmp_path / "reports")
    assert runner.execution_environment_digest([sys.executable, "-X", "presite=external_hook", "-c", "pass"], 30) is None


def test_unproved_external_path_command_is_not_reusable(service, actors, seeded, tmp_path):
    from helpers.w7 import submit_fake
    artifact = submit_fake(service, actors, seeded["ticket"]["ticket_id"])["candidate"]
    external = tmp_path / "external"
    metadata = external / "audit_addon-1.0.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: audit-addon\nVersion: 1.0\n", encoding="utf-8")
    command = [sys.executable, "-B", "-c", f"import sys; sys.path.insert(0, {str(external)!r}); from importlib.metadata import version; assert version('audit-addon') == '1.0'"]
    with service.store.connection(immediate=True) as db:
        db.execute("INSERT INTO candidate_test_profiles(candidate_id,test_command,timeout_seconds,configured_at,provenance_modules) VALUES (?,?,30,'fixture','[]')",
                   (artifact["candidate_id"], json.dumps(command)))
    captured = service._capture_qa_environment(artifact["candidate_id"])
    job = {"candidate_id": artifact["candidate_id"], "execution_environment_digest": captured["digest"]}
    with service.store.connection() as db:
        assert not service._qa_job_environment_matches(db, job, captured=captured)
    assert service.project_tests.execute(command, str(tmp_path), 30).exit_code == 0
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: audit-addon\nVersion: 2.0\n", encoding="utf-8")
    assert service.project_tests.execute(command, str(tmp_path), 30).exit_code == 1
    changed = service._capture_qa_environment(artifact["candidate_id"])
    with service.store.connection() as db:
        assert not service._qa_job_environment_matches(db, job, captured=changed)
