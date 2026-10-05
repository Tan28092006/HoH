from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Iterator

from hoh_control_plane.domain import DomainError
from hoh_control_plane.store_migrations import (  # noqa: F401  (re-exported)
    ADDITIVE_COLUMNS,
    MIGRATED_INDEXES,
    SCHEMA_VERSION,
    SQLiteRebuildMigrations,
    SchemaMigrationError,
    SchemaVersionError,
    apply_additive_columns,
    backfill_experience_scopes,
    read_schema_version,
    record_schema_version,
    require_supported_schema_version,
)
from hoh_control_plane.store_schema import (  # noqa: F401  (re-exported)
    CHECKSUM_ARTIFACT,
    CHECKSUM_CLAIMED,
    SCHEMA,
)


class ContentionError(DomainError):
    """The database could not acquire its write lock under concurrent load.

    This is the one documented, retryable error a caller sees instead of a raw
    SQLite "database is locked"/"database is busy" message: no statement in the
    attempted transaction ran, so retrying the same request from scratch is safe
    and is expected to succeed once the competing writer finishes.
    """

    code = "database_contention"
    status_code = 503


# SQLite's own busy handler (set by connect(timeout=...) and PRAGMA busy_timeout
# below) already blocks a single BEGIN IMMEDIATE for this long before raising --
# it is a brief, self-clearing wait for a short-lived local transaction, not a
# way to mask a slow external operation that should never run inside a
# transaction in the first place (see connection() below). The bounded
# retry/backoff policy this budget participates in lives one layer up, in
# mcp_server.invoke(), which retries the whole call after a ContentionError.
_DEFAULT_BUSY_TIMEOUT_SECONDS = 10.0


class StoreIntegrityError(DomainError):
    """A write violated a durable constraint the schema declares.

    Named here rather than caught as a driver exception so a service module can
    recognise a foreign key or uniqueness violation without knowing which driver
    raised it, and so no raw storage-engine text reaches a caller.
    """

    code = "state_conflict"
    status_code = 409


@dataclass(frozen=True)
class Dialect:
    """What a backend must supply that SQLite currently provides implicitly.

    Written down so a second backend is a list of decisions rather than a hunt
    through the schema for everything that silently depended on SQLite.
    """

    name: str
    # The parameter marker the driver expects. Statements are written in qmark
    # style everywhere and translated on the way out.
    placeholder: str
    # How a monotonic sequence column is declared.
    identity_column: str
    # Run once per connection to enforce foreign keys, if the backend does not
    # enforce them by default.
    foreign_keys_statement: str | None
    # Run once per connection to bound how long a statement waits for a lock.
    lock_timeout_statement: str | None
    # Whether the backend can alter a constraint in place. SQLite cannot, which
    # is the entire reason for the rename-create-copy-drop machinery below.
    alters_constraints_in_place: bool


SQLITE_DIALECT = Dialect(
    name="sqlite",
    placeholder="?",
    identity_column="INTEGER PRIMARY KEY AUTOINCREMENT",
    foreign_keys_statement="PRAGMA foreign_keys = ON",
    lock_timeout_statement=None,  # set through connect(timeout=...) instead
    alters_constraints_in_place=False,
)


def translate_placeholders(statement: str, placeholder: str) -> str:
    """Rewrite qmark parameter markers for a backend that expects another style.

    Only markers outside string literals are rewritten: a question mark inside
    quoted SQL text is data, not a parameter, and rewriting it would silently
    corrupt the statement.
    """
    if placeholder == "?":
        return statement
    out: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(statement):
        character = statement[index]
        if quote is not None:
            out.append(character)
            if character == quote:
                # A doubled quote is an escaped quote, not the end of the literal.
                if index + 1 < len(statement) and statement[index + 1] == quote:
                    out.append(statement[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if character in ("'", '"'):
            quote = character
            out.append(character)
        elif character == "?":
            out.append(placeholder)
        else:
            out.append(character)
        index += 1
    return "".join(out)


class StoreConnection:
    """A connection the service layer can use without naming a driver.

    Wraps the driver's own connection so statements stay written in one
    placeholder style and so every failure a caller can reasonably act on
    arrives as a control-plane error rather than as driver text.
    """

    def __init__(
        self,
        raw: Any,
        dialect: Dialect,
        integrity_errors: tuple[type[BaseException], ...] = (sqlite3.IntegrityError,),
    ) -> None:
        self._raw = raw
        self.dialect = dialect
        # Which driver exception means "a constraint refused this". Passed in
        # rather than hardcoded, so a second backend translates its own errors
        # without this class learning about every driver.
        self._integrity_errors = integrity_errors
        self.event_changed = False

    def execute(self, statement: str, parameters: Any = ()) -> Any:
        try:
            result = self._raw.execute(
                translate_placeholders(statement, self.dialect.placeholder), parameters
            )
            words = statement.lstrip().lower().split()
            if words and words[0] in {'insert', 'update', 'delete'} and any(
                table in words[:5] for table in ('audit_events', 'qa_test_jobs', 'merge_jobs',
                    'candidate_submission_jobs', 'worktree_provision_jobs', 'planner_jobs')
            ):
                self.event_changed = True
            return result
        except self._integrity_errors as error:
            raise StoreIntegrityError(
                "This change conflicts with a constraint the schema enforces."
            ) from error

    def executescript(self, script: str) -> Any:
        return self._raw.executescript(script)

    def commit(self) -> None:
        self._raw.commit()
        if self.event_changed:
            self.event_changed = False
            from hoh_control_plane.event_wait import notify_waiters
            notify_waiters(self)

    def rollback(self) -> None:
        self._raw.rollback()
        self.event_changed = False

    def close(self) -> None:
        self._raw.close()

    @property
    def raw(self) -> Any:
        """The driver connection, for migration code that is backend-specific."""
        return self._raw

    def __getattr__(self, name: str) -> Any:
        # Anything the seam does not need to shape is the driver's own; falling
        # through keeps this a seam rather than a reimplementation of DB-API.
        return getattr(self._raw, name)


def _is_lock_contention(error: sqlite3.OperationalError) -> bool:
    message = str(error).lower()
    return "locked" in message or "busy" in message


class SQLiteStore(SQLiteRebuildMigrations):
    """Small transactional store for the local-pilot control plane.

    This is single-machine, local-pilot storage: one SQLite file on this disk,
    guarded by short, in-process write transactions with WAL-mode concurrent
    reads. It assumes every reader and writer runs on this machine against this
    file -- it is not a multi-machine-safe store, must never be placed on a
    network share, and is expected to be replaced by a PostgreSQL-backed store
    (see HOH-204) before agents span machines.
    """

    def __init__(
        self, path: str | Path, *, busy_timeout_seconds: float = _DEFAULT_BUSY_TIMEOUT_SECONDS
    ) -> None:
        self.path = Path(path)
        # Overridable so tests can exercise real lock-wait-then-fail behavior in
        # well under a second instead of waiting out the production default.
        self.busy_timeout_seconds = busy_timeout_seconds
        self.dialect = SQLITE_DIALECT
        # Where this store's companion artifacts live. For a file-backed store
        # that is the directory holding the file, which is where they have always
        # been; a store that is not a file has to be told.
        self.artifact_root = self.path.parent

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            # A database a newer release upgraded is refused before anything,
            # even a pragma, touches it.
            columns_of = self._existing_columns(connection)
            require_supported_schema_version(
                read_schema_version(connection, columns_of), str(self.path)
            )
            # WAL allows many MCP reader processes and one short-lived writer to coexist.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(SCHEMA)
            # The migrations below rebuild a table by rename-create-copy-drop.
            # Two pragmas make that safe, and neither can be changed once a
            # transaction is open, so both are set here rather than inside a
            # migration:
            #   legacy_alter_table keeps the RENAME from rewriting *other*
            #   tables' foreign keys to aim at the temporary table this
            #   migration is about to drop -- that rewrite is exactly how
            #   planner_interventions and planner_job_leases were left
            #   referencing a planner_jobs_legacy table that no longer exists;
            #   foreign_keys=OFF lets a rebuild copy rows across while a parent
            #   table is momentarily renamed away, as SQLite's own table-rebuild
            #   procedure requires.
            # Enforcement is not weakened for anyone else: this connection is
            # used only for the upgrade and closed immediately after, every
            # serving connection still opens with foreign_keys=ON, and
            # _require_clean_foreign_keys re-proves each rebuilt table before
            # this transaction commits.
            connection.execute("PRAGMA legacy_alter_table = ON")
            connection.execute("PRAGMA foreign_keys = OFF")
            self._begin_immediate(connection)
            # Again under the write lock: another process may have upgraded the
            # file between the first check and this transaction.
            require_supported_schema_version(
                read_schema_version(connection, columns_of), str(self.path)
            )
            # The rebuilds below repair foreign keys and CHECK constraints that
            # only a SQLite database ever had wrong, so they stay here rather
            # than moving into the shared list: a PostgreSQL database created
            # from the current schema never had the defect they undo.
            self._migrate_qa_test_job_lease_foreign_key(connection)
            self._migrate_planner_job_types(connection)
            self._migrate_planner_intervention_types(connection)
            apply_additive_columns(connection, self._existing_columns(connection))
            tickets_rebuilt = self._migrate_ticket_status_check(connection)
            repaired = self._migrate_stale_migration_table_references(connection)
            self._require_clean_foreign_keys(
                connection,
                [*repaired, "planner_interventions", *(["tickets"] if tickets_rebuilt else [])],
            )
            record_schema_version(connection)

    @staticmethod
    def _existing_columns(connection: sqlite3.Connection) -> Any:
        """How this backend answers "which columns does this table have?"."""

        def columns_of(table: str) -> set[str]:
            return {
                row["name"]
                for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            }

        return columns_of

    @contextmanager
    def connection(self, *, immediate: bool = False) -> Iterator[StoreConnection]:
        raw = sqlite3.connect(self.path, timeout=self.busy_timeout_seconds)
        raw.row_factory = sqlite3.Row
        if self.dialect.foreign_keys_statement:
            raw.execute(self.dialect.foreign_keys_statement)
        raw.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_seconds * 1000)}")
        connection = StoreConnection(raw, self.dialect)
        try:
            if immediate:
                self._begin_immediate(connection)
            yield connection
            connection.commit()
        except sqlite3.OperationalError as error:
            connection.rollback()
            if _is_lock_contention(error):
                raise ContentionError(
                    "The database is busy with another operation; retry this request."
                ) from error
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _begin_immediate(connection: sqlite3.Connection) -> None:
        """Acquire the write lock before any statement in the transaction runs.

        SQLite's busy handler already waits up to busy_timeout_seconds inside this
        single call. A caller whose wait is exhausted gets ContentionError, a clean
        domain error that is always safe to retry from scratch -- since nothing has
        executed yet, a retry can never re-apply a partial mutation. The bounded
        retry/backoff policy across attempts lives in mcp_server.invoke().
        """
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            if not _is_lock_contention(error):
                raise
            raise ContentionError(
                "The database is busy with another operation; retry this request."
            ) from error
