"""The core transaction invariants, run against PostgreSQL (HOH-204).

Section 20 only lets the store move once the lease and queue guarantees hold on
the new backend. These are those guarantees, exercised through the ordinary
service layer — not a second set of assertions written to suit the backend.

Skipped when no server is configured, so a SQLite-only checkout stays green
without PostgreSQL installed. Point HOH_TEST_POSTGRES_DSN at a throwaway database
to run them.
"""
from __future__ import annotations

import os
import uuid

import pytest

from hoh_control_plane.domain import Actor, ConflictError, Role
from hoh_control_plane.service import ControlPlaneService

psycopg = pytest.importorskip("psycopg", reason="PostgreSQL driver is not installed")

DSN = os.getenv("HOH_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set HOH_TEST_POSTGRES_DSN to run the PostgreSQL conformance suite"
)


@pytest.fixture
def pg_service():
    """A service on its own PostgreSQL schema, dropped afterwards."""
    from hoh_control_plane.postgres_store import PostgresStore, translate_schema

    schema = f"hoh_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    store = PostgresStore(DSN)
    original_connect = store._connect

    def scoped():
        connection = original_connect()
        connection.execute(f'SET search_path TO "{schema}"')
        return connection

    store._connect = scoped
    store.initialize()
    try:
        yield ControlPlaneService(store)
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def pg_seeded(pg_service):
    planner = Actor("planner-pg", Role.PLANNER, frozenset({"project-pg"}))
    pg_service.create_project(
        planner, project_id="project-pg", name="PG", description="", default_branch="main"
    )
    pg_service.create_feature(
        planner, project_id="project-pg", feature_id="f-pg", title="F", objective="O"
    )
    ticket = pg_service.create_ticket(
        planner,
        feature_id="f-pg",
        ticket_id="PG-1",
        title="Work on PostgreSQL",
        objective="Prove the guarantees survive the backend change",
        required_role=Role.DEVELOPER,
        acceptance_criteria=["It behaves as it did on SQLite"],
        preservation_constraints=[],
        file_scope=["src/**"],
        ready=True,
    )
    return {"planner": planner, "ticket": ticket}


def _developer(suffix: str = "1") -> Actor:
    return Actor(f"dev-pg-{suffix}", Role.DEVELOPER, frozenset({"project-pg"}))


def test_the_schema_lands_whole_on_postgres(pg_service) -> None:
    with pg_service.store.connection() as connection:
        tables = connection.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = current_schema()"
        ).fetchone()[0]

    assert tables >= 30, "every table the control plane needs exists"


def test_statements_written_in_qmark_style_run_unchanged(pg_service, pg_seeded) -> None:
    """The port did not require rewriting call sites one at a time."""
    with pg_service.store.connection() as connection:
        row = connection.execute(
            "SELECT title FROM tickets WHERE ticket_id = ?", ("PG-1",)
        ).fetchone()

    assert row["title"] == "Work on PostgreSQL"
    assert row[0] == "Work on PostgreSQL", "rows answer to a name and a position"


def test_a_claim_is_exclusive(pg_service, pg_seeded) -> None:
    ticket = pg_seeded["ticket"]
    pg_service.claim_ticket(_developer("1"), ticket["ticket_id"], ticket["version"], 900)

    with pytest.raises(ConflictError):
        pg_service.claim_ticket(
            _developer("2"), ticket["ticket_id"], ticket["version"], 900
        )


def test_a_queue_offer_is_handed_out_once(pg_service, pg_seeded) -> None:
    """Two agents polling the same queue must not both be handed the same work."""
    ticket = pg_seeded["ticket"]
    one, two = _developer("1"), _developer("2")
    for agent in (one, two):
        pg_service.register_agent(agent, skills=["hoh-developer"])

    first = pg_service.get_agent_inbox(one, "project-pg")
    pg_service.claim_ticket(one, ticket["ticket_id"], ticket["version"], 900)
    second = pg_service.get_agent_inbox(two, "project-pg")

    assert ticket["ticket_id"] in {
        entry["ticket"]["ticket_id"] for entry in first.get("offers", [])
    }, "the first poller is offered the work"
    assert ticket["ticket_id"] not in {
        entry["ticket"]["ticket_id"] for entry in second.get("offers", [])
    }, "and once claimed it is not offered again"


def test_a_stale_expected_version_is_rejected(pg_service, pg_seeded) -> None:
    ticket = pg_seeded["ticket"]

    with pytest.raises(ConflictError):
        pg_service.claim_ticket(
            _developer("1"), ticket["ticket_id"], ticket["version"] + 7, 900
        )


def test_an_idempotency_record_replays_rather_than_duplicating(
    pg_service, pg_seeded
) -> None:
    ticket = pg_seeded["ticket"]
    developer = _developer("1")
    key = "pg-claim-retry-key"
    payload = {"ticket_id": ticket["ticket_id"], "expected_version": ticket["version"]}

    first = pg_service.run_idempotent(
        developer, operation="work.claim", idempotency_key=key, payload=payload,
        run=lambda: pg_service.claim_ticket(
            developer, ticket["ticket_id"], ticket["version"], 900
        ),
    )
    replay = pg_service.run_idempotent(
        developer, operation="work.claim", idempotency_key=key, payload=payload,
        run=lambda: pg_service.claim_ticket(
            developer, ticket["ticket_id"], ticket["version"], 900
        ),
    )

    assert replay["lease"]["lease_id"] == first["lease"]["lease_id"]
    with pg_service.store.connection() as connection:
        leases = connection.execute(
            "SELECT COUNT(*) FROM leases WHERE ticket_id = ?", (ticket["ticket_id"],)
        ).fetchone()[0]
    assert leases == 1, "a replay did not create a second lease"


def test_a_constraint_violation_arrives_as_the_control_plane_s_own_error(
    pg_service,
) -> None:
    from hoh_control_plane.store import StoreIntegrityError

    with pytest.raises(StoreIntegrityError) as error:
        with pg_service.store.connection(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO features (feature_id, project_id, title, objective, status,
                                      priority, version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("orphan", "no-such-project", "t", "o", "planned", 50, 1, "now", "now"),
            )

    assert "psycopg" not in str(error.value).lower()
    assert "postgres" not in str(error.value).lower()


def test_the_monotonic_sequence_column_still_orders_events(
    pg_service, pg_seeded
) -> None:
    """AUTOINCREMENT became BIGSERIAL; the ordering it guaranteed must survive."""
    ticket = pg_seeded["ticket"]
    pg_service.claim_ticket(_developer("1"), ticket["ticket_id"], ticket["version"], 900)

    events = pg_service.list_events(pg_seeded["planner"], "project-pg")

    sequences = [event["sequence"] for event in events["events"]]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences), "sequence numbers are unique"


def test_foreign_keys_are_enforced_without_being_switched_on(pg_service) -> None:
    """SQLite needs a pragma per connection; this backend has no such switch."""
    from hoh_control_plane.postgres_store import POSTGRES_DIALECT

    assert POSTGRES_DIALECT.foreign_keys_statement is None
    assert POSTGRES_DIALECT.alters_constraints_in_place is True


def test_sqlite_remains_the_default_and_needs_no_driver() -> None:
    """A local single-host install must not acquire a PostgreSQL dependency."""
    import tomllib
    from pathlib import Path

    manifest = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    required = " ".join(manifest["project"]["dependencies"]).lower()

    assert "psycopg" not in required, "the driver is optional, not required"
