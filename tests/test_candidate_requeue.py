from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hoh_control_plane.domain import AuthorizationError, ConflictError
from helpers.integration import _git, _repository


def _setup_candidate_ticket(service, actors, seeded, tmp_path: Path):
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    ticket = seeded["ticket"]
    claim = service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)
    prepared = service.prepare_worktree(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
    )["worktree"]
    worktree = Path(prepared["worktree_path"])
    change = worktree / "src" / "checkout" / "retry.txt"
    change.parent.mkdir(parents=True, exist_ok=True)
    change.write_text("safe retry\n", encoding="utf-8")
    _git(worktree, "add", "src/checkout/retry.txt")
    _git(worktree, "commit", "-m", "Implement retry")

    commit_sha = _git(worktree, "rev-parse", "HEAD")
    submitted = service.submit_candidate(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
        commit_sha,
        prepared["base_commit"],
        prepared["branch_ref"],
        ["Feature is implemented and ready for QA verification."],
        context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
    )
    return repository, submitted["ticket"], submitted["candidate"], worktree


def test_planner_requeues_stale_candidate_after_mainline_drift(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, candidate, _worktree = _setup_candidate_ticket(
        service, actors, seeded, tmp_path
    )
    assert ticket["status"] == "QA_READY"

    # Advance mainline so candidate base becomes stale
    (repository / "mainline_update.txt").write_text("mainline moved\n", encoding="utf-8")
    _git(repository, "add", "mainline_update.txt")
    _git(repository, "commit", "-m", "Advance mainline")

    result = service.requeue_candidate(
        actors["planner"],
        ticket["ticket_id"],
        ticket["version"],
        reason="Base drifted after mainline update",
    )

    # AC 1 & 2: Ticket moves to REWORK, candidate preserved, reason recorded
    assert result["ticket"]["status"] == "REWORK"
    assert result["ticket"]["blocked_reason"] == "Base drifted after mainline update"
    assert result["candidate"]["candidate_id"] == candidate["candidate_id"]
    assert result["context_entry"]["entry_type"] == "candidate_requeued"
    assert result["context_entry"]["content"] == "Base drifted after mainline update"
    assert result["context_entry"]["source_ref"] == f"candidate:{candidate['candidate_id']}"

    # Verify audit event
    with service.store.connection() as connection:
        event = connection.execute(
            """
            SELECT * FROM audit_events
            WHERE aggregate_id = ? AND event_type = 'candidate.requeued'
            ORDER BY sequence DESC LIMIT 1
            """,
            (ticket["ticket_id"],),
        ).fetchone()
        assert event is not None
        assert candidate["candidate_id"] in event["payload"]

    # AC 2: Eligible for a fresh Developer claim via inbox
    service.register_agent(actors["developer"], skills=["hoh-developer"])
    inbox = service.get_agent_inbox(actors["developer"], "project-1")
    assert any(offer["ticket"]["ticket_id"] == ticket["ticket_id"] for offer in inbox["offers"])


def test_requeue_fails_closed_when_base_is_current_main(
    service, actors, seeded, tmp_path: Path
) -> None:
    _repository, ticket, _candidate, _worktree = _setup_candidate_ticket(
        service, actors, seeded, tmp_path
    )
    # Mainline has NOT drifted; base is current main
    with pytest.raises(ConflictError) as excinfo:
        service.requeue_candidate(
            actors["planner"],
            ticket["ticket_id"],
            ticket["version"],
            reason="Attempted requeue without drift",
        )
    assert "not stale" in str(excinfo.value).lower() or "not drifted" in str(excinfo.value).lower()


def test_requeue_fails_closed_when_active_qa_lease_exists(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, _candidate, _worktree = _setup_candidate_ticket(
        service, actors, seeded, tmp_path
    )
    # Advance mainline
    (repository / "update.txt").write_text("update\n", encoding="utf-8")
    _git(repository, "add", "update.txt")
    _git(repository, "commit", "-m", "Advance main")

    # Active lease exists while ticket is QA_READY
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO leases (lease_id, ticket_id, agent_id, role, acquired_at, heartbeat_at, expires_at)
            VALUES ('lease_active_1', ?, 'qa-01', 'qa', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2099-01-01T00:00:00Z')
            """,
            (ticket["ticket_id"],),
        )

    with pytest.raises(ConflictError) as excinfo:
        service.requeue_candidate(
            actors["planner"],
            ticket["ticket_id"],
            ticket["version"],
            reason="Mainline drifted while lease active",
        )
    assert "active lease" in str(excinfo.value).lower()


def test_requeue_fails_closed_for_non_qa_ready_statuses(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    ticket = seeded["ticket"]

    # Ticket is READY, no candidate
    with pytest.raises(ConflictError) as excinfo:
        service.requeue_candidate(actors["planner"], ticket["ticket_id"], ticket["version"])
    assert "qa_ready" in str(excinfo.value).lower()

    # Claim for DEV_IN_PROGRESS
    claim = service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)
    with pytest.raises(ConflictError) as excinfo:
        service.requeue_candidate(
            actors["planner"], ticket["ticket_id"], claim["ticket"]["version"]
        )
    assert "qa_ready" in str(excinfo.value).lower()


def test_requeue_fails_closed_when_job_is_running(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, candidate, _worktree = _setup_candidate_ticket(
        service, actors, seeded, tmp_path
    )
    # Advance mainline
    (repository / "update.txt").write_text("update\n", encoding="utf-8")
    _git(repository, "add", "update.txt")
    _git(repository, "commit", "-m", "Advance main")

    # Simulate a running worktree provision job
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO worktree_provision_jobs
                (provision_id, project_id, ticket_id, lease_id, expected_version, requested_by, status, created_at)
            VALUES ('prov_dummy_1', 'project-1', ?, 'lease_dummy_1', 1, 'dev-01', 'RUNNING', '2026-01-01T00:00:00Z')
            """,
            (ticket["ticket_id"],),
        )

    with pytest.raises(ConflictError) as excinfo:
        service.requeue_candidate(
            actors["planner"],
            ticket["ticket_id"],
            ticket["version"],
            reason="Mainline drifted",
        )
    assert "running job" in str(excinfo.value).lower()


def test_requeue_fails_closed_for_unauthorized_roles(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, _candidate, _worktree = _setup_candidate_ticket(
        service, actors, seeded, tmp_path
    )
    (repository / "update.txt").write_text("update\n", encoding="utf-8")
    _git(repository, "add", "update.txt")
    _git(repository, "commit", "-m", "Advance main")

    with pytest.raises(AuthorizationError):
        service.requeue_candidate(
            actors["developer"],
            ticket["ticket_id"],
            ticket["version"],
        )

    with pytest.raises(AuthorizationError):
        service.requeue_candidate(
            actors["qa"],
            ticket["ticket_id"],
            ticket["version"],
        )


def test_requeue_fails_closed_for_stale_version(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, _candidate, _worktree = _setup_candidate_ticket(
        service, actors, seeded, tmp_path
    )
    (repository / "update.txt").write_text("update\n", encoding="utf-8")
    _git(repository, "add", "update.txt")
    _git(repository, "commit", "-m", "Advance main")

    with pytest.raises(ConflictError) as excinfo:
        service.requeue_candidate(
            actors["planner"],
            ticket["ticket_id"],
            ticket["version"] - 1,
        )
    assert "version" in str(excinfo.value).lower()


def test_requeue_idempotency_and_duplicate_retries(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, candidate, _worktree = _setup_candidate_ticket(
        service, actors, seeded, tmp_path
    )
    (repository / "update.txt").write_text("update\n", encoding="utf-8")
    _git(repository, "add", "update.txt")
    _git(repository, "commit", "-m", "Advance main")

    # First call with idempotency key
    key = "requeue-idemp-key-1"
    first = service.requeue_candidate(
        actors["planner"],
        ticket["ticket_id"],
        ticket["version"],
        reason="Drifted",
        idempotency_key=key,
    )
    assert first["ticket"]["status"] == "REWORK"

    # Replay with identical key and arguments succeeds with cached response
    replay = service.requeue_candidate(
        actors["planner"],
        ticket["ticket_id"],
        ticket["version"],
        reason="Drifted",
        idempotency_key=key,
    )
    assert replay["ticket"]["ticket_id"] == first["ticket"]["ticket_id"]
    assert replay["ticket"]["version"] == first["ticket"]["version"]

    # Duplicate call without idempotency key fails closed (ticket already REWORK, not QA_READY)
    with pytest.raises(ConflictError):
        service.requeue_candidate(
            actors["planner"],
            ticket["ticket_id"],
            first["ticket"]["version"],
            reason="Drifted duplicate",
        )


def test_developer_rebase_takeover_and_replacement_candidate_creation(
    service, actors, seeded, tmp_path: Path
) -> None:
    repository, ticket, original_candidate, _old_worktree = _setup_candidate_ticket(
        service, actors, seeded, tmp_path
    )
    (repository / "mainline_update.txt").write_text("mainline moved\n", encoding="utf-8")
    _git(repository, "add", "mainline_update.txt")
    _git(repository, "commit", "-m", "Advance mainline")
    new_main = _git(repository, "rev-parse", "main")

    # Planner requeues
    requeued = service.requeue_candidate(
        actors["planner"],
        ticket["ticket_id"],
        ticket["version"],
        reason="Base drift requires rework",
    )
    rework_ticket = requeued["ticket"]
    assert rework_ticket["status"] == "REWORK"

    # Developer 2 claims rework ticket
    dev_claim = service.claim_ticket(
        actors["developer_2"],
        rework_ticket["ticket_id"],
        rework_ticket["version"],
        900,
    )
    assert dev_claim["ticket"]["status"] == "DEV_IN_PROGRESS"

    # AC 4: Takeover prepares fresh worktree based on current main (rebase_required)
    prepared = service.prepare_worktree(
        actors["developer_2"],
        rework_ticket["ticket_id"],
        dev_claim["lease"]["lease_id"],
        dev_claim["ticket"]["version"],
    )["worktree"]

    # The prepared worktree base commit matches the updated mainline commit
    assert prepared["base_commit"] == new_main

    # Developer applies changes on top of updated main
    dev_worktree = Path(prepared["worktree_path"])
    retry_file = dev_worktree / "src" / "checkout" / "retry.txt"
    retry_file.parent.mkdir(parents=True, exist_ok=True)
    retry_file.write_text("rebased safe retry\n", encoding="utf-8")
    _git(dev_worktree, "add", "src/checkout/retry.txt")
    _git(dev_worktree, "commit", "-m", "Implement rebased retry")

    new_commit = _git(dev_worktree, "rev-parse", "HEAD")

    # Developer submits fresh candidate
    submission = service.submit_candidate(
        actors["developer_2"],
        rework_ticket["ticket_id"],
        dev_claim["lease"]["lease_id"],
        dev_claim["ticket"]["version"],
        new_commit,
        prepared["base_commit"],
        prepared["branch_ref"],
        ["Rebased feature ready for QA."],
        context_snapshot_id=service.get_ticket_context(actors["developer_2"], rework_ticket["ticket_id"])["context_snapshot_id"],
    )

    # AC 4: New immutable candidate_id created, old candidate preserved
    new_candidate = submission["candidate"]
    assert new_candidate["candidate_id"] != original_candidate["candidate_id"]
    assert new_candidate["commit_sha"] == new_commit
    assert submission["ticket"]["status"] == "QA_READY"

    # Both candidates exist in the database
    with service.store.connection() as connection:
        rows = connection.execute(
            "SELECT candidate_id FROM candidates WHERE ticket_id = ? ORDER BY submitted_at ASC",
            (rework_ticket["ticket_id"],),
        ).fetchall()
        candidate_ids = [row["candidate_id"] for row in rows]
        assert original_candidate["candidate_id"] in candidate_ids
        assert new_candidate["candidate_id"] in candidate_ids
