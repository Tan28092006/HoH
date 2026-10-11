"""HOH-283: admission ceilings and durable lifecycle on throwaway databases.

The contract is independent of CLI execution. Real store transactions expose
double claims, budget bypass and replay bugs; only host probes/Git are faked.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
import json
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
import sys

import pytest

from conftest import FakeProber
from hoh_control_plane.domain import AuthorizationError, ConflictError, Role, ValidationError
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.service._common import _iso, _now


@pytest.fixture
def crew(service, actors, seeded):
    service.configure_verification_profile(actors["planner"], "project-1", ["build_execution"], quality_policy="legacy")
    service.capability_prober = FakeProber(equipped=["build_execution"])
    for role in ("developer", "qa", "integrator"):
        service.register_agent(actors[role], skills=[f"hoh-{role}"],
            model_family="anthropic" if role == "qa" else "openai",
            machine_id="qa-host-a", execution_capabilities=["build_execution"], presence_seconds=3600)
    service.run_capability_preflight(actors["qa"], "project-1")
    return {role: actors[role].agent_id for role in ("developer", "qa", "integrator")}


def start(service, actors, seeded, crew, **overrides):
    limits = dict(wall_seconds=600, launch_attempts=10, in_flight=1,
                  suite_starts=4, landed_tickets=2, crash_retries=1)
    limits.update(overrides)
    return service.autonomy_start(actors["planner"], "project-1", limits, crew, "run-start",
        ticket_ids=[seeded["ticket"]["ticket_id"]])["run"]["run_id"]


def admit(service, actors, run_id, ticket_id, key="worker-1"):
    packet = service.get_ticket_context(actors["developer"], ticket_id)
    return service.autonomy_admit(actors["developer"], run_id, ticket_id,
        packet["ticket"]["version"], packet["context_snapshot_id"], key)


def clear_lease(service, actors, claim):
    # Supported lifecycle: report a stopped worker and preserve its work.
    service.report_blocker(actors["developer"], claim["ticket"]["ticket_id"], claim["lease"]["lease_id"],
                           claim["ticket"]["version"], "Disposable simulated worker stopped")


def test_atomic_last_launch_and_replay_survive_new_service(service, actors, seeded, crew):
    second = service.create_ticket(actors["planner"], seeded["feature"]["feature_id"], "DEV-2",
        "Other scope", "Prove last-slot serialization", Role.DEVELOPER, ["One admission"], [], ["other/**"], ready=True)
    limits = dict(wall_seconds=600, launch_attempts=1, in_flight=2, suite_starts=4, landed_tickets=2)
    run_id = service.autonomy_start(actors["planner"], "project-1", limits, crew, "two-tickets",
        ticket_ids=[seeded["ticket"]["ticket_id"], second["ticket_id"]])["run"]["run_id"]
    tickets = [seeded["ticket"]["ticket_id"], second["ticket_id"]]
    packets = [service.get_ticket_context(actors["developer"], ticket) for ticket in tickets]
    barrier = Barrier(2)
    def call(index):
        barrier.wait(timeout=10)
        packet = packets[index]
        return service.autonomy_admit(actors["developer"], run_id, tickets[index],
            packet["ticket"]["version"], packet["context_snapshot_id"], f"slot-{index}")
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(call, range(2)))
    assert sum(result["admitted"] for result in outcomes) == 1
    winner = next(index for index, result in enumerate(outcomes) if result["admitted"])
    restarted = ControlPlaneService(service.store)
    replay = restarted.autonomy_admit(actors["developer"], run_id, tickets[winner],
        packets[winner]["ticket"]["version"], packets[winner]["context_snapshot_id"], f"slot-{winner}")
    assert replay["lease"]["lease_id"] == outcomes[winner]["lease"]["lease_id"]
    assert replay["attempt_id"] == outcomes[winner]["attempt_id"]
    with service.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM autonomy_attempts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 1


def test_scope_role_key_conflicts_and_ordinary_claim_cannot_bypass(service, actors, seeded, crew):
    run_id = start(service, actors, seeded, crew)
    ticket_id = seeded["ticket"]["ticket_id"]
    with pytest.raises(ConflictError, match="autonomy.admit"):
        service.claim_ticket(actors["developer"], ticket_id, seeded["ticket"]["version"], 900)
    packet = service.get_ticket_context(actors["developer"], ticket_id)
    with pytest.raises(AuthorizationError):
        service.autonomy_admit(actors["developer_2"], run_id, ticket_id,
            packet["ticket"]["version"], packet["context_snapshot_id"], "other-actor")
    claim = admit(service, actors, run_id, ticket_id)
    with pytest.raises(ConflictError, match="different input"):
        service.autonomy_admit(actors["developer"], run_id, ticket_id,
            claim["ticket"]["version"], packet["context_snapshot_id"], "worker-1")
    assert service.autonomy_status(actors["planner"], run_id)["counts"]["launch_attempts"] == 1


def test_stop_drains_heartbeat_finish_notice_and_explicit_resume(service, actors, seeded, crew):
    run_id = start(service, actors, seeded, crew)
    claim = admit(service, actors, run_id, seeded["ticket"]["ticket_id"])
    stopped = service.autonomy_stop(actors["planner"], run_id)
    assert stopped["run"]["state"] == "DRAINING"
    assert not admit(service, actors, run_id, seeded["ticket"]["ticket_id"], "blocked-admission")["admitted"]
    service.heartbeat(actors["developer"], claim["lease"]["lease_id"])
    clear_lease(service, actors, claim)
    telemetry = service.autonomy_finish_attempt(actors["developer"], claim["attempt_id"], 27, "success", "finish")
    assert telemetry["observed_tokens"] == 27
    service.autonomy_finish_attempt(actors["developer"], claim["attempt_id"], 27, "success", "finish")
    status = service.autonomy_status(actors["planner"], run_id)
    assert status["run"]["state"] == "PAUSED"
    assert len(status["notifications"]) == 1
    assert status["counts"]["observed_tokens"] == 27
    repeat = ControlPlaneService(service.store).autonomy_stop(actors["planner"], run_id)
    assert repeat["notifications"] == status["notifications"]
    resumed = service.autonomy_resume(actors["planner"], run_id, status["run"]["version"])
    assert resumed["run"]["state"] == "ACTIVE"
    assert resumed["counts"]["launch_attempts"] == 1
    service.autonomy_acknowledge(actors["planner"], run_id, status["notifications"][0]["notification_id"])
    assert service.autonomy_status(actors["planner"], run_id)["notifications"] == []


@pytest.mark.parametrize("tokens,outcome,reason", [(None, "success", "unknown_usage"), (25, "provider_limit", "provider_limit"), (25, "crash", "worker_failure"), (100, "success", "token_threshold")])
def test_worker_signals_stop_admissions_with_redacted_reason(service, actors, seeded, crew, tokens, outcome, reason):
    run_id = start(service, actors, seeded, crew, observed_tokens=100)
    claim = admit(service, actors, run_id, seeded["ticket"]["ticket_id"])
    clear_lease(service, actors, claim)
    service.autonomy_finish_attempt(actors["developer"], claim["attempt_id"], tokens, outcome, "end")
    status = service.autonomy_status(actors["planner"], run_id)
    assert status["run"]["state"] == "PAUSED"
    assert status["run"]["stop_reason"] == reason
    assert status["notifications"][0]["payload"]["reason"] == reason
    assert len(status["notifications"]) == 1


@pytest.mark.parametrize("clock_seconds,reason", [(601, "wall_budget"), (301, "idle")])
def test_server_clock_prohibits_admission(service, actors, seeded, crew, monkeypatch, clock_seconds, reason):
    run_id = start(service, actors, seeded, crew)
    now = _now()
    monkeypatch.setattr("hoh_control_plane.service.autonomy._now", lambda: now + timedelta(seconds=clock_seconds))
    # _iso() shares the service's clock, so explicitly patch its zero-arg form.
    real_iso = _iso
    monkeypatch.setattr("hoh_control_plane.service.autonomy._iso", lambda value=None: real_iso(value or now + timedelta(seconds=clock_seconds)))
    denied = admit(service, actors, run_id, seeded["ticket"]["ticket_id"])
    assert not denied["admitted"]
    assert denied["reason"] == reason
    assert service.autonomy_status(actors["planner"], run_id)["counts"]["launch_attempts"] == 0


def test_start_validates_independence_capability_and_retry_input(service, actors, seeded, crew):
    with pytest.raises(ValidationError):
        start(service, actors, seeded, crew, launch_attempts=-1)
    service.register_agent(actors["qa"], skills=["hoh-qa"], model_family="openai",
        machine_id="qa-host-a", execution_capabilities=["build_execution"])
    with pytest.raises(ConflictError, match="independent"):
        start(service, actors, seeded, crew)
    with service.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM autonomy_runs").fetchone()[0] == 0


def test_finalization_rejects_historical_green_and_relevant_live_work(service, actors, seeded, crew, monkeypatch):
    run_id = start(service, actors, seeded, crew)
    ticket_id = seeded["ticket"]["ticket_id"]
    # Public scenario fixture: terminal board with a freshly observed exact SHA.
    # Git observation is tested elsewhere; no real branch is touched here.
    def observe(*args, **kwargs):
        with service.store.connection(immediate=True) as connection:
            connection.execute("UPDATE project_repositories SET observed_at = ? WHERE project_id = 'project-1'", (_iso(),))
        return {"changed": False}
    monkeypatch.setattr(service, "sync_repository", observe)
    with service.store.connection(immediate=True) as connection:
        connection.execute("INSERT INTO project_repositories (project_id, repository_path, workspace_model, observed_mainline_commit, observed_at, configured_by, configured_at) VALUES ('project-1', 'disposable-fixture', 'colocated', ?, ?, 'fixture', ?)", ("a" * 40, _iso(), _iso()))
        connection.execute("UPDATE tickets SET status = 'DONE' WHERE ticket_id = ?", (ticket_id,))
        connection.execute("INSERT INTO mainline_checks (check_id, project_id, mainline_commit, trigger_ref, status, outcome, created_at) VALUES ('old', 'project-1', ?, 'fixture', 'COMPLETED', 'green', ?)", ("b" * 40, _iso()))
    assert service.autonomy_status(actors["planner"], run_id)["run"]["state"] == "ACTIVE"
    with service.store.connection(immediate=True) as connection:
        connection.execute("INSERT INTO mainline_checks (check_id, project_id, mainline_commit, trigger_ref, status, outcome, created_at) VALUES ('current', 'project-1', ?, 'fixture', 'COMPLETED', 'green', ?)", ("a" * 40, _iso()))
        connection.execute("INSERT INTO worktree_provision_jobs (provision_id, project_id, ticket_id, lease_id, expected_version, requested_by, status, created_at) VALUES ('pending', 'project-1', ?, 'fixture', 1, 'fixture', 'QUEUED', ?)", (ticket_id, _iso()))
    assert service.autonomy_status(actors["planner"], run_id)["run"]["state"] == "ACTIVE"
    with service.store.connection(immediate=True) as connection:
        connection.execute("UPDATE worktree_provision_jobs SET status = 'COMPLETED' WHERE provision_id = 'pending'")
    status = service.autonomy_status(actors["planner"], run_id)
    assert status["run"]["state"] == "COMPLETED"
    assert status["notifications"][0]["payload"]["delivered_ticket_ids"] == [ticket_id]


def test_runner_guard_covers_actual_retry_and_zero_start_validation(tmp_path, monkeypatch):
    from hoh_control_plane import qa_runner
    runner = qa_runner.ProjectTestRunner(tmp_path / "evidence")
    calls = []
    @contextmanager
    def guard(index):
        calls.append(("start", index))
        try:
            yield
        finally:
            calls.append(("finish", index))
    def run(arguments, **kwargs):
        # Actual runner classifies the first subprocess as resource exhausted,
        # then launches its internal retry with a separately reserved unit.
        report = Path(arguments[2])
        report.write_text(json.dumps({"phase": "session_finished", "provenance_status": "not_applicable", "test_counts": {"passed": 1}}))
        return SimpleNamespace(returncode=1 if len(calls) == 1 else 0,
            stdout="", stderr="MemoryError" if len(calls) == 1 else "",
            timed_out=False, cancelled=False, stdout_truncated=False,
            stderr_truncated=False, capture_errors=())
    monkeypatch.setattr(qa_runner, "run_managed", run)
    monkeypatch.setattr(qa_runner, "resource_snapshot", lambda: {})
    execution = runner.execute([sys.executable, "-m", "pytest", "-n", "2"], str(tmp_path), 30,
                               provenance_modules=[], execution_guard=guard)
    assert execution.termination_reason == "passed"
    assert calls == [("start", 0), ("finish", 0), ("start", 1), ("finish", 1)]
    calls.clear()
    monkeypatch.setattr(qa_runner, "resource_snapshot", lambda: {"available_commit_bytes": 0})
    assert runner.execute([sys.executable, "-m", "pytest"], str(tmp_path), 30, provenance_modules=[], execution_guard=guard).termination_reason == "resource_unready"
    assert calls == []


def bind_qa_job(service, claim, job_id="testrun-fixture"):
    """Disposable persisted worker-job input; candidate execution is not mocked."""
    with service.store.connection(immediate=True) as connection:
        connection.execute("INSERT INTO candidates (candidate_id, ticket_id, base_commit, commit_sha, branch_ref, developer_claims, submitted_by, submitted_at, frozen_at) VALUES ('cand_fixture', ?, ?, ?, 'fixture', '[]', 'fixture', ?, ?)",
            (claim["ticket"]["ticket_id"], "b" * 40, "a" * 40, _iso(), _iso()))
        connection.execute("""INSERT INTO qa_test_jobs
            (test_run_id, project_id, candidate_id, ticket_id, lease_id, test_command,
             command_digest, timeout_seconds, worktree_path, candidate_commit,
             requested_by, status, created_at)
            VALUES (?, 'project-1', 'cand_fixture', ?, ?, '[]', 'fixture', 30,
                    'disposable-fixture', ?, 'fixture', 'RUNNING', ?)""",
            (job_id, claim["ticket"]["ticket_id"], claim["lease"]["lease_id"], "a" * 40, _iso()))
    return f"qa:{job_id}"


def test_suite_reservations_charge_actual_starts_once_and_hold_drain(service, actors, seeded, crew):
    run_id = start(service, actors, seeded, crew, suite_starts=1)
    claim = admit(service, actors, run_id, seeded["ticket"]["ticket_id"])
    job_ref = bind_qa_job(service, claim)
    guard = service._autonomy_execution_guard(job_ref)
    assert service.autonomy_status(actors["planner"], run_id)["counts"]["suite_starts"] == 0
    with guard(0):
        stopped = service.autonomy_stop(actors["planner"], run_id)
        assert stopped["run"]["state"] == "DRAINING"
        assert stopped["counts"]["suite_starts"] == 1
    with pytest.raises(ConflictError, match="ceiling"):
        with guard(1):
            pytest.fail("Unreserved retry was allowed to execute")
    with pytest.raises(ConflictError, match="already exists"):
        with guard(0):
            pytest.fail("Replay launched a duplicate child")
    with service.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM autonomy_reservations").fetchone()[0] == 1
        assert connection.execute("SELECT state FROM autonomy_reservations").fetchone()[0] == "SETTLED"


def test_durable_submission_survives_nonzero_cli_and_retry_telemetry(service, actors, seeded, crew):
    run_id = start(service, actors, seeded, crew)
    claim = admit(service, actors, run_id, seeded["ticket"]["ticket_id"])
    with service.store.connection(immediate=True) as connection:
        connection.execute("""INSERT INTO candidate_submission_jobs (submission_id, project_id,
            ticket_id, lease_id, expected_version, commit_sha, base_commit, branch_ref,
            developer_claims, requested_by, status, created_at)
            VALUES ('submission-fixture', 'project-1', ?, ?, ?, ?, ?, 'fixture', '[]', 'fixture', 'COMPLETED', ?)""",
            (claim["ticket"]["ticket_id"], claim["lease"]["lease_id"], claim["ticket"]["version"], "a" * 40, "b" * 40, _iso()))
    result = service.autonomy_finish_attempt(actors["developer"], claim["attempt_id"], 23, "failure", "exit")
    assert result["result_ref"] == "candidate_submission_jobs:submission-fixture"
    assert service.autonomy_status(actors["planner"], run_id)["run"]["state"] == "ACTIVE"
    with pytest.raises(ConflictError, match="different telemetry"):
        service.autonomy_finish_attempt(actors["developer"], claim["attempt_id"], 999, "success", "exit")
    assert service.autonomy_status(actors["planner"], run_id)["counts"]["observed_tokens"] == 23


def test_project_red_main_and_typed_controller_signal_stop_launches(service, actors, seeded, crew):
    run_id = start(service, actors, seeded, crew)
    with service.store.connection(immediate=True) as connection:
        connection.execute("INSERT INTO project_run_guardrails (project_id, merge_queue_paused, pause_reason, updated_at) VALUES ('project-1', 1, 'mainline_check_failed', ?)", (_iso(),))
    denied = admit(service, actors, run_id, seeded["ticket"]["ticket_id"])
    assert not denied["admitted"] and denied["reason"] == "red_main"
    status = service.autonomy_status(actors["planner"], run_id)
    with pytest.raises(ConflictError, match="remediation"):
        service.autonomy_resume(actors["planner"], run_id, status["run"]["version"])
    assert service.autonomy_signal(actors["developer"], run_id, "history_rewritten")["run"]["state"] == "PAUSED"
    with pytest.raises(ValidationError, match="redacted"):
        service.autonomy_signal(actors["developer"], run_id, "secret untrusted free text")


def test_expired_unspawned_attempt_reconciles_once_instead_of_draining_forever(service, actors, seeded, crew):
    run_id = start(service, actors, seeded, crew)
    claim = admit(service, actors, run_id, seeded["ticket"]["ticket_id"])
    with service.store.connection(immediate=True) as connection:
        connection.execute("UPDATE leases SET expires_at = ? WHERE lease_id = ?", (_iso(_now() - timedelta(seconds=1)), claim["lease"]["lease_id"]))
    restarted = ControlPlaneService(service.store)
    status = restarted.autonomy_status(actors["planner"], run_id)
    assert status["run"]["state"] == "PAUSED"
    assert status["run"]["stop_reason"] == "unknown_usage"
    assert status["counts"]["in_flight"] == 0
    assert status["attempts"][0]["state"] == "SETTLED"
    assert status["attempts"][0]["observed_tokens"] is None
    assert len(restarted.autonomy_status(actors["planner"], run_id)["notifications"]) == 1


def test_budget_block_holds_job_preserves_prior_result_and_excludes_supervisor(service, actors, seeded, crew):
    from hoh_control_plane.domain import AutonomyExecutionPaused
    from hoh_control_plane.qa_runner import TestExecution as Execution
    from hoh_control_plane.supervisor import Supervisor
    run_id = start(service, actors, seeded, crew, suite_starts=0)
    claim = admit(service, actors, run_id, seeded["ticket"]["ticket_id"])
    job_ref = bind_qa_job(service, claim)
    guard = service._autonomy_execution_guard(job_ref)
    with pytest.raises(AutonomyExecutionPaused) as failure:
        with guard(0):
            pytest.fail("No-budget child executed")
    failure.value.prior_execution = Execution(command_digest="first", exit_code=1, timed_out=False,
        duration_ms=1, stdout="first-run-output", stderr="MemoryError", termination_reason="resource_exhausted")
    held = service._autonomy_hold_job(job_ref, failure.value)
    assert held["status"] == "QUEUED" and held["autonomy_hold_run_id"] == run_id
    assert service.execute_queued_project_test(held["test_run_id"])["status"] == "QUEUED"
    supervisor = Supervisor(service, max_workers=1)
    try:
        assert ("qa", held["test_run_id"]) not in supervisor._pending()
    finally:
        if supervisor.pool:
            supervisor.pool.shutdown(wait=True)
    with service.store.connection() as connection:
        prior = connection.execute("SELECT termination_reason, report_path FROM qa_test_runs").fetchone()
        assert prior["termination_reason"] == "resource_exhausted"
        assert "first-run-output" in Path(prior["report_path"]).read_text()
        assert connection.execute("SELECT COUNT(*) FROM autonomy_reservations").fetchone()[0] == 0
    clear_lease(service, actors, claim)
    service.autonomy_finish_attempt(actors["developer"], claim["attempt_id"], 2, "success", "held-exit")
    status = service.autonomy_status(actors["planner"], run_id)
    assert status["run"]["state"] == "PAUSED"
    with pytest.raises(ConflictError, match="exhausted"):
        service.autonomy_resume(actors["planner"], run_id, status["run"]["version"])
    extended = dict(status["run"]["limits"], suite_starts=3)
    resumed = service.autonomy_resume(actors["planner"], run_id, status["run"]["version"], extended)
    assert resumed["run"]["state"] == "ACTIVE"
    assert resumed["counts"]["launch_attempts"] == 1
    with service.store.connection() as connection:
        assert connection.execute("SELECT autonomy_hold_run_id FROM qa_test_jobs").fetchone()[0] is None
        audit = connection.execute("SELECT payload FROM audit_events WHERE event_type = 'autonomy.resumed'").fetchone()[0]
        assert json.loads(audit)["old_limits"]["suite_starts"] == 0
        assert json.loads(audit)["new_limits"]["suite_starts"] == 3
    with pytest.raises(ConflictError, match="current version"):
        service.autonomy_resume(actors["planner"], run_id, status["run"]["version"], extended)


def test_lost_suite_callback_is_charged_and_reconciles_after_job_recovery(service, actors, seeded, crew):
    run_id = start(service, actors, seeded, crew)
    claim = admit(service, actors, run_id, seeded["ticket"]["ticket_id"])
    job_ref = bind_qa_job(service, claim)
    reservation = service._autonomy_reserve(job_ref, "suite_starts", f"{job_ref}:cand_fixture:0")
    pending = service.autonomy_reconcile(actors["planner"], run_id)
    assert pending["unresolved_reservation_ids"] == [reservation]
    assert "recovery" in pending["required_action"]
    service.fail_queued_project_test("testrun-fixture", "Disposable interrupted supervisor recovery")
    clear_lease(service, actors, claim)
    status = ControlPlaneService(service.store).autonomy_reconcile(actors["planner"], run_id)
    assert status["run"]["state"] == "PAUSED"
    assert status["counts"]["suite_starts"] == 1
    assert status["unresolved_reservation_ids"] == []
    assert len(status["notifications"]) == 1
    with service.store.connection() as connection:
        assert connection.execute("SELECT state FROM autonomy_reservations WHERE reservation_id = ?", (reservation,)).fetchone()[0] == "SETTLED"


@pytest.mark.parametrize("landed,head,expected", [(True, "a" * 40, "SETTLED"), (False, "b" * 40, "RELEASED"), (False, "c" * 40, "RESERVED")])
def test_landing_reconciliation_uses_git_truth_and_preserves_ambiguity(service, actors, seeded, crew, monkeypatch, landed, head, expected):
    run_id = start(service, actors, seeded, crew)
    claim = admit(service, actors, run_id, seeded["ticket"]["ticket_id"])
    bind_qa_job(service, claim)
    with service.store.connection(immediate=True) as connection:
        connection.execute("UPDATE qa_test_jobs SET status = 'COMPLETED'")
        connection.execute("INSERT INTO project_repositories (project_id, repository_path, configured_by, configured_at) VALUES ('project-1', 'disposable-fixture', 'fixture', ?)", (_iso(),))
        connection.execute("INSERT INTO merge_jobs (merge_job_id, project_id, ticket_id, lease_id, expected_version, candidate_id, requested_by, status, created_at) VALUES ('merge-fixture', 'project-1', ?, ?, 1, 'cand_fixture', 'fixture', 'RUNNING', ?)",
            (claim["ticket"]["ticket_id"], claim["lease"]["lease_id"], _iso()))
    reservation = service._autonomy_reserve("merge:merge-fixture", "landed_tickets", claim["ticket"]["ticket_id"], claim["ticket"]["ticket_id"])
    with service.store.connection(immediate=True) as connection:
        connection.execute("UPDATE merge_jobs SET status = 'FAILED' WHERE merge_job_id = 'merge-fixture'")
    monkeypatch.setattr(service.git_worktrees, "commit_is_on_branch", lambda **kwargs: landed)
    monkeypatch.setattr(service.git_worktrees, "_resolve_commit", lambda *args: head)
    result = service.autonomy_reconcile(actors["planner"], run_id)
    with service.store.connection() as connection:
        assert connection.execute("SELECT state FROM autonomy_reservations WHERE reservation_id = ?", (reservation,)).fetchone()[0] == expected
    assert (reservation in result["unresolved_reservation_ids"]) == (expected == "RESERVED")
    assert result["counts"]["landed_tickets"] == (0 if expected == "RELEASED" else 1)
    assert service.autonomy_reconcile(actors["planner"], run_id)["counts"]["landed_tickets"] == result["counts"]["landed_tickets"]


def test_versioned_resume_extension_has_one_winner_and_never_resets_counts(service, actors, seeded, crew):
    run_id = start(service, actors, seeded, crew)
    claim = admit(service, actors, run_id, seeded["ticket"]["ticket_id"])
    clear_lease(service, actors, claim)
    service.autonomy_finish_attempt(actors["developer"], claim["attempt_id"], 7, "success", "finish")
    stopped = service.autonomy_stop(actors["planner"], run_id)
    limits = dict(stopped["run"]["limits"], launch_attempts=20)
    barrier = Barrier(2)
    def resume(index):
        barrier.wait(timeout=10)
        try:
            return service.autonomy_resume(actors["planner"], run_id, stopped["run"]["version"], limits)["run"]["state"]
        except ConflictError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(resume, (0, 1)))
    assert sorted(results) == ["ACTIVE", "conflict"]
    result = service.autonomy_status(actors["planner"], run_id)
    assert result["counts"]["launch_attempts"] == 1
    assert result["counts"]["observed_tokens"] == 7
    with service.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_events WHERE event_type = 'autonomy.resumed'").fetchone()[0] == 1


def test_schema_three_upgrade_retains_rows_and_requires_explicit_migration(store, seeded):
    from hoh_control_plane.store import SchemaUpgradeRequired
    with store.connection(immediate=True) as connection:
        connection.execute("UPDATE schema_meta SET value = '3' WHERE key = 'schema_version'")
        for table in ("autonomy_notifications", "autonomy_reservations", "autonomy_attempts", "autonomy_runs"):
            connection.execute(f"DROP TABLE {table}")
    with pytest.raises(SchemaUpgradeRequired):
        store.initialize(allow_migration=False)
    store.initialize()
    with store.connection() as connection:
        assert connection.execute("SELECT title FROM tickets WHERE ticket_id = ?", (seeded["ticket"]["ticket_id"],)).fetchone()[0] == seeded["ticket"]["title"]
        assert connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()[0] == "4"
        assert connection.execute("SELECT COUNT(*) FROM autonomy_runs").fetchone()[0] == 0


@pytest.mark.anyio
async def test_autonomy_mcp_surfaces_separate_owner_control_and_execution(service, actors):
    from hoh_control_plane.mcp.server import create_mcp_server
    planner = create_mcp_server(service, actors["planner"])
    developer = create_mcp_server(service, actors["developer"])
    qa = create_mcp_server(service, actors["qa"])
    owner_names = {tool.name for tool in await planner.list_tools()}
    for execution in (developer, qa):
        names = {tool.name for tool in await execution.list_tools()}
        assert {"autonomy.admit", "autonomy.finish_attempt", "autonomy.signal", "autonomy.status"} <= names
        assert not names & {"autonomy.start", "autonomy.resume", "autonomy.reconcile", "autonomy.acknowledge"}
    assert {"autonomy.start", "autonomy.resume", "autonomy.reconcile", "autonomy.stop", "autonomy.status"} <= owner_names
    assert "autonomy.admit" not in owner_names
