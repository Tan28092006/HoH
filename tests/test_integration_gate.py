from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from helpers.barrier import keep_open_tickets
from helpers.merge import merge_through_queue
from helpers.gate import (
    _FAILING_REGRESSION,
    _PASSING_REGRESSION,
    _gate_ready_candidate,
    _git,
    _merge,
    _repository,
)

from hoh_control_plane.domain import AuthorizationError


def _gate_run(service, ticket_id: str) -> dict:
    """The regression run the integration gate itself recorded (not the seeded release run)."""
    with service.store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM qa_test_runs WHERE ticket_id = ? AND test_run_id NOT LIKE 'testrun_seed_%' "
            "ORDER BY created_at DESC LIMIT 1",
            (ticket_id,),
        ).fetchone()
    return dict(row)


@pytest.mark.parametrize("reason,code", [("incomplete_evidence", 0), ("resource_exhausted", 1)])
def test_infrastructure_gate_neither_merges_nor_requests_source_rework(service, actors, seeded, tmp_path, monkeypatch, reason, code):
    from hoh_control_plane.qa_runner import TestExecution
    repository, ticket, candidate, claim = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_PASSING_REGRESSION)
    before = _git(repository, "rev-parse", "main")
    monkeypatch.setattr(service.project_tests, "execute", lambda *a: TestExecution(
        command_digest="a" * 64, exit_code=code, timed_out=False, duration_ms=1,
        stdout="", stderr="", termination_reason=reason))
    outcome = _merge(service, actors["integrator"], ticket, claim)
    assert outcome["status"] == "FAILED"
    assert "infrastructure" in outcome["error_message"]
    assert _git(repository, "rev-parse", "main") == before
    assert outcome["ticket"]["status"] != "REWORK"


def test_passing_regression_gate_still_fast_forwards_main(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, candidate, integration = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_PASSING_REGRESSION
    )

    merged = _merge(service, actors["integrator"], ticket, integration)

    assert merged["merge_status"] == "merged"
    assert merged["ticket"]["status"] == "DONE"
    assert _git(repository, "rev-parse", "main") == candidate["candidate"]["commit_sha"]
    gate_run = _gate_run(service, ticket["ticket_id"])
    assert gate_run["exit_code"] == 0
    assert gate_run["candidate_id"] == candidate["candidate"]["candidate_id"]


def test_failing_regression_gate_leaves_main_untouched_and_reworks_the_ticket(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, candidate, integration = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_FAILING_REGRESSION
    )
    mainline_before = _git(repository, "rev-parse", "main")

    gated = _merge(service, actors["integrator"], ticket, integration)

    assert gated["merge_status"] != "merged"
    assert gated["ticket"]["status"] == "REWORK"
    assert _git(repository, "rev-parse", "main") == mainline_before
    assert mainline_before != candidate["candidate"]["commit_sha"]
    assert _gate_run(service, ticket["ticket_id"])["exit_code"] != 0


def test_failing_regression_gate_records_rework_context_for_the_developer(
    service, actors, seeded, tmp_path: Path
) -> None:
    _repository_path, ticket, candidate, integration = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_FAILING_REGRESSION
    )

    _merge(service, actors["integrator"], ticket, integration)
    gate_run = _gate_run(service, ticket["ticket_id"])

    context = service.get_ticket_context(actors["developer"], ticket["ticket_id"])
    entry_types = {entry["entry_type"] for entry in context["context_entries"]}
    assert "integration_regression_failed" in entry_types
    failure = next(
        entry
        for entry in context["context_entries"]
        if entry["entry_type"] == "integration_regression_failed"
    )
    with service.store.connection() as connection:
        content = connection.execute(
            "SELECT content FROM context_entries WHERE ticket_id = ? "
            "AND entry_type = 'integration_regression_failed'",
            (ticket["ticket_id"],),
        ).fetchone()[0]
    assert gate_run["storage_ref"] in content
    # The queued gate binds the entry to the rejected candidate by reference and
    # records the mainline it left unchanged (the candidate's base).
    assert failure["source_ref"] == f"candidate:{candidate['candidate']['candidate_id']}"
    assert failure["artifact_commit"] == candidate["candidate"]["base_commit"][:12]
    assert context["ticket"]["blocked_reason"]
    assert any(
        run["test_run_id"] == gate_run["test_run_id"]
        for run in context["test_runs"]
    )


def test_gate_runs_the_candidate_on_an_isolated_checkout_that_is_cleaned_up(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, candidate, integration = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_PASSING_REGRESSION
    )
    developer_worktree = Path(
        service.get_ticket_context(actors["developer"], ticket["ticket_id"])["worktree"][
            "worktree_path"
        ]
    )

    _merge(service, actors["integrator"], ticket, integration)

    integration_root = service.git_worktrees.integration_root
    assert not integration_root.exists() or not any(integration_root.rglob("regression.py"))
    assert developer_worktree.exists()
    listed = _git(repository, "worktree", "list", "--porcelain")
    assert ".integration" not in listed


def test_gate_reruns_cleanly_after_an_earlier_integration_attempt(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, _candidate, integration = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_FAILING_REGRESSION
    )
    first = _merge(service, actors["integrator"], ticket, integration)
    assert first["ticket"]["status"] == "REWORK"
    # A regression opens a Planner reconciliation atomically with its result.
    keep_open_tickets(service, actors["planner"])

    development = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], first["ticket"]["version"], 900
    )
    worktree = Path(
        service.get_ticket_context(actors["developer"], ticket["ticket_id"])["worktree"][
            "worktree_path"
        ]
    )
    (worktree / "src" / "checkout").mkdir(parents=True, exist_ok=True)
    (worktree / "src" / "checkout" / "regression.py").write_text(
        _PASSING_REGRESSION, encoding="utf-8"
    )
    _git(worktree, "add", "src/checkout/regression.py")
    _git(worktree, "commit", "-m", "Repair the regression")
    repaired = service.submit_candidate(
        actors["developer"],
        ticket["ticket_id"],
        development["lease"]["lease_id"],
        development["ticket"]["version"],
        _git(worktree, "rev-parse", "HEAD"),
        _git(worktree, "rev-parse", "main"),
        _git(worktree, "branch", "--show-current"),
        ["Regression repaired."],
        context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
    )
    qa_claim = service.claim_qa(
        actors["qa"], ticket["ticket_id"], repaired["ticket"]["version"], 900
    )
    verified = service.submit_qa_verdict(
        actors["qa"],
        ticket["ticket_id"],
        qa_claim["lease"]["lease_id"],
        repaired["candidate"]["candidate_id"],
        qa_claim["ticket"]["version"],
        "passed",
        [
            {
                "criterion": criterion,
                "status": "verified",
                "severity": "none",
                "observation": "Re-verified against the repaired candidate.",
                "evidence": [
                    {
                        "type": "test_result",
                        "observation": "passed",
                        "storage_ref": f"evidence://qa/re-{index}.json",
                    }
                ],
            }
            for index, criterion in enumerate(ticket["acceptance_criteria"], start=1)
        ],
        context_snapshot_id=service.get_ticket_context(actors["qa"], ticket["ticket_id"])["context_snapshot_id"],
    )
    second_integration = service.claim_integration(
        actors["integrator"], ticket["ticket_id"], verified["ticket"]["version"], 900
    )

    merged = _merge(service, actors["integrator"], ticket, second_integration)

    assert merged["merge_status"] == "merged"
    assert _git(repository, "rev-parse", "main") == repaired["candidate"]["commit_sha"]


@pytest.mark.parametrize("role", ["developer", "qa"])
def test_only_the_integrator_may_run_the_gate_and_move_main(
    service, actors, seeded, tmp_path: Path, role: str
) -> None:
    repository, ticket, _candidate, integration = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_PASSING_REGRESSION
    )
    mainline_before = _git(repository, "rev-parse", "main")

    with pytest.raises(AuthorizationError):
        service.queue_merge_candidate(
            actors[role],
            ticket["ticket_id"],
            integration["lease"]["lease_id"],
            integration["ticket"]["version"],
            context_snapshot_id=service.get_ticket_context(actors[role], ticket["ticket_id"])["context_snapshot_id"],
        )

    assert _git(repository, "rev-parse", "main") == mainline_before
