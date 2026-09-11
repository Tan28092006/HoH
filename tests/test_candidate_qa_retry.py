from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from hoh_control_plane.domain import (
    Actor,
    AuthorizationError,
    ConflictError,
    Role,
    ValidationError,
)

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


def _blocked_candidate_with_insufficient_evidence(service, actors, seeded, tmp_path: Path):
    """Reproduce HOH-208's real lifecycle: a candidate whose only QA rejection was an
    infrastructure evidence gap on every criterion, escalated by Developer to BLOCKED.
    """
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    service.configure_test_command(
        actors["planner"], "project-1", [sys.executable, "-c", "print('passed')"], 60
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
    )
    candidate_id = candidate["candidate"]["candidate_id"]
    qa_claim = service.claim_qa(
        actors["qa"], ticket["ticket_id"], candidate["ticket"]["version"], 900
    )
    rejected = service.submit_qa_verdict(
        actors["qa"],
        ticket["ticket_id"],
        qa_claim["lease"]["lease_id"],
        candidate_id,
        qa_claim["ticket"]["version"],
        "failed",
        [
            {
                "criterion": criterion,
                "status": "insufficient_evidence",
                "severity": "high",
                "observation": (
                    "The planner-frozen command resolved hoh_control_plane from the "
                    "ambient install rather than this frozen candidate worktree."
                ),
                "evidence": [
                    {
                        "type": "project_test_run",
                        "observation": "Ambient import shadowed the candidate.",
                        "storage_ref": "evidence://local/testrun-shadowed.json",
                    }
                ],
            }
            for criterion in ticket["acceptance_criteria"]
        ],
    )
    with service.store.connection() as connection:
        finding_ids = [
            row["finding_id"]
            for row in connection.execute(
                "SELECT finding_id FROM qa_findings WHERE candidate_id = ? ORDER BY created_at",
                (candidate_id,),
            ).fetchall()
        ]
    re_claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], rejected["ticket"]["version"], 900
    )
    blocked = service.report_blocker(
        actors["developer"],
        ticket["ticket_id"],
        re_claim["lease"]["lease_id"],
        re_claim["ticket"]["version"],
        (
            "QA rejection was an infrastructure artifact mismatch, not a logic defect; "
            "per standing Planner policy this candidate must not be altered or resubmitted."
        ),
    )
    return ticket, candidate["candidate"], blocked["ticket"], finding_ids


def test_recovery_reopens_a_blocked_ticket_for_an_independent_fresh_qa_verdict(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, candidate, blocked_ticket, finding_ids = _blocked_candidate_with_insufficient_evidence(
        service, actors, seeded, tmp_path
    )
    assert blocked_ticket["status"] == "BLOCKED"

    recovered = service.recover_candidate_for_qa(
        actors["planner"],
        ticket["ticket_id"],
        blocked_ticket["version"],
        finding_ids[0],
    )

    assert recovered["ticket"]["status"] == "QA_READY"
    assert recovered["ticket"]["blocked_reason"] is None
    assert recovered["candidate"]["candidate_id"] == candidate["candidate_id"]
    assert recovered["candidate"]["commit_sha"] == candidate["commit_sha"]

    context = service.get_ticket_context(actors["planner"], ticket["ticket_id"])
    decision = next(
        entry
        for entry in context["context_entries"]
        if entry["entry_type"] == "planning_decision"
        and entry["source_ref"] == finding_ids[0]
    )
    assert candidate["commit_sha"] in decision["content"]
    assert decision["artifact_commit"] == candidate["commit_sha"]

    # An independent QA agent claims the SAME candidate and can now pass it outright.
    qa_claim = service.claim_qa(
        actors["qa"], ticket["ticket_id"], recovered["ticket"]["version"], 900
    )
    verified = service.submit_qa_verdict(
        actors["qa"],
        ticket["ticket_id"],
        qa_claim["lease"]["lease_id"],
        candidate["candidate_id"],
        qa_claim["ticket"]["version"],
        "passed",
        [
            {
                "criterion": criterion,
                "status": "verified",
                "severity": "none",
                "observation": "Re-verified against the same frozen candidate.",
                "evidence": [
                    {
                        "type": "test_result",
                        "observation": "passed",
                        "storage_ref": f"evidence://qa/recovered-{index}.json",
                    }
                ],
            }
            for index, criterion in enumerate(ticket["acceptance_criteria"])
        ],
    )
    assert verified["ticket"]["status"] == "VERIFIED"


def test_recovery_accepts_a_candidate_artifact_mismatch_test_job_as_evidence(
    service, actors, seeded, tmp_path: Path
) -> None:
    """The newer, more precise candidate_artifact_mismatch failure mode (from the QA
    regression runner fix) is just as valid a recovery basis as an older
    insufficient_evidence finding -- test the evidence-type dispatch in isolation.
    """
    ticket, candidate, blocked_ticket, _finding_ids = _blocked_candidate_with_insufficient_evidence(
        service, actors, seeded, tmp_path
    )
    test_run_id = "testrun_mismatch0000000000000000"
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO qa_test_jobs
                (test_run_id, project_id, candidate_id, ticket_id, lease_id, test_command,
                 command_digest, timeout_seconds, worktree_path, candidate_commit,
                 requested_by, status, error_message, created_at)
            VALUES (?, 'project-1', ?, ?, 'lease_gone00000000000000000000', '[]',
                    'digest', 60, 'unused', ?, 'qa-01', 'FAILED', ?, '2026-01-01T00:00:00Z')
            """,
            (
                test_run_id,
                candidate["candidate_id"],
                ticket["ticket_id"],
                candidate["commit_sha"],
                (
                    "candidate_artifact_mismatch: expected hoh_control_plane under the "
                    "frozen worktree, observed D:\\HoH\\src\\hoh_control_plane\\__init__.py"
                ),
            ),
        )

    recovered = service.recover_candidate_for_qa(
        actors["planner"], ticket["ticket_id"], blocked_ticket["version"], test_run_id
    )

    assert recovered["ticket"]["status"] == "QA_READY"
    assert recovered["candidate"]["candidate_id"] == candidate["candidate_id"]


def test_recovery_refuses_a_candidate_with_a_logical_qa_failure(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, _candidate, blocked_ticket, finding_ids = _blocked_candidate_with_insufficient_evidence(
        service, actors, seeded, tmp_path
    )
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE qa_findings SET status = 'failed' WHERE finding_id = ?",
            (finding_ids[0],),
        )

    with pytest.raises(ValidationError, match="logical QA failure"):
        service.recover_candidate_for_qa(
            actors["planner"],
            ticket["ticket_id"],
            blocked_ticket["version"],
            finding_ids[-1],
        )


def test_recovery_refuses_a_ticket_that_is_not_blocked_or_rework(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]

    with pytest.raises(ConflictError, match="BLOCKED or REWORK"):
        service.recover_candidate_for_qa(
            actors["planner"], ticket["ticket_id"], ticket["version"], "finding_missing"
        )


def test_recovery_refuses_an_active_lease_on_the_ticket(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, _candidate, blocked_ticket, finding_ids = _blocked_candidate_with_insufficient_evidence(
        service, actors, seeded, tmp_path
    )
    # BLOCKED and REWORK tickets have no lease in the normal state machine (claim_ticket
    # only accepts READY/REWORK and report_blocker deletes the lease it used); a stray
    # lease row is an edge case this guard must still catch.
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO leases
                (lease_id, ticket_id, agent_id, role, acquired_at, heartbeat_at, expires_at)
            VALUES ('lease_stray0000000000000000000', ?, 'someone-else', 'qa', ?, ?, ?)
            """,
            (ticket["ticket_id"], "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z"),
        )

    with pytest.raises(ConflictError, match="active lease"):
        service.recover_candidate_for_qa(
            actors["planner"], ticket["ticket_id"], blocked_ticket["version"], finding_ids[0]
        )


def test_recovery_refuses_an_active_project_test_run(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, candidate, blocked_ticket, finding_ids = _blocked_candidate_with_insufficient_evidence(
        service, actors, seeded, tmp_path
    )
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO qa_test_jobs
                (test_run_id, project_id, candidate_id, ticket_id, lease_id, test_command,
                 command_digest, timeout_seconds, worktree_path, candidate_commit,
                 requested_by, status, created_at)
            VALUES ('testrun_active00000000000000000', 'project-1', ?, ?,
                    'lease_gone00000000000000000000', '[]', 'digest', 60, 'unused', ?,
                    'qa-01', 'RUNNING', '2026-01-01T00:00:00Z')
            """,
            (candidate["candidate_id"], ticket["ticket_id"], candidate["commit_sha"]),
        )

    with pytest.raises(ConflictError, match="active project test run"):
        service.recover_candidate_for_qa(
            actors["planner"], ticket["ticket_id"], blocked_ticket["version"], finding_ids[0]
        )


def test_recovery_refuses_a_ticket_with_more_than_one_frozen_candidate(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, _candidate, blocked_ticket, finding_ids = _blocked_candidate_with_insufficient_evidence(
        service, actors, seeded, tmp_path
    )
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO candidates
                (candidate_id, ticket_id, base_commit, commit_sha, branch_ref,
                 developer_claims, submitted_by, submitted_at, frozen_at)
            VALUES ('cand_extra0000000000000000000000', ?, ?, ?, 'refs/heads/extra', '[]',
                    'dev-01', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """,
            (ticket["ticket_id"], "a" * 40, "b" * 40),
        )

    with pytest.raises(ValidationError, match="exactly one frozen candidate"):
        service.recover_candidate_for_qa(
            actors["planner"], ticket["ticket_id"], blocked_ticket["version"], finding_ids[0]
        )


def test_recovery_refuses_an_unrecognized_evidence_reference(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, _candidate, blocked_ticket, _finding_ids = _blocked_candidate_with_insufficient_evidence(
        service, actors, seeded, tmp_path
    )

    with pytest.raises(ValidationError, match="evidence_reference"):
        service.recover_candidate_for_qa(
            actors["planner"], ticket["ticket_id"], blocked_ticket["version"], "not-a-real-id"
        )


def test_recovery_refuses_evidence_that_does_not_belong_to_the_candidate(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, _candidate, blocked_ticket, _finding_ids = _blocked_candidate_with_insufficient_evidence(
        service, actors, seeded, tmp_path
    )

    with pytest.raises(ValidationError, match="does not belong"):
        service.recover_candidate_for_qa(
            actors["planner"],
            ticket["ticket_id"],
            blocked_ticket["version"],
            "finding_doesnotexist00000000000",
        )


def test_recovery_refuses_a_finding_that_is_not_an_evidence_gap(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, _candidate, blocked_ticket, finding_ids = _blocked_candidate_with_insufficient_evidence(
        service, actors, seeded, tmp_path
    )
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE qa_findings SET status = 'verified' WHERE finding_id = ?",
            (finding_ids[0],),
        )

    with pytest.raises(ValidationError, match="insufficient-evidence gap"):
        service.recover_candidate_for_qa(
            actors["planner"], ticket["ticket_id"], blocked_ticket["version"], finding_ids[0]
        )


def test_recovery_refuses_an_unauthorized_role(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, _candidate, blocked_ticket, finding_ids = _blocked_candidate_with_insufficient_evidence(
        service, actors, seeded, tmp_path
    )

    for role_actor in (actors["developer"], actors["qa"], actors["integrator"]):
        with pytest.raises(AuthorizationError):
            service.recover_candidate_for_qa(
                role_actor, ticket["ticket_id"], blocked_ticket["version"], finding_ids[0]
            )


def test_recovery_refuses_a_stale_expected_version(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, _candidate, blocked_ticket, finding_ids = _blocked_candidate_with_insufficient_evidence(
        service, actors, seeded, tmp_path
    )

    with pytest.raises(ConflictError, match="stale"):
        service.recover_candidate_for_qa(
            actors["planner"],
            ticket["ticket_id"],
            blocked_ticket["version"] + 1,
            finding_ids[0],
        )


def test_recovery_never_touches_default_branch_or_the_candidate_row(
    service, actors, seeded, tmp_path: Path
) -> None:
    ticket, candidate, blocked_ticket, finding_ids = _blocked_candidate_with_insufficient_evidence(
        service, actors, seeded, tmp_path
    )

    service.recover_candidate_for_qa(
        actors["planner"], ticket["ticket_id"], blocked_ticket["version"], finding_ids[0]
    )

    with service.store.connection() as connection:
        stored_candidate = connection.execute(
            "SELECT * FROM candidates WHERE candidate_id = ?", (candidate["candidate_id"],)
        ).fetchone()
        candidate_count = connection.execute(
            "SELECT COUNT(*) AS n FROM candidates WHERE ticket_id = ?", (ticket["ticket_id"],)
        ).fetchone()["n"]
    assert stored_candidate["commit_sha"] == candidate["commit_sha"]
    assert stored_candidate["base_commit"] == candidate["base_commit"]
    assert candidate_count == 1
