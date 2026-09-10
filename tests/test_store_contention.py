from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hoh_control_plane.domain import Role
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store import ContentionError, SQLiteStore


def _hold_raw_write_lock(db_path: Path, hold_seconds: float, ready: threading.Event) -> None:
    """Simulate a competing writer process holding SQLite's write lock."""
    connection = sqlite3.connect(db_path, timeout=10)
    try:
        connection.execute("BEGIN IMMEDIATE")
        ready.set()
        time.sleep(hold_seconds)
        connection.commit()
    finally:
        connection.close()


def _fast_timeout_store(tmp_path: Path) -> SQLiteStore:
    """A store whose busy handler waits well under a second, so a real
    lock-wait-then-fail or lock-wait-then-succeed can be exercised quickly
    instead of waiting out the production 10-second default.
    """
    store = SQLiteStore(tmp_path / "fast-timeout.db", busy_timeout_seconds=0.3)
    store.initialize()
    return store


def test_a_hold_shorter_than_the_busy_timeout_is_absorbed_transparently(
    tmp_path: Path,
) -> None:
    store = _fast_timeout_store(tmp_path)
    ready = threading.Event()
    holder = threading.Thread(
        target=_hold_raw_write_lock, args=(store.path, 0.1, ready), daemon=True
    )
    holder.start()
    ready.wait(timeout=2)

    started = time.perf_counter()
    with store.connection(immediate=True) as connection:
        connection.execute(
            "INSERT INTO projects (project_id, name, description, default_branch, version, "
            "created_at, updated_at) VALUES ('proj-retry', 'P', 'D', 'main', 1, 'now', 'now')"
        )
    elapsed = time.perf_counter() - started

    holder.join(timeout=2)
    assert elapsed < 2, "a hold shorter than the busy timeout must not fail or hang"
    with store.connection() as connection:
        row = connection.execute(
            "SELECT project_id FROM projects WHERE project_id = 'proj-retry'"
        ).fetchone()
    assert row is not None


def test_a_hold_longer_than_the_busy_timeout_raises_a_clean_retryable_domain_error(
    tmp_path: Path,
) -> None:
    store = _fast_timeout_store(tmp_path)
    ready = threading.Event()
    holder = threading.Thread(
        target=_hold_raw_write_lock, args=(store.path, 1.5, ready), daemon=True
    )
    holder.start()
    ready.wait(timeout=2)

    started = time.perf_counter()
    with pytest.raises(ContentionError) as excinfo:
        with store.connection(immediate=True) as connection:
            connection.execute(
                "INSERT INTO projects (project_id, name, description, default_branch, "
                "version, created_at, updated_at) "
                "VALUES ('proj-exhausted', 'P', 'D', 'main', 1, 'now', 'now')"
            )
    elapsed = time.perf_counter() - started
    holder.join(timeout=3)

    assert elapsed < 1, "the wait must be bounded by busy_timeout_seconds, not the full hold"
    assert excinfo.value.code == "database_contention"
    assert excinfo.value.status_code == 503
    assert "locked" not in str(excinfo.value).lower()
    assert "sqlite" not in str(excinfo.value).lower()
    with store.connection() as connection:
        row = connection.execute(
            "SELECT project_id FROM projects WHERE project_id = 'proj-exhausted'"
        ).fetchone()
    assert row is None, "a failed acquisition must not leave a partial mutation"


def test_a_non_contention_operational_error_is_not_translated_or_retried(
    store: SQLiteStore,
) -> None:
    with pytest.raises(sqlite3.OperationalError) as excinfo:
        with store.connection(immediate=True) as connection:
            connection.execute("SELECT * FROM this_table_does_not_exist")

    assert "no such table" in str(excinfo.value).lower()


def test_concurrent_developer_and_qa_activity_on_different_tickets_completes_cleanly(
    store: SQLiteStore,
) -> None:
    """Two independent role workflows on two different tickets, driven from two
    threads against the SAME database file, must both complete without raising a
    raw SQLite error and without corrupting each other's ticket state.
    """
    service = ControlPlaneService(store)
    planner = _actor("planner-01", Role.PLANNER)
    developer = _actor("dev-01", Role.DEVELOPER)
    qa = _actor("qa-01", Role.QA)
    service.create_project(planner, "project-1", "Project", "Description", "main")
    service.create_feature(planner, "project-1", "feature-1", "Feature", "Objective")
    dev_ticket = service.create_ticket(
        planner, "feature-1", "DEV-CONTENTION-1", "Dev work", "Observable result",
        Role.DEVELOPER, ["Behavior is visible"], [], ["src/**"], ready=True,
    )
    qa_ticket = service.create_ticket(
        planner, "feature-1", "DEV-CONTENTION-2", "QA-bound work", "Observable result",
        Role.DEVELOPER, ["Behavior is visible"], [], ["src/**"], ready=True,
    )

    errors: list[BaseException] = []

    def _developer_workflow() -> None:
        try:
            for _ in range(20):
                ticket = service.get_ticket_context(developer, dev_ticket["ticket_id"])["ticket"]
                try:
                    claim = service.claim_ticket(
                        developer, ticket["ticket_id"], ticket["version"], 900
                    )
                except Exception:
                    continue
                service.append_progress(
                    developer, ticket["ticket_id"], claim["lease"]["lease_id"], "Working."
                )
                service.submit_candidate(
                    developer, ticket["ticket_id"], claim["lease"]["lease_id"],
                    claim["ticket"]["version"], "a" * 40, "b" * 40,
                    "agent/dev-01/DEV-CONTENTION-1", ["Ready for QA."],
                )
                return
        except BaseException as error:  # noqa: BLE001 - surfaced to the test thread
            errors.append(error)

    def _qa_setup_and_activity() -> None:
        try:
            for _ in range(20):
                ticket = service.get_ticket_context(developer, qa_ticket["ticket_id"])["ticket"]
                try:
                    claim = service.claim_ticket(
                        developer, ticket["ticket_id"], ticket["version"], 900
                    )
                except Exception:
                    continue
                candidate = service.submit_candidate(
                    developer, ticket["ticket_id"], claim["lease"]["lease_id"],
                    claim["ticket"]["version"], "c" * 40, "d" * 40,
                    "agent/dev-01/DEV-CONTENTION-2", ["Ready for QA."],
                )
                qa_claim = service.claim_qa(
                    qa, ticket["ticket_id"], candidate["ticket"]["version"], 900
                )
                service.submit_qa_verdict(
                    qa, ticket["ticket_id"], qa_claim["lease"]["lease_id"],
                    candidate["candidate"]["candidate_id"], qa_claim["ticket"]["version"],
                    "passed",
                    [{
                        "criterion": "Behavior is visible",
                        "status": "verified",
                        "severity": "none",
                        "observation": "Verified.",
                        "evidence": [{
                            "type": "test_result", "observation": "passed",
                            "storage_ref": "evidence://x",
                        }],
                    }],
                )
                return
        except BaseException as error:  # noqa: BLE001 - surfaced to the test thread
            errors.append(error)

    threads = [
        threading.Thread(target=_developer_workflow),
        threading.Thread(target=_qa_setup_and_activity),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, f"concurrent role activity must not raise: {errors}"
    with store.connection() as connection:
        dev_status = connection.execute(
            "SELECT status FROM tickets WHERE ticket_id = 'DEV-CONTENTION-1'"
        ).fetchone()["status"]
        qa_status = connection.execute(
            "SELECT status FROM tickets WHERE ticket_id = 'DEV-CONTENTION-2'"
        ).fetchone()["status"]
        candidate_counts = connection.execute(
            "SELECT ticket_id, COUNT(*) AS n FROM candidates GROUP BY ticket_id"
        ).fetchall()
    assert dev_status == "QA_READY"
    assert qa_status == "VERIFIED"
    assert all(row["n"] == 1 for row in candidate_counts), "no duplicate candidate rows"


def test_database_created_by_the_previous_schema_supports_concurrent_activity(
    tmp_path: Path,
) -> None:
    """A database migrated from the pre-HOH-214 schema must not regress: it should
    still normalize contention and support simultaneous role activity afterward.
    """
    store = SQLiteStore(tmp_path / "legacy-control-plane.db")
    store.initialize()
    service = ControlPlaneService(store)
    actors_by_role = {
        "planner": _actor("planner-01", Role.PLANNER),
        "developer": _actor("dev-01", Role.DEVELOPER),
        "qa": _actor("qa-01", Role.QA),
    }
    service.create_project(actors_by_role["planner"], "project-1", "Project", "Description", "main")
    service.create_feature(actors_by_role["planner"], "project-1", "feature-1", "Feature", "Objective")
    ticket = service.create_ticket(
        actors_by_role["planner"], "feature-1", "DEV-LEGACY-1", "Work", "Observable result",
        Role.DEVELOPER, ["Behavior is visible"], [], ["src/**"], ready=True,
    )
    repository = tmp_path / "checkout-repository"
    repository.mkdir(parents=True)
    for arguments in (
        ["init", "--initial-branch=main"],
        ["config", "user.email", "hoh-tests@example.invalid"],
        ["config", "user.name", "HoH Tests"],
    ):
        subprocess.run(["git", "-C", str(repository), *arguments], check=True)
    (repository / "README.md").write_text("# Checkout\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-m", "Initial checkout"], check=True)
    service.configure_repository(actors_by_role["planner"], "project-1", str(repository))
    service.configure_test_command(
        actors_by_role["planner"], "project-1", [sys.executable, "-c", "print('ok')"], 30
    )
    claim = service.claim_ticket(actors_by_role["developer"], ticket["ticket_id"], ticket["version"], 900)
    prepared = service.prepare_worktree(
        actors_by_role["developer"], ticket["ticket_id"],
        claim["lease"]["lease_id"], claim["ticket"]["version"],
    )
    worktree = Path(prepared["worktree"]["worktree_path"])
    (worktree / "src").mkdir(parents=True, exist_ok=True)
    (worktree / "src" / "file.txt").write_text("change\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "src/file.txt"], check=True)
    subprocess.run(["git", "-C", str(worktree), "commit", "-m", "Change"], check=True)
    head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    candidate = service.submit_candidate(
        actors_by_role["developer"], ticket["ticket_id"], claim["lease"]["lease_id"],
        claim["ticket"]["version"], head, prepared["worktree"]["base_commit"],
        prepared["worktree"]["branch_ref"], ["Ready for QA."],
    )
    qa_claim = service.claim_qa(actors_by_role["qa"], ticket["ticket_id"], candidate["ticket"]["version"], 900)
    queued = service.queue_project_tests(
        actors_by_role["qa"], ticket["ticket_id"], qa_claim["lease"]["lease_id"]
    )["test_run"]
    job_columns = (
        "test_run_id, project_id, candidate_id, ticket_id, lease_id, test_command, "
        "command_digest, timeout_seconds, worktree_path, candidate_commit, requested_by, "
        "status, error_message, created_at, started_at, completed_at"
    )
    with store.connection(immediate=True) as connection:
        connection.execute("DROP TABLE qa_test_jobs")
        connection.execute(
            """
            CREATE TABLE qa_test_jobs (
                test_run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
                ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
                lease_id TEXT NOT NULL REFERENCES leases(lease_id),
                test_command TEXT NOT NULL,
                command_digest TEXT NOT NULL,
                timeout_seconds INTEGER NOT NULL,
                worktree_path TEXT NOT NULL,
                candidate_commit TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            )
            """
        )
        connection.execute(
            f"INSERT INTO qa_test_jobs ({job_columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(
                json.dumps(queued[column]) if column == "test_command" else queued[column]
                for column in job_columns.split(", ")
            ),
        )

    # Re-run the fresh-startup migration against this now-legacy-shaped database,
    # then prove ordinary contention handling still works afterward.
    store.initialize()

    ready = threading.Event()
    holder = threading.Thread(
        target=_hold_raw_write_lock, args=(store.path, 0.15, ready), daemon=True
    )
    holder.start()
    ready.wait(timeout=2)
    with store.connection(immediate=True) as connection:
        connection.execute(
            "INSERT INTO projects (project_id, name, description, default_branch, "
            "version, created_at, updated_at) VALUES ('proj-legacy', 'P', 'D', 'main', 1, 'now', 'now')"
        )
    holder.join(timeout=2)

    with store.connection() as connection:
        migrated_job = connection.execute(
            "SELECT status FROM qa_test_jobs WHERE test_run_id = ?", (queued["test_run_id"],)
        ).fetchone()
        lease_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(qa_test_jobs)"
        ).fetchall()
    assert migrated_job is not None
    assert not any(
        foreign_key["table"] == "leases" for foreign_key in lease_foreign_keys
    )


def _actor(agent_id: str, role: Role):
    from hoh_control_plane.domain import Actor

    return Actor(agent_id, role, frozenset({"project-1"}))
