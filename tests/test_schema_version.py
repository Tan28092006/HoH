"""The database records which schema version wrote it.

An older build started against a database a newer build upgraded must refuse
before touching it; a database older than the code is upgraded through the
existing migrations and stamped with the current version. Every test here uses
a throwaway file under tmp_path.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hoh_control_plane.store import (
    ADDITIVE_COLUMNS,
    MIGRATED_INDEXES,
    SCHEMA,
    SCHEMA_VERSION,
    SQLiteStore,
    SchemaVersionError,
)


def _recorded_version(path: Path) -> int | None:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else int(row[0])


def _tables(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        connection.close()


def test_a_new_database_records_the_current_version(tmp_path) -> None:
    path = tmp_path / "new.db"
    SQLiteStore(path).initialize()
    assert _recorded_version(path) == SCHEMA_VERSION


def test_an_unversioned_older_database_is_upgraded_and_stamped(tmp_path) -> None:
    """Version 0: written before schema_meta existed, and missing later columns."""
    path = tmp_path / "unversioned.db"
    SQLiteStore(path).initialize()
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE schema_meta")
        # Same order the real upgrade implies: indexes on added columns first.
        for statement in MIGRATED_INDEXES:
            name = statement.split("IF NOT EXISTS ", 1)[1].split()[0]
            connection.execute(f'DROP INDEX IF EXISTS "{name}"')
        for table, column, _declaration in ADDITIVE_COLUMNS:
            connection.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        connection.commit()
    finally:
        connection.close()
    assert "schema_meta" not in _tables(path)

    SQLiteStore(path).initialize()

    assert _recorded_version(path) == SCHEMA_VERSION
    connection = sqlite3.connect(path)
    try:
        for table, column, _declaration in ADDITIVE_COLUMNS:
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            assert column in columns, f"{table}.{column}: the additive migrations ran first"
    finally:
        connection.close()


def test_an_older_recorded_version_is_raised_to_the_current_one(tmp_path) -> None:
    path = tmp_path / "older.db"
    SQLiteStore(path).initialize()
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE schema_meta SET value = ?", (str(SCHEMA_VERSION - 1),))
        connection.commit()
    finally:
        connection.close()

    SQLiteStore(path).initialize()

    assert _recorded_version(path) == SCHEMA_VERSION


def test_a_newer_database_is_refused_before_anything_changes(tmp_path) -> None:
    path = tmp_path / "newer.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO schema_meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION + 1),)
        )
        connection.commit()
        journal_before = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()

    with pytest.raises(SchemaVersionError) as raised:
        SQLiteStore(path).initialize()

    message = str(raised.value)
    assert f"schema version {SCHEMA_VERSION + 1}" in message
    assert f"version {SCHEMA_VERSION} this HoH build supports" in message
    assert raised.value.details == {
        "database_schema_version": SCHEMA_VERSION + 1,
        "supported_schema_version": SCHEMA_VERSION,
    }
    # Not one table created, not the journal mode switched, the version kept.
    assert _tables(path) == {"schema_meta"}
    assert _recorded_version(path) == SCHEMA_VERSION + 1
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == journal_before
    finally:
        connection.close()


def test_a_newer_full_database_is_refused_on_restart(tmp_path) -> None:
    """The real case: a populated file a newer release stamped, then an old binary."""
    path = tmp_path / "upgraded-by-newer.db"
    SQLiteStore(path).initialize()
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE schema_meta SET value = ?", (str(SCHEMA_VERSION + 5),))
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SchemaVersionError, match="newer"):
        SQLiteStore(path).initialize()
    assert _recorded_version(path) == SCHEMA_VERSION + 5


def test_an_unreadable_version_is_refused(tmp_path) -> None:
    path = tmp_path / "garbled.db"
    SQLiteStore(path).initialize()
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE schema_meta SET value = 'two'")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SchemaVersionError, match="unreadable"):
        SQLiteStore(path).initialize()


def test_the_version_table_is_part_of_the_shared_schema() -> None:
    assert "CREATE TABLE IF NOT EXISTS schema_meta" in SCHEMA
    assert SCHEMA_VERSION >= 1
