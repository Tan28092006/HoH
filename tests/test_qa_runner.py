from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def test_candidate_tests_do_not_inherit_control_plane_credentials(tmp_path, monkeypatch):
    from hoh_control_plane.qa_runner import ProjectTestRunner
    monkeypatch.setenv("HOH_MCP_TOKENS_JSON", "private-token-map")
    monkeypatch.setenv("HOH_POSTGRES_DSN", "private-database-credential")
    runner = ProjectTestRunner(tmp_path / "evidence")
    execution = runner.execute([sys.executable, "-c",
        "import os; assert not any(k.startswith('HOH_') for k in os.environ)"], str(tmp_path), 30)
    assert execution.exit_code == 0, execution.stderr

from hoh_control_plane import qa_worker
from hoh_control_plane.domain import Actor, ConflictError, Role, ValidationError
from hoh_control_plane.qa_runner import ProjectTestRunner
from hoh_control_plane.qa_runner import TestExecution as _TestExecution


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "checkout-repository"
    repository.mkdir(parents=True)
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    (repository / "README.md").write_text("# Checkout\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "Initial checkout")
    return repository


def _candidate_waiting_for_qa(service, actors, seeded, tmp_path: Path, test_command: list[str]):
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    service.configure_test_command(actors["planner"], "project-1", test_command, 120)
    ticket = seeded["ticket"]
    development = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    prepared = service.prepare_worktree(
        actors["developer"],
        ticket["ticket_id"],
        development["lease"]["lease_id"],
        development["ticket"]["version"],
    )
    worktree = Path(prepared["worktree"]["worktree_path"])
    retry_path = worktree / "src" / "checkout" / "retry.txt"
    retry_path.parent.mkdir(parents=True)
    retry_path.write_text("safe retry\n", encoding="utf-8")
    _git(worktree, "add", "src/checkout/retry.txt")
    _git(worktree, "commit", "-m", "Implement safe retry")
    candidate = service.submit_candidate(
        actors["developer"],
        ticket["ticket_id"],
        development["lease"]["lease_id"],
        development["ticket"]["version"],
        _git(worktree, "rev-parse", "HEAD"),
        prepared["worktree"]["base_commit"],
        prepared["worktree"]["branch_ref"],
        ["Retry implementation is ready for QA."],
        context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
    )
    qa_claim = service.claim_qa(
        actors["qa"], ticket["ticket_id"], candidate["ticket"]["version"], 900
    )
    return ticket, candidate, qa_claim


def test_qa_runs_the_planner_registered_command_and_persists_evidence(
    service, actors, seeded, tmp_path: Path
) -> None:
    test_command = [
        sys.executable,
        "-c",
        "from pathlib import Path; assert Path('src/checkout/retry.txt').read_text() == 'safe retry\\n'; print('passed')",
    ]
    ticket, candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, test_command
    )

    result = service.run_project_tests(
        actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
    )
    context = service.get_ticket_context(actors["qa"], ticket["ticket_id"])

    assert candidate["candidate"]["candidate_id"] == result["test_run"]["candidate_id"]
    assert result["test_run"]["exit_code"] == 0
    assert Path(result["test_run"]["report_path"]).is_file()
    assert context["test_runs"][0]["storage_ref"].startswith("evidence://local/")


def test_qa_queues_project_tests_then_polls_a_completed_durable_run(
    service, actors, seeded, tmp_path: Path
) -> None:
    """The MCP-facing request must finish before the isolated test command runs."""
    test_command = [
        sys.executable,
        "-c",
        "from pathlib import Path; assert Path('src/checkout/retry.txt').is_file(); print('passed')",
    ]
    ticket, candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, test_command
    )

    queued = service.queue_project_tests(
        actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
    )

    assert queued["test_run"]["status"] == "QUEUED"
    assert queued["test_run"]["candidate_id"] == candidate["candidate"]["candidate_id"]
    assert service.get_ticket_context(actors["qa"], ticket["ticket_id"])["test_runs"] == []

    qa_worker.main(
        [
            "--database",
            str(service.store.path),
            "--test-run-id",
            queued["test_run"]["test_run_id"],
        ]
    )
    polled = service.get_project_test_run(
        actors["qa"],
        ticket["ticket_id"],
        qa_claim["lease"]["lease_id"],
        queued["test_run"]["test_run_id"],
    )

    assert polled["test_run"]["status"] == "COMPLETED"
    assert polled["test_run"]["exit_code"] == 0
    assert Path(polled["test_run"]["report_path"]).is_file()


def test_qa_queues_without_running_git_checks_in_the_mcp_request(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    ticket, _candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, [sys.executable, "-c", "print('passed')"]
    )

    def fail_if_called(*_args, **_kwargs):
        pytest.fail("QA queueing must not wait for a Git verification")

    monkeypatch.setattr(service.git_worktrees, "verify_frozen_candidate", fail_if_called)

    queued = service.queue_project_tests(
        actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
    )

    assert queued["test_run"]["status"] == "QUEUED"


def test_qa_cannot_queue_two_active_runs_for_one_frozen_candidate(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, _candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, [sys.executable, "-c", "print('passed')"]
    )
    service.queue_project_tests(actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"])

    with pytest.raises(ConflictError, match="active"):
        service.queue_project_tests(
            actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
        )


def test_expiring_a_qa_lease_keeps_its_durable_test_job_but_requeues_the_ticket(
    service, store, actors, seeded, tmp_path: Path
) -> None:
    """A QA test job retains its audit lease id after the live lease is reclaimed."""
    ticket, _candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, [sys.executable, "-c", "print('passed')"]
    )
    queued = service.queue_project_tests(
        actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
    )
    with store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE leases SET expires_at = ? WHERE lease_id = ?",
            (
                (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                qa_claim["lease"]["lease_id"],
            ),
        )

    swept = service.sweep_expired_leases(
        Actor("test-scheduler", Role.ADMIN, frozenset({"*"}))
    )

    assert swept["count"] == 1
    with store.connection() as connection:
        retained_job = connection.execute(
            "SELECT lease_id, status FROM qa_test_jobs WHERE test_run_id = ?",
            (queued["test_run"]["test_run_id"],),
        ).fetchone()
    assert retained_job is not None
    assert retained_job["lease_id"] == qa_claim["lease"]["lease_id"]
    assert retained_job["status"] == "FAILED"
    replacement = service.claim_qa(actors["qa"], ticket["ticket_id"], swept["requeued"][0]["version"], 900)
    new_run = service.queue_project_tests(actors["qa"], ticket["ticket_id"], replacement["lease"]["lease_id"])
    assert new_run["test_run"]["test_run_id"] != queued["test_run"]["test_run_id"]
    assert new_run["test_run"]["status"] == "QUEUED"


def test_store_migrates_legacy_qa_test_job_lease_foreign_key_without_losing_job(
    service, store, actors, seeded, tmp_path: Path
) -> None:
    """Existing databases must release lease rows while keeping QA audit history."""
    ticket, _candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, [sys.executable, "-c", "print('passed')"]
    )
    queued = service.queue_project_tests(
        actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
    )["test_run"]
    job_columns = (
        "test_run_id, project_id, candidate_id, ticket_id, lease_id, test_command, "
        "command_digest, timeout_seconds, worktree_path, candidate_commit, requested_by, "
        "status, error_message, created_at, started_at, completed_at"
    )
    with store.connection(immediate=True) as connection:
        connection.execute("DROP TABLE qa_test_jobs")
        connection.execute(
            """
            CREATE TABLE qa_test_jobs (
                test_run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
                ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
                lease_id TEXT NOT NULL REFERENCES leases(lease_id),
                test_command TEXT NOT NULL,
                command_digest TEXT NOT NULL,
                timeout_seconds INTEGER NOT NULL,
                worktree_path TEXT NOT NULL,
                candidate_commit TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            )
            """
        )
        connection.execute(
            f"INSERT INTO qa_test_jobs ({job_columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(
                json.dumps(queued[column]) if column == "test_command" else queued[column]
                for column in job_columns.split(", ")
            ),
        )

    store.initialize()

    with store.connection() as connection:
        lease_foreign_keys = connection.execute("PRAGMA foreign_key_list(qa_test_jobs)").fetchall()
        migrated_job = connection.execute(
            "SELECT test_run_id, lease_id, status FROM qa_test_jobs WHERE test_run_id = ?",
            (queued["test_run_id"],),
        ).fetchone()
    assert all(foreign_key["table"] != "leases" for foreign_key in lease_foreign_keys)
    assert migrated_job is not None
    assert migrated_job["lease_id"] == qa_claim["lease"]["lease_id"]
    assert migrated_job["status"] == "QUEUED"


def test_qa_records_a_failing_registered_test_without_accepting_the_candidate(
    service, actors, seeded, tmp_path: Path
) -> None:
    test_command = [sys.executable, "-c", "raise SystemExit(7)"]
    ticket, _candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, test_command
    )

    result = service.run_project_tests(
        actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
    )
    context = service.get_ticket_context(actors["qa"], ticket["ticket_id"])

    assert result["test_run"]["exit_code"] == 7
    assert context["ticket"]["status"] == "QA_IN_PROGRESS"
    assert context["test_runs"][0]["exit_code"] == 7


def test_qa_uses_frozen_snapshot_when_the_developer_worktree_becomes_dirty(
    service, actors, seeded, tmp_path: Path
) -> None:
    test_command = [sys.executable, "-c", "from pathlib import Path; assert not Path('untracked.txt').exists();print('frozen source')"]
    ticket, _candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, test_command
    )
    context = service.get_ticket_context(actors["qa"], ticket["ticket_id"])
    worktree = Path(context["worktree"]["worktree_path"])
    (worktree / "untracked.txt").write_text("not frozen\n", encoding="utf-8")

    result = service.run_project_tests(actors['qa'], ticket['ticket_id'], qa_claim['lease']['lease_id'])
    assert result['test_run']['exit_code'] == 0
    assert (worktree / 'untracked.txt').read_text() == 'not frozen\n'


def test_test_command_requires_a_verified_project_repository(service, actors, seeded) -> None:
    with pytest.raises(ValidationError, match="repository"):
        service.configure_test_command(
            actors["planner"], "project-1", ["python", "-m", "pytest"], 120
        )


def _write_fake_package(root: Path, source_label: str) -> None:
    package = root / "hoh_control_plane"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f"SOURCE = {source_label!r}\n", encoding="utf-8")


def test_execute_resolves_the_candidate_source_over_an_ambient_shadow(
    tmp_path: Path, monkeypatch
) -> None:
    """Reproduces the real defect: a shared venv's editable install shadows the
    candidate's own copy of hoh_control_plane. Without the PYTHONPATH fix in
    ProjectTestRunner, this command would print 'ambient', not 'candidate'.
    """
    ambient_root = tmp_path / "ambient-site-packages"
    _write_fake_package(ambient_root, "ambient")
    monkeypatch.setenv("PYTHONPATH", str(ambient_root))

    worktree = tmp_path / "worktree"
    _write_fake_package(worktree / "src", "candidate")

    runner = ProjectTestRunner(tmp_path / "evidence")
    command = [
        sys.executable,
        "-c",
        "import hoh_control_plane, sys; sys.stdout.write(hoh_control_plane.SOURCE)",
    ]

    execution = runner.execute(command, str(worktree), 30)

    assert execution.artifact_mismatch is False
    assert execution.exit_code == 0
    assert execution.stdout.strip() == "candidate"
    assert execution.resolved_source is not None
    assert Path(execution.resolved_source).resolve() == (
        worktree / "src" / "hoh_control_plane" / "__init__.py"
    ).resolve()


def test_execute_discards_host_pythonpath_and_keeps_only_candidate_source(
    tmp_path: Path, monkeypatch
) -> None:
    other_root = tmp_path / "other-tooling"
    other_root.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(other_root))

    worktree = tmp_path / "worktree"
    _write_fake_package(worktree / "src", "candidate")

    runner = ProjectTestRunner(tmp_path / "evidence")
    command = [
        sys.executable,
        "-c",
        "import os, sys; sys.stdout.write(os.environ.get('PYTHONPATH', ''))",
    ]

    execution = runner.execute(command, str(worktree), 30)

    entries = execution.stdout.strip().split(os.pathsep)
    assert str((worktree / "src").resolve()) == str(Path(entries[0]).resolve())
    assert str(other_root) not in entries
    assert len(entries) == 1


def test_execute_fails_closed_on_artifact_mismatch_without_running_the_configured_command(
    tmp_path: Path, monkeypatch
) -> None:
    worktree = tmp_path / "worktree"
    _write_fake_package(worktree / "src", "candidate")
    observed = str(tmp_path / "elsewhere" / "hoh_control_plane" / "__init__.py")

    runner = ProjectTestRunner(tmp_path / "evidence")
    monkeypatch.setattr(runner, "_resolve_candidate_source", lambda *a, **k: observed)

    def fail_if_run(*_args, **_kwargs):
        pytest.fail("The configured command must not run after an artifact mismatch")

    monkeypatch.setattr(subprocess, "run", fail_if_run)

    execution = runner.execute(
        [sys.executable, "-c", "print('should not run')"], str(worktree), 30
    )

    assert execution.artifact_mismatch is True
    assert execution.exit_code is None
    assert execution.timed_out is False
    assert execution.resolved_source == observed
    assert "candidate_artifact_mismatch" in execution.stderr
    assert str(worktree.resolve()) in execution.stderr
    assert observed in execution.stderr


def test_execute_skips_the_identity_probe_when_the_worktree_has_no_such_package(
    tmp_path: Path,
) -> None:
    """A registered command for a differently-shaped project must never be blocked
    by a check for a package it does not ship."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    runner = ProjectTestRunner(tmp_path / "evidence")
    execution = runner.execute([sys.executable, "-c", "print('ok')"], str(worktree), 30)

    assert execution.artifact_mismatch is False
    assert execution.resolved_source is None
    assert execution.exit_code == 0


def test_write_report_includes_the_verified_candidate_commit_and_resolved_source(
    tmp_path: Path,
) -> None:
    runner = ProjectTestRunner(tmp_path / "evidence")
    execution = _TestExecution(
        command_digest="digest",
        exit_code=0,
        timed_out=False,
        duration_ms=12,
        stdout="ok",
        stderr="",
        resolved_source="/worktree/src/hoh_control_plane/__init__.py",
    )

    _storage_ref, report_path, checksum = runner.write_report(
        candidate_id="cand_abc123",
        test_run_id="testrun_xyz789",
        execution=execution,
        candidate_commit="deadbeef" * 5,
    )

    body = Path(report_path).read_text(encoding="utf-8")
    payload = json.loads(body)
    assert payload["candidate_commit"] == "deadbeef" * 5
    assert payload["resolved_source"] == "/worktree/src/hoh_control_plane/__init__.py"
    assert payload["artifact_mismatch"] is False
    # The checksum covers the retained bytes, so a later reader can prove them.
    assert checksum == hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_artifact_mismatch_fails_the_job_without_a_candidate_verdict_and_allows_retry(
    service, actors, seeded, tmp_path: Path, monkeypatch
) -> None:
    ticket, candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, [sys.executable, "-c", "print('passed')"]
    )
    mismatch = _TestExecution(
        command_digest="digest",
        exit_code=None,
        timed_out=False,
        duration_ms=5,
        stdout="",
        stderr=(
            "candidate_artifact_mismatch: expected hoh_control_plane to resolve under "
            "the frozen worktree, observed D:\\HoH\\src\\hoh_control_plane\\__init__.py"
        ),
        resolved_source="D:\\HoH\\src\\hoh_control_plane\\__init__.py",
        artifact_mismatch=True,
    )
    real_execute = service.project_tests.execute
    outcomes = iter([mismatch])

    def fake_execute(command, worktree_path, timeout_seconds):
        try:
            return next(outcomes)
        except StopIteration:
            return real_execute(command, worktree_path, timeout_seconds)

    monkeypatch.setattr(service.project_tests, "execute", fake_execute)

    queued = service.queue_project_tests(
        actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
    )
    failed = service.execute_queued_project_test(queued["test_run"]["test_run_id"])

    assert failed["status"] == "FAILED"
    assert "candidate_artifact_mismatch" in failed["error_message"]
    assert failed["test_run"] is None
    context = service.get_ticket_context(actors["qa"], ticket["ticket_id"])
    assert context["ticket"]["status"] == "BLOCKED"
    assert context["test_runs"] == []

    # Retry without any Developer action or a new candidate: same candidate_id, a fresh
    # test_run_id, and this time the (patched-through) real command runs and completes.
    readiness = service.append_planner_context(actors["planner"], ticket_id=ticket["ticket_id"],
        entry_type="planner_decision", content="Runner source resolution repaired.", source_ref="evidence://runner/fix")
    recovered = service.recover_candidate_for_qa(actors["planner"], ticket["ticket_id"],
        context["ticket"]["version"], failed["test_run_id"], readiness["entry_id"])
    qa_claim = service.claim_qa(actors["qa"], ticket["ticket_id"], recovered["ticket"]["version"], 900)
    retried = service.queue_project_tests(
        actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
    )
    assert retried["test_run"]["test_run_id"] != queued["test_run"]["test_run_id"]
    assert retried["test_run"]["candidate_id"] == candidate["candidate"]["candidate_id"]

    completed = service.execute_queued_project_test(retried["test_run"]["test_run_id"])

    assert completed["status"] == "COMPLETED"
    assert completed["exit_code"] == 0


def test_concurrent_queue_project_tests_calls_never_create_duplicate_active_jobs(
    service, actors, seeded, tmp_path: Path
) -> None:
    """Two threads racing to queue the same candidate's project tests must never
    both succeed: SQLite's serialized writers plus the active-job guard leave
    exactly one QUEUED job, and the loser sees the existing ConflictError -- not
    a raw database error and not a second active job.
    """
    ticket, _candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, [sys.executable, "-c", "print('passed')"]
    )

    results: list[dict | Exception] = []
    barrier = threading.Barrier(2)

    def _attempt() -> None:
        barrier.wait(timeout=5)
        try:
            results.append(
                service.queue_project_tests(
                    actors["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
                )
            )
        except Exception as error:  # noqa: BLE001 - captured for assertion below
            results.append(error)

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, Exception)]
    assert len(successes) == 1, f"exactly one attempt should win the race: {results}"
    assert len(failures) == 1
    assert isinstance(failures[0], ConflictError)
    assert "active project test run" in str(failures[0])
    with service.store.connection() as connection:
        active_jobs = connection.execute(
            """
            SELECT COUNT(*) AS n FROM qa_test_jobs
            WHERE ticket_id = ? AND status IN ('QUEUED', 'RUNNING')
            """,
            (ticket["ticket_id"],),
        ).fetchone()["n"]
    assert active_jobs == 1


def test_a_second_run_on_the_same_candidate_reuses_the_passing_result(
    service, actors, seeded, tmp_path: Path
) -> None:
    # The candidate is immutable, so running the same frozen command again only
    # repeats a full suite. QA gets the passing run back unless it asks to rerun.
    ticket, _candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, [sys.executable, "-c", "print('passed')"]
    )
    lease_id = qa_claim["lease"]["lease_id"]
    first = service.run_project_tests(actors["qa"], ticket["ticket_id"], lease_id)

    again = service.queue_project_tests(actors["qa"], ticket["ticket_id"], lease_id)
    forced = service.queue_project_tests(actors["qa"], ticket["ticket_id"], lease_id, rerun=True)

    assert again["reused"] is True
    assert again["test_run"]["test_run_id"] == first["test_run"]["test_run_id"]
    assert "reused" not in forced
    assert forced["test_run"]["test_run_id"] != first["test_run"]["test_run_id"]


def test_a_failing_run_is_never_reused(service, actors, seeded, tmp_path: Path) -> None:
    ticket, _candidate, qa_claim = _candidate_waiting_for_qa(
        service, actors, seeded, tmp_path, [sys.executable, "-c", "raise SystemExit(7)"]
    )
    lease_id = qa_claim["lease"]["lease_id"]
    first = service.run_project_tests(actors["qa"], ticket["ticket_id"], lease_id)

    again = service.queue_project_tests(actors["qa"], ticket["ticket_id"], lease_id)

    assert "reused" not in again
    assert again["test_run"]["test_run_id"] != first["test_run"]["test_run_id"]


def test_the_probe_does_not_let_its_own_package_shadow_candidate_imports(tmp_path: Path) -> None:
    # The probe runs as a script, so Python puts the installed hoh_control_plane
    # directory first on sys.path; its mcp/ subpackage then hid the MCP SDK and
    # every candidate test importing mcp failed (0.3.30-0.3.32 QA regression).
    import json as _json
    import subprocess as _subprocess
    from hoh_control_plane import qa_runner as _qa_runner

    probe = Path(_qa_runner.__file__).with_name("pytest_probe.py")
    (tmp_path / "test_sdk_import.py").write_text(
        "import mcp\n"
        "import mcp.server.fastmcp as fastmcp\n"
        "def test_mcp_is_the_sdk():\n"
        "    assert 'hoh_control_plane' not in (mcp.__file__ or '')\n"
        "    assert fastmcp.FastMCP\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.json"
    config.write_text(_json.dumps({"modules": []}), encoding="utf-8")
    report = tmp_path / "report.json"

    completed = _subprocess.run(
        [sys.executable, str(probe), str(report), str(config),
         "-q", "-p", "no:cacheprovider", "test_sdk_import.py"],
        cwd=tmp_path, capture_output=True, text=True, timeout=300,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
