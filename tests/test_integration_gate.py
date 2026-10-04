from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from conftest import keep_open_tickets

from hoh_control_plane.domain import AuthorizationError


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


def _gate_ready_candidate(service, actors, seeded, tmp_path: Path, *, retry_body: str):
    """Drive a ticket to MERGE_READY with a Planner-registered regression command.

    The registered command runs `regression.py` from the checkout it is given, so the
    gate's verdict is decided by the candidate's own content.
    """
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    service.configure_test_command(
        actors["planner"], "project-1", [sys.executable, "src/checkout/regression.py"], 60
    )
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
    (worktree / "src" / "checkout").mkdir(parents=True, exist_ok=True)
    (worktree / "src" / "checkout" / "regression.py").write_text(retry_body, encoding="utf-8")
    _git(worktree, "add", "src/checkout/regression.py")
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
    )
    qa_claim = service.claim_qa(
        actors["qa"], ticket["ticket_id"], candidate["ticket"]["version"], 900
    )
    verified = service.submit_qa_verdict(
        actors["qa"],
        ticket["ticket_id"],
        qa_claim["lease"]["lease_id"],
        candidate["candidate"]["candidate_id"],
        qa_claim["ticket"]["version"],
        "passed",
        [
            {
                "criterion": criterion,
                "status": "verified",
                "severity": "none",
                "observation": "Verified against the frozen candidate.",
                "evidence": [
                    {
                        "type": "test_result",
                        "observation": "passed",
                        "storage_ref": f"evidence://qa/{index}.json",
                    }
                ],
            }
            for index, criterion in enumerate(ticket["acceptance_criteria"], start=1)
        ],
    )
    integration = service.claim_integration(
        actors["integrator"],
        ticket["ticket_id"],
        verified["ticket"]["version"],
        900,
    )
    return repository, ticket, candidate, integration


_PASSING_REGRESSION = "raise SystemExit(0)\n"
_FAILING_REGRESSION = "raise SystemExit('retry still duplicates orders')\n"


@pytest.mark.parametrize("reason,code", [("incomplete_evidence", 0), ("resource_exhausted", 1)])
def test_infrastructure_gate_neither_merges_nor_requests_source_rework(service, actors, seeded, tmp_path, monkeypatch, reason, code):
    from hoh_control_plane.domain import ValidationError
    from hoh_control_plane.qa_runner import TestExecution
    repository, ticket, candidate, claim = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_PASSING_REGRESSION)
    before = _git(repository, "rev-parse", "main")
    monkeypatch.setattr(service.project_tests, "execute", lambda *a: TestExecution(
        command_digest="a" * 64, exit_code=code, timed_out=False, duration_ms=1,
        stdout="", stderr="", termination_reason=reason))
    with pytest.raises(ValidationError, match="infrastructure"):
        service.merge_candidate(actors["integrator"], ticket["ticket_id"],
            claim["lease"]["lease_id"], claim["ticket"]["version"])
    assert _git(repository, "rev-parse", "main") == before
    assert service.get_ticket_context(actors["planner"], ticket["ticket_id"])["ticket"]["status"] == "MERGE_READY"


def test_passing_regression_gate_still_fast_forwards_main(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, candidate, integration = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_PASSING_REGRESSION
    )

    merged = service.merge_candidate(
        actors["integrator"],
        ticket["ticket_id"],
        integration["lease"]["lease_id"],
        integration["ticket"]["version"],
    )

    assert merged["merge_status"] == "merged"
    assert merged["ticket"]["status"] == "DONE"
    # main advances by two: the fast-forwarded candidate, then the record of what
    # it was for. The candidate is main's parent, not main itself.
    assert _git(repository, "rev-parse", "main~1") == candidate["candidate"]["commit_sha"]
    assert merged["test_run"]["exit_code"] == 0
    assert merged["test_run"]["candidate_id"] == candidate["candidate"]["candidate_id"]


def test_failing_regression_gate_leaves_main_untouched_and_reworks_the_ticket(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, candidate, integration = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_FAILING_REGRESSION
    )
    mainline_before = _git(repository, "rev-parse", "main")

    gated = service.merge_candidate(
        actors["integrator"],
        ticket["ticket_id"],
        integration["lease"]["lease_id"],
        integration["ticket"]["version"],
    )

    assert gated["merge_status"] == "regression_failed"
    assert gated["ticket"]["status"] == "REWORK"
    assert _git(repository, "rev-parse", "main") == mainline_before
    assert mainline_before != candidate["candidate"]["commit_sha"]
    assert gated["test_run"]["exit_code"] != 0


def test_failing_regression_gate_records_rework_context_for_the_developer(
    service, actors, seeded, tmp_path: Path
) -> None:
    _repository_path, ticket, candidate, integration = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_FAILING_REGRESSION
    )

    gated = service.merge_candidate(
        actors["integrator"],
        ticket["ticket_id"],
        integration["lease"]["lease_id"],
        integration["ticket"]["version"],
    )

    context = service.get_ticket_context(actors["developer"], ticket["ticket_id"])
    entry_types = {entry["entry_type"] for entry in context["context_entries"]}
    assert "integration_regression_failed" in entry_types
    failure = next(
        entry
        for entry in context["context_entries"]
        if entry["entry_type"] == "integration_regression_failed"
    )
    assert failure["source_ref"] == gated["test_run"]["storage_ref"]
    assert failure["artifact_commit"] == candidate["candidate"]["commit_sha"]
    assert context["ticket"]["blocked_reason"]
    assert any(
        run["test_run_id"] == gated["test_run"]["test_run_id"]
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

    service.merge_candidate(
        actors["integrator"],
        ticket["ticket_id"],
        integration["lease"]["lease_id"],
        integration["ticket"]["version"],
    )

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
    first = service.merge_candidate(
        actors["integrator"],
        ticket["ticket_id"],
        integration["lease"]["lease_id"],
        integration["ticket"]["version"],
    )
    assert first["merge_status"] == "regression_failed"
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
    )
    second_integration = service.claim_integration(
        actors["integrator"], ticket["ticket_id"], verified["ticket"]["version"], 900
    )

    merged = service.merge_candidate(
        actors["integrator"],
        ticket["ticket_id"],
        second_integration["lease"]["lease_id"],
        second_integration["ticket"]["version"],
    )

    assert merged["merge_status"] == "merged"
    assert _git(repository, "rev-parse", "main~1") == repaired["candidate"]["commit_sha"]


def test_a_candidate_with_no_configured_test_command_still_merges(
    service, actors, seeded, tmp_path: Path
) -> None:
    """A project that never registered a regression command must merge exactly as
    it did before this gate existed -- the gate only applies when one is configured.
    """
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
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
    (worktree / "src" / "checkout").mkdir(parents=True, exist_ok=True)
    (worktree / "src" / "checkout" / "retry.txt").write_text("safe retry\n", encoding="utf-8")
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
        ["No project test command is configured yet."],
    )
    qa_claim = service.claim_qa(
        actors["qa"], ticket["ticket_id"], candidate["ticket"]["version"], 900
    )
    verified = service.submit_qa_verdict(
        actors["qa"],
        ticket["ticket_id"],
        qa_claim["lease"]["lease_id"],
        candidate["candidate"]["candidate_id"],
        qa_claim["ticket"]["version"],
        "passed",
        [
            {
                "criterion": criterion,
                "status": "verified",
                "severity": "none",
                "observation": "Verified manually.",
                "evidence": [
                    {
                        "type": "test_result",
                        "observation": "passed",
                        "storage_ref": f"evidence://qa/{index}.json",
                    }
                ],
            }
            for index, criterion in enumerate(ticket["acceptance_criteria"], start=1)
        ],
    )
    integration = service.claim_integration(
        actors["integrator"], ticket["ticket_id"], verified["ticket"]["version"], 900
    )

    merged = service.merge_candidate(
        actors["integrator"],
        ticket["ticket_id"],
        integration["lease"]["lease_id"],
        integration["ticket"]["version"],
    )

    assert merged["merge_status"] == "merged"
    assert merged["test_run"] is None
    # main advances by two: the fast-forwarded candidate, then the record of what
    # it was for. The candidate is main's parent, not main itself.
    assert _git(repository, "rev-parse", "main~1") == candidate["candidate"]["commit_sha"]


@pytest.mark.parametrize("role", ["developer", "qa"])
def test_only_the_integrator_may_run_the_gate_and_move_main(
    service, actors, seeded, tmp_path: Path, role: str
) -> None:
    repository, ticket, _candidate, integration = _gate_ready_candidate(
        service, actors, seeded, tmp_path, retry_body=_PASSING_REGRESSION
    )
    mainline_before = _git(repository, "rev-parse", "main")

    with pytest.raises(AuthorizationError):
        service.merge_candidate(
            actors[role],
            ticket["ticket_id"],
            integration["lease"]["lease_id"],
            integration["ticket"]["version"],
        )

    assert _git(repository, "rev-parse", "main") == mainline_before
