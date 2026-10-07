"""Gaps the audit found whose fix belongs to an open wave 8 branch.

Each test states the correct behavior and is a strict xfail: when the owning
branch lands the fix, the test passes, strict mode turns that into a failure,
and whoever merges removes the marker. Nothing here is fixed on this branch.
"""
from __future__ import annotations

import pytest
from helpers.integration import _git, _repository

from hoh_control_plane.domain import ValidationError
from hoh_control_plane.git_worktrees import GitWorktreeManager


def _ticket(service) -> dict:
    with service.store.connection() as connection:
        return service._ticket(connection, "DEV-142")


def _finding(criterion: str, status: str) -> dict:
    return {
        "criterion": criterion, "status": status,
        "severity": "none" if status == "verified" else "high",
        "observation": f"{status} on the frozen candidate",
        "evidence": [{"type": "test_result", "observation": status,
                      "storage_ref": f"evidence://qa/{status}.json"}],
    }


@pytest.mark.xfail(strict=True, reason="R9/B1, wave 8A: passed must require every finding verified")
def test_a_passed_verdict_with_a_failed_finding_is_refused(service, actors, seeded) -> None:
    claim = service.claim_ticket(actors["developer"], "DEV-142", _ticket(service)["version"], 900)
    submitted = service.submit_candidate(
        actors["developer"], "DEV-142", claim["lease"]["lease_id"], claim["ticket"]["version"],
        "a" * 40, "b" * 40, "agent/dev-01/DEV-142", ["Ready."],
        context_snapshot_id=service.get_ticket_context(
            actors["developer"], "DEV-142")["context_snapshot_id"],
    )
    qa = service.claim_qa(actors["qa"], "DEV-142", submitted["ticket"]["version"], 900)
    criteria = _ticket(service)["acceptance_criteria"]
    findings = [_finding(criterion, "verified") for criterion in criteria]
    findings.append(_finding(criteria[0], "failed"))

    with pytest.raises(ValidationError):
        service.submit_qa_verdict(
            actors["qa"], "DEV-142", qa["lease"]["lease_id"],
            submitted["candidate"]["candidate_id"], qa["ticket"]["version"], "passed", findings,
            context_snapshot_id=qa["current_context_snapshot_id"],
        )


@pytest.mark.xfail(strict=True, reason="A2, wave 8E: changed_paths must read -z, not C-quoted names")
def test_changed_paths_returns_non_ascii_names_verbatim(tmp_path) -> None:
    repository = _repository(tmp_path)
    base = _git(repository, "rev-parse", "HEAD")
    name = "src/checkout/thành-toán.txt"
    path = repository / name
    path.parent.mkdir(parents=True)
    path.write_text("retry\n", encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-m", "Non-ASCII path")
    head = _git(repository, "rev-parse", "HEAD")

    paths = GitWorktreeManager(tmp_path / "worktrees").changed_paths(
        repository_path=str(repository), base_commit=base, candidate_commit=head)

    assert paths == [name]
