"""Audit payloads used as authorization evidence must match exactly (C7)."""

from __future__ import annotations

import json
import sqlite3

from hoh_control_plane.service._common import _audit_event_has_field


def _connection(*events: tuple[str, str, object]) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE audit_events (aggregate_id TEXT, event_type TEXT, payload TEXT)"
    )
    for aggregate_id, event_type, payload in events:
        connection.execute(
            "INSERT INTO audit_events VALUES (?, ?, ?)",
            (aggregate_id, event_type, payload if isinstance(payload, str) else json.dumps(payload)),
        )
    return connection


def test_counter_id_prefix_does_not_match_longer_id() -> None:
    connection = _connection(
        ("ticket_1", "ticket.verified", {"candidate_id": "candidate_10000", "verdict": "passed"}),
        ("ticket_1", "qa.infrastructure_blocked", {"test_run_id": "testrun_10000"}),
    )
    assert not _audit_event_has_field(
        connection, "ticket_1", "ticket.verified", "candidate_id", "candidate_1000"
    )
    assert not _audit_event_has_field(
        connection, "ticket_1", "qa.infrastructure_blocked", "test_run_id", "testrun_1000"
    )
    assert _audit_event_has_field(
        connection, "ticket_1", "ticket.verified", "candidate_id", "candidate_10000"
    )
    assert _audit_event_has_field(
        connection, "ticket_1", "qa.infrastructure_blocked", "test_run_id", "testrun_10000"
    )


def test_reference_in_another_field_or_text_does_not_match() -> None:
    connection = _connection(
        ("ticket_1", "qa.infrastructure_blocked",
         {"test_run_id": "testrun_2", "reason": "retry of testrun_1000"}),
        ("ticket_1", "ticket.verified", "not json candidate_1000"),
        ("ticket_2", "ticket.verified", {"candidate_id": "candidate_1000"}),
        ("ticket_1", "ticket.rework_requested", {"candidate_id": "candidate_1000"}),
    )
    assert not _audit_event_has_field(
        connection, "ticket_1", "qa.infrastructure_blocked", "test_run_id", "testrun_1000"
    )
    assert not _audit_event_has_field(
        connection, "ticket_1", "ticket.verified", "candidate_id", "candidate_1000"
    )
