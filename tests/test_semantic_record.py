"""What a merge leaves behind in the repository about why a change exists (HOH-248)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hoh_control_plane import semantic_record
from hoh_control_plane.domain import Role


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments], check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


TICKET = {
    "ticket_id": "DEV-142",
    "title": "Repair checkout retry behaviour",
    "objective": "Retry a failed payment without creating a duplicate order.",
    "acceptance_criteria": ["A failed payment can be retried", "Exactly one order results"],
    "preservation_constraints": ["Existing successful checkout remains valid"],
}
CANDIDATE = {
    "commit_sha": "a" * 40,
    "base_commit": "b" * 40,
    "branch_ref": "hoh/project-1/DEV-142/developer-01",
    "submitted_by": "developer-01",
}
FINDINGS = [
    {
        "criterion": "A failed payment can be retried",
        "status": "verified",
        "observation": "Retry succeeded on the frozen candidate.",
        "evidence_refs": ["ev_0123456789abcdef"],
    }
]
DECISIONS = [{"content": "A retry must stay idempotent for the same order id."}]


# --- what the record says -----------------------------------------------------


def test_the_record_answers_why_the_change_exists() -> None:
    rendered = semantic_record.render(
        ticket=TICKET, candidate=CANDIDATE, findings=FINDINGS,
        decisions=DECISIONS, mainline_commit="c" * 40,
    )

    assert "DEV-142" in rendered
    assert TICKET["objective"] in rendered
    for criterion in TICKET["acceptance_criteria"]:
        assert criterion in rendered
    assert CANDIDATE["commit_sha"] in rendered
    assert CANDIDATE["base_commit"] in rendered
    assert DECISIONS[0]["content"] in rendered


def test_a_finding_names_the_criterion_it_answered() -> None:
    """So a reader can follow a source line back to what it was written to satisfy."""
    rendered = semantic_record.render(
        ticket=TICKET, candidate=CANDIDATE, findings=FINDINGS,
        decisions=DECISIONS, mainline_commit="c" * 40,
    )

    assert "A failed payment can be retried" in rendered
    assert "verified" in rendered
    assert "ev_0123456789abcdef" in rendered


def test_the_record_carries_no_machine_local_fact() -> None:
    """A worktree path or a branch means nothing in anyone else's clone."""
    rendered = semantic_record.render(
        ticket=TICKET, candidate=CANDIDATE, findings=FINDINGS,
        decisions=DECISIONS, mainline_commit="c" * 40,
    )

    assert CANDIDATE["branch_ref"] not in rendered
    assert CANDIDATE["submitted_by"] not in rendered
    assert "worktree" not in rendered.lower()
    assert "lease" not in rendered.lower()


def test_a_record_that_would_leak_a_local_value_is_refused() -> None:
    with pytest.raises(ValueError):
        semantic_record.assert_carries_nothing_local(
            "merged from hoh/project-1/DEV-142/developer-01", ["hoh/project-1/DEV-142/developer-01"]
        )


def test_it_reads_as_prose_rather_than_as_rows() -> None:
    rendered = semantic_record.render(
        ticket=TICKET, candidate=CANDIDATE, findings=FINDINGS,
        decisions=DECISIONS, mainline_commit="c" * 40,
    )

    assert rendered.startswith("# DEV-142")
    assert "## Why this change exists" in rendered
    assert "{" not in rendered and "[]" not in rendered, "not a serialized row dump"


def test_a_correction_says_what_it_replaces_rather_than_editing_it() -> None:
    rendered = semantic_record.render(
        ticket=TICKET, candidate=CANDIDATE, findings=FINDINGS, decisions=DECISIONS,
        mainline_commit="c" * 40, supersedes="d" * 40,
    )

    assert "Supersedes" in rendered
    assert "d" * 40 in rendered


def test_a_candidate_with_no_findings_says_so_rather_than_implying_approval() -> None:
    rendered = semantic_record.render(
        ticket=TICKET, candidate=CANDIDATE, findings=[], decisions=[],
        mainline_commit="c" * 40,
    )

    assert "No findings were recorded" in rendered


def test_one_file_per_ticket_not_per_table() -> None:
    assert semantic_record.record_path("DEV-142") == "docs/decisions/DEV-142.md"


def test_a_ticket_id_cannot_escape_the_record_directory() -> None:
    path = semantic_record.record_path("../../etc/passwd")
    assert path.startswith(semantic_record.RECORD_DIRECTORY + "/")
    assert ".." not in path
    with pytest.raises(ValueError):
        semantic_record.record_path("../")


# --- what a merge actually lands ----------------------------------------------


def _merged(service, actors, seeded, tmp_path: Path, *, passing: bool = True):
    """Drive a ticket all the way to a protected merge."""
    from tests.test_integration_gate import (  # noqa: PLC0415
        _FAILING_REGRESSION,
        _PASSING_REGRESSION,
        _gate_ready_candidate,
    )

    repository, ticket, candidate, integration = _gate_ready_candidate(
        service, actors, seeded, tmp_path,
        retry_body=_PASSING_REGRESSION if passing else _FAILING_REGRESSION,
    )
    merged = service.merge_candidate(
        actors["integrator"],
        ticket["ticket_id"],
        integration["lease"]["lease_id"],
        integration["ticket"]["version"],
    )
    return repository, ticket, candidate, merged


def test_a_merge_lands_the_record_in_the_same_operation(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, _candidate, merged = _merged(service, actors, seeded, tmp_path)

    assert merged["merge_status"] == "merged"
    assert merged["semantic_record_commit"], "the merge reports where the record landed"
    written = Path(repository) / semantic_record.record_path(ticket["ticket_id"])
    assert written.is_file()
    assert ticket["objective"] in written.read_text(encoding="utf-8")


def test_the_record_is_committed_on_the_default_branch(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, candidate, merged = _merged(service, actors, seeded, tmp_path)

    head = _git(Path(repository), "rev-parse", "main")
    assert head == merged["semantic_record_commit"]
    assert _git(Path(repository), "rev-parse", "main~1") == candidate["candidate"]["commit_sha"]
    tracked = _git(Path(repository), "ls-tree", "--name-only", "-r", "main")
    assert semantic_record.record_path(ticket["ticket_id"]) in tracked


def test_an_unmerged_outcome_leaves_the_repository_undescribed(
    service, actors, seeded, tmp_path: Path
) -> None:
    """The repository describes what landed, never what was attempted."""
    repository, ticket, _candidate, outcome = _merged(
        service, actors, seeded, tmp_path, passing=False
    )

    assert outcome["merge_status"] != "merged"
    assert not (Path(repository) / semantic_record.record_path(ticket["ticket_id"])).exists()


def test_the_first_record_is_accepted_in_a_repository_that_has_none(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, _candidate, merged = _merged(service, actors, seeded, tmp_path)

    assert merged["merge_status"] == "merged"
    assert (Path(repository) / semantic_record.RECORD_DIRECTORY).is_dir()
