"""Ticket lifecycle writes are guarded in SQL, not only by the global write lock.

Every service check of a ticket's version and status is followed by an UPDATE
that repeats both as predicates (``_transition_ticket``). Under SQLite's single
writer those predicates never refuse anything; they exist so that a writer
outside the lock, or a move to PostgreSQL row locks, cannot silently overwrite a
newer ticket state. These tests interleave the "other writer" by hand.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from hoh_control_plane.domain import Actor, ConflictError, Role, TicketStatus
from hoh_control_plane.service._common import _transition_ticket
from hoh_control_plane.store import SCHEMA, SQLiteStore, StoreIntegrityError
from hoh_control_plane.store_schema import TICKET_STATUS_CHECK, TICKET_STATUSES

SOURCE = Path(__file__).resolve().parents[1] / "src" / "hoh_control_plane"
PLANNER = Actor("planner-01", Role.PLANNER, frozenset({"project-1"}))


def _ticket_row(service, ticket_id: str) -> dict:
    with service.store.connection() as connection:
        row = connection.execute(
            "SELECT status, version, blocked_reason FROM tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
    return dict(row)


def test_a_stale_version_is_refused_by_the_update_itself(service, seeded) -> None:
    ticket = seeded["ticket"]
    with pytest.raises(ConflictError) as raised:
        with service.store.connection(immediate=True) as connection:
            # Another writer moved the ticket after this one read version 1.
            connection.execute(
                "UPDATE tickets SET version = version + 1 WHERE ticket_id = ?", (ticket["ticket_id"],)
            )
            _transition_ticket(
                connection, ticket["ticket_id"], actor=PLANNER, expected_version=ticket["version"],
                from_statuses=(TicketStatus.READY,),
                changes={"status": TicketStatus.BLOCKED, "blocked_reason": "stale"},
            )
    assert raised.value.details["reason"] == "ticket_transition_conflict"
    assert raised.value.details["current_version"] == ticket["version"] + 1
    # The whole transaction rolled back: neither write landed.
    assert _ticket_row(service, ticket["ticket_id"]) == {
        "status": "READY", "version": ticket["version"], "blocked_reason": None,
    }


def test_a_status_the_caller_did_not_check_is_refused(service, seeded) -> None:
    ticket = seeded["ticket"]
    with pytest.raises(ConflictError):
        with service.store.connection(immediate=True) as connection:
            _transition_ticket(
                connection, ticket["ticket_id"], actor=PLANNER, expected_version=ticket["version"],
                from_statuses=(TicketStatus.BACKLOG, TicketStatus.BLOCKED),
                changes={"status": TicketStatus.READY},
            )
    assert _ticket_row(service, ticket["ticket_id"])["version"] == ticket["version"]


def test_a_matching_transition_bumps_the_version_once(service, seeded) -> None:
    ticket = seeded["ticket"]
    with service.store.connection(immediate=True) as connection:
        new_version = _transition_ticket(
            connection, ticket["ticket_id"], actor=PLANNER, expected_version=ticket["version"],
            from_statuses=(TicketStatus.READY,),
            changes={"status": TicketStatus.BLOCKED, "blocked_reason": "waiting"},
        )
    assert new_version == ticket["version"] + 1
    assert _ticket_row(service, ticket["ticket_id"]) == {
        "status": "BLOCKED", "version": new_version, "blocked_reason": "waiting",
    }


def test_only_lifecycle_columns_can_be_written(service, seeded) -> None:
    with service.store.connection(immediate=True) as connection:
        with pytest.raises(ValueError):
            _transition_ticket(
                connection, seeded["ticket"]["ticket_id"], actor=PLANNER, expected_version=1,
                from_statuses=(TicketStatus.READY,), changes={"version": 99},
            )
        with pytest.raises(ValueError):
            _transition_ticket(
                connection, seeded["ticket"]["ticket_id"], actor=PLANNER, expected_version=1,
                from_statuses=(), changes={"status": TicketStatus.BLOCKED},
            )


def test_a_service_write_interleaved_with_another_writer_is_refused(
    service, seeded, actors, monkeypatch
) -> None:
    """set_ticket_ready checked version and status in Python; the SQL still refuses."""
    backlog = service.create_ticket(
        actors["planner"], feature_id=seeded["feature"]["feature_id"], ticket_id="DEV-143",
        title="Backlog work", objective="Not ready yet", required_role=Role.DEVELOPER,
        acceptance_criteria=["It works"], preservation_constraints=[],
        file_scope=["src/other/**"], ready=False,
    )

    original_one = service._one

    def interleaving_one(connection, statement, parameters=()):
        # set_ticket_ready reads the project after its Python checks passed and
        # before it writes: the other writer lands exactly there.
        if "FROM projects" in statement:
            connection.execute(
                "UPDATE tickets SET status = 'CANCELLED', version = version + 1 "
                "WHERE ticket_id = 'DEV-143'"
            )
        return original_one(connection, statement, parameters)

    monkeypatch.setattr(service, "_one", interleaving_one)

    with pytest.raises(ConflictError) as raised:
        service.set_ticket_ready(actors["planner"], "DEV-143", backlog["version"])
    assert raised.value.details["current_status"] == "CANCELLED"
    monkeypatch.undo()
    assert _ticket_row(service, "DEV-143") == {
        "status": "BACKLOG", "version": backlog["version"], "blocked_reason": None,
    }


def test_no_ticket_update_bypasses_the_guarded_transition() -> None:
    """New code must route ticket writes through _transition_ticket."""
    offenders = []
    for path in SOURCE.rglob("*.py"):
        if path.name == "_common.py":
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"UPDATE\s+tickets\b", text):
            offenders.append(str(path.relative_to(SOURCE)))
    assert offenders == []


def test_the_status_check_matches_the_lifecycle() -> None:
    assert TICKET_STATUSES == tuple(status.value for status in TicketStatus)
    assert TICKET_STATUS_CHECK in SCHEMA


def test_a_new_database_refuses_an_unknown_status(service, seeded) -> None:
    with pytest.raises(StoreIntegrityError):
        with service.store.connection(immediate=True) as connection:
            connection.execute(
                "UPDATE tickets SET status = 'PASS' WHERE ticket_id = ?",
                (seeded["ticket"]["ticket_id"],),
            )


def _pre_check_database(path: Path) -> None:
    """A database written before tickets.status had a CHECK."""
    legacy = SCHEMA.replace(" " + TICKET_STATUS_CHECK, "")
    assert TICKET_STATUS_CHECK not in legacy
    connection = sqlite3.connect(path)
    try:
        connection.executescript(legacy)
        connection.commit()
    finally:
        connection.close()


def _tickets_sql(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tickets'"
        ).fetchone()[0]
    finally:
        connection.close()


def test_an_existing_database_gains_the_status_check_without_losing_rows(
    tmp_path, actors
) -> None:
    from hoh_control_plane.service import ControlPlaneService
    from test_store_migrations import seed_project

    path = tmp_path / "pre-check.db"
    _pre_check_database(path)
    service = ControlPlaneService(SQLiteStore(path))
    seeded = seed_project(service, actors["planner"])
    connection = sqlite3.connect(path)
    try:
        before_tickets = connection.execute("SELECT * FROM tickets ORDER BY ticket_id").fetchall()
        before_indexes = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'tickets' ORDER BY name"
        ).fetchall()
        before_events = connection.execute("SELECT count(*) FROM audit_events").fetchone()[0]
    finally:
        connection.close()
    assert TICKET_STATUS_CHECK not in _tickets_sql(path)

    SQLiteStore(path).initialize()

    assert TICKET_STATUS_CHECK in _tickets_sql(path)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT * FROM tickets ORDER BY ticket_id").fetchall() == before_tickets
        assert connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'tickets' ORDER BY name"
        ).fetchall() == before_indexes
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone()[0] == before_events
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert not any(
            name.endswith("__rebuild")
            for (name,) in connection.execute("SELECT name FROM sqlite_master")
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE tickets SET status = 'PASS' WHERE ticket_id = ?",
                (seeded["ticket"]["ticket_id"],),
            )
    finally:
        connection.close()
    # The service still works on the rebuilt table, child rows included.
    reopened = ControlPlaneService(SQLiteStore(path))
    claimed = reopened.claim_ticket(
        actors["developer"], seeded["ticket"]["ticket_id"], seeded["ticket"]["version"], 900
    )
    blocked = reopened.report_blocker(
        actors["developer"], seeded["ticket"]["ticket_id"], claimed["lease"]["lease_id"],
        claimed["ticket"]["version"], "Waiting on a contract decision.",
    )
    assert blocked["ticket"]["status"] == "BLOCKED"


def test_an_unknown_stored_status_leaves_the_table_unrebuilt(tmp_path, actors) -> None:
    from hoh_control_plane.service import ControlPlaneService
    from test_store_migrations import seed_project

    path = tmp_path / "odd-status.db"
    _pre_check_database(path)
    service = ControlPlaneService(SQLiteStore(path))
    seed_project(service, actors["planner"])
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE tickets SET status = 'ARCHIVED'")
        connection.commit()
    finally:
        connection.close()

    SQLiteStore(path).initialize()  # starts, rather than refusing over old data

    assert TICKET_STATUS_CHECK not in _tickets_sql(path)
