"""Identifiers are short counters, and every citation a packet makes resolves.

A 16-hex identifier cost about ten tokens each time a packet, decision or tool
call quoted it, and was mistyped often enough by a model copying it back that
"unknown id" retries were a visible share of failed calls. A prefix plus a
per-prefix counter is one or two tokens, reads as an ordinal, and is unique for
the same reason the row it names is: it is allocated inside that row's write.
"""
from __future__ import annotations

import re

import pytest

from hoh_control_plane.domain import (
    AuthorizationError,
    ContextStaleError,
    NotFoundError,
    Role,
    ValidationError,
)
from hoh_control_plane.service._common import _id

SHORT_ID = re.compile(r"^[a-z_]+_\d{4,}$")


def test_identifiers_are_a_prefix_and_a_per_prefix_counter(service) -> None:
    with service.store.connection(immediate=True) as connection:
        first = _id(connection, "cand")
        second = _id(connection, "cand")
        other = _id(connection, "lease")
    with service.store.connection(immediate=True) as connection:
        third = _id(connection, "cand")

    assert (first, second, third) == ("cand_0001", "cand_0002", "cand_0003")
    assert other == "lease_0001", "each prefix counts on its own"


def test_the_counter_widens_past_four_digits_rather_than_wrapping(service) -> None:
    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "INSERT INTO id_sequences (prefix, next_value) VALUES ('event', 9999)"
        )
        widened = _id(connection, "event")
    assert widened == "event_10000"


def test_every_identifier_a_packet_cites_is_short(service, actors, seeded) -> None:
    ticket = seeded["ticket"]
    claim = service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)
    decision = service.append_planner_context(
        actors["planner"],
        entry_type="planner_decision",
        content="Keep the retry idempotent.",
        ticket_id=ticket["ticket_id"],
    )
    packet = service.get_ticket_context(actors["developer"], ticket["ticket_id"])

    assert SHORT_ID.match(claim["lease"]["lease_id"])
    assert SHORT_ID.match(decision["entry_id"])
    assert SHORT_ID.match(packet["context_snapshot_id"])
    assert packet["context_snapshot_id"].startswith("ctxsnap_")


def test_unchanged_context_keeps_one_short_snapshot_and_changed_context_gets_a_new_one(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]
    developer = actors["developer"]

    first = service.get_ticket_context(developer, ticket["ticket_id"])["context_snapshot_id"]
    again = service.get_ticket_context(developer, ticket["ticket_id"])["context_snapshot_id"]
    service.append_planner_context(
        actors["planner"],
        entry_type="planner_decision",
        content="Scope changed: only src/checkout/retry.py.",
        ticket_id=ticket["ticket_id"],
    )
    changed = service.get_ticket_context(developer, ticket["ticket_id"])["context_snapshot_id"]

    assert first == again
    assert changed != first
    with pytest.raises(ContextStaleError):
        service.claim_ticket(
            developer, ticket["ticket_id"], ticket["version"], 900,
            context_snapshot_id=first,
        )
    claim = service.claim_ticket(
        developer, ticket["ticket_id"], ticket["version"], 900,
        context_snapshot_id=changed,
    )
    assert claim["lease"]["ticket_id"] == ticket["ticket_id"]


def test_a_feature_packet_carries_a_real_snapshot_name_not_a_placeholder(
    service, actors, seeded
) -> None:
    packet = service.get_feature_context(actors["planner"], "payment-retry")
    assert SHORT_ID.match(packet["context_snapshot_id"])
    assert packet["context_snapshot_id"] != "ctxsnap_0000"


# ---------------------------------------------------------------------------
# A packet cites what it compacted; the citation must resolve for the role.


def _long_decision(service, actors, ticket_id: str) -> dict:
    return service.append_planner_context(
        actors["planner"],
        entry_type="planner_decision",
        content="Decision: " + "retry must be idempotent across restarts; " * 20,
        ticket_id=ticket_id,
        source_ref="docs/spec.md#retry",
    )


def test_a_checkpointed_entry_resolves_to_its_full_text_for_a_role_that_sees_it(
    service, actors, seeded
) -> None:
    ticket = seeded["ticket"]
    decision = _long_decision(service, actors, ticket["ticket_id"])
    packet = service.get_ticket_context(actors["developer"], ticket["ticket_id"])
    cited = [
        reference
        for section in packet["omitted_sections"]
        if section["section"] == "context_entries.content"
        for reference in section["references"]
    ]
    assert f"ctx:entry:{decision['entry_id']}" in cited

    resolved = service.resolve_context_reference(
        actors["developer"], f"ctx:entry:{decision['entry_id']}"
    )

    assert resolved["entry"]["content"] == decision["content"]
    assert resolved["entry"]["source_ref"] == "docs/spec.md#retry"
    assert resolved["reference"] == f"ctx:entry:{decision['entry_id']}"


def test_resolution_follows_the_packet_visibility_rules(service, actors, seeded) -> None:
    ticket = seeded["ticket"]
    claim = service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)
    blocked = service.report_blocker(
        actors["developer"], ticket["ticket_id"], claim["lease"]["lease_id"],
        claim["ticket"]["version"], "Waiting on the payment sandbox credentials.",
    )
    entry_id = blocked["context_entry"]["entry_id"] if "context_entry" in blocked else None
    if entry_id is None:
        with service.store.connection() as connection:
            entry_id = connection.execute(
                "SELECT entry_id FROM context_entries WHERE ticket_id = ? AND entry_type = 'developer_blocker'",
                (ticket["ticket_id"],),
            ).fetchone()["entry_id"]

    # A Developer blocker is in the Planner's and Developer's packet vocabulary...
    assert service.resolve_context_reference(actors["planner"], f"ctx:entry:{entry_id}")
    # ...but QA never sees developer_blocker entries, in a packet or by reference.
    with pytest.raises(AuthorizationError):
        service.resolve_context_reference(actors["qa"], f"ctx:entry:{entry_id}")
    # An execution role resolves the history its packet cites, but only the
    # entry types its packet may carry.
    qa_history = service.resolve_context_reference(
        actors["qa"], f"ctx:ticket:{ticket['ticket_id']}:v1:history"
    )
    assert all(entry["entry_id"] != entry_id for entry in qa_history["entries"])
    history = service.resolve_context_reference(
        actors["planner"], f"ctx:ticket:{ticket['ticket_id']}:v1:history"
    )
    assert any(entry["entry_id"] == entry_id for entry in history["entries"])


def test_unknown_or_malformed_references_are_refused_not_guessed(service, actors) -> None:
    with pytest.raises(ValidationError):
        service.resolve_context_reference(actors["developer"], "ticket:DEV-142")
    with pytest.raises(ValidationError):
        service.resolve_context_reference(actors["developer"], "ctx:candidate:cand_0001:secrets")
    with pytest.raises(NotFoundError):
        service.resolve_context_reference(actors["developer"], "ctx:entry:ctx_9999")
    with pytest.raises(NotFoundError):
        service.resolve_context_reference(actors["qa"], "ctx:finding:finding_9999")


# ---------------------------------------------------------------------------
# Discovery rows carry identity and routing; the Planner row drops the one call
# it could only ever recommend.


def test_planner_discovery_rows_omit_the_constant_recommended_call(
    service, actors, seeded
) -> None:
    planner = service.discover_work(actors["planner"], "project-1", detail=True)
    developer = service.discover_work(actors["developer"], "project-1")

    planner_rows = [t for f in planner["features"] for t in f["tickets"]]
    developer_rows = [t for f in developer["features"] for t in f["tickets"]]
    assert planner_rows and developer_rows
    assert all("recommended_calls" not in row for row in planner_rows)
    assert all(row["recommended_calls"][0]["tool"] == "ticket.get_context" for row in developer_rows)
    for feature in planner["features"]:
        assert "pending_ticket_refs" not in feature
        assert feature["pending_ticket_count"] == len(feature["tickets"])
