"""Drive an integration through the queued merge path the MCP uses."""
from __future__ import annotations

import hashlib


def seed_release_regression_run(service, ticket_id: str) -> None:
    """Record the passing registered-regression run the release policy requires.

    The row has no QA job behind it, so the integration gate still executes the
    regression command itself rather than reusing a QA run.
    """
    with service.store.connection(immediate=True) as connection:
        ticket = service._ticket(connection, ticket_id)
        candidate = connection.execute(
            "SELECT candidate_id FROM candidates WHERE ticket_id = ? AND commit_sha = ?",
            (ticket_id, ticket["candidate_commit"]),
        ).fetchone()
        profile = connection.execute(
            "SELECT test_command FROM candidate_test_profiles WHERE candidate_id = ?",
            (candidate["candidate_id"],),
        ).fetchone()
        if profile is None:
            return
        command = profile["test_command"]
        if isinstance(command, str):
            import json  # noqa: PLC0415

            command = json.loads(command)
        digest = hashlib.sha256("\0".join(command).encode()).hexdigest()
        connection.execute(
            """
            INSERT INTO qa_test_runs
                (test_run_id, candidate_id, ticket_id, command_digest, exit_code, timed_out,
                 duration_ms, storage_ref, report_path, executed_by, created_at,
                 termination_reason)
            VALUES (?, ?, ?, ?, 0, 0, 10, 'evidence://local/run.json', 'run.json', 'qa-01', ?,
                    'passed')
            """,
            (f"testrun_seed_{candidate['candidate_id']}", candidate["candidate_id"], ticket_id,
             digest, "2026-01-01T00:00:00Z"),
        )


def merge_through_queue(service, actor, ticket_id: str, lease_id: str, expected_version: int,
                        *, seed_release_evidence: bool = True) -> dict:
    """Queue then execute a merge; return the terminal job plus the current ticket."""
    if seed_release_evidence:
        seed_release_regression_run(service, ticket_id)
    queued = service.queue_merge_candidate(actor, ticket_id, lease_id, expected_version, context_snapshot_id=service.get_ticket_context(actor, ticket_id)["context_snapshot_id"])
    job = service.execute_queued_merge(queued["merge_job"]["merge_job_id"])
    with service.store.connection() as connection:
        ticket = service._ticket(connection, ticket_id)
    return {**job, "ticket": ticket}
