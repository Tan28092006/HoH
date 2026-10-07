"""A ticket with a candidate, findings, a worktree and a long history, for packet tests."""
from __future__ import annotations

import subprocess
from pathlib import Path

from hoh_control_plane.domain import Actor

# Below the protected floor: forces every optional section out of the packet.
TIGHT_BUDGET = 400


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


def _version(service, actor, ticket_id: str) -> int:
    return service.get_ticket_context(actor, ticket_id)["ticket"]["version"]


def _rich_ticket(service, actors, seeded, tmp_path: Path, *, checkpoints: int = 12):
    """A ticket carrying a candidate, QA findings, a worktree and a verbose history."""
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    lease_id = claim["lease"]["lease_id"]
    prepared = service.prepare_worktree(
        actors["developer"], ticket["ticket_id"], lease_id, claim["ticket"]["version"]
    )
    worktree = Path(prepared["worktree"]["worktree_path"])
    retry = worktree / "src" / "checkout" / "retry.txt"
    retry.parent.mkdir(parents=True)
    retry.write_text("safe retry\n", encoding="utf-8")
    _git(worktree, "add", "src/checkout/retry.txt")
    _git(worktree, "commit", "-m", "Implement safe retry")

    for index in range(checkpoints):
        service.append_progress(
            actors["developer"],
            ticket["ticket_id"],
            lease_id,
            f"Checkpoint {index}: "
            + "verbose developer narration that a bounded packet should compact. " * 12,
        )
    decision = service.append_planner_context(
        actors["planner"],
        entry_type="design_decision",
        content=(
            "Durable decision: a retry must stay idempotent for the same order id. " * 12
        ),
        ticket_id=ticket["ticket_id"],
    )

    candidate = service.submit_candidate(
        actors["developer"],
        ticket["ticket_id"],
        lease_id,
        _version(service, actors["developer"], ticket["ticket_id"]),
        _git(worktree, "rev-parse", "HEAD"),
        prepared["worktree"]["base_commit"],
        prepared["worktree"]["branch_ref"],
        ["Retry implementation is ready for QA."],
        context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
    )
    qa_claim = service.claim_qa(
        actors["qa"], ticket["ticket_id"], candidate["ticket"]["version"], 900
    )
    service.submit_qa_verdict(
        actors["qa"],
        ticket["ticket_id"],
        qa_claim["lease"]["lease_id"],
        candidate["candidate"]["candidate_id"],
        qa_claim["ticket"]["version"],
        "failed",
        [
            {
                "criterion": ticket["acceptance_criteria"][0],
                "status": "failed",
                "severity": "high",
                "observation": "The retry still duplicates an order under load.",
                "evidence": [
                    {
                        "type": "test_result",
                        "observation": "duplicate order observed",
                        "storage_ref": "evidence://qa/duplicate-order.json",
                    }
                ],
            }
        ],
        context_snapshot_id=service.get_ticket_context(actors["qa"], ticket["ticket_id"])["context_snapshot_id"],
    )
    return ticket, candidate, decision


def _all_roles(actors) -> dict[str, Actor]:
    return {
        "developer": actors["developer"],
        "qa": actors["qa"],
        "planner": actors["planner"],
        "integrator": actors["integrator"],
    }
