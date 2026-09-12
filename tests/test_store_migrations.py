"""Regressions for the persisted-schema upgrade path.

These model the exact legacy-to-current upgrade a real control-plane file takes,
rather than asserting against a freshly created database: the defect these cover
only exists on a database that was created before Integrator outcomes became a
planning source and is later opened by current code.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hoh_control_plane.domain import Actor, Role
from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store import SCHEMA, SchemaMigrationError, SQLiteStore

# The planner_jobs CHECK as it stands now, and as it stood before an Integrator
# conflict could open a planning job.  Substituting one for the other reproduces
# the legacy file shape from the shipped schema, so this cannot silently drift
# into testing a schema the project no longer has -- legacy_schema() fails loudly
# if the current text is ever reformatted.
_CURRENT_JOB_TYPE_CHECK = (
    "job_type TEXT NOT NULL CHECK (\n"
    "        job_type IN ('qa_failure', 'developer_blocker', 'integration_conflict')\n"
    "    ),"
)
_LEGACY_JOB_TYPE_CHECK = (
    "job_type TEXT NOT NULL CHECK (job_type IN ('qa_failure', 'developer_blocker')),"
)


def legacy_schema() -> str:
    if _CURRENT_JOB_TYPE_CHECK not in SCHEMA:
        raise AssertionError(
            "The planner_jobs job_type CHECK no longer matches the text this "
            "regression narrows; update _CURRENT_JOB_TYPE_CHECK so the legacy "
            "upgrade is still actually exercised."
        )
    return SCHEMA.replace(_CURRENT_JOB_TYPE_CHECK, _LEGACY_JOB_TYPE_CHECK)


def write_legacy_database(path: Path) -> None:
    """Create a database exactly as the pre-widening schema would have."""
    connection = sqlite3.connect(path)
    try:
        connection.executescript(legacy_schema())
        connection.commit()
    finally:
        connection.close()


def legacy_service(path: Path) -> ControlPlaneService:
    """A service speaking to a legacy database that has not yet been upgraded."""
    write_legacy_database(path)
    return ControlPlaneService(SQLiteStore(path))


def seed_project(service: ControlPlaneService, planner: Actor) -> dict[str, object]:
    project = service.create_project(
        planner,
        project_id="project-1",
        name="Checkout Platform",
        description="Reliable checkout",
        default_branch="main",
    )
    feature = service.create_feature(
        planner,
        project_id=project["project_id"],
        feature_id="payment-retry",
        title="Payment retry",
        objective="Let customers retry failed payments safely",
    )
    ticket = service.create_ticket(
        planner,
        feature_id=feature["feature_id"],
        ticket_id="DEV-142",
        title="Repair checkout retry behavior",
        objective="Retry without creating duplicate orders",
        required_role=Role.DEVELOPER,
        acceptance_criteria=[
            "A failed payment can be retried",
            "A successful retry creates exactly one order",
        ],
        preservation_constraints=["Existing successful checkout remains valid"],
        file_scope=["src/checkout/**", "tests/checkout/**"],
        ready=True,
    )
    return {"project": project, "feature": feature, "ticket": ticket}


def record_planning_history(
    service: ControlPlaneService, actors: dict[str, Actor], ticket: dict[str, object]
) -> dict[str, object]:
    """Real planning history: a blocker, its Planner job and its wake request."""
    claim = service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)
    blocked = service.report_blocker(
        actors["developer"],
        ticket["ticket_id"],
        claim["lease"]["lease_id"],
        claim["ticket"]["version"],
        "The upstream payment contract is undecided.",
    )
    interventions = service.list_planner_interventions(actors["planner"], "project-1")
    assert len(interventions) == 1, "a blocker must raise exactly one wake request"
    return {**blocked, "intervention": interventions[0]}


def damage_with_the_shipped_migration(path: Path) -> None:
    """Reproduce a database upgraded by the code that shipped before this repair.

    The migration body itself is called unchanged; what shipped ran it under
    SQLite's default legacy_alter_table=OFF, so renaming planner_jobs out of the
    way rewrote every *other* table's foreign key to aim at the temporary table
    the same migration then dropped.  Driving the real migration keeps this from
    drifting away from the damage that actually exists on disk.
    """
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA legacy_alter_table = OFF")
        connection.execute("BEGIN IMMEDIATE")
        SQLiteStore._migrate_planner_job_types(connection)
        connection.commit()
    finally:
        connection.close()


def foreign_key_targets(path: Path, table: str) -> dict[str, str]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return {
            row["from"]: row["table"]
            for row in connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        }
    finally:
        connection.close()


def table_names(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        connection.close()


@pytest.fixture
def upgraded(tmp_path: Path, actors: dict[str, Actor]) -> dict[str, object]:
    """A legacy database carrying real planning history, then upgraded in place."""
    path = tmp_path / "legacy-control-plane.db"
    service = legacy_service(path)
    seeded = seed_project(service, actors["planner"])
    blocked = record_planning_history(service, actors, seeded["ticket"])
    with service.store.connection() as connection:
        before = {
            "jobs": connection.execute(
                "SELECT * FROM planner_jobs ORDER BY planner_job_id"
            ).fetchall(),
            "interventions": connection.execute(
                "SELECT * FROM planner_interventions ORDER BY intervention_id"
            ).fetchall(),
            "audit": connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0],
        }

    SQLiteStore(path).initialize()

    return {"path": path, "seeded": seeded, "blocked": blocked, "before": before}


def test_upgrading_a_legacy_database_keeps_intervention_foreign_keys_on_the_live_table(
    upgraded,
) -> None:
    """AC1: never a renamed or dropped temporary table."""
    path = upgraded["path"]

    assert foreign_key_targets(path, "planner_interventions")["planner_job_id"] == "planner_jobs"
    # The same rename damaged the lease table alongside the intervention table,
    # so a Planner could not claim the job it was woken for either.
    assert foreign_key_targets(path, "planner_job_leases")["planner_job_id"] == "planner_jobs"
    assert not any(name.endswith("_legacy") for name in table_names(path))
    assert not any(name.endswith("__rebuild") for name in table_names(path))


def test_upgrading_preserves_planning_rows_delivery_state_and_audit_history(upgraded) -> None:
    """AC2: nothing is discarded, and foreign_key_check is clean afterwards."""
    before = upgraded["before"]
    connection = sqlite3.connect(upgraded["path"])
    connection.row_factory = sqlite3.Row
    try:
        after_jobs = connection.execute(
            "SELECT * FROM planner_jobs ORDER BY planner_job_id"
        ).fetchall()
        after_interventions = connection.execute(
            "SELECT * FROM planner_interventions ORDER BY intervention_id"
        ).fetchall()
        after_audit = connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()

    assert [dict(row) for row in after_jobs] == [dict(row) for row in before["jobs"]]
    assert [dict(row) for row in after_interventions] == [
        dict(row) for row in before["interventions"]
    ]
    assert after_interventions, "the fixture must carry a delivery record to preserve"
    assert after_audit == before["audit"]
    assert violations == []


def test_upgrading_leaves_the_intervention_table_writable(upgraded) -> None:
    """The damage was only visible on the next write, so prove a write lands."""
    service = ControlPlaneService(SQLiteStore(upgraded["path"]))
    intervention = upgraded["blocked"]["intervention"]

    with service.store.connection(immediate=True) as connection:
        connection.execute(
            "UPDATE planner_interventions SET delivery_attempts = delivery_attempts + 1 "
            "WHERE intervention_id = ?",
            (intervention["intervention_id"],),
        )
        row = connection.execute(
            "SELECT delivery_attempts FROM planner_interventions WHERE intervention_id = ?",
            (intervention["intervention_id"],),
        ).fetchone()

    assert row["delivery_attempts"] == intervention["delivery_attempts"] + 1


def test_upgrading_still_widens_the_planner_job_kinds(upgraded) -> None:
    """The repair must not cost the widening the migration existed to deliver."""
    connection = sqlite3.connect(upgraded["path"])
    try:
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'planner_jobs'"
        ).fetchone()[0]
    finally:
        connection.close()

    assert "integration_conflict" in definition


def test_a_database_already_damaged_by_the_shipped_migration_is_repaired(
    tmp_path: Path, actors: dict[str, Actor]
) -> None:
    """AC1/AC2 for files that already took the damage before this fix existed."""
    path = tmp_path / "damaged-control-plane.db"
    service = legacy_service(path)
    seeded = seed_project(service, actors["planner"])
    blocked = record_planning_history(service, actors, seeded["ticket"])
    damage_with_the_shipped_migration(path)
    assert (
        foreign_key_targets(path, "planner_interventions")["planner_job_id"]
        == "planner_jobs_legacy"
    ), "the fixture must actually reproduce the shipped damage"

    SQLiteStore(path).initialize()

    assert foreign_key_targets(path, "planner_interventions")["planner_job_id"] == "planner_jobs"
    assert foreign_key_targets(path, "planner_job_leases")["planner_job_id"] == "planner_jobs"
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT * FROM planner_interventions").fetchall()
        indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'planner_interventions' AND sql IS NOT NULL"
            ).fetchall()
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
    assert [row["intervention_id"] for row in rows] == [
        blocked["intervention"]["intervention_id"]
    ]
    # A rebuilt table keeps the constraints that make the row set meaningful:
    # without these, a second open request per job could be recorded.
    assert indexes == {
        "planner_interventions_one_open_per_job",
        "planner_interventions_project_status",
    }


def test_repairing_a_damaged_database_keeps_the_one_open_request_per_job_rule(
    tmp_path: Path, actors: dict[str, Actor]
) -> None:
    """The rebuilt unique index must still refuse a second live request."""
    path = tmp_path / "damaged-index.db"
    service = legacy_service(path)
    seeded = seed_project(service, actors["planner"])
    blocked = record_planning_history(service, actors, seeded["ticket"])
    damage_with_the_shipped_migration(path)
    SQLiteStore(path).initialize()

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        existing = connection.execute(
            "SELECT * FROM planner_interventions WHERE intervention_id = ?",
            (blocked["intervention"]["intervention_id"],),
        ).fetchone()
        duplicate = dict(existing)
        duplicate["intervention_id"] = "intv_duplicate"
        columns = ", ".join(duplicate)
        placeholders = ", ".join("?" for _ in duplicate)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO planner_interventions ({columns}) VALUES ({placeholders})",
                tuple(duplicate.values()),
            )
    finally:
        connection.close()


def test_the_upgrade_is_idempotent(upgraded) -> None:
    """Opening the upgraded file again must be a no-op, not a second rebuild."""
    path = upgraded["path"]
    connection = sqlite3.connect(path)
    try:
        before = connection.execute(
            "SELECT name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
    finally:
        connection.close()

    SQLiteStore(path).initialize()
    SQLiteStore(path).initialize()

    connection = sqlite3.connect(path)
    try:
        after = connection.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    finally:
        connection.close()
    assert after == before


def test_a_fresh_database_is_never_rebuilt(tmp_path: Path) -> None:
    """A file created by the current schema has nothing to repair."""
    path = tmp_path / "fresh.db"
    SQLiteStore(path).initialize()

    assert foreign_key_targets(path, "planner_interventions")["planner_job_id"] == "planner_jobs"
    assert not any(name.endswith(("_legacy", "__rebuild")) for name in table_names(path))


def test_an_upgrade_refuses_rather_than_committing_an_unrepaired_reference(
    tmp_path: Path,
) -> None:
    """A reference shape the repair cannot rewrite must stop the upgrade.

    Committing here would leave a table still aimed at a dropped temporary while
    reporting success -- exactly the silent half-migration this ticket exists to
    remove.
    """
    path = tmp_path / "unrepairable.db"
    write_legacy_database(path)
    connection = sqlite3.connect(path)
    try:
        # A bracket-quoted reference is valid SQLite that the textual rewrite does
        # not recognize, standing in for any reference shape it cannot repair.
        connection.executescript(
            """
            CREATE TABLE planner_job_notes (
                note_id TEXT PRIMARY KEY,
                planner_job_id TEXT NOT NULL REFERENCES [planner_jobs_legacy](planner_job_id)
            );
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SchemaMigrationError) as excinfo:
        SQLiteStore(path).initialize()

    assert "planner_job_notes" in str(excinfo.value)
    # The refusal must roll back, not leave the database half-upgraded.
    assert "_rebuild" not in " ".join(table_names(path))


def test_an_upgrade_refuses_when_a_repaired_row_no_longer_resolves(
    tmp_path: Path, actors: dict[str, Actor]
) -> None:
    """A rebuilt table whose rows point at nothing is not quietly committed."""
    path = tmp_path / "orphaned.db"
    service = legacy_service(path)
    seeded = seed_project(service, actors["planner"])
    record_planning_history(service, actors, seeded["ticket"])
    damage_with_the_shipped_migration(path)
    connection = sqlite3.connect(path)
    try:
        # Only possible because the damaged reference is unenforceable: the
        # intervention row is left describing a Planner job that is gone.
        connection.execute("DELETE FROM planner_job_leases")
        connection.execute("DELETE FROM planner_jobs")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SchemaMigrationError) as excinfo:
        SQLiteStore(path).initialize()

    assert "planner_interventions" in str(excinfo.value)


def test_upgrading_a_database_written_before_event_tracing_succeeds(tmp_path) -> None:
    """An index on a column a migration has not added yet breaks the whole upgrade.

    Found against a real control-plane file, not a fixture: the schema script runs
    before the migrations, so an index declared beside the CREATE TABLE references
    a column that does not exist on any database predating it. Every test until
    now used a fresh database, where the column is there from the start.
    """
    import sqlite3

    database = tmp_path / "before-tracing.db"
    older = SCHEMA.replace("    correlation_id TEXT,\n    causation_id TEXT,\n", "")
    assert "correlation_id" not in older, "the fixture must really predate the columns"
    raw = sqlite3.connect(database)
    raw.executescript(older)
    raw.execute(
        """
        INSERT INTO audit_events
            (event_id, event_type, aggregate_type, aggregate_id, aggregate_version,
             project_id, actor_id, actor_role, payload, occurred_at)
        VALUES ('event_old', 'ticket.created', 'ticket', 'DEV-1', 1, 'p', 'a', 'planner',
                '{}', '2026-01-01T00:00:00Z')
        """
    )
    raw.commit()
    raw.close()

    SQLiteStore(database).initialize()

    store = SQLiteStore(database)
    with store.connection() as connection:
        columns = {
            row["name"] for row in connection.execute('PRAGMA table_info("audit_events")')
        }
        indexes = [row["name"] for row in connection.execute('PRAGMA index_list("audit_events")')]
        surviving = connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_id = 'event_old'"
        ).fetchone()[0]

    assert {"correlation_id", "causation_id"} <= columns
    assert any("correlation" in name for name in indexes), "the index arrives with the columns"
    assert surviving == 1, "an event written before tracing existed survives untouched"


# --- one list of additive columns, applied by whichever backend is in use -------


def test_every_additive_column_is_in_the_shared_schema(tmp_path) -> None:
    """A column added to one and forgotten in the other is the failure to stop."""
    from hoh_control_plane.store import ADDITIVE_COLUMNS, SQLiteStore

    path = tmp_path / "fresh.db"
    SQLiteStore(path).initialize()

    connection = sqlite3.connect(path)
    try:
        for table, column, _declaration in ADDITIVE_COLUMNS:
            present = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            assert present, f"{table} is not in the schema at all"
            assert column in present, (
                f"{table}.{column} is migrated onto old databases but missing from "
                "SCHEMA, so a new database would never get it"
            )
    finally:
        connection.close()


def test_a_database_missing_every_added_column_upgrades_cleanly(tmp_path) -> None:
    """The whole list at once, which is what a long-dormant deployment looks like."""
    from hoh_control_plane.store import ADDITIVE_COLUMNS, SQLiteStore

    path = tmp_path / "old.db"
    SQLiteStore(path).initialize()
    connection = sqlite3.connect(path)
    try:
        # The migrated index is built on one of these columns, so it has to go
        # first -- which is the same order the real upgrade path implies: the
        # index is created by the migration, after the column it indexes.
        connection.execute("DROP INDEX IF EXISTS audit_events_correlation")
        for table, column, _declaration in ADDITIVE_COLUMNS:
            connection.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        connection.commit()
    finally:
        connection.close()

    SQLiteStore(path).initialize()

    connection = sqlite3.connect(path)
    try:
        for table, column, _declaration in ADDITIVE_COLUMNS:
            present = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            assert column in present, f"{table}.{column} was not restored"
    finally:
        connection.close()


def test_migrating_twice_changes_nothing(tmp_path) -> None:
    from hoh_control_plane.store import SQLiteStore

    path = tmp_path / "twice.db"
    SQLiteStore(path).initialize()

    SQLiteStore(path).initialize()
    SQLiteStore(path).initialize()

    connection = sqlite3.connect(path)
    try:
        columns = {row[1] for row in connection.execute('PRAGMA table_info("agent_presence")')}
    finally:
        connection.close()
    assert "endpoint" in columns, "still there, and no duplicate-column error was raised"
