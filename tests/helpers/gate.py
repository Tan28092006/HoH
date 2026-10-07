"""A MERGE_READY ticket whose registered regression runs the candidate's own script."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from helpers.merge import merge_through_queue


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
        context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
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
        context_snapshot_id=service.get_ticket_context(actors["qa"], ticket["ticket_id"])["context_snapshot_id"],
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


def _merge(service, actor, ticket, integration) -> dict:
    return merge_through_queue(
        service, actor, ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
    )
