"""Detached worker processes started by MCP tools (legacy worker mode)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from hoh_control_plane.domain import ValidationError


def _worker_location(database_path: Path | str) -> tuple[str, dict[str, str] | None]:
    location = str(database_path)
    if location.startswith(("postgresql://", "postgres://")):
        environment = os.environ.copy()
        environment["HOH_DATABASE_URL"] = location
        # The store factory selects the server URL from env. Never expose its
        # password in command-line arguments / process listings.
        return "data/control-plane.db", environment
    return location, None


def _supervised_workers() -> bool:
    mode = os.getenv('HOH_WORKER_MODE', 'legacy')
    if mode not in {'legacy', 'supervised'}:
        raise ValidationError('HOH_WORKER_MODE must be legacy or supervised')
    return mode == 'supervised'


def _spawn_worker(module: str, id_flag: str, database_path: Path, job_id: str) -> None:
    """Start `python -m <module> --database <db> <id_flag> <job_id>` detached."""
    if _supervised_workers():
        return  # The durable queue is drained by hoh-supervisor.
    database_argument, environment = _worker_location(database_path)
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            module,
            "--database",
            database_argument,
            id_flag,
            job_id,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=environment,
    )


def _start_qa_test_worker(database_path: Path, test_run_id: str) -> None:
    """Launch one isolated runner so stdio MCP calls stay available for polling."""
    _spawn_worker("hoh_control_plane.qa_worker", "--test-run-id", database_path, test_run_id)


def _start_worktree_provision_worker(database_path: Path, provision_id: str) -> None:
    """Launch Git worktree setup away from the synchronous MCP stdio request."""
    _spawn_worker("hoh_control_plane.worktree_worker", "--provision-id", database_path, provision_id)


def _start_candidate_submission_worker(database_path: Path, submission_id: str) -> None:
    """Validate candidate Git identity away from the MCP request."""
    _spawn_worker(
        "hoh_control_plane.candidate_submission_worker", "--submission-id", database_path, submission_id
    )


def _start_merge_worker(database_path: Path, merge_job_id: str) -> None:
    """Fast-forward the protected default branch away from the MCP stdio request."""
    _spawn_worker("hoh_control_plane.integration_worker", "--merge-job-id", database_path, merge_job_id)
