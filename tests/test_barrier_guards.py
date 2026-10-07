"""Every call site of the reconciliation barrier refuses work while it is closed (review F2).

The barrier was only tested at claim and offer. Each guard below is reached the
way production reaches it -- a request, a queued worker's preflight, or the
re-check after Git ran without the write lock -- with a barrier opened at that
moment, and the test checks that nothing was written. The inventory test keeps
this list complete: a new guard call site in ``src`` fails it until a test here
names it.
"""
from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest
from helpers.barrier import keep_open_tickets
from helpers.integration import (
    _candidate_ready_for_integration,
    _claimed_for_integration,
    _git,
    _repository,
)

from hoh_control_plane.domain import ConflictError, Role

SERVICE_SOURCE = Path(__file__).resolve().parents[1] / "src" / "hoh_control_plane" / "service"
REFUSAL = "repository_reconciliation_required"


def _open_barrier(service, actors, *, frozen: list[str] | None = None, ref: str = "test:barrier") -> str:
    """Open a pending reconciliation as an observed outside commit (or a scoped result) would."""
    with service.store.connection(immediate=True) as connection:
        return service._open_commit_reconciliation(
            connection, actors["planner"], "project-1", commit="e" * 40,
            subject="outside push", relation="mainline", observation_ref=ref,
            frozen_ticket_ids=frozen,
        )


def _refused(call) -> ConflictError:
    with pytest.raises(ConflictError) as refused:
        call()
    assert refused.value.details["reason"] == REFUSAL
    return refused.value


def _ticket(service, ticket_id: str = "DEV-142") -> dict:
    with service.store.connection() as connection:
        return service._ticket(connection, ticket_id)


def _count(service, table: str, ticket_id: str = "DEV-142") -> int:
    with service.store.connection() as connection:
        return connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()[0]


def _snapshot(service, actor, ticket_id: str = "DEV-142") -> str:
    return service.get_ticket_context(actor, ticket_id)["context_snapshot_id"]


def _claimed(service, actors):
    ticket = _ticket(service)
    return service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)


# --- inventory ---------------------------------------------------------------

#: (module, enclosing function) of every guard call site, and the test proving it.
GUARDED = {
    ("leases.py", "_claim"): "test_claim_is_refused",
    ("candidates.py", "_check_candidate_submittable"): "test_submit_is_refused_before_and_after_git",
    ("candidates.py", "queue_candidate_submission"): "test_queueing_a_submission_is_refused",
    ("candidates.py", "execute_queued_candidate_submission"): "test_submission_worker_preflight_fails_the_job",
    ("candidates.py", "_complete_queued_candidate_submission"): "test_submission_completion_fails_the_job",
    ("ticket_authoring.py", "create_ticket"): "test_creating_ready_work_is_refused_but_backlog_is_not",
    ("project_policy.py", "configure_validation_contract"): "test_changing_a_validation_contract_is_refused",
    ("worktrees.py", "_worktree_preparation_snapshot"): "test_preparing_a_worktree_is_refused_before_git",
    ("worktrees.py", "_assign_worktree"): "test_a_barrier_opened_during_worktree_git_refuses_the_assignment",
    ("worktrees.py", "execute_queued_worktree_provision"): "test_provision_worker_preflight_fails_the_job",
    ("integration.py", "queue_merge_candidate"): "test_queueing_a_merge_is_refused",
    ("integration.py", "_execute_queued_merge_locked"): "test_merge_worker_preflight_fails_without_git",
    ("integration.py", "_finish_gate_and_merge"): "test_a_barrier_opened_during_the_gate_stops_the_fast_forward",
    ("integration.py", "_complete_queued_merge"): "test_a_regression_result_waits_for_a_barrier_opened_during_the_gate",
    ("discovery.py", "_offer_queue"): "test_no_offer_is_made_while_the_project_is_closed",
}


def _guard_call_sites() -> set[tuple[str, str]]:
    sites: set[tuple[str, str]] = set()
    for path in sorted(SERVICE_SOURCE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if function.name == "_require_repository_reconciled":
                continue  # the guard itself
            for node in ast.walk(function):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                name = node.func.attr
                owner = node.func.value
                if name == "_require_repository_reconciled" or (
                    isinstance(owner, ast.Name) and owner.id == "ExecutionGate"
                    and name in {"is_closed", "require_open"}
                ):
                    sites.add((path.name, function.name))
    return sites


def test_every_barrier_guard_call_site_has_a_test_here() -> None:
    sites = _guard_call_sites()
    assert sites - set(GUARDED) == set(), "a new barrier guard needs a test in this module"
    assert set(GUARDED) - sites == set(), "a guard named here is gone; drop or move its test"
    names = {name for name in globals() if name.startswith("test_")}
    assert set(GUARDED.values()) <= names


# --- requests that need no repository -----------------------------------------

def test_claim_is_refused(service, actors, seeded) -> None:
    _open_barrier(service, actors)
    ticket = _ticket(service)
    _refused(lambda: service.claim_ticket(actors["developer"], "DEV-142", ticket["version"], 900))
    assert _count(service, "leases") == 0


def test_submit_is_refused_before_and_after_git(service, actors, seeded, monkeypatch) -> None:
    claim = _claimed(service, actors)
    snapshot = _snapshot(service, actors["developer"])

    def submit():
        return service.submit_candidate(
            actors["developer"], "DEV-142", claim["lease"]["lease_id"], claim["ticket"]["version"],
            "a" * 40, "b" * 40, "agent/dev-01/DEV-142", ["Ready."], context_snapshot_id=snapshot,
        )

    # Re-checked after Git: the barrier opens between the read and the write.
    real_check = service._check_candidate_submittable
    calls = []

    def check_then_open(connection, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            # Inside the write transaction that re-checks; as if committed just before it.
            service._open_commit_reconciliation(
                connection, actors["planner"], "project-1", commit="e" * 40,
                subject="outside push", relation="mainline", observation_ref="test:during-git",
            )
        return real_check(connection, *args, **kwargs)

    monkeypatch.setattr(service, "_check_candidate_submittable", check_then_open)
    _refused(submit)
    assert len(calls) == 2
    monkeypatch.undo()
    assert _count(service, "candidates") == 0
    assert _ticket(service)["status"] == "DEV_IN_PROGRESS"

    # That refusal rolled its own transaction back, barrier included. A barrier
    # already open is refused by the first check, before any Git would run.
    _open_barrier(service, actors)
    calls.clear()
    monkeypatch.setattr(service, "_check_candidate_submittable", check_then_open)
    _refused(submit)
    assert len(calls) == 1
    assert _count(service, "candidates") == 0


def test_queueing_a_submission_is_refused(service, actors, seeded) -> None:
    claim = _claimed(service, actors)
    snapshot = _snapshot(service, actors["developer"])
    _open_barrier(service, actors)
    _refused(lambda: service.queue_candidate_submission(
        actors["developer"], "DEV-142", claim["lease"]["lease_id"], claim["ticket"]["version"],
        "a" * 40, "b" * 40, "agent/dev-01/DEV-142", ["Ready."], context_snapshot_id=snapshot,
    ))
    assert _count(service, "candidate_submission_jobs") == 0


def _queued_submission(service, actors) -> str:
    claim = _claimed(service, actors)
    queued = service.queue_candidate_submission(
        actors["developer"], "DEV-142", claim["lease"]["lease_id"], claim["ticket"]["version"],
        "a" * 40, "b" * 40, "agent/dev-01/DEV-142", ["Ready."],
        context_snapshot_id=_snapshot(service, actors["developer"]),
    )
    return queued["submission"]["submission_id"]


def test_submission_worker_preflight_fails_the_job(service, actors, seeded) -> None:
    submission_id = _queued_submission(service, actors)
    _open_barrier(service, actors)

    job = service.execute_queued_candidate_submission(submission_id)

    assert job["status"] == "FAILED"
    assert "reconcile" in job["error_message"]
    assert _count(service, "candidates") == 0
    assert _ticket(service)["status"] == "DEV_IN_PROGRESS"


def test_submission_completion_fails_the_job(service, actors, seeded, monkeypatch) -> None:
    submission_id = _queued_submission(service, actors)
    real_inputs = service._candidate_submission_git_inputs

    def inputs_then_open(submission):
        # The worker has passed its preflight and is about to run Git.
        _open_barrier(service, actors, ref="test:during-validation")
        return real_inputs(submission)

    monkeypatch.setattr(service, "_candidate_submission_git_inputs", inputs_then_open)
    job = service.execute_queued_candidate_submission(submission_id)

    assert job["status"] == "FAILED"
    assert "reconcile" in job["error_message"]
    assert _count(service, "candidates") == 0
    assert _ticket(service)["status"] == "DEV_IN_PROGRESS"


def _new_ticket(service, actors, ticket_id: str, *, ready: bool):
    return service.create_ticket(
        actors["planner"], feature_id="payment-retry", ticket_id=ticket_id,
        title=f"Work {ticket_id}", objective="Other work on the board",
        required_role=Role.DEVELOPER, acceptance_criteria=["It works"],
        preservation_constraints=["Checkout stays valid"], file_scope=["src/other/**"],
        ready=ready,
    )


def test_creating_ready_work_is_refused_but_backlog_is_not(service, actors, seeded) -> None:
    # A ticket-scoped freeze says nothing about new work: it is created ready.
    _open_barrier(service, actors, frozen=["DEV-142"], ref="test:scoped")
    assert _new_ticket(service, actors, "DEV-200", ready=True)["status"] == "READY"

    _open_barrier(service, actors, ref="test:project")
    _refused(lambda: _new_ticket(service, actors, "DEV-201", ready=True))
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT 1 FROM tickets WHERE ticket_id = 'DEV-201'"
        ).fetchone() is None
    # Planning itself goes on: backlog work does not execute.
    assert _new_ticket(service, actors, "DEV-202", ready=False)["status"] == "BACKLOG"


def test_changing_a_validation_contract_is_refused(service, actors, seeded) -> None:
    ticket = _ticket(service)
    contract = [
        {
            "criterion": criterion, "scenario_ref": f"scenario:criterion-{index}",
            "method": "automated", "expected_observation": criterion,
            "required_evidence_types": ["test_result"], "required_capabilities": [],
        }
        for index, criterion in enumerate(ticket["acceptance_criteria"], start=1)
    ]
    _open_barrier(service, actors, frozen=["DEV-142"])
    _refused(lambda: service.configure_validation_contract(
        actors["planner"], "DEV-142", ticket["version"], contract, "Bind the evidence plan.",
    ))
    assert _ticket(service)["version"] == ticket["version"]


def test_no_offer_is_made_while_the_project_is_closed(service, actors, seeded) -> None:
    service.register_agent(actors["developer"], skills=["hoh-developer"], capacity=1)
    _open_barrier(service, actors)

    inbox = service.get_agent_inbox(actors["developer"], "project-1")

    assert inbox["offers"] == []
    with service.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM work_offers").fetchone()[0] == 0

    keep_open_tickets(service, actors["planner"])
    reopened = service.get_agent_inbox(actors["developer"], "project-1")
    assert [offer["ticket"]["ticket_id"] for offer in reopened["offers"]] == ["DEV-142"]


# --- worktrees (real Git) ------------------------------------------------------

def _with_repository(service, actors, tmp_path: Path) -> Path:
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    keep_open_tickets(service, actors["planner"])
    return repository


def test_preparing_a_worktree_is_refused_before_git(service, actors, seeded, tmp_path, monkeypatch) -> None:
    _with_repository(service, actors, tmp_path)
    claim = _claimed(service, actors)
    _open_barrier(service, actors)
    monkeypatch.setattr(service, "_plan_worktree_takeover",
                        lambda *_a, **_k: pytest.fail("Git must not run behind a closed barrier"))

    _refused(lambda: service.prepare_worktree(
        actors["developer"], "DEV-142", claim["lease"]["lease_id"], claim["ticket"]["version"]))
    assert _count(service, "worktrees") == 0


def test_a_barrier_opened_during_worktree_git_refuses_the_assignment(
    service, actors, seeded, tmp_path, monkeypatch
) -> None:
    _with_repository(service, actors, tmp_path)
    claim = _claimed(service, actors)
    real_plan = service._plan_worktree_takeover

    def plan_then_open(snapshot):
        planned = real_plan(snapshot)
        _open_barrier(service, actors, ref="test:during-provision")
        return planned

    monkeypatch.setattr(service, "_plan_worktree_takeover", plan_then_open)
    _refused(lambda: service.prepare_worktree(
        actors["developer"], "DEV-142", claim["lease"]["lease_id"], claim["ticket"]["version"]))
    assert _count(service, "worktrees") == 0


def test_provision_worker_preflight_fails_the_job(service, actors, seeded, tmp_path) -> None:
    _with_repository(service, actors, tmp_path)
    claim = _claimed(service, actors)
    queued = service.queue_worktree_provision(
        actors["developer"], "DEV-142", claim["lease"]["lease_id"], claim["ticket"]["version"])
    _open_barrier(service, actors)

    job = service.execute_queued_worktree_provision(queued["provision"]["provision_id"])

    assert job["status"] == "FAILED"
    assert "reconcile" in job["error_message"]
    assert _count(service, "worktrees") == 0


# --- merges (real Git) ---------------------------------------------------------

def _merge_ready(service, actors, seeded, tmp_path):
    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path)
    keep_open_tickets(service, actors["planner"])
    # Planner has seen the QA result; claim against the ticket's current version.
    integration = _claimed_for_integration(service, actors, ticket, {"ticket": _ticket(service)})
    return repository, candidate, integration


def _queue_merge(service, actors, integration) -> str:
    queued = service.queue_merge_candidate(
        actors["integrator"], "DEV-142", integration["lease"]["lease_id"],
        integration["ticket"]["version"],
        context_snapshot_id=_snapshot(service, actors["integrator"]),
    )
    return queued["merge_job"]["merge_job_id"]


def test_queueing_a_merge_is_refused(service, actors, seeded, tmp_path) -> None:
    _repo, _candidate, integration = _merge_ready(service, actors, seeded, tmp_path)
    snapshot = _snapshot(service, actors["integrator"])
    _open_barrier(service, actors, frozen=["DEV-142"])

    _refused(lambda: service.queue_merge_candidate(
        actors["integrator"], "DEV-142", integration["lease"]["lease_id"],
        integration["ticket"]["version"], context_snapshot_id=snapshot))
    assert _count(service, "merge_jobs") == 0


def test_merge_worker_preflight_fails_without_git(service, actors, seeded, tmp_path) -> None:
    repository, candidate, integration = _merge_ready(service, actors, seeded, tmp_path)
    merge_job_id = _queue_merge(service, actors, integration)
    main_before = _git(repository, "rev-parse", "main")
    _open_barrier(service, actors)

    job = service.execute_queued_merge(merge_job_id)

    assert job["status"] == "FAILED"
    assert "reconcile" in job["error_message"]
    assert _git(repository, "rev-parse", "main") == main_before
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM merge_intents WHERE merge_job_id = ?", (merge_job_id,)
        ).fetchone()[0] == 0


def _open_during_gate(service, actors, monkeypatch, *, exit_code: int | None = None) -> None:
    real_execute = service.project_tests.execute

    def execute_then_open(*args, **kwargs):
        execution = real_execute(*args, **kwargs)
        _open_barrier(service, actors, ref="test:during-gate")
        if exit_code is not None:
            execution = dataclasses.replace(execution, exit_code=exit_code)
        return execution

    monkeypatch.setattr(service.project_tests, "execute", execute_then_open)


def test_a_barrier_opened_during_the_gate_stops_the_fast_forward(
    service, actors, seeded, tmp_path, monkeypatch
) -> None:
    repository, candidate, integration = _merge_ready(service, actors, seeded, tmp_path)
    merge_job_id = _queue_merge(service, actors, integration)
    main_before = _git(repository, "rev-parse", "main")
    _open_during_gate(service, actors, monkeypatch)

    job = service.execute_queued_merge(merge_job_id)

    assert job["status"] == "FAILED"
    assert "reconcile" in job["error_message"]
    assert _git(repository, "rev-parse", "main") == main_before
    assert _ticket(service)["status"] != "DONE"


def test_a_regression_result_waits_for_a_barrier_opened_during_the_gate(
    service, actors, seeded, tmp_path, monkeypatch
) -> None:
    # A failing gate leaves main where it was, so its REWORK result is a plan
    # change that must wait for Planner like any other execution.
    repository, candidate, integration = _merge_ready(service, actors, seeded, tmp_path)
    merge_job_id = _queue_merge(service, actors, integration)
    main_before = _git(repository, "rev-parse", "main")
    _open_during_gate(service, actors, monkeypatch, exit_code=1)

    job = service.execute_queued_merge(merge_job_id)

    assert job["status"] == "FAILED"
    assert "reconcile" in job["error_message"]
    assert job.get("merge_status") != "merged"
    assert _git(repository, "rev-parse", "main") == main_before
    assert _ticket(service)["status"] != "REWORK"
