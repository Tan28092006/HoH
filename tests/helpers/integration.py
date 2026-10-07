"""A candidate that QA verified, ready for the Integrator, on a real Git repository."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments], check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "integration-repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "hoh-tests@example.invalid")
    _git(repository, "config", "user.name", "HoH Tests")
    (repository / "README.md").write_text("# project\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "Initial commit")
    return repository


REGRESSION_COMMAND = ["python", "-c", "pass"]


def record_regression_run(service, candidate_id: str, ticket_id: str, exit_code: int = 0) -> None:
    """Seed the registered regression evidence the release policy requires.

    The control plane only writes a qa_test_runs row for a run whose resolved
    source was the frozen candidate, so seeding one here stands in for that run
    without executing it in every merge-mechanics test.
    """
    digest = hashlib.sha256("\0".join(REGRESSION_COMMAND).encode()).hexdigest()
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO qa_test_runs
                (test_run_id, candidate_id, ticket_id, command_digest, exit_code, timed_out,
                 duration_ms, storage_ref, report_path, executed_by, created_at)
            VALUES (?, ?, ?, ?, ?, 0, 10, 'evidence://local/run.json', 'run.json', 'qa-01', ?)
            """,
            (f"testrun_{candidate_id[-8:]}_{exit_code}", candidate_id, ticket_id, digest,
             exit_code, "2026-01-01T00:00:00Z"),
        )


def _candidate_ready_for_integration(service, actors, seeded, tmp_path: Path):
    repository = _repository(tmp_path)
    service.configure_repository(actors["planner"], "project-1", str(repository))
    service.configure_test_command(actors["planner"], "project-1", REGRESSION_COMMAND, 60)
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
    change = worktree / "src" / "checkout" / "retry.txt"
    change.parent.mkdir(parents=True)
    change.write_text("safe retry\n", encoding="utf-8")
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
    record_regression_run(
        service, candidate["candidate"]["candidate_id"], ticket["ticket_id"]
    )
    return repository, ticket, candidate, verified


def _claimed_for_integration(service, actors, ticket, verified):
    return service.claim_integration(
        actors["integrator"], ticket["ticket_id"], verified["ticket"]["version"], 900
    )


def _mark_run_as_qa_job(service, candidate, ticket, timeout_seconds: int = 60) -> None:
    candidate_id = candidate["candidate"]["candidate_id"]
    digest = hashlib.sha256("\0".join(REGRESSION_COMMAND).encode()).hexdigest()
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO qa_test_jobs
                (test_run_id, project_id, candidate_id, ticket_id, lease_id, test_command,
                 command_digest, timeout_seconds, worktree_path, candidate_commit,
                 requested_by, status, created_at)
            VALUES (?, 'project-1', ?, ?, 'lease_qa', '[]', ?, ?, 'unused', ?, 'qa-01',
                    'COMPLETED', '2026-01-01T00:00:00Z')
            """,
            (f"testrun_{candidate_id[-8:]}_0", candidate_id, ticket["ticket_id"], digest,
             timeout_seconds, candidate["candidate"]["commit_sha"]),
        )


def _add_context(service, ticket, entry_type: str, artifact_commit: str | None, created_at: str):
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO context_entries
                (entry_id, project_id, feature_id, ticket_id, entry_type, content,
                 artifact_commit, actor_id, actor_role, created_at)
            VALUES (?, 'project-1', ?, ?, ?, 'Planner decision', ?, 'planner-01', 'planner', ?)
            """,
            (
                f"ctx_{entry_type}_{created_at}",
                ticket["feature_id"],
                ticket["ticket_id"],
                entry_type,
                artifact_commit,
                created_at,
            ),
        )


def _merge(service, actors, seeded, tmp_path):
    repository, ticket, candidate, verified = _candidate_ready_for_integration(
        service, actors, seeded, tmp_path
    )
    integration = _claimed_for_integration(service, actors, ticket, verified)
    queued = service.queue_merge_candidate(
        actors["integrator"], ticket["ticket_id"],
        integration["lease"]["lease_id"], integration["ticket"]["version"],
        context_snapshot_id=service.get_ticket_context(actors["integrator"], ticket["ticket_id"])["context_snapshot_id"],
    )
    return service.execute_queued_merge(queued["merge_job"]["merge_job_id"])


def queued_merge(service, actors, seeded, tmp_path):
    repository, ticket, candidate, verified = _candidate_ready_for_integration(service, actors, seeded, tmp_path)
    claim = _claimed_for_integration(service, actors, ticket, verified)
    job = service.queue_merge_candidate(actors['integrator'], ticket['ticket_id'],
                                       claim['lease']['lease_id'], claim['ticket']['version'], context_snapshot_id=service.get_ticket_context(actors['integrator'], ticket['ticket_id'])["context_snapshot_id"])['merge_job']
    return repository, candidate['candidate'], job
