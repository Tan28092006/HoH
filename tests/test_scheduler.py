from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hoh_control_plane.domain import Actor, AuthorizationError, Role, TicketStatus
from hoh_control_plane.scheduler import sweep_once


def _scheduler() -> Actor:
    return Actor("hoh-scheduler", Role.ADMIN, frozenset({"*"}))


def _expire(store, lease_id: str) -> None:
    with store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE leases SET expires_at = ? WHERE lease_id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), lease_id),
        )


def test_scheduler_requeues_an_expired_developer_lease(
    service, store, actors, seeded
) -> None:
    ticket = seeded["ticket"]
    claim = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    _expire(store, claim["lease"]["lease_id"])

    swept = sweep_once(store.path)
    context = service.get_ticket_context(actors["developer_2"], ticket["ticket_id"])

    assert swept["count"] == 1
    assert swept["requeued"][0]["status"] == TicketStatus.READY.value
    assert context["ticket"]["status"] == TicketStatus.READY.value
    assert context["ticket"]["version"] == claim["ticket"]["version"] + 1


def test_scheduler_requeues_an_expired_qa_lease_to_qa_ready(
    service, store, actors, seeded
) -> None:
    ticket = seeded["ticket"]
    development = service.claim_ticket(
        actors["developer"], ticket["ticket_id"], ticket["version"], 900
    )
    candidate = service.submit_candidate(
        actors["developer"],
        ticket["ticket_id"],
        development["lease"]["lease_id"],
        development["ticket"]["version"],
        "a" * 40,
        "b" * 40,
        "hoh/project-1/DEV-142/dev-01",
        ["Candidate is ready for QA."],
        context_snapshot_id=service.get_ticket_context(actors["developer"], ticket["ticket_id"])["context_snapshot_id"],
    )
    qa = service.claim_qa(
        actors["qa"], ticket["ticket_id"], candidate["ticket"]["version"], 900
    )
    _expire(store, qa["lease"]["lease_id"])

    swept = service.sweep_expired_leases(_scheduler())
    context = service.get_ticket_context(actors["qa"], ticket["ticket_id"])

    assert swept["requeued"][0]["status"] == TicketStatus.QA_READY.value
    assert context["ticket"]["status"] == TicketStatus.QA_READY.value


def test_only_the_internal_admin_scheduler_can_sweep_leases(service, actors) -> None:
    try:
        service.sweep_expired_leases(actors["developer"])
    except AuthorizationError:
        pass
    else:
        raise AssertionError("Developer must not sweep every agent lease")


def test_scheduler_refuses_to_start_without_a_database_path(monkeypatch, tmp_path) -> None:
    import pytest
    from hoh_control_plane import scheduler

    monkeypatch.delenv("HOH_DATABASE_URL", raising=False)
    monkeypatch.delenv("HOH_DATABASE_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="no default"):
        scheduler.database_path_from_environment()
    # Nothing was created relative to the start directory.
    assert not (tmp_path / "data").exists()


def test_scheduler_refuses_a_relative_database_path(monkeypatch) -> None:
    import pytest
    from hoh_control_plane import scheduler

    monkeypatch.delenv("HOH_DATABASE_URL", raising=False)
    monkeypatch.setenv("HOH_DATABASE_PATH", "data/control-plane.db")
    with pytest.raises(SystemExit, match="absolute"):
        scheduler.database_path_from_environment()


def test_scheduler_uses_an_absolute_path_or_the_database_url(monkeypatch, tmp_path) -> None:
    from hoh_control_plane import scheduler

    monkeypatch.delenv("HOH_DATABASE_URL", raising=False)
    monkeypatch.setenv("HOH_DATABASE_PATH", str(tmp_path / "control-plane.db"))
    assert scheduler.database_path_from_environment() == tmp_path / "control-plane.db"
    monkeypatch.setenv("HOH_DATABASE_URL", "postgresql://example.invalid/hoh")
    assert scheduler.database_path_from_environment() is None


def test_one_failed_sweep_does_not_end_the_scheduler(monkeypatch, tmp_path, capsys) -> None:
    import sys

    import pytest
    from hoh_control_plane import scheduler
    from hoh_control_plane.store import ContentionError

    monkeypatch.delenv("HOH_DATABASE_URL", raising=False)
    monkeypatch.setenv("HOH_DATABASE_PATH", str(tmp_path / "control-plane.db"))
    monkeypatch.setattr(sys, "argv", ["hoh-scheduler", "--interval-seconds", "5"])
    passes = []

    def sweep(_path):
        passes.append(_path)
        if len(passes) == 1:
            raise ContentionError("database is locked at postgresql://user:secret@host")
        return {"count": 0, "repositories": {"candidates_already_on_main": [],
                "stuck_merge_jobs": [], "failed": []},
                "dispatch": {"offered_count": 0}, "agent_wakeups": {"delivered": []}}

    class Stop(Exception):
        pass

    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise Stop

    monkeypatch.setattr(scheduler, "sweep_once", sweep)
    monkeypatch.setattr(scheduler.time, "sleep", sleep)
    with pytest.raises(Stop):
        scheduler.main()

    assert len(passes) == 2
    output = capsys.readouterr().out
    assert "Sweep failed (ContentionError); retrying in 5 s." in output
    # Only the error type is printed: a message can carry a DSN.
    assert "secret" not in output


def test_a_failed_single_sweep_exits_nonzero(monkeypatch, tmp_path) -> None:
    import sys

    import pytest
    from hoh_control_plane import scheduler

    monkeypatch.delenv("HOH_DATABASE_URL", raising=False)
    monkeypatch.setenv("HOH_DATABASE_PATH", str(tmp_path / "control-plane.db"))
    monkeypatch.setattr(sys, "argv", ["hoh-scheduler", "--once"])

    def sweep(_path):
        raise RuntimeError("boom")

    monkeypatch.setattr(scheduler, "sweep_once", sweep)
    with pytest.raises(SystemExit) as stopped:
        scheduler.main()
    assert stopped.value.code == 1
