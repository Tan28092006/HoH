"""Upgrades of an existing database: additive columns and indexes shared by both
backends, and the SQLite-only table rebuilds that repair constraints."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from hoh_control_plane.domain import DomainError
from hoh_control_plane.store_schema import TICKET_STATUS_CHECK, TICKET_STATUSES


class SchemaMigrationError(DomainError):
    """An upgrade could not leave the persisted schema in a usable state.

    This is raised instead of committing a half-migrated database, so the fault
    surfaces once at startup with the table that caused it rather than as a raw
    storage error on some unrelated write hours later.
    """

    code = "schema_migration_failed"
    status_code = 500


class SchemaVersionError(SchemaMigrationError):
    """The database was written by a newer HoH than this one.

    Raised before anything is executed against it: older code cannot know which
    columns, constraints or meanings a newer release added, so running it would
    risk writing rows the newer release then misreads.
    """

    code = "schema_version_unsupported"


# The schema this code creates and upgrades to, recorded in schema_meta.
# A database with no record predates versioning and is version 0; the additive
# migrations and rebuilds below take any such database to this version.
# Bump it whenever a release changes the schema, so an older binary started on
# the upgraded file refuses rather than guessing.
#   1 -- first recorded version (after 0.3.30): schema_meta, tickets.status CHECK.
#   2 -- idempotency_records.correlation_id; ticket-scoped reconciliations and
#        the merge shadow partition (repository_reconciliations.scope,
#        frozen_ticket_ids, shadow_*), agent_presence.session_started_at and
#        ticket_usage_reports.
#   3 -- wave 7: candidates.derived_from and candidates.qa_evidence_from (a
#        queued candidate rebased onto a moved main, 7B);
#        tickets.planning_base_stale (7C-1); project_run_guardrails and
#        mainline_checks (7C-2); agent model_family, ticket risk tiers and
#        high-risk approvals (7C-3). Wave 8A adds, still under 3 (every
#        column is additive and nullable, so a wave 7 build reads it safely):
#        qa_test_runs.termination_reason and provenance_status (R1).
SCHEMA_VERSION = 3


def read_schema_version(connection: Any, columns_of: Any) -> int:
    """The version recorded in the database, 0 when it was never recorded."""
    if "value" not in columns_of("schema_meta"):
        return 0
    row = connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError) as error:
        raise SchemaVersionError(
            f"The database records an unreadable schema version {row[0]!r}; "
            "refusing to start rather than guess what wrote it."
        ) from error


def require_supported_schema_version(found: int, location: str) -> None:
    if found > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"Database {location} has schema version {found}, newer than the "
            f"version {SCHEMA_VERSION} this HoH build supports. It was upgraded by a "
            "newer release; start that release (or a newer one) against it, or "
            "point this build at a different database. Nothing was changed.",
            {"database_schema_version": found, "supported_schema_version": SCHEMA_VERSION},
        )


def record_schema_version(connection: Any) -> None:
    connection.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )


# A table rebuilt in place is renamed to this suffix, copied from, and dropped.
# The suffix is also how a foreign key left pointing at one of those dropped
# temporaries is recognized and repaired below.
_MIGRATION_TABLE_SUFFIX = "_legacy"

# The tickets.status column as every earlier schema declared it, followed by the
# comma or newline that ends the declaration. ``\b`` keeps merge_status and the
# like from matching.
_TICKET_STATUS_COLUMN = re.compile(r"(\bstatus\s+TEXT\s+NOT\s+NULL)\s*(?=,|\n|\))", re.IGNORECASE)


# Columns added to tables that already existed by the time they were needed.
#
# Declared once rather than implemented per backend. Every one of these is the
# same operation -- add the column if the table is missing it -- and the only
# thing that differs between SQLite and PostgreSQL is how you ask which columns
# a table has. Written as two implementations, the PostgreSQL copy is the one
# that silently does not get the next entry, and the failure shows up as a
# query against a column that does not exist rather than as a failed upgrade.
#
# Every declaration is valid in both dialects, and every default states what was
# already true of the rows being migrated rather than inventing a new fact.
ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # Quality contracts introduced after the first local releases. Existing
    # projects remain in legacy mode until a Planner explicitly enables the
    # claim/evidence gate; existing tickets therefore migrate without invented
    # test requirements.
    ("projects", "verification_profile", "TEXT NOT NULL DEFAULT '{}'"),
    ("tickets", "validation_contract", "TEXT NOT NULL DEFAULT '[]'"),
    # Skill and protocol compatibility, for presence rows written before it.
    ("agent_presence", "skill_manifest", "TEXT NOT NULL DEFAULT '[]'"),
    ("agent_presence", "execution_capabilities", "TEXT NOT NULL DEFAULT '[]'"),
    ("agent_presence", "approved_execution_capabilities", "TEXT NOT NULL DEFAULT '[]'"),
    ("agent_presence", "capability_approval_refs", "TEXT NOT NULL DEFAULT '[]'"),
    ("agent_presence", "capability_approved_machine_id", "TEXT"),
    ("agent_presence", "capability_approved_by", "TEXT"),
    ("agent_presence", "capability_approved_at", "TEXT"),
    ("agent_presence", "protocol_version", "TEXT"),
    ("agent_presence", "skill_compatibility", "TEXT NOT NULL DEFAULT '{}'"),
    # Where to knock. No endpoint means polling, which was the only path before.
    ("agent_presence", "endpoint", "TEXT"),
    # A pending delivery on an existing offer is one that was never attempted.
    ("work_offers", "delivery_status", "TEXT NOT NULL DEFAULT 'pending'"),
    ("work_offers", "delivery_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("work_offers", "delivered_at", "TEXT"),
    # Trace columns. Older events keep NULL, which reads as "not traced" rather
    # than as an event that belonged to no request.
    ("audit_events", "correlation_id", "TEXT"),
    ("audit_events", "causation_id", "TEXT"),
    # A reservation written before this column cannot say which request it
    # belongs to; NULL keeps such a stale row answering idempotency_unknown.
    ("idempotency_records", "correlation_id", "TEXT"),
    # An existing project was necessarily co-located, because that was the only
    # model the service could serve, so the default states the truth about it.
    ("project_repositories", "workspace_model", "TEXT NOT NULL DEFAULT 'colocated'"),
    ("project_repositories", "observed_mainline_commit", "TEXT"),
    ("project_repositories", "observed_upstream_commit", "TEXT"),
    ("project_repositories", "observed_remote_refs", "TEXT NOT NULL DEFAULT '{}'"),
    ("project_repositories", "observed_at", "TEXT"),
    # Provenance. Older rows keep NULL, which reads as "not recorded" rather
    # than as a verified environment.
    ("evidence", "checksum_source", "TEXT"),
    ("evidence", "scenario_ref", "TEXT"),
    ("evidence", "environment_digest", "TEXT"),
    ("evidence", "storage_checksum", "TEXT"),
    ("qa_test_runs", "environment_digest", "TEXT"),
    ("qa_test_runs", "storage_checksum", "TEXT"),
    # Heading level, so a cold reader can ask for an outline instead of all of it.
    ("spec_sections", "depth", "INTEGER NOT NULL DEFAULT 1"),
    # Subtree extent, so trimming the outline costs a page rather than a scan.
    # NULL until the next sync recomputes it, which the reader falls back for.
    ("spec_sections", "subtree_end_line", "INTEGER"),
    ("spec_sections", "subtree_tokens", "INTEGER"),
    # A snapshot is now named by a counter and deduplicated by its content
    # digest. Rows written before the column was added were named by the digest
    # itself and keep NULL here; they remain valid freshness tokens.
    ("context_snapshots", "content_digest", "TEXT"),
    # Experience provenance and retirement. Older notes keep NULL: no recorded
    # commit, and not retired.
    ("experience_notes", "source_commit", "TEXT"),
    ("experience_notes", "retired_at", "TEXT"),
    ("experience_notes", "retired_by", "TEXT"),
    ("experience_notes", "retired_reason", "TEXT"),
    # Every reconciliation before these columns closed the whole project, so
    # 'project' states what was true of each existing row. No row had a frozen
    # set, a shadow prediction or an agreement to record.
    ("repository_reconciliations", "scope", "TEXT NOT NULL DEFAULT 'project'"),
    ("repository_reconciliations", "frozen_ticket_ids", "TEXT"),
    ("repository_reconciliations", "shadow_partition", "TEXT"),
    ("repository_reconciliations", "shadow_agreed", "INTEGER"),
    # When the current agent session registered. NULL for a presence recorded
    # before the column: its session start is unknown, so no ticket count and
    # no restart advice is derived from it.
    ("agent_presence", "session_started_at", "TEXT"),
    # Wave 7B auto-rebase: a server-derived candidate names the candidate it
    # was rebased from, and the candidate whose QA verdict it carries over
    # (NULL when the derived candidate must be assessed by QA again).
    ("candidates", "derived_from", "TEXT"),
    ("candidates", "qa_evidence_from", "TEXT"),
    # A non-blocking flag set when a merge moved main under a ticket's plan.
    # NULL for every older ticket: none was planned against a moved base the
    # flag could describe.
    ("tickets", "planning_base_stale", "TEXT"),
    # Model families (wave 7C3). NULL is "unknown", which is the truth about
    # every session and candidate recorded before the column.
    ("agent_presence", "model_family", "TEXT"),
    ("agent_presence", "model", "TEXT"),
    ("candidates", "maker_model_family", "TEXT"),
    ("candidates", "maker_model", "TEXT"),
    # Wave 8A: why a run ended, as the runner classified it, and how its
    # imported source was attested. NULL on an older row means "not recorded";
    # only 'passed' counts for reuse or the merge gate.
    ("qa_test_runs", "termination_reason", "TEXT"),
    ("qa_test_runs", "provenance_status", "TEXT"),
)


# Indexes that cannot be declared in SCHEMA. The schema script runs before these
# migrations, so on a database that predates the column an index declared there
# would reference something that does not exist yet -- fine on a new database,
# fatal on every existing one.
MIGRATED_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS audit_events_correlation "
    "ON audit_events (correlation_id)",
    "CREATE INDEX IF NOT EXISTS audit_events_kind_aggregate "
    "ON audit_events (event_type, aggregate_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS context_snapshots_content "
    "ON context_snapshots (content_digest)",
)


def apply_additive_columns(connection: Any, columns_of: Any) -> None:
    """Add every missing column, asking the backend which ones are missing.

    columns_of takes a table name and returns the set of column names it has,
    or an empty set if the table does not exist -- in which case there is
    nothing to migrate, because the schema script just created it whole.
    """
    seen: dict[str, set[str]] = {}
    for table, column, declaration in ADDITIVE_COLUMNS:
        if table not in seen:
            seen[table] = columns_of(table)
        existing = seen[table]
        if not existing or column in existing:
            continue
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        existing.add(column)
    for statement in MIGRATED_INDEXES:
        connection.execute(statement)
    backfill_experience_scopes(connection)


def backfill_experience_scopes(connection: Any) -> int:
    """Index the scopes of notes written before the index existed.

    A note absent from this table is invisible to the query that surfaces it, so
    this is not an optimisation that can be skipped: without it, upgrading loses
    every lesson already recorded. Cheap to run always, because it only looks at
    notes that have no rows yet.
    """
    from hoh_control_plane.scopes import roots_of

    rows = connection.execute(
        """
        SELECT note_id, file_scope FROM experience_notes
        WHERE note_id NOT IN (SELECT note_id FROM experience_note_scopes)
        """
    ).fetchall()
    indexed = 0
    for row in rows:
        note_id = row["note_id"]
        for root in roots_of(json.loads(row["file_scope"])):
            connection.execute(
                "INSERT INTO experience_note_scopes (note_id, scope_root) VALUES (?, ?)",
                (note_id, root),
            )
            indexed += 1
    return indexed


class SQLiteRebuildMigrations:
    """Table rebuilds only a SQLite database ever needed; SQLiteStore inherits them."""

    @classmethod
    def _migrate_stale_migration_table_references(
        cls, connection: sqlite3.Connection
    ) -> list[str]:
        """Repoint foreign keys left aiming at a dropped rebuild temporary.

        A database upgraded by the earlier rename-create-copy-drop code carries
        real damage: renaming planner_jobs out of the way silently rewrote every
        other table's foreign key to the temporary planner_jobs_legacy, and the
        same migration then dropped it. The child tables -- planner_interventions
        and planner_job_leases -- were left pointing at nothing, so the first
        insert into either failed with a raw storage error that surfaced as a
        generic state conflict, and an evidence-bearing QA FAIL verdict could not
        commit. Setting legacy_alter_table stops new damage; this heals databases
        that already took it, in the same transaction, without hand repair.

        Returns the tables it rebuilt. Idempotent: a database whose references
        are already live is left untouched.
        """
        existing = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        repaired: list[str] = []
        for table in sorted(existing):
            if table.startswith("sqlite_"):
                continue
            stale_targets = {
                target
                for target in (
                    foreign_key["table"]
                    for foreign_key in connection.execute(
                        f'PRAGMA foreign_key_list("{table}")'
                    ).fetchall()
                )
                # Only a name this project's own rebuild would have produced, and
                # only when the table it stood in for is present: a foreign key to
                # some other missing table is a different fault and is left alone
                # for PRAGMA foreign_key_check to report rather than guessed at.
                if target not in existing
                and target.endswith(_MIGRATION_TABLE_SUFFIX)
                and target[: -len(_MIGRATION_TABLE_SUFFIX)] in existing
            }
            if stale_targets:
                cls._repoint_foreign_keys(connection, table, stale_targets)
                repaired.append(table)
        return repaired

    @staticmethod
    def _repoint_foreign_keys(
        connection: sqlite3.Connection, table: str, stale_targets: set[str]
    ) -> None:
        """Rebuild one table with its foreign keys aimed back at the live parents.

        The stored definition is reused verbatim apart from the reference target,
        so any column, check or default this table has acquired since it was first
        created survives the repair untouched.
        """
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        repaired_sql = definition["sql"]
        for stale in sorted(stale_targets):
            live = stale[: -len(_MIGRATION_TABLE_SUFFIX)]
            # SQLite quotes the target when it rewrites a reference during a
            # rename, but a hand-written definition may not be quoted at all.
            repaired_sql = repaired_sql.replace(f'REFERENCES "{stale}"', f'REFERENCES "{live}"')
            repaired_sql = repaired_sql.replace(f"REFERENCES {stale}", f"REFERENCES {live}")
        if any(stale in repaired_sql for stale in stale_targets):
            raise SchemaMigrationError(
                f"Foreign key references to a dropped migration table remain in {table}"
            )
        SQLiteRebuildMigrations._rebuild_table(connection, table, repaired_sql)

    @staticmethod
    def _rebuild_table(connection: sqlite3.Connection, table: str, definition_sql: str) -> None:
        """Recreate one table from ``definition_sql`` and copy every row across.

        SQLite's own table-rebuild procedure: needs foreign_keys=OFF and
        legacy_alter_table=ON (set by ``initialize``), so the rename does not
        rewrite other tables' references to the temporary name. Indexes are
        recreated verbatim and the row count is proved unchanged.
        """
        before = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        columns = ", ".join(
            f'"{row["name"]}"'
            for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        )
        indexes = [
            row["sql"]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? "
                "AND sql IS NOT NULL",
                (table,),
            ).fetchall()
        ]
        index_names = [
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ? "
                "AND sql IS NOT NULL",
                (table,),
            ).fetchall()
        ]
        # The rebuilt table reclaims these names, so release them before the
        # rename rather than colliding with the copies riding on the old table.
        for index_name in index_names:
            connection.execute(f'DROP INDEX IF EXISTS "{index_name}"')
        rebuild_name = f"{table}__rebuild"
        connection.execute(f'ALTER TABLE "{table}" RENAME TO "{rebuild_name}"')
        connection.execute(definition_sql)
        connection.execute(
            f'INSERT INTO "{table}" ({columns}) SELECT {columns} FROM "{rebuild_name}"'
        )
        connection.execute(f'DROP TABLE "{rebuild_name}"')
        for index_sql in indexes:
            connection.execute(index_sql)
        after = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        if after != before:
            raise SchemaMigrationError(
                f"Rebuilding {table} copied {after} of {before} rows"
            )

    @classmethod
    def _migrate_ticket_status_check(cls, connection: sqlite3.Connection) -> bool:
        """Give a database created before it the tickets.status CHECK.

        Ticket transitions are guarded in SQL by version and status; the CHECK
        stops a write from storing a status the lifecycle does not have. SQLite
        cannot add a CHECK in place, so the table is rebuilt from its own stored
        definition with only the status column changed: every column, default,
        foreign key and index it has acquired survives.

        Left alone, and so still enforced only by the service, when the stored
        definition does not have exactly one recognisable ``status TEXT NOT
        NULL`` column, when a row already holds a status outside the
        vocabulary, or when a ticket row already has a dangling foreign key:
        refusing to start over data the upgrade did not write would
        take the control plane down for a fault this migration cannot repair.
        Returns whether it rebuilt the table.
        """
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tickets'"
        ).fetchone()
        if definition is None or TICKET_STATUS_CHECK in definition["sql"]:
            return False
        matches = list(_TICKET_STATUS_COLUMN.finditer(definition["sql"]))
        if len(matches) != 1:
            return False
        present = {
            row[0] for row in connection.execute("SELECT DISTINCT status FROM tickets")
        }
        if not present <= set(TICKET_STATUSES):
            return False
        if connection.execute('PRAGMA foreign_key_check("tickets")').fetchall():
            # A dangling reference that predates the upgrade would fail the
            # post-rebuild proof and block startup; it is not this migration's.
            return False
        match = matches[0]
        rebuilt_sql = (
            definition["sql"][: match.end(1)]
            + " "
            + TICKET_STATUS_CHECK
            + definition["sql"][match.end(1):]
        )
        cls._rebuild_table(connection, "tickets", rebuilt_sql)
        return True

    @staticmethod
    def _require_clean_foreign_keys(connection: sqlite3.Connection, tables: list[str]) -> None:
        """Prove each rebuilt table's rows still resolve, before committing.

        Scoped to the tables this upgrade actually touched: a violation that
        predates the upgrade elsewhere in the database is not this migration's to
        adjudicate, and refusing to start over one would take the control plane
        down for a fault the upgrade neither caused nor can fix.
        """
        for table in tables:
            violations = connection.execute(
                f'PRAGMA foreign_key_check("{table}")'
            ).fetchall()
            if violations:
                raise SchemaMigrationError(
                    f"Upgraded table {table} has {len(violations)} unresolved "
                    "foreign key references"
                )

    @staticmethod
    def _migrate_planner_job_types(connection: sqlite3.Connection) -> None:
        """Widen the planner job kinds without discarding existing planning work.

        A database created before Integrator outcomes became a planning source
        still carries the narrower CHECK, which SQLite cannot alter in place.
        """
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'planner_jobs'"
        ).fetchone()
        if definition is None or "delivery_completed" in definition["sql"]:
            return

        columns = (
            "planner_job_id, project_id, ticket_id, job_type, status, summary, candidate_id, "
            "evidence_refs, context_entry_id, created_by, created_at, updated_at, resolution, "
            "resolved_by, resolved_at"
        )
        connection.execute("DROP INDEX IF EXISTS planner_jobs_project_status")
        connection.execute("DROP INDEX IF EXISTS planner_jobs_one_open_per_ticket")
        connection.execute("ALTER TABLE planner_jobs RENAME TO planner_jobs_legacy")
        connection.execute(
            """
            CREATE TABLE planner_jobs (
                planner_job_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
                job_type TEXT NOT NULL CHECK (
                    job_type IN ('qa_failure', 'developer_blocker', 'integration_conflict', 'delivery_completed')
                ),
                status TEXT NOT NULL CHECK (status IN ('READY', 'IN_PROGRESS', 'RESOLVED')),
                summary TEXT NOT NULL,
                candidate_id TEXT REFERENCES candidates(candidate_id),
                evidence_refs TEXT NOT NULL,
                context_entry_id TEXT REFERENCES context_entries(entry_id),
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolution TEXT,
                resolved_by TEXT,
                resolved_at TEXT
            )
            """
        )
        connection.execute(
            f"INSERT INTO planner_jobs ({columns}) SELECT {columns} FROM planner_jobs_legacy"
        )
        connection.execute("DROP TABLE planner_jobs_legacy")
        connection.execute(
            "CREATE INDEX planner_jobs_project_status ON planner_jobs(project_id, status, created_at)"
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX planner_jobs_one_open_per_ticket
                ON planner_jobs(ticket_id) WHERE status IN ('READY', 'IN_PROGRESS')
            """
        )

    @staticmethod
    def _migrate_planner_intervention_types(connection: sqlite3.Connection) -> None:
        """Allow the durable wakeup outbox to carry successful delivery reports."""
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'planner_interventions'"
        ).fetchone()
        if definition is None or "delivery_completed" in definition["sql"]:
            return

        columns = (
            "intervention_id, planner_job_id, project_id, ticket_id, event_class, severity, "
            "context_ref, artifact_commit, candidate_id, evidence_refs, status, "
            "delivery_attempts, last_error, nonce, created_at, updated_at, delivered_at, "
            "acknowledged_at, acknowledged_by"
        )
        connection.execute("DROP INDEX IF EXISTS planner_interventions_one_open_per_job")
        connection.execute("DROP INDEX IF EXISTS planner_interventions_project_status")
        connection.execute(
            "ALTER TABLE planner_interventions RENAME TO planner_interventions_legacy"
        )
        connection.execute(
            """
            CREATE TABLE planner_interventions (
                intervention_id TEXT PRIMARY KEY,
                planner_job_id TEXT NOT NULL REFERENCES planner_jobs(planner_job_id),
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
                event_class TEXT NOT NULL CHECK (
                    event_class IN ('developer_blocker', 'qa_failure', 'integration_conflict', 'delivery_completed')
                ),
                severity TEXT NOT NULL CHECK (severity IN ('high', 'normal')),
                context_ref TEXT NOT NULL,
                artifact_commit TEXT,
                candidate_id TEXT,
                evidence_refs TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('PENDING', 'DELIVERED', 'ACKNOWLEDGED')),
                delivery_attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                nonce TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                delivered_at TEXT,
                acknowledged_at TEXT,
                acknowledged_by TEXT
            )
            """
        )
        connection.execute(
            f"INSERT INTO planner_interventions ({columns}) "
            f"SELECT {columns} FROM planner_interventions_legacy"
        )
        connection.execute("DROP TABLE planner_interventions_legacy")
        connection.execute(
            """CREATE UNIQUE INDEX planner_interventions_one_open_per_job
               ON planner_interventions(planner_job_id)
               WHERE status IN ('PENDING', 'DELIVERED')"""
        )
        connection.execute(
            """CREATE INDEX planner_interventions_project_status
               ON planner_interventions(project_id, status, created_at)"""
        )

    @staticmethod
    def _migrate_qa_test_job_lease_foreign_key(connection: sqlite3.Connection) -> None:
        """Remove the legacy live-lease FK without discarding durable QA history.

        An asynchronous test run is authorized by a lease but can outlive it.  The
        old foreign key made lease recovery fail after a test job had been created,
        which blocked every later inbox and claim operation in that transaction.
        """
        foreign_keys = connection.execute("PRAGMA foreign_key_list(qa_test_jobs)").fetchall()
        if not any(
            foreign_key["table"] == "leases" and foreign_key["from"] == "lease_id"
            for foreign_key in foreign_keys
        ):
            return

        columns = (
            "test_run_id, project_id, candidate_id, ticket_id, lease_id, test_command, "
            "command_digest, timeout_seconds, worktree_path, candidate_commit, requested_by, "
            "status, error_message, created_at, started_at, completed_at"
        )
        connection.execute("DROP INDEX IF EXISTS qa_test_jobs_candidate_created")
        connection.execute("DROP INDEX IF EXISTS qa_test_jobs_active_candidate")
        connection.execute("ALTER TABLE qa_test_jobs RENAME TO qa_test_jobs_legacy")
        connection.execute(
            """
            CREATE TABLE qa_test_jobs (
                test_run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
                ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
                lease_id TEXT NOT NULL,
                test_command TEXT NOT NULL,
                command_digest TEXT NOT NULL,
                timeout_seconds INTEGER NOT NULL,
                worktree_path TEXT NOT NULL,
                candidate_commit TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'STALE')),
                error_message TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            )
            """
        )
        connection.execute(
            f"INSERT INTO qa_test_jobs ({columns}) SELECT {columns} FROM qa_test_jobs_legacy"
        )
        connection.execute("DROP TABLE qa_test_jobs_legacy")
        connection.execute(
            "CREATE INDEX qa_test_jobs_candidate_created ON qa_test_jobs(candidate_id, created_at)"
        )
        connection.execute(
            "CREATE INDEX qa_test_jobs_active_candidate ON qa_test_jobs(candidate_id, status)"
        )
