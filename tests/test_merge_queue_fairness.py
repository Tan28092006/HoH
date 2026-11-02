"""Real queue/guard fixtures; captured dispatch is selection, not Git execution."""
from concurrent.futures import Future
from contextlib import contextmanager

import pytest

from hoh_control_plane.domain import Actor, Role
from hoh_control_plane.supervisor import Supervisor


def _queued_merges(service, project_id, identifiers, created_at):
    planner = Actor("queue-fixture", Role.PLANNER, frozenset({project_id}))
    service.create_project(planner, project_id, project_id, "Disposable merge queue", "main")
    service.create_feature(planner, project_id, project_id + "-feature", "Queue selection", "Selection only")
    for identifier in identifiers:
        ticket = service.create_ticket(planner, project_id + "-feature", identifier, "Queued merge",
            "Selection only", Role.DEVELOPER, ["Select admissible work"], [], ["src/queue.py"])
        with service.store.connection(immediate=True) as db:
            db.execute("INSERT INTO candidates (candidate_id, ticket_id, base_commit, commit_sha, "
                "branch_ref, developer_claims, submitted_by, submitted_at, frozen_at) "
                "VALUES (?, ?, ?, ?, ?, '[]', 'fixture', ?, ?)",
                (identifier, ticket["ticket_id"], "a" * 40, "b" * 40, "fixture/" + identifier, created_at, created_at))
            db.execute("INSERT INTO merge_jobs (merge_job_id, project_id, ticket_id, lease_id, "
                "expected_version, candidate_id, requested_by, status, created_at) "
                "VALUES (?, ?, ?, 'fixture-lease', ?, ?, 'fixture', 'QUEUED', ?)",
                (identifier, project_id, identifier, ticket["version"], identifier, created_at))
    return planner


def _pause(service, project_id):
    with service.store.connection(immediate=True) as db:
        service._pause_merge_queue(db, project_id, pause_reason="mainline_check_failed",
            detail="Disposable queue fixture", ticket=None, evidence_refs=[])


@pytest.mark.parametrize("max_workers", [1, 4])
@pytest.mark.parametrize("row_style", ["sqlite", "postgres_row"])
def test_paused_prefix_cannot_hide_an_admissible_project(service, monkeypatch, max_workers, row_style):
    paused = [f"paused-{i:02}" for i in range(max_workers * 2 + 1)]
    _queued_merges(service, "paused", paused, "2026-01-01T00:00:00Z")
    _queued_merges(service, "open", ["open-merge"], "2026-01-02T00:00:00Z")
    _pause(service, "paused")
    if row_style == "postgres_row":
        from hoh_control_plane.postgres_store import Row
        connection = service.store.connection

        @contextmanager
        def postgres_rows(**kwargs):
            with connection(**kwargs) as db:
                db.raw.row_factory = lambda cursor, values: Row(tuple(column[0] for column in cursor.description), values)
                yield db
        monkeypatch.setattr(service.store, "connection", postgres_rows)

    submitted = []
    with Supervisor(service, max_workers=max_workers) as supervisor:
        monkeypatch.setattr(supervisor, "_submit", lambda kind, identifier:
                            (submitted.append((kind, identifier)), Future())[1])
        assert supervisor.tick() == {"started": 1, "active": 1}
        assert submitted == [("merge", "open-merge")]
        assert supervisor.tick() == {"started": 0, "active": 1}
        assert submitted == [("merge", "open-merge")]
    assert service.merge_queue_admits(paused[0]) is False


def test_worker_rechecks_a_pause_after_dispatch_before_git(service, monkeypatch):
    _queued_merges(service, "open", ["open-merge"], "2026-01-01T00:00:00Z")
    def forbidden(*args, **kwargs):
        pytest.fail("Paused worker reached Git or a project suite")
    monkeypatch.setattr(service.git_worktrees, "repository_lock", forbidden)
    monkeypatch.setattr(service.project_tests, "execute", forbidden)
    with Supervisor(service, max_workers=1) as supervisor:
        monkeypatch.setattr(supervisor, "_submit", lambda *_: Future())
        assert supervisor.tick() == {"started": 1, "active": 1}
        _pause(service, "open")
        assert service.execute_queued_merge("open-merge")["status"] == "QUEUED"
    with service.store.connection() as db:
        assert db.execute("SELECT count(*) FROM merge_intents").fetchone()[0] == 0
