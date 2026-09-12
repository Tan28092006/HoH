"""A PostgreSQL backend for the control plane's store seam.

SQLite is the default and stays the local single-host answer. This exists for the
case SQLite genuinely cannot serve: agents on more than one machine, where a
shared file's locking is unsafe and a real transactional server is required.

Almost nothing here is SQL translation, because the schema was already portable:
across 32 tables there is one PRAGMA and two AUTOINCREMENT columns. What this
module supplies is the handful of behaviours SQLite provided implicitly.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from hoh_control_plane.store import (
    SCHEMA,
    ContentionError,
    Dialect,
    StoreConnection,
    StoreIntegrityError,
    apply_additive_columns,
)


POSTGRES_DIALECT = Dialect(
    name="postgres",
    # psycopg does not accept qmark, so the seam rewrites statements on the way out.
    placeholder="%s",
    identity_column="BIGSERIAL PRIMARY KEY",
    # Always enforced; there is no equivalent of SQLite's per-connection switch.
    foreign_keys_statement=None,
    # SQLite waits through connect(timeout=...); here a statement waiting on a
    # lock is bounded explicitly so a stuck writer surfaces rather than hanging.
    lock_timeout_statement="SET lock_timeout = '10s'",
    # The reason SQLite needs rename-create-copy-drop migrations and this does not.
    alters_constraints_in_place=True,
)


class Row(dict):
    """A row that answers to both a column name and a position.

    sqlite3.Row does both, and the service layer uses both: row["status"] where it
    reads a field, row[0] where it counts. Neither psycopg row factory offers both,
    so the seam supplies one rather than making 283 call sites choose a style.
    """

    def __init__(self, columns: tuple[str, ...], values: tuple[Any, ...]) -> None:
        super().__init__(zip(columns, values, strict=False))
        self._values = values

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)

    def keys(self) -> Any:  # noqa: D102 - dict protocol
        return super().keys()


def _row_factory(cursor: Any) -> Any:
    columns = tuple(column.name for column in (cursor.description or ()))

    def make(values: tuple[Any, ...]) -> Row:
        return Row(columns, values)

    return make


def translate_schema(schema: str = SCHEMA) -> str:
    """Render the shared schema in PostgreSQL's dialect.

    Deliberately a translation of the one schema rather than a second copy: two
    schemas drift, and a drift between backends is the kind of fault that only
    shows up as data loss on whichever one is less exercised.
    """
    translated = schema.replace(
        "INTEGER PRIMARY KEY AUTOINCREMENT", POSTGRES_DIALECT.identity_column
    )
    # Foreign keys are always on, so the switch has no meaning here.
    translated = re.sub(r"^\s*PRAGMA[^;]*;\s*$", "", translated, flags=re.MULTILINE)
    return translated


class PostgresStore:
    """The same store contract, backed by a server instead of a file."""

    def __init__(
        self,
        dsn: str,
        *,
        artifact_root: Path | str = Path("data"),
        lock_timeout_seconds: float = 10.0,
    ) -> None:
        self.dsn = dsn
        self.lock_timeout_seconds = lock_timeout_seconds
        self.dialect = POSTGRES_DIALECT
        # Named for parity with SQLiteStore, which exposes the file it opens.
        self.path = dsn
        # A server-backed store has no directory of its own, so where worktrees,
        # evidence and recovery patches live has to be stated rather than inferred.
        self.artifact_root = Path(artifact_root)

    def _connect(self) -> Any:
        import psycopg  # imported here so SQLite installs need no driver at all

        return psycopg.connect(self.dsn, autocommit=False, row_factory=_row_factory)

    def initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute(translate_schema())
            # The schema script creates anything missing whole, but it cannot
            # add a column to a table that already exists, so a database from an
            # earlier version needs the same additive migrations SQLite runs.
            # The list is shared rather than restated: a second copy is the one
            # that quietly misses the next entry, and that failure surfaces as a
            # query against a column that is not there, long after startup.
            #
            # Wrapped rather than used raw: the shared migration is written in
            # qmark style like every other statement in this codebase, and only
            # the seam knows to translate it for this driver.
            apply_additive_columns(
                StoreConnection(connection, self.dialect),
                self._existing_columns(connection),
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _existing_columns(connection: Any) -> Any:
        """How this backend answers "which columns does this table have?".

        Scoped to current_schema() so a deployment that puts the control plane
        in its own schema, and the test suite that gives each run a throwaway
        one, both see their own tables rather than a same-named table elsewhere
        on the search path.
        """

        def columns_of(table: str) -> set[str]:
            rows = connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s",
                (table,),
            ).fetchall()
            return {row[0] for row in rows}

        return columns_of

    @contextmanager
    def connection(self, *, immediate: bool = False) -> Iterator[StoreConnection]:
        import psycopg

        raw = self._connect()
        raw.execute(f"SET lock_timeout = '{int(self.lock_timeout_seconds * 1000)}ms'")
        wrapped = StoreConnection(
            raw, self.dialect, integrity_errors=(psycopg.errors.IntegrityError,)
        )
        try:
            if immediate:
                # SQLite's BEGIN IMMEDIATE takes the write lock up front so two
                # writers cannot both believe they hold it. The equivalent here is
                # an explicit transaction that serialises on the rows it touches.
                raw.execute("BEGIN")
            yield wrapped
            raw.commit()
        except psycopg.errors.IntegrityError as error:
            raw.rollback()
            raise StoreIntegrityError(
                "This change conflicts with a constraint the schema enforces."
            ) from error
        except psycopg.errors.LockNotAvailable as error:
            raw.rollback()
            raise ContentionError(
                "The database is busy with another operation; retry this request."
            ) from error
        except psycopg.errors.SerializationFailure as error:
            raw.rollback()
            raise ContentionError(
                "The database is busy with another operation; retry this request."
            ) from error
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.close()
